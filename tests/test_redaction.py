"""The redaction service must scrub every secret shape from every output path."""

from msgraph_tui.core.redaction import REDACTED, redact, redact_text


def test_jwt_redacted():
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
    assert REDACTED in redact_text(f"token was {jwt} ok")
    assert "SflKx" not in redact_text(jwt)


def test_bearer_header_redacted():
    out = redact_text("Authorization: Bearer abcdef123456789TOKEN")
    assert "abcdef123456789TOKEN" not in out
    assert "Bearer" in out


def test_client_secret_kv_redacted():
    out = redact_text("client_secret=Sup3rS3cretValue&grant_type=client_credentials")
    assert "Sup3rS3cretValue" not in out
    assert "grant_type=client_credentials" in out


def test_pem_block_redacted():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
    assert "MIIEvQ" not in redact_text(pem)


def test_sensitive_keys_redacted_recursively():
    data = {
        "displayName": "Ada",
        "password": "hunter2",
        "nested": {"clientSecret": "abc", "list": [{"refresh_token": "xyz"}]},
        "authorization": "Basic 1234567890abc",
    }
    out = redact(data)
    assert out["displayName"] == "Ada"
    assert out["password"] == REDACTED
    assert out["nested"]["clientSecret"] == REDACTED
    assert out["nested"]["list"][0]["refresh_token"] == REDACTED
    assert out["authorization"] == REDACTED


def test_safe_metadata_keys_not_redacted():
    data = {"certificate_thumbprint": "AB:CD", "token_expiry": "2026-01-01", "secret_hint": "***1"}
    out = redact(data)
    assert out == data


def test_non_string_values_pass_through():
    assert redact({"count": 5, "ok": True, "none": None}) == {"count": 5, "ok": True, "none": None}
