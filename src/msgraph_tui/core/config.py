"""Configuration system.

Config file (JSON) lives at ~/.config/msgraph-tui/config.json by default;
state (audit logs, snapshots, exports) under ~/.local/share/msgraph-tui/.
Environment variables (GRAPHDECK_*) override file values; CLI flags override
both. All locations are documented in docs/user-guide.md.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("graphdeck.config")

# Recognised Microsoft identity/Graph hosts (incl. sovereign clouds). Bearer
# tokens are only ever sent to hosts named by graph_base/authority, so an
# unexpected host here is flagged loudly.
_MICROSOFT_HOSTS = (
    "graph.microsoft.com",
    "graph.microsoft.us",
    "dod-graph.microsoft.us",
    "microsoftgraph.chinacloudapi.cn",
    "login.microsoftonline.com",
    "login.microsoftonline.us",
    "login.partner.microsoftonline.cn",
    "login.chinacloudapi.cn",
)
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class Mode(str, Enum):
    LIVE = "live"
    MOCK = "mock"
    DRY_RUN = "dry-run"


def default_config_dir() -> Path:
    return Path(os.environ.get("GRAPHDECK_CONFIG_DIR", "~/.config/msgraph-tui")).expanduser()


def default_state_dir() -> Path:
    return Path(os.environ.get("GRAPHDECK_STATE_DIR", "~/.local/share/msgraph-tui")).expanduser()


@dataclass
class AppConfig:
    mode: Mode = Mode.MOCK
    tenant_id: str | None = None
    client_id: str | None = None          # app registration for delegated auth
    # "delegated" (interactive device-code sign-in) or "app-only" (unattended
    # client-credential auth via a client certificate). See F-AUTH-2.
    auth_mode: str = "delegated"
    # PEM file containing both the certificate and its private key, used for
    # app-only auth. Never read into this dataclass — see client_certificate().
    client_certificate_path: Path | None = None
    client_certificate_thumbprint: str | None = None
    authority: str = "https://login.microsoftonline.com/{tenant}"
    graph_base: str = "https://graph.microsoft.com"
    allow_beta: bool = False
    require_change_reason: bool = True
    # provider preferences: action id or service name -> provider id
    provider_preferences: dict[str, str] = field(default_factory=dict)
    default_scopes: list[str] = field(
        default_factory=lambda: [
            "User.Read.All",
            "Group.Read.All",
            "Organization.Read.All",
        ]
    )
    page_size: int = 100
    max_pages: int = 20
    audit_retention_days: int = 365
    debug_log_retention_days: int = 30
    log_reads_to_audit: bool = True
    powershell_executable: str | None = None   # auto-detect if None
    state_dir: Path = field(default_factory=default_state_dir)
    fixtures_dir: Path | None = None            # override bundled fixtures
    # Optional path to a key file; when set, the audit chain is HMAC-signed so
    # it cannot be silently reforged without the key. See docs/security-model.md.
    audit_hmac_key_path: Path | None = None

    @property
    def audit_dir(self) -> Path:
        return self.state_dir / "audit"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def exports_dir(self) -> Path:
        return self.state_dir / "exports"

    @property
    def debug_log_path(self) -> Path:
        return self.state_dir / "logs" / "debug.log"

    def ensure_dirs(self) -> None:
        for d in (self.audit_dir, self.snapshots_dir, self.exports_dir, self.debug_log_path.parent):
            d.mkdir(parents=True, exist_ok=True)

    def provider_preference_for(self, action_id: str, service: str) -> str | None:
        return self.provider_preferences.get(action_id) or self.provider_preferences.get(service)

    def audit_hmac_key(self) -> bytes | None:
        """Read the audit signing key, if a key path is configured."""
        if self.audit_hmac_key_path is None:
            return None
        key = Path(self.audit_hmac_key_path).expanduser().read_bytes().strip()
        if not key:
            raise ValueError(f"Audit HMAC key file is empty: {self.audit_hmac_key_path}")
        return key

    def client_certificate(self) -> tuple[str, str] | None:
        """Read the app-only client certificate (thumbprint, private key PEM).

        Returns None when no certificate is configured. The private key is
        read from disk on demand and MUST NEVER be stored on this object,
        logged, previewed, or written to the audit log — treat like
        audit_hmac_key(). Callers must keep the returned key in memory only.
        """
        if self.client_certificate_path is None or not self.client_certificate_thumbprint:
            return None
        pem = Path(self.client_certificate_path).expanduser().read_text()
        if not pem.strip():
            raise ValueError(f"Client certificate file is empty: {self.client_certificate_path}")
        return self.client_certificate_thumbprint, pem

    def validate_endpoints(self) -> list[str]:
        """Reject non-HTTPS endpoints and warn on non-Microsoft hosts, so a
        planted/typo'd config cannot silently ship bearer tokens off-tenant.
        Returns a list of human-readable warnings."""
        warnings: list[str] = []
        for label, url in (("graph_base", self.graph_base), ("authority", self.authority)):
            parsed = urlparse(url.replace("{tenant}", "tenant"))
            if parsed.scheme != "https" and parsed.hostname not in _LOCAL_HOSTS:
                raise ValueError(
                    f"{label} must use https:// (got {url!r}) — refusing to send "
                    "credentials over an insecure or unexpected scheme."
                )
            host = parsed.hostname or ""
            if host not in _LOCAL_HOSTS and not any(
                host == h or host.endswith("." + h) for h in _MICROSOFT_HOSTS
            ):
                warnings.append(
                    f"{label} host {host!r} is not a recognised Microsoft endpoint — "
                    "verify this is intentional (sovereign cloud?) before signing in."
                )
        return warnings


_ENV_MAP = {
    "GRAPHDECK_MODE": "mode",
    "GRAPHDECK_TENANT_ID": "tenant_id",
    "GRAPHDECK_CLIENT_ID": "client_id",
    "GRAPHDECK_ALLOW_BETA": "allow_beta",
}


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or default_config_dir() / "config.json"
    data: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid config file {cfg_path}: {exc}") from exc

    for env, key in _ENV_MAP.items():
        if env in os.environ:
            data[key] = os.environ[env]

    cfg = AppConfig()
    for key, value in data.items():
        if not hasattr(cfg, key):
            continue  # forward-compatible: ignore unknown keys
        if key == "mode":
            value = Mode(value)
        elif (
            key in ("state_dir", "fixtures_dir", "audit_hmac_key_path", "client_certificate_path")
            and value is not None
        ):
            value = Path(value).expanduser()
        elif key == "allow_beta" and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes")
        setattr(cfg, key, value)
    for warning in cfg.validate_endpoints():
        log.warning("%s", warning)
    return cfg


def write_default_config(path: Path | None = None) -> Path:
    cfg_path = path or default_config_dir() / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    if not cfg_path.exists():
        cfg_path.write_text(
            json.dumps(
                {
                    "mode": "mock",
                    "tenant_id": None,
                    "client_id": None,
                    "allow_beta": False,
                    "require_change_reason": True,
                    "provider_preferences": {},
                },
                indent=2,
            )
            + "\n"
        )
    return cfg_path
