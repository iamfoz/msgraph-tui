"""Four-eyes approval workflow (PRD F-COMP-1).

The write pipeline already separates *planning* from *committing*. Four-eyes
splits those across two people:

1. The **requester** plans a change. If policy requires approval for the
   action's risk level, the confirmation gate submits a :class:`ChangeRequest`
   instead of executing.
2. An **approver** — a different identity — reviews the request and records an
   :class:`Approval` (``graphdeck approve <id> --as <name>``).
3. The change executes only with a valid approval: the approval must match the
   *exact* change (a stable content hash over action + params + tenant, so an
   edited request invalidates it), be an ``approve`` decision, and come from
   someone other than the requester and the executor (segregation of duties).

Signing, strongest first:

- **Ed25519 per approver** (``approver_trust_store_path``): each approver signs
  with their own private key; the approval verifies only against a public key
  registered to that approver in the trust store. This binds the approval to a
  person, and signed approval files can travel over any channel (git branch,
  email, chat) and be imported safely. See :mod:`.signing`.
- **Shared HMAC** (``approval_signing_key_path``): tamper-evident, but proves
  only that *someone holding the key* approved — a process control, not
  non-repudiation.
- Unsigned: typed identities only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..core.actions import RESERVED_PARAMS, ActionDefinition
from ..core.envelope import utc_now_iso
from ..core.redaction import redact
from .audit import canonical_json
from .signing import ED25519, HMAC_SHA256, ApproverKey, TrustStore, verify_signature

PENDING, APPROVED, REJECTED, APPLIED = "pending", "approved", "rejected", "applied"


def content_hash_for(action_id: str, params: dict[str, Any], tenant_id: str) -> str:
    """Stable, reproducible hash of exactly what would run.

    Excludes volatile fields (ids, timestamps) so re-planning the same change
    yields the same hash — and changing any param yields a different one.
    """
    stable = {k: v for k, v in params.items() if k not in RESERVED_PARAMS}
    payload = canonical_json({"action_id": action_id, "params": stable, "tenant_id": tenant_id})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bulk_content_hash(action_id: str, items: list[dict[str, Any]], tenant_id: str) -> str:
    """One hash covering every object in a bulk change (order-sensitive)."""
    parts = [content_hash_for(action_id, p, tenant_id) for p in items]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _sign(key: bytes, content_hash: str, approver: str, decision: str) -> str:
    message = f"{content_hash}|{approver}|{decision}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _ed25519_message(approval: Approval) -> bytes:
    """Everything an Ed25519 approval vouches for, canonically encoded."""
    return canonical_json({
        "v": 1,
        "request_id": approval.request_id,
        "content_hash": approval.content_hash,
        "approver": approval.approver,
        "decision": approval.decision,
        "comment": approval.comment,
        "signed_at": approval.signed_at,
        "key_id": approval.key_id,
    }).encode("utf-8")


@dataclass
class ChangeRequest:
    action_id: str
    action_name: str
    params: dict[str, Any]
    risk: str
    requester: str
    tenant_id: str
    preview: str
    content_hash: str
    reason: dict[str, str] = field(default_factory=dict)
    bulk_items: list[dict[str, Any]] = field(default_factory=list)  # set for bulk requests
    status: str = PENDING
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(default_factory=utc_now_iso)

    @property
    def is_bulk(self) -> bool:
        return bool(self.bulk_items)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeRequest:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Approval:
    request_id: str
    content_hash: str
    approver: str
    decision: str  # "approve" | "reject"
    comment: str = ""
    signature: str | None = None
    signed_at: str = field(default_factory=utc_now_iso)
    signature_alg: str | None = None  # "ed25519" | "hmac-sha256" | None (unsigned)
    key_id: str | None = None         # Ed25519 public-key fingerprint

    @property
    def approved(self) -> bool:
        return self.decision == "approve"

    @property
    def algorithm(self) -> str | None:
        """Signature algorithm; approvals from before per-approver keys are HMAC."""
        if not self.signature:
            return None
        return self.signature_alg or HMAC_SHA256

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Approval:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def assert_requestable(action: ActionDefinition, params: dict[str, Any]) -> None:
    """Refuse to persist a change request that would put secrets at rest.

    Requests are stored locally so they can be applied later; an action with a
    secret parameter (or params that redaction would alter) must be run
    interactively instead."""
    if any(p.secret for p in action.params):
        raise ValueError(
            f"{action.id} has secret parameters and cannot be queued for approval; "
            "run it interactively with the approver present."
        )
    if redact(params) != params:
        raise ValueError(
            f"{action.id} parameters contain secret-shaped values; refusing to store them."
        )


def expected_content_hash(request: ChangeRequest) -> str:
    """Recompute the hash from the request's own action/params/tenant."""
    if request.is_bulk:
        return bulk_content_hash(request.action_id, request.bulk_items, request.tenant_id)
    return content_hash_for(request.action_id, request.params, request.tenant_id)


def check_request_integrity(request: ChangeRequest) -> None:
    """Refuse a request whose stated hash doesn't match its parameters.

    An approver reviews the params, but signs the *hash*. A request file edited
    to show harmless params next to the hash of a harmful change would trick
    them into approving the harmful one, so the hash is always re-derived."""
    if expected_content_hash(request) != request.content_hash:
        raise ValueError(
            f"Change request {request.request_id} is inconsistent: its content hash does not "
            "match its parameters (edited or corrupted). Do not approve it."
        )


