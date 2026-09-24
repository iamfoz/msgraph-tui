"""Read-only tenant smoke test.

Runs every *read* action in the registry once, so an operator can point
Graphdeck at their own tenant and confirm every read path actually works
(auth, scopes/roles, provider selection, parsing) before trusting it for
anything else. It never plans or commits a write, and it never mutates the
tenant — see ``run_smoke`` for the defensive guard that enforces this even if
an action's risk metadata were ever wrong.

Detail/get actions that require an id (e.g. ``users.get`` needs ``user_id``)
have no id to work with on their own, so their params are derived from the
first row of a related list action that has already run in this pass — see
``PARAM_SOURCES`` below. An action whose params cannot be derived is recorded
as "skipped", never "failed".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TextIO

from ..core.envelope import utc_now_iso
from ..core.redaction import redact_text

if TYPE_CHECKING:
    from ..core.actions import ActionDefinition
    from ..core.config import AppConfig
    from .context import AppContext

_STATUS_ICON = {"ok": "✓", "failed": "✗", "skipped": "–"}


@dataclass
class SmokeResult:
    action_id: str
    service: str
    provider: str | None
    status: str  # "ok" | "failed" | "skipped"
    duration_ms: int
    rows: int | None
    detail: str


@dataclass
class SmokeReport:
    results: list[SmokeResult]
    mode: str
    tenant: str
    started_at: str
    finished_at: str

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tenant": self.tenant,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": [
                {
                    "action_id": r.action_id,
                    "service": r.service,
                    "provider": r.provider,
                    "status": r.status,
                    "duration_ms": r.duration_ms,
                    "rows": r.rows,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


@dataclass(frozen=True)
class ParamSource:
    """How to derive a detail action's required params from the first row of
    an already-run list action."""

    source_action: str
    # target param name -> field name on the source action's row
    field_map: dict[str, str] = field(default_factory=dict)


# Explicit derivation table, built from the actual param names (modules/*.py)
# and row shapes the mock fixtures/provider produce (fixtures/*.json,
# providers/mock.py). Every read action that declares a required param must
# have an entry here or it is reported "skipped".
PARAM_SOURCES: dict[str, ParamSource] = {
    "users.get": ParamSource("users.list", {"user_id": "id"}),
    "users.licenses": ParamSource("users.list", {"user_id": "id"}),
    "users.memberships": ParamSource("users.list", {"user_id": "id"}),
    "groups.get": ParamSource("groups.list", {"group_id": "id"}),
    "groups.members": ParamSource("groups.list", {"group_id": "id"}),
    "groups.owners": ParamSource("groups.list", {"group_id": "id"}),
    "licenses.users_by_sku": ParamSource("licenses.skus", {"sku_id": "skuId"}),
    "exchange.mailbox.get": ParamSource("exchange.mailboxes.list", {"mailbox_id": "id"}),
    "exchange.mailbox_permissions": ParamSource("exchange.mailboxes.list", {"mailbox_id": "id"}),
    "exchange.inbox_rules": ParamSource("exchange.mailboxes.list", {"mailbox_id": "id"}),
    # Teams has no user list of its own (Get-CsOnlineUser needs an identity to
    # look up); the tenant's Entra ID user list supplies one.
    "teams.user_policies.get": ParamSource("users.list", {"user_id": "id"}),
    "sharepoint.site.get": ParamSource("sharepoint.sites.list", {"site_url": "url"}),
}


def _now_iso() -> str:
    return utc_now_iso()


def _format_error(errors: list[Any], summary: str) -> str:
    if not errors:
        return redact_text(summary or "failed")
    err = errors[0]
    label = err.category.value if err.provider_code is None else f"{err.category.value}/{err.provider_code}"
    text = f"[{label}] {err.message}"
    if err.guidance:
        text += f" — {err.guidance}"
    return redact_text(text)


def _required_params(action: ActionDefinition) -> list[str]:
    return [p.name for p in action.params if p.required]


def _resolve_params(
    action: ActionDefinition, rows_cache: dict[str, list[dict]]
) -> tuple[dict[str, Any] | None, str]:
    """Derive params for a detail action from an already-run list action's
    first row. Returns (params, "") on success, or (None, skip-reason)."""
    required = _required_params(action)
    if not required:
        return {}, ""
    source = PARAM_SOURCES.get(action.id)
    if source is None:
        return None, f"needs {', '.join(required)}"
    rows = rows_cache.get(source.source_action)
    if not rows:
        return None, (
            f"needs {', '.join(required)} (no rows from {source.source_action}, "
            "not run or returned nothing)"
        )
    row = rows[0]
    params: dict[str, Any] = {}
    for target, source_field in source.field_map.items():
        if target not in required and target not in (p.name for p in action.params):
            continue
        value = row.get(source_field)
        if value in (None, ""):
            return None, f"needs {target} (field {source_field!r} missing on {source.source_action} row)"
        params[target] = value
    missing = [name for name in required if name not in params]
    if missing:
        return None, f"needs {', '.join(missing)}"
    return params, ""


async def _run_one(ctx: AppContext, action: ActionDefinition, params: dict[str, Any], timeout_s: float) -> tuple[SmokeResult, list[dict] | None]:
    # Defensive: this function is the only place that calls into the
    # executor, and it refuses point-blank to run a write action, no matter
    # how it got here (a bad filter, a bug in the caller, a future action
    # whose risk was mis-classified).
    # (An explicit raise, not an assert: asserts vanish under ``python -O``.)
    if action.is_write:
        raise RuntimeError(f"smoke test refuses to execute write action {action.id}")

    started = time.monotonic()

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        envelope = await asyncio.wait_for(ctx.executor.read(action.id, params), timeout=timeout_s)
    except TimeoutError:
        return SmokeResult(
            action_id=action.id, service=action.service, provider=None, status="failed",
            duration_ms=_elapsed_ms(), rows=None,
            detail=redact_text(f"timeout after {timeout_s}s"),
        ), None
    except Exception as exc:  # never let one bad action crash the whole smoke run
        return SmokeResult(
            action_id=action.id, service=action.service, provider=None, status="failed",
            duration_ms=_elapsed_ms(), rows=None,
            detail=redact_text(f"{type(exc).__name__}: {exc}"),
        ), None

    duration_ms = _elapsed_ms()
    row_count = len(envelope.data) if isinstance(envelope.data, list) else None
    if envelope.success:
        return SmokeResult(
            action_id=action.id, service=action.service, provider=envelope.provider,
            status="ok", duration_ms=duration_ms, rows=row_count, detail="",
        ), envelope.rows
    return SmokeResult(
        action_id=action.id, service=action.service, provider=envelope.provider,
        status="failed", duration_ms=duration_ms, rows=row_count,
        detail=_format_error(envelope.errors, envelope.error_summary()),
    ), None


async def run_smoke(
    ctx: AppContext, *, services: list[str] | None = None, timeout_s: float = 60.0
) -> SmokeReport:
    """Run every read action in the registry once against ``ctx``.

    Write actions are never executed (see the guard in ``_run_one``);
    when the registry filter somehow lets one through it is recorded as
    "skipped" and nothing is called for it.
    """
    started_at = _now_iso()
    all_actions = list(ctx.actions.all())

    scoped = all_actions
    if services:
        wanted = {s.lower() for s in services}
        # Accept the display name ("Microsoft Teams") or the action prefix ("teams").
        scoped = [
            a for a in all_actions
            if a.service.lower() in wanted or a.id.split(".", 1)[0].lower() in wanted
        ]

    results: dict[str, SmokeResult] = {}
    rows_cache: dict[str, list[dict]] = {}

    write_actions = [a for a in scoped if a.is_write]
    read_actions = [a for a in scoped if not a.is_write]
    no_param_actions = sorted(
        (a for a in read_actions if not _required_params(a)), key=lambda a: a.id
    )
    detail_actions = sorted(
        (a for a in read_actions if _required_params(a)), key=lambda a: a.id
    )

    for action in write_actions:
        results[action.id] = SmokeResult(
            action_id=action.id, service=action.service, provider=None, status="skipped",
            duration_ms=0, rows=None, detail="write action — never run by smoke test",
        )

    # Phase 1: run every list-style read (no required params) first, so their
    # rows are available to derive detail-action params from in phase 2 —
    # this must happen regardless of alphabetical id order (e.g. "users.get"
    # sorts before "users.list").
    for action in no_param_actions:
        result, rows = await _run_one(ctx, action, {}, timeout_s)
        results[action.id] = result
        if rows is not None:
            rows_cache[action.id] = rows

    # Phase 2: detail/get actions, params derived from phase-1 rows.
    for action in detail_actions:
        params, skip_reason = _resolve_params(action, rows_cache)
        if params is None:
            results[action.id] = SmokeResult(
                action_id=action.id, service=action.service, provider=None, status="skipped",
                duration_ms=0, rows=None, detail=skip_reason,
            )
            continue
        result, rows = await _run_one(ctx, action, params, timeout_s)
        results[action.id] = result
        if rows is not None:
            rows_cache[action.id] = rows

    finished_at = _now_iso()
    ordered = [results[a.id] for a in sorted(scoped, key=lambda a: a.id)]
    return SmokeReport(
        results=ordered,
        mode=ctx.config.mode.value,
        tenant=ctx.session.tenant_id,
        started_at=started_at,
        finished_at=finished_at,
    )


def _print_report(report: SmokeReport, out: TextIO) -> None:
    note = ""
    if report.mode == "mock":
        note = "  (mock mode: results reflect bundled fixtures, not a live tenant)"
    print(f"Graphdeck read-only smoke test — mode={report.mode} tenant={report.tenant}{note}", file=out)
    print(f"  started {report.started_at}  finished {report.finished_at}", file=out)
    print(file=out)

    by_service: dict[str, list[SmokeResult]] = {}
    for r in report.results:
        by_service.setdefault(r.service, []).append(r)

    for svc in sorted(by_service):
        print(svc, file=out)
        for r in by_service[svc]:
            icon = _STATUS_ICON.get(r.status, "?")
            provider = r.provider or "-"
            rows = "-" if r.rows is None else str(r.rows)
            detail = r.detail if len(r.detail) <= 80 else r.detail[:77] + "..."
            print(
                f"  {icon} {r.action_id:<34} {provider:<16} rows={rows:<5} "
                f"{r.duration_ms:>6}ms  {detail}",
                file=out,
            )
        print(file=out)

    print(
        f"Summary: {report.ok} ok, {report.failed} failed, {report.skipped} skipped "
        f"({len(report.results)} actions checked; writes are always skipped)",
        file=out,
    )


def _ensure_signed_in(ctx: AppContext, out: TextIO) -> bool:
    """Live + delegated has no TUI sign-in screen here: run device code in the terminal.

    App-only auth is attached by ``build_context``; mock/dry-run need nothing."""
    from ..core.config import Mode

    if ctx.config.mode is not Mode.LIVE or ctx.graph_rest.is_available():
        return True
    from ..providers.graph_rest import MsalDeviceCodeTokenProvider

    try:
        provider = MsalDeviceCodeTokenProvider(
            ctx.config, message_callback=lambda message: print(message, file=out, flush=True)
        )
        provider.get_token()
    except Exception as exc:  # misconfiguration / auth failure is user-facing
        print("Sign-in failed: " + redact_text(str(exc)) + " — check tenant_id and client_id, "
              "or use auth_mode 'app-only'.", file=out)
        return False
    ctx.graph_rest.attach_token_provider(provider)
    ctx.session.actor = provider.account_label()
    ctx.session.auth_mode = "delegated (device code)"
    return True


def smoke_test(
    config: AppConfig,
    *,
    services: list[str] | None = None,
    as_json: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    """Build a context, run the read-only smoke test, print/save the report.

    Exit code: 0 if no action failed, 1 otherwise.
    """
    from .context import build_context  # local import: avoid a hard import cycle

    ctx = build_context(config)
    if not _ensure_signed_in(ctx, out):
        return 2
    report = asyncio.run(run_smoke(ctx, services=services))

    if as_json:
        print(json.dumps(report.to_dict(), indent=2), file=out)
    else:
        _print_report(report, out)

    config.exports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = config.exports_dir / f"smoke-{timestamp}.json"
    report_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    if not as_json:
        print(f"\nReport written: {report_path}", file=out)

    return 0 if report.failed == 0 else 1
