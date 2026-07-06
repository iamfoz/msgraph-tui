"""Application context: wires config, registries, engines and services.

Used identically by the TUI and by tests, so everything the app can do is
exercisable without a terminal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..compliance.audit import AuditLog
from ..compliance.rollback import RollbackStore
from ..core.actions import ActionRegistry
from ..core.config import AppConfig, Mode
from ..core.providers import ProviderRegistry
from ..modules import build_registry
from ..providers.graph_rest import GraphRestProvider
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


def _setup_debug_logging(config: AppConfig) -> None:
    """Debug log is a separate stream from the audit log (PRD data handling)."""
    handler = logging.FileHandler(config.debug_log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
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
    graph_rest = GraphRestProvider(config)  # token provider attached at sign-in
    providers.register(graph_rest)
    providers.register(GraphSdkProvider())
    for ps in make_powershell_providers(config.powershell_executable):
        providers.register(ps)

    if config.mode in (Mode.MOCK, Mode.DRY_RUN):
        org = mock.tenant.organization
        session = SessionInfo(
            actor="mock-admin@contoso.example",
            tenant_id=org["id"],
            tenant_name=org["displayName"],
            auth_mode="mock" if config.mode is Mode.MOCK else "dry-run (mock data)",
        )
    else:
        session = SessionInfo(
            actor="not signed in",
            tenant_id=config.tenant_id or "unknown-tenant",
            tenant_name="",
            auth_mode="none",
        )

    audit_log = AuditLog(config.audit_dir / "audit.jsonl")
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