def make_approval(
    request: ChangeRequest,
    approver: str,
    *,
    approve: bool,
    comment: str = "",
    key: bytes | None = None,
    private_key: ApproverKey | None = None,
) -> Approval:
    if not approver.strip():
        raise ValueError("An approver identity is required.")
    if approver.strip().lower() == request.requester.strip().lower():
        raise ValueError("Segregation of duties: the requester cannot approve their own change.")
    check_request_integrity(request)
    approval = Approval(
        request_id=request.request_id,
        content_hash=request.content_hash,
        approver=approver.strip(),
        decision="approve" if approve else "reject",
        comment=comment,
    )
    if private_key is not None:
        approval.signature_alg = ED25519
        approval.key_id = private_key.key_id
        approval.signature = private_key.sign(_ed25519_message(approval))
    elif key:
        approval.signature_alg = HMAC_SHA256
        approval.signature = _sign(key, approval.content_hash, approval.approver, approval.decision)
    return approval


def verify_signature_only(
    approval: Approval, *, key: bytes | None = None, trust_store: TrustStore | None = None
) -> tuple[bool, str]:
    """Check just the signature against the configured policy.

    A trust store demands an Ed25519 signature from a key registered to the
    named approver; otherwise an HMAC key demands a valid HMAC; with neither,
    unsigned approvals are accepted."""
    if trust_store is not None:
        if approval.algorithm != ED25519:
            return False, "approval is not signed with a personal (Ed25519) approver key"
        public = trust_store.find_key(approval.approver, approval.key_id)
        if public is None:
            return False, (f"key {approval.key_id} is not registered to {approval.approver} "
                           "in the approver trust store")
        if not verify_signature(public, _ed25519_message(approval), approval.signature or ""):
            return False, "approval signature is invalid (tampered or wrong key)"
        return True, f"signed by {approval.approver} (key {approval.key_id})"
    if key is not None:
        if approval.algorithm != HMAC_SHA256:
            return False, "approval is unsigned but a signing key is configured"
        expected = _sign(key, approval.content_hash, approval.approver, approval.decision)
        if not hmac.compare_digest(expected, approval.signature or ""):
            return False, "approval signature is invalid (tampered or wrong key)"
        return True, "HMAC signature valid"
    return True, "unsigned"


def verify_approval(
    approval: Approval | None,
    *,
    content_hash: str,
    requester: str,
    executor: str,
    key: bytes | None = None,
    trust_store: TrustStore | None = None,
) -> tuple[bool, str]:
    """Check an approval authorises this exact change. Returns (ok, reason)."""
    if approval is None:
        return False, "no approval recorded"
    if not approval.approved:
        return False, f"request was rejected by {approval.approver}"
    if approval.content_hash != content_hash:
        return False, "approval does not match this exact change (parameters or tenant differ)"
    approver = approval.approver.strip().lower()
    if approver in (requester.strip().lower(), executor.strip().lower()):
        return False, "segregation of duties: approver must differ from requester and executor"
    ok, why = verify_signature_only(approval, key=key, trust_store=trust_store)
    if not ok:
        return False, why
    return True, f"approved by {approval.approver}"


class ApprovalStore:
    """Local store: one JSON file per request and per approval."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _req_path(self, request_id: str) -> Path:
        return self.directory / f"request-{_safe(request_id)}.json"

    def _appr_path(self, request_id: str) -> Path:
        return self.directory / f"approval-{_safe(request_id)}.json"

    def save_request(self, request: ChangeRequest) -> Path:
        path = self._req_path(request.request_id)
        path.write_text(json.dumps(request.to_dict(), indent=2, default=str), encoding="utf-8")
        return path

    def load_request(self, request_id: str) -> ChangeRequest | None:
        path = self._req_path(request_id)
        if not path.exists():
            return None
        try:
            return ChangeRequest.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def list_requests(self) -> list[ChangeRequest]:
        out = []
        for path in sorted(self.directory.glob("request-*.json")):
            try:
                out.append(ChangeRequest.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError):
                continue
        return sorted(out, key=lambda r: r.created_at)

    def set_status(self, request_id: str, status: str) -> None:
        request = self.load_request(request_id)
        if request is not None:
            request.status = status
            self.save_request(request)

    def save_approval(self, approval: Approval) -> Path:
        path = self._appr_path(approval.request_id)
        path.write_text(json.dumps(approval.to_dict(), indent=2), encoding="utf-8")
        return path

    def load_approval(self, request_id: str) -> Approval | None:
        path = self._appr_path(request_id)
        if not path.exists():
            return None
        try:
            return Approval.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def find_approved(self, content_hash: str) -> tuple[ChangeRequest, Approval] | None:
        """An approved, not-yet-applied request for this exact change, if any."""
        for request in reversed(self.list_requests()):
            if request.content_hash != content_hash or request.status != APPROVED:
                continue
            approval = self.load_approval(request.request_id)
            if approval is not None and approval.approved:
                return request, approval
        return None


def _safe(request_id: str) -> str:
    return "".join(c for c in request_id if c.isalnum() or c in "-_")
