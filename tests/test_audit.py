"""Audit log: hash chaining, tamper detection, redaction, evidence pack."""

import json

from msgraph_tui.compliance.audit import AuditEvent, AuditLog, ChangeReason
from msgraph_tui.compliance.evidence import generate_evidence_pack
from msgraph_tui.core.redaction import REDACTED


def _event(i: int, **overrides) -> AuditEvent:
    base = dict(
        actor="admin@contoso.example",
        tenant_id="t1",
        action_id=f"users.action{i}",
        operation_id=f"op-{i}",
        provider="mock",
        risk="medium",
        event_type="change",
        result={"success": True},
    )
    base.update(overrides)
    return AuditEvent(**base)


def test_chain_appends_and_verifies(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append(_event(i))
    result = log.verify()
    assert result.ok and result.entries == 5


def test_chain_survives_reopen(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).append(_event(1))
    AuditLog(path).append(_event(2))  # new instance must continue the chain
    assert AuditLog(path).verify().ok


def test_tampered_content_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append(_event(i))
    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["actor"] = "attacker@evil.example"
    lines[1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    result = AuditLog(path).verify()
    assert not result.ok
    assert result.first_bad_line == 2


def test_deleted_entry_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append(_event(i))
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not AuditLog(path).verify().ok


def test_secrets_never_stored(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(_event(
        1,
        before_state={"password": "hunter2", "displayName": "Ada"},
        request_preview="Authorization: Bearer abc123456789def",
    ))
    raw = (tmp_path / "audit.jsonl").read_text()
    assert "hunter2" not in raw
    assert "abc123456789def" not in raw
    assert REDACTED in raw
    assert "Ada" in raw  # non-secret data preserved


def test_reason_metadata_recorded(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    reason = ChangeReason(reason="offboarding", ticket="CHG-42", approval_ref="APPR-7")
    log.append(_event(1, reason=reason.to_dict()))
    entry = log.entries()[0]
    assert entry["reason"] == {"reason": "offboarding", "ticket": "CHG-42", "approval_ref": "APPR-7"}


def test_evidence_pack(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append(_event(1, risk="high"))
    log.append(_event(2, result={"success": False}))
    log.append(_event(3, event_type="rollback", rollback_of_operation="op-1"))
    pack = generate_evidence_pack(log, tmp_path / "exports")
    manifest = json.loads((pack / "manifest.json").read_text())
    assert manifest["chain_verification_ok"] is True
    assert manifest["changes"] == 3
    assert manifest["high_risk"] == 1
    assert manifest["failures"] == 1
    assert manifest["rollbacks"] == 1
    assert "does not by itself constitute ISO/IEC 27001 compliance" in manifest["disclaimer"]
    for name in manifest["file_hashes_sha256"]:
        assert (pack / name).exists()
