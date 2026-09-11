"""Security-hardening coverage: redaction gaps, audit integrity, endpoint and
parameter guards. Complements test_redaction.py / test_audit.py."""

from pathlib import Path

import pytest

from msgraph_tui.compliance.audit import AuditEvent, AuditLog
from msgraph_tui.core.config import AppConfig
from msgraph_tui.core.redaction import REDACTED, redact_text
from msgraph_tui.modules import build_registry


# --- redaction: shapes the key=value / key-name rules used to miss ---------

@pytest.mark.parametrize("text,secret", [
    ("Set-Mailbox -Identity x -Password 'S3cret!Value'", "S3cret!Value"),
    ("New-App -ClientSecret abc123XYZ.def", "abc123XYZ.def"),
    ("DefaultEndpointsProtocol=https;AccountKey=aB3xQ==zz99;Endpoint=x", "aB3xQ==zz99"),
    ("{'refresh_token': '0.AYcArealtokenvalue'}", "0.AYcArealtokenvalue"),
    ("client_assertion=eyOPAQUEnotjwtvalue123", "eyOPAQUEnotjwtvalue123"),
    ("connect -passphrase 'my long passphrase'", "my long passphrase"),
])
def test_secret_shapes_are_redacted(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert REDACTED in out


def test_benign_values_not_over_redacted():
    text = "status_code=500 country_code=GB grant_type=client_credentials"
    out = redact_text(text)
    assert out == text  # nothing secret-shaped here


def test_bearer_literal_preserved_but_value_gone():
    out = redact_text("Authorization: Bearer abcdef123456789TOKEN")
    assert "abcdef123456789TOKEN" not in out
    assert "Bearer" in out  # the dedicated bearer rule keeps the scheme label


# --- audit integrity: head anchor (truncation) + HMAC keying ---------------

def _ev(i: int) -> AuditEvent:
    return AuditEvent(actor="a@x", tenant_id="t", action_id=f"act{i}",
                      operation_id=f"op{i}", provider="mock", result={"success": True})


def test_tail_truncation_detected_via_head_anchor(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(5):
        log.append(_ev(i))
    assert log.verify().ok
    # attacker drops the two most recent (most incriminating) entries
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:3]) + "\n")
    result = AuditLog(path).verify()
    assert not result.ok
    assert "count" in result.detail or "anchor" in result.detail


def test_head_anchor_file_written(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_ev(1))
    head = path.with_name("audit.jsonl.head")
    assert head.exists()
    assert '"count":1' in head.read_text().replace(" ", "")


def test_hmac_keying_blocks_forgery_without_key(tmp_path):
    path = tmp_path / "audit.jsonl"
    key = b"operator-signing-key"
    log = AuditLog(path, hmac_key=key)
    for i in range(3):
        log.append(_ev(i))
    assert log.algorithm == "hmac-sha256"
    assert AuditLog(path, hmac_key=key).verify().ok          # right key verifies
    assert not AuditLog(path).verify().ok                    # no key -> fails
    assert not AuditLog(path, hmac_key=b"wrong").verify().ok  # wrong key -> fails


def test_deleted_log_with_surviving_anchor_flagged(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_ev(1))
    path.unlink()  # log wiped but head anchor remains
    result = AuditLog(path).verify()
    assert not result.ok
    assert "missing" in result.detail


# --- config endpoint + key guards ------------------------------------------

def test_non_https_endpoint_rejected():
    with pytest.raises(ValueError, match="https"):
        AppConfig(graph_base="http://graph.microsoft.com").validate_endpoints()


def test_unrecognised_host_warns_not_raises():
    warnings = AppConfig(graph_base="https://attacker.example").validate_endpoints()
    assert warnings and "not a recognised Microsoft endpoint" in warnings[0]


def test_default_and_sovereign_endpoints_clean():
    assert AppConfig().validate_endpoints() == []
    assert AppConfig(graph_base="https://graph.microsoft.us").validate_endpoints() == []


def test_audit_hmac_key_read_from_file(tmp_path):
    key_file = tmp_path / "audit.key"
    key_file.write_text("s3cret-key\n")
    cfg = AppConfig(audit_hmac_key_path=key_file)
    assert cfg.audit_hmac_key() == b"s3cret-key"
    assert AppConfig().audit_hmac_key() is None


# --- reserved control params -----------------------------------------------

def test_only_reserved_underscore_params_allowed():
    action = build_registry().get("users.list")
    assert action.validate_params({"_simulate": "throttled"}) == {"_simulate": "throttled"}
    with pytest.raises(ValueError, match="Unknown parameter"):
        action.validate_params({"_evil": 1})
