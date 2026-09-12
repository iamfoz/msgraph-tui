"""App-only (client-credential) certificate auth: F-AUTH-2.

Covers config plumbing for the certificate, the MSAL client-credential token
provider (against a monkeypatched msal.ConfidentialClientApplication so no
real network/crypto is exercised), and build_context's headless auto-attach.
"""

from __future__ import annotations

import json
import sys

import msal
import pytest

from msgraph_tui.core.config import AppConfig, Mode, load_config
from msgraph_tui.core.redaction import redact_text
from msgraph_tui.providers.graph_rest import MsalClientCredentialTokenProvider
from msgraph_tui.services.context import build_context

FAKE_PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "not-a-real-key-just-test-fixture-bytes\n"
    "-----END PRIVATE KEY-----\n"
)


def _write_cert(tmp_path):
    cert_path = tmp_path / "app.pem"
    cert_path.write_text(FAKE_PEM)
    return cert_path


# --- config -----------------------------------------------------------------


def test_config_loads_app_only_fields_and_coerces_path(tmp_path):
    cert_path = _write_cert(tmp_path)
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "auth_mode": "app-only",
                "client_certificate_path": str(cert_path),
                "client_certificate_thumbprint": "ABCDEF0123456789",
            }
        )
    )
    cfg = load_config(cfg_file)
    assert cfg.auth_mode == "app-only"
    assert cfg.client_certificate_path == cert_path
    assert isinstance(cfg.client_certificate_path, type(tmp_path))  # Path


def test_client_certificate_reads_thumbprint_and_pem(tmp_path):
    cert_path = _write_cert(tmp_path)
    cfg = AppConfig(
        client_certificate_path=cert_path,
        client_certificate_thumbprint="ABCDEF0123456789",
    )
    result = cfg.client_certificate()
    assert result == ("ABCDEF0123456789", FAKE_PEM)


def test_client_certificate_none_when_unset():
    cfg = AppConfig()
    assert cfg.client_certificate() is None


def test_client_certificate_none_when_thumbprint_missing(tmp_path):
    cert_path = _write_cert(tmp_path)
    cfg = AppConfig(client_certificate_path=cert_path, client_certificate_thumbprint=None)
    assert cfg.client_certificate() is None


# --- MsalClientCredentialTokenProvider ---------------------------------------


class _FakeConfidentialClientApplication:
    """Stands in for msal.ConfidentialClientApplication."""

    last_client_credential: dict | None = None

    def __init__(self, client_id, authority=None, client_credential=None):
        self.client_id = client_id
        self.authority = authority
        # Recorded on the class so tests can assert on it without threading
        # instances through; also proves the private key never leaves this
        # fake as anything but an opaque value under an obviously-sensitive key.
        _FakeConfidentialClientApplication.last_client_credential = client_credential

    def acquire_token_silent(self, scopes, account=None):
        return None

    def acquire_token_for_client(self, scopes):
        return {"access_token": "fake-app-only-access-token", "token_type": "Bearer"}


def _app_only_config(tmp_path) -> AppConfig:
    cert_path = _write_cert(tmp_path)
    return AppConfig(
        mode=Mode.LIVE,
        tenant_id="tenant-1",
        client_id="client-1",
        auth_mode="app-only",
        client_certificate_path=cert_path,
        client_certificate_thumbprint="ABCDEF0123456789",
    )


def test_client_credential_provider_acquires_token(monkeypatch, tmp_path):
    monkeypatch.setattr(msal, "ConfidentialClientApplication", _FakeConfidentialClientApplication)
    provider = MsalClientCredentialTokenProvider(_app_only_config(tmp_path))
    token = provider.get_token()
    assert token == "fake-app-only-access-token"
    assert provider.account_label() == "app client-1 (app-only)"


def test_client_credential_provider_does_not_leak_private_key(monkeypatch, tmp_path):
    monkeypatch.setattr(msal, "ConfidentialClientApplication", _FakeConfidentialClientApplication)
    config = _app_only_config(tmp_path)
    provider = MsalClientCredentialTokenProvider(config)
    provider.get_token()

    # The provider itself must not retain the PEM as a plain attribute.
    for value in vars(provider).values():
        assert FAKE_PEM not in repr(value)

    # If the key ever ended up in a preview/exception string, redaction must
    # still strip it (defense in depth for the redaction layer itself).
    leaked = f"boom: private_key={FAKE_PEM} access_token=fake-app-only-access-token"
    scrubbed = redact_text(leaked)
    assert "not-a-real-key" not in scrubbed
    assert "fake-app-only-access-token" not in scrubbed


def test_client_credential_provider_requires_msal(monkeypatch, tmp_path):
    # Simulate the optional dependency not being installed: `import msal`
    # raises ImportError when sys.modules maps the name to None.
    monkeypatch.setitem(sys.modules, "msal", None)
    with pytest.raises(RuntimeError, match="msal"):
        MsalClientCredentialTokenProvider(_app_only_config(tmp_path))


def test_client_credential_provider_requires_cert(tmp_path):
    config = AppConfig(mode=Mode.LIVE, tenant_id="t", client_id="c", auth_mode="app-only")
    with pytest.raises(RuntimeError, match="certificate"):
        MsalClientCredentialTokenProvider(config)


# --- build_context auto-attach ----------------------------------------------


def test_build_context_auto_attaches_app_only_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(msal, "ConfidentialClientApplication", _FakeConfidentialClientApplication)
    config = _app_only_config(tmp_path)
    config.state_dir = tmp_path / "state"
    ctx = build_context(config)
    assert ctx.graph_rest.is_available()
    assert ctx.session.auth_mode == "app-only"
    assert "client-1" in ctx.session.actor


def test_build_context_leaves_unattached_when_cert_missing(tmp_path, caplog):
    config = AppConfig(
        mode=Mode.LIVE,
        tenant_id="tenant-1",
        client_id="client-1",
        auth_mode="app-only",
        state_dir=tmp_path / "state",
    )
    ctx = build_context(config)
    assert not ctx.graph_rest.is_available()
    assert ctx.session.auth_mode == "none"


def test_build_context_delegated_live_mode_unaffected(tmp_path):
    """Delegated LIVE mode keeps requiring the interactive sign-in flow."""
    config = AppConfig(
        mode=Mode.LIVE,
        tenant_id="tenant-1",
        client_id="client-1",
        auth_mode="delegated",
        state_dir=tmp_path / "state",
    )
    ctx = build_context(config)
    assert not ctx.graph_rest.is_available()
    assert ctx.session.actor == "not signed in"


def test_build_context_mock_mode_unaffected(tmp_path):
    config = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    ctx = build_context(config)
    assert ctx.session.auth_mode == "mock"
