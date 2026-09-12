"""Application context: wires config, registries, engines and services.

Used identically by the TUI and by tests, so everything the app can do is
exercisable without a terminal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler

from ..compliance.audit import AuditLog
from ..compliance.rollback import RollbackStore
from ..core.actions import ActionRegistry
from ..core.config import AppConfig, Mode
from ..core.providers import ProviderRegistry
from ..core.redaction import redact_text
from ..modules import build_registry
from ..providers.graph_rest import GraphRestProvider, MsalClientCredentialTokenProvider
from ..providers.graph_sdk import GraphSdkProvider
from ..providers.mock import MockProvider
from ..providers.powershell import make_powershell_providers
from .executor import Executor, SessionInfo


@dataclass
class AppContext:
    config: AppConfig
    actions: ActionRegistry
    providers: ProviderRegistry
    audit_log: AuditLog
    rollback_store: RollbackStore
    session: SessionInfo
    executor: Executor
    mock: MockProvider
    graph_rest: GraphRestProvider


class _RedactingFilter(logging.Filter):
    """Guarantees redaction at the stream, not the call site — so any future
    log line carrying untrusted data cannot leak secrets to disk."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:  # pragma: no cover - never let logging crash the app
            pass
        return True


def _setup_debug_logging(config: AppConfig) -> None:
    """Debug log is a separate, redacted, rotated stream — kept apart from the
    audit log (PRD data handling)."""
    handler = TimedRotatingFileHandler(
        config.debug_log_path,
        when="midnight",
        backupCount=max(1, config.debug_log_retention_days),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger("graphdeck")
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        root.addHandler(handler)


def build_context(config: AppConfig) -> AppContext:
    config.ensure_dirs()
    _setup_debug_logging(config)

    actions = build_registry()
    providers = ProviderRegistry()

    mock = MockProvider(fixtures_dir=config.fixtures_dir)
    providers.register(mock)
    # In delegated LIVE mode, the token provider is attached later by the
    # Session screen's interactive sign-in flow (SessionView.sign_in). In
    # app-only LIVE mode there is no interactive step to wait for, so we
    # attach a client-credential token provider here, headlessly. Until
    # attached (either way), graph_rest reports itself unavailable.
    graph_rest = GraphRestProvider(config)
    providers.register(graph_rest)
    app_only_provider: MsalClientCredentialTokenProvider | None = None
    if config.mode is Mode.LIVE and config.auth_mode == "app-only":
        try:
            app_only_provider = MsalClientCredentialTokenProvider(config)
            graph_rest.attach_token_provider(app_only_provider)
        except Exception as exc:  # missing msal, unreadable/misconfigured cert, etc.
            logging.getLogger("graphdeck.context").warning(
                "App-only auth not attached: %s", redact_text(str(exc))
            )
    providers.register(GraphSdkProvider())
    # In live mode, workload providers share one persistent PowerShell host so a
    # single Connect-ExchangeOnline/Teams/SPO/SCC is reused across commands.
    for ps in make_powershell_providers(
        config.powershell_executable, persistent=config.mode is Mode.LIVE
    ):
        providers.register(ps)

    if config.mode in (Mode.MOCK, Mode.DRY_RUN):
        org = mock.tenant.organization
        session = SessionInfo(
            actor="mock-admin@contoso.example",
            tenant_id=org["id"],
            tenant_name=org["displayName"],
            auth_mode="mock" if config.mode is Mode.MOCK else "dry-run (mock data)",
        )
    elif app_only_provider is not None:
        # app-only auth was attached headlessly above; no interactive sign-in.
        session = SessionInfo(
            actor=app_only_provider.account_label(),
            tenant_id=config.tenant_id or "unknown-tenant",
            tenant_name="",
            auth_mode="app-only",
        )
    else:
        session = SessionInfo(
            actor="not signed in",
            tenant_id=config.tenant_id or "unknown-tenant",
            tenant_name="",
            auth_mode="none",
        )

    audit_log = AuditLog(config.audit_dir / "audit.jsonl", hmac_key=config.audit_hmac_key())
    rollback_store = RollbackStore(config.snapshots_dir)
    executor = Executor(config, actions, providers, audit_log, rollback_store, session)
    return AppContext(
        config=config,
        actions=actions,
        providers=providers,
        audit_log=audit_log,
        rollback_store=rollback_store,
        session=session,
        executor=executor,
        mock=mock,
        graph_rest=graph_rest,
    )
