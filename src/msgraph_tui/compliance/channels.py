"""Approval channels: carry four-eyes requests to reviewers (PRD F-COMP-1).

:mod:`.approval` stores change requests and approvals as local JSON files, which
only works when requester and approver share a machine. This module adds two
ways to move them between people:

**GitApprovalChannel** — each change request becomes a branch in a shared
"approvals" git repository. The requester's branch adds
``change-requests/<id>/request.json``; the approver adds ``approval.json`` on
top of it. Reviewers can open a pull request from that branch, so the review,
discussion and merge history live in the tooling the organisation already
audits. Design constraints:

* The local clone is someone's real checkout, so the channel **never touches
  the working tree, the index or the current branch**. Everything is git
  plumbing: trees are built in a temporary index (``GIT_INDEX_FILE`` in a
  tempdir), commits with ``commit-tree``, and published with a plain
  ``git push <remote> <commit>:refs/heads/<branch>``.
* Nothing is ever overwritten: a request branch that already exists, or one
  that already carries an approval, is refused, and pushes are never forced
  (the remote rejects anything that is not a fast-forward).
* Ids are sanitised with :func:`.approval._safe` before they reach a ref name
  or a path; git is always invoked with an argument list, never a shell.
* Serialisation goes through ``to_dict``/``from_dict`` only, so fields added to
  :class:`.approval.Approval` or :class:`.approval.ChangeRequest` travel along.

**WebhookNotifier** — posts a short JSON summary to an incoming webhook (Teams,
Slack or anything that accepts JSON) when approval is requested, granted,
rejected or a change is applied. The payload deliberately omits ``params``,
``bulk_items`` and ``preview`` (they can contain personal data) and is run
through :func:`..core.redaction.redact` anyway. Notification is best effort:
``notify`` never raises, and failures are logged without the webhook URL's
path or query string, which typically embed a secret.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..core.redaction import redact
from .approval import Approval, ChangeRequest, _safe

log = logging.getLogger(__name__)

_STDERR_LIMIT = 300
_REQUEST_FILE = "request.json"
_APPROVAL_FILE = "approval.json"


class GitChannelError(Exception):
    """A git operation of the approval channel failed or was refused."""


def _require_id(request_id: str) -> str:
    safe = _safe(request_id or "")
    if not safe:
        raise GitChannelError(f"Invalid change request id: {request_id!r}")
    return safe


def _to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True, default=str) + "\n"


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _STDERR_LIMIT else text[: _STDERR_LIMIT - 3] + "..."


_GITHUB_URL = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com(?::\d+)?/)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


class GitApprovalChannel:
    """Publish and fetch change requests / approvals as branches of a shared repo."""

    def __init__(
        self,
        repo_dir: Path,
        *,
        remote: str = "origin",
        base_branch: str = "main",
        branch_prefix: str = "graphdeck/cr-",
        git: str = "git",
        timeout: float = 60.0,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.remote = remote
        self.base_branch = base_branch
        self.branch_prefix = branch_prefix
        self.git = git
        self.timeout = timeout

    # --- public API ---------------------------------------------------------

    def branch_for(self, request_id: str) -> str:
        return self.branch_prefix + _require_id(request_id)

    def publish_request(self, request: ChangeRequest) -> str:
        """Create the request branch on the remote; returns its name."""
        rid = _require_id(request.request_id)
        branch = self.branch_for(rid)
        if self._remote_branch_exists(branch):
            raise GitChannelError(
                f"Change request {rid} is already published ({branch} exists on {self.remote})."
            )
        base = self._fetch_tip(self.base_branch)
        commit = self._commit_file(
            parent=base,
            path=self._path(rid, _REQUEST_FILE),
            content=_to_json(request.to_dict()),
            message=(
                f"Change request {rid}: {request.action_name} "
                f"({request.risk} risk) by {request.requester}"
            ),
        )
        self._push(commit, branch)
        return branch

    def fetch_request(self, request_id: str) -> ChangeRequest | None:
        rid = _require_id(request_id)
        data = self._fetch_json(rid, _REQUEST_FILE)
        if data is None:
            return None
        try:
            request = ChangeRequest.from_dict(data)
        except TypeError as exc:
            raise GitChannelError(f"Malformed change request {rid}: {exc}") from exc
        if _safe(str(request.request_id)) != rid:
            raise GitChannelError(
                f"Change request file on {self.branch_for(rid)} carries a different id "
                f"({request.request_id!r}); refusing it."
            )
        return request

    def publish_approval(self, approval: Approval) -> None:
        """Add the approval on top of the request branch (fast-forward push)."""
        rid = _require_id(approval.request_id)
        branch = self.branch_for(rid)
        if not self._remote_branch_exists(branch):
            raise GitChannelError(f"No published change request {rid} ({branch} not found).")
        tip = self._fetch_tip(branch)
        if self._has_file(tip, self._path(rid, _APPROVAL_FILE)):
            raise GitChannelError(f"Change request {rid} already has a recorded decision.")
        raw = self._read_file(tip, self._path(rid, _REQUEST_FILE))
        if raw is None:
            raise GitChannelError(f"{branch} does not contain a change request file.")
        request = self._parse(raw, rid)
        if request.get("content_hash") != approval.content_hash:
            raise GitChannelError(
                f"Approval does not match change request {rid} (content hash differs)."
            )
        verb = "Approved" if approval.approved else "Rejected"
        commit = self._commit_file(
            parent=tip,
            path=self._path(rid, _APPROVAL_FILE),
            content=_to_json(approval.to_dict()),
            message=f"{verb} change request {rid} by {approval.approver}",
        )
        self._push(commit, branch)

    def fetch_approval(self, request_id: str) -> Approval | None:
        rid = _require_id(request_id)
        data = self._fetch_json(rid, _APPROVAL_FILE)
        if data is None:
            return None
        try:
            return Approval.from_dict(data)
        except TypeError as exc:
            raise GitChannelError(f"Malformed approval for {rid}: {exc}") from exc

    def compare_hint(self, request_id: str) -> str:
        """Human hint for opening a pull request from the request branch."""
        branch = self.branch_for(request_id)
        hint = f"Open a PR from {branch} into {self.base_branch}"
        proc = self._run("remote", "get-url", self.remote)
        match = _GITHUB_URL.match(proc.stdout.strip()) if proc.returncode == 0 else None
        if match:
            hint += (
                f": https://github.com/{match['owner']}/{match['repo']}"
                f"/compare/{self.base_branch}...{branch}?expand=1"
            )
        return hint

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _path(rid: str, name: str) -> str:
        return f"change-requests/{rid}/{name}"

    def _run(
        self,
        *args: str,
        input: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # never hang on a credential prompt
        env["LC_ALL"] = "C"
        if env_extra:
            env.update(env_extra)
        try:
            return subprocess.run(
                [self.git, "-C", str(self.repo_dir), *args],
                input=input,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise GitChannelError(f"git executable not found: {self.git!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitChannelError(f"git {args[0]} timed out after {self.timeout:g}s") from exc

    def _git(
        self,
        *args: str,
        input: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> str:
        """Run git and return stdout; raise :class:`GitChannelError` on failure."""
        proc = self._run(*args, input=input, env_extra=env_extra)
        if proc.returncode != 0:
            raise GitChannelError(
                f"git {args[0]} failed ({proc.returncode}): {_short(proc.stderr or proc.stdout)}"
            )
        return proc.stdout

    def _remote_branch_exists(self, branch: str) -> bool:
        proc = self._run("ls-remote", "--exit-code", "--heads", self.remote, f"refs/heads/{branch}")
        if proc.returncode == 0:
            return True
        if proc.returncode == 2:  # --exit-code: no matching ref
            return False
        raise GitChannelError(
            f"git ls-remote {self.remote} failed ({proc.returncode}): {_short(proc.stderr)}"
        )

    def _fetch_tip(self, branch: str) -> str:
        self._git("fetch", "--no-tags", self.remote, f"refs/heads/{branch}")
        return self._git("rev-parse", "--verify", "FETCH_HEAD^{commit}").strip()

    def _has_file(self, commit: str, path: str) -> bool:
        return bool(self._git("ls-tree", "--name-only", commit, "--", path).strip())

    def _read_file(self, commit: str, path: str) -> str | None:
        if not self._has_file(commit, path):
            return None
        return self._git("cat-file", "blob", f"{commit}:{path}")

    @staticmethod
    def _parse(raw: str, rid: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GitChannelError(f"Invalid JSON for change request {rid}: {exc}") from exc
        if not isinstance(data, dict):
            raise GitChannelError(f"Invalid JSON for change request {rid}: not an object")
        return data

    def _fetch_json(self, rid: str, name: str) -> dict[str, Any] | None:
        branch = self.branch_for(rid)
        if not self._remote_branch_exists(branch):
            return None
        tip = self._fetch_tip(branch)
        raw = self._read_file(tip, self._path(rid, name))
        return None if raw is None else self._parse(raw, rid)

    def _commit_file(self, *, parent: str, path: str, content: str, message: str) -> str:
        """Commit ``parent``'s tree plus one file, using a throwaway index."""
        with tempfile.TemporaryDirectory(prefix="graphdeck-idx-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            self._git("read-tree", parent, env_extra=env)
            blob = self._git("hash-object", "-w", "--stdin", input=content).strip()
            self._git(
                "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env_extra=env
            )
            tree = self._git("write-tree", env_extra=env).strip()
        return self._git("commit-tree", tree, "-p", parent, "-m", message).strip()

    def _push(self, commit: str, branch: str) -> None:
        # Plain push: the remote refuses anything that is not a fast-forward.
        self._git("push", "--quiet", self.remote, f"{commit}:refs/heads/{branch}")


# --- webhooks ------------------------------------------------------------------

EVENTS = ("approval_requested", "approval_granted", "approval_rejected", "change_applied")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _validate_webhook_url(url: str) -> str:
    """Return a loggable ``scheme://host[:port]`` or raise ValueError."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Invalid webhook URL: {exc}") from exc
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("Webhook URL must include a host.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Webhook URL must not embed credentials (user:pass@).")
    scheme = parts.scheme.lower()
    if scheme == "http":
        if host not in _LOCAL_HOSTS:
            raise ValueError("Webhook URL must use https (http is only allowed for localhost).")
    elif scheme != "https":
        raise ValueError("Webhook URL must use https.")
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port else "")


def _summary(event: str, request: ChangeRequest, approval: Approval | None) -> str:
    rid, what = request.request_id, f"{request.action_name} ({request.risk} risk)"
    who = approval.approver if approval is not None else "unknown"
    if event == "approval_requested":
        return f"Approval requested: {what} by {request.requester} [request {rid}]"
    if event == "approval_granted":
        return f"Approved by {who}: {what} requested by {request.requester} [request {rid}]"
    if event == "approval_rejected":
        return f"Rejected by {who}: {what} requested by {request.requester} [request {rid}]"
    return f"Change applied: {what} requested by {request.requester} [request {rid}]"


def build_payload(
    event: str, request: ChangeRequest, approval: Approval | None = None
) -> dict[str, Any]:
    """Webhook body for ``event``. Excludes params, bulk items and preview."""
    if event not in EVENTS:
        raise ValueError(f"Unknown webhook event {event!r}; expected one of {', '.join(EVENTS)}")
    rid = request.request_id
    payload: dict[str, Any] = {
        "text": _summary(event, request, approval),
        "event": event,
        "request_id": rid,
        "action_id": request.action_id,
        "action_name": request.action_name,
        "risk": request.risk,
        "requester": request.requester,
        "tenant_id": request.tenant_id,
        "content_hash": request.content_hash,
        "objects": len(request.bulk_items) or 1,
        "reason": request.reason,
        "commands": {
            "approve": f"graphdeck approve {rid} --as <name>",
            "apply": f"graphdeck apply {rid}",
        },
    }
    if approval is not None:
        payload["approver"] = approval.approver
        payload["decision"] = approval.decision
        payload["comment"] = approval.comment
    result: dict[str, Any] = redact(payload)
    return result


class WebhookNotifier:
    """Best-effort JSON POST to an incoming webhook (Teams, Slack, ...)."""

    def __init__(
        self, url: str, *, client: httpx.Client | None = None, timeout: float = 10.0
    ) -> None:
        self._target = _validate_webhook_url(url)  # scheme + host only, safe to log
        self.url = url
        self.client = client
        self.timeout = timeout

    def notify(self, event: str, request: ChangeRequest, approval: Approval | None = None) -> bool:
        """POST the event; True on 2xx. Never raises."""
        try:
            payload = build_payload(event, request, approval)
            if self.client is not None:
                response = self.client.post(self.url, json=payload)
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.post(self.url, json=payload)
        except Exception as exc:  # notification must never block a change request
            log.warning(
                "Webhook %s to %s failed: %s", event, self._target, type(exc).__name__
            )
            return False
        if 200 <= response.status_code < 300:
            return True
        log.warning(
            "Webhook %s to %s returned HTTP %s", event, self._target, response.status_code
        )
        return False
