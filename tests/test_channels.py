"""Approval channels: git branches for change requests, webhook notifications."""

import json
import logging
import shutil
import subprocess

import httpx
import pytest

from msgraph_tui.compliance.approval import ChangeRequest, content_hash_for, make_approval
from msgraph_tui.compliance.channels import (
    GitApprovalChannel,
    GitChannelError,
    WebhookNotifier,
    build_payload,
)
from msgraph_tui.core.redaction import REDACTED


def _request(**overrides) -> ChangeRequest:
    base = dict(
        action_id="users.set_account_enabled", action_name="Disable", params={"user_id": "u1"},
        risk="high", requester="alice@x", tenant_id="t",
        preview="PATCH /users/jane.doe@contoso.example",
        content_hash=content_hash_for("users.set_account_enabled", {"user_id": "u1"}, "t"),
        reason={"reason": "leaver offboarding", "ticket": "HR-9"},
    )
    base.update(overrides)
    return ChangeRequest(**base)


# --- git channel ------------------------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(*args, cwd=None) -> str:
    cmd = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def _clone(origin, dest, name):
    _git("clone", "-q", str(origin), str(dest))
    _git("config", "user.name", name, cwd=dest)
    _git("config", "user.email", f"{name}@example.test", cwd=dest)
    _git("config", "commit.gpgsign", "false", cwd=dest)
    return dest


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    seed = _clone(origin, tmp_path / "seed", "seed")
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    (seed / "README.md").write_text("approvals\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    base = _git("rev-parse", "HEAD", cwd=seed)
    requester = _clone(origin, tmp_path / "requester", "alice")
    approver = _clone(origin, tmp_path / "approver", "bob")
    return {"origin": origin, "requester": requester, "approver": approver, "base": base}


@needs_git
def test_full_request_approval_round_trip(repos):
    req_ch = GitApprovalChannel(repos["requester"])
    appr_ch = GitApprovalChannel(repos["approver"])
    req = _request()
    head_before = _git("symbolic-ref", "HEAD", cwd=repos["requester"])

    branch = req_ch.publish_request(req)
    assert branch == f"graphdeck/cr-{req.request_id}"
    path = f"change-requests/{req.request_id}/request.json"
    raw = _git("show", f"refs/heads/{branch}:{path}", cwd=repos["origin"])
    assert json.loads(raw) == json.loads(json.dumps(req.to_dict()))
    subject = _git("log", "-1", "--format=%s %an", f"refs/heads/{branch}", cwd=repos["origin"])
    assert subject == f"Change request {req.request_id}: Disable (high risk) by alice@x alice"
    # the branch descends from main, so a PR into main is possible
    _git("merge-base", "--is-ancestor", repos["base"], f"refs/heads/{branch}", cwd=repos["origin"])

    fetched = appr_ch.fetch_request(req.request_id)
    assert fetched == req
    assert appr_ch.fetch_approval(req.request_id) is None     # branch exists, no decision yet

    approval = make_approval(fetched, "bob@x", approve=True, comment="ok")
    appr_ch.publish_approval(approval)
    assert req_ch.fetch_approval(req.request_id) == approval
    subject = _git("log", "-1", "--format=%s", f"refs/heads/{branch}", cwd=repos["origin"])
    assert subject == f"Approved change request {req.request_id} by bob@x"

    # no overwriting: same request twice, decision twice
    with pytest.raises(GitChannelError, match="already published"):
        req_ch.publish_request(req)
    with pytest.raises(GitChannelError, match="already has"):
        appr_ch.publish_approval(make_approval(fetched, "carol@x", approve=False))

    # the requester's checkout was never touched
    assert _git("status", "--porcelain", cwd=repos["requester"]) == ""
    assert _git("symbolic-ref", "HEAD", cwd=repos["requester"]) == head_before
    assert _git("rev-parse", "HEAD", cwd=repos["requester"]) == repos["base"]


@needs_git
def test_unknown_ids_and_missing_branch(repos):
    ch = GitApprovalChannel(repos["approver"])
    assert ch.fetch_request("nope123") is None
    assert ch.fetch_approval("nope123") is None
    with pytest.raises(GitChannelError, match="No published"):
        ch.publish_approval(make_approval(_request(request_id="nope123"), "bob@x", approve=True))
    with pytest.raises(GitChannelError, match="Invalid"):
        ch.fetch_request("../..")


@needs_git
def test_ids_are_sanitised(repos):
    ch = GitApprovalChannel(repos["requester"])
    req = _request(request_id="../../x")
    assert ch.branch_for(req.request_id) == "graphdeck/cr-x"
    assert ch.publish_request(req) == "graphdeck/cr-x"
    _git("cat-file", "-e", "refs/heads/graphdeck/cr-x:change-requests/x/request.json",
         cwd=repos["origin"])
    assert GitApprovalChannel(repos["approver"]).fetch_request("../../x") == req


@needs_git
def test_mismatched_request_file_is_refused(repos):
    ch = GitApprovalChannel(repos["requester"])
    ch.publish_request(_request(request_id="abc"))
    # plant another request's content under this id via a regular commit + push
    clone = repos["approver"]
    _git("fetch", "-q", "origin", "graphdeck/cr-abc", cwd=clone)
    _git("checkout", "-q", "-b", "tamper", "FETCH_HEAD", cwd=clone)
    target = clone / "change-requests" / "abc" / "request.json"
    target.write_text(json.dumps(_request(request_id="other").to_dict()))
    _git("commit", "-qam", "tamper", cwd=clone)
    _git("push", "-q", "origin", "HEAD:refs/heads/graphdeck/cr-abc", cwd=clone)
    with pytest.raises(GitChannelError, match="different id"):
        ch.fetch_request("abc")


@needs_git
def test_approval_must_match_request_hash(repos):
    ch = GitApprovalChannel(repos["requester"])
    req = _request()
    ch.publish_request(req)
    approval = make_approval(req, "bob@x", approve=True)
    approval.content_hash = "0" * 64
    with pytest.raises(GitChannelError, match="content hash"):
        GitApprovalChannel(repos["approver"]).publish_approval(approval)


@needs_git
def test_unreachable_remote_raises_with_stderr(repos, tmp_path):
    ch = GitApprovalChannel(repos["requester"], remote=str(tmp_path / "missing.git"))
    with pytest.raises(GitChannelError) as err:
        ch.fetch_request("abc")
    assert "ls-remote" in str(err.value) and len(str(err.value)) < 450


@needs_git
def test_compare_hint(repos):
    ch = GitApprovalChannel(repos["requester"])
    assert ch.compare_hint("abc") == "Open a PR from graphdeck/cr-abc into main"
    for url in ("git@github.com:contoso/approvals.git", "https://github.com/contoso/approvals"):
        _git("remote", "set-url", "origin", url, cwd=repos["requester"])
        assert ch.compare_hint("abc") == (
            "Open a PR from graphdeck/cr-abc into main: "
            "https://github.com/contoso/approvals/compare/main...graphdeck/cr-abc?expand=1"
        )


# --- webhook ----------------------------------------------------------------

def _capture(status=200):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


def test_payload_excludes_personal_data_and_is_redacted():
    req = _request(reason={"reason": "rotate password=hunter2secret", "ticket": "HR-9"},
                   bulk_items=[{"user_id": "u1"}, {"user_id": "u2"}])
    payload = build_payload("approval_requested", req)
    assert not {"params", "bulk_items", "preview"} & set(payload)
    assert payload["objects"] == 2
    assert payload["commands"] == {
        "approve": f"graphdeck approve {req.request_id} --as <name>",
        "apply": f"graphdeck apply {req.request_id}",
    }
    assert "hunter2secret" not in json.dumps(payload) and REDACTED in payload["reason"]["reason"]
    assert "approver" not in payload
    assert build_payload("change_applied", _request())["objects"] == 1
    with pytest.raises(ValueError):
        build_payload("bogus", req)


def test_notify_posts_json():
    seen, client = _capture()
    req = _request()
    approval = make_approval(req, "bob@x", approve=False, comment="not now")
    notifier = WebhookNotifier("https://hooks.example.test/in/abc?sig=s3cr3t", client=client)
    assert notifier.notify("approval_rejected", req, approval) is True
    body = json.loads(seen[0].content)
    assert seen[0].method == "POST"
    assert body["request_id"] == req.request_id and body["event"] == "approval_rejected"
    assert body["approver"] == "bob@x" and body["decision"] == "reject"
    assert body["comment"] == "not now" and "bob@x" in body["text"]
    assert "preview" not in body and "params" not in body


def test_notify_failures_return_false_and_hide_url(caplog):
    _, client = _capture(status=500)
    url = "https://hooks.example.test/in/abc?sig=s3cr3t"
    caplog.set_level(logging.WARNING)
    assert WebhookNotifier(url, client=client).notify("approval_requested", _request()) is False

    def boom(request):
        raise httpx.ConnectError("down")

    failing = httpx.Client(transport=httpx.MockTransport(boom))
    assert WebhookNotifier(url, client=failing).notify("approval_requested", _request()) is False
    assert WebhookNotifier(url, client=failing).notify("bogus", _request()) is False
    assert "https://hooks.example.test" in caplog.text
    assert "s3cr3t" not in caplog.text and "/in/abc" not in caplog.text


@pytest.mark.parametrize("url", [
    "http://example.com/hook",
    "https://user:pass@example.com/hook",
    "https://token@example.com/hook",
    "ftp://example.com/hook",
    "https:///nohost",
    "not a url",
])
def test_url_validation_rejects(url):
    with pytest.raises(ValueError):
        WebhookNotifier(url)


@pytest.mark.parametrize("url", [
    "http://localhost:8080/hook",
    "http://127.0.0.1/hook",
    "http://[::1]:9000/hook",
    "https://example.webhook.office.com/webhookb2/x",
])
def test_url_validation_accepts(url):
    WebhookNotifier(url)
