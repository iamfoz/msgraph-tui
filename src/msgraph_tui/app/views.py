"""Main content views: dashboard, session, browse (tables), audit, changes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, RichLog, Static

from ..compliance.evidence import generate_evidence_pack
from ..core.export import export_rows
from ..core.errors import GraphdeckError
from ..services.context import AppContext
from .modals import (
    DetailModal,
    ExportModal,
    PreviewConfirmModal,
    RollbackModal,
    WriteFormModal,
)


# ---------------------------------------------------------------------------
# Browse specs — pure data describing each table view
# ---------------------------------------------------------------------------

@dataclass
class RowOp:
    key: str
    label: str
    action_id: str
    kind: str  # "table" | "detail" | "write"
    params_from_row: dict[str, str] = field(default_factory=dict)  # param -> row column
    prefill: Callable[[dict], dict] | None = None


@dataclass
class BrowseSpec:
    id: str
    title: str
    list_action: str
    columns: list[tuple[str, str]]  # (row key, label)
    detail_action: str | None = None
    detail_param: str | None = None
    row_ops: list[RowOp] = field(default_factory=list)
    warning: Callable[[dict], str | None] | None = None


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "✓" if value else "✗"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


class BrowseView(Vertical):
    """Reusable searchable/sortable table over a list action."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("p", "show_preview", "Request preview"),
        ("ctrl+e", "export", "Export"),
        ("slash", "focus_search", "Search"),
    ]

    def __init__(self, ctx: AppContext, spec: BrowseSpec) -> None:
        super().__init__(id=f"view-{spec.id}", classes="browse-view")
        self.ctx = ctx
        self.spec = spec
        self._all_rows: list[dict] = []
        self._visible_rows: list[dict] = []

    def compose(self) -> ComposeResult:
        ops = "  ".join(f"[b]{op.key}[/b]:{op.label}" for op in self.spec.row_ops)
        yield Label(self.spec.title, classes="view-title")
        yield Input(placeholder="Filter rows… ( / )", id=f"search-{self.spec.id}", classes="search-box")
        table: DataTable = DataTable(id=f"table-{self.spec.id}")
        table.cursor_type = "row"
        yield table
        hint = "Enter: detail  r: refresh  p: preview  Ctrl+E: export"
        if ops:
            hint += "  |  " + ops
        yield Static(hint, classes="hint-line")
        yield Static("", id=f"status-{self.spec.id}", classes="status-line")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*[label for _key, label in self.spec.columns])
        self.action_refresh()

    # -- data loading -------------------------------------------------

    @work(exclusive=True)
    async def action_refresh(self) -> None:
        status = self.query_one(f"#status-{self.spec.id}", Static)
        status.update("Loading…")
        try:
            envelope = await self.ctx.executor.read(self.spec.list_action, {})
        except (GraphdeckError, ValueError) as exc:
            status.update(f"[red]{exc}[/red]")
            return
        if not envelope.success:
            err = envelope.errors[0] if envelope.errors else None
            detail = f"{err.message}  —  {err.guidance or ''}" if err else "unknown error"
            status.update(f"[red]{self.spec.list_action} failed ({err.category.value if err else '?'}): {detail}[/red]")
            self.app.notify(f"{self.spec.list_action} failed", severity="error")
            return
        self._all_rows = envelope.rows
        for w in envelope.warnings:
            self.app.notify(w, severity="warning")
        self._apply_filter(self.query_one(Input).value)
        status.update(
            f"{len(self._all_rows)} rows via [b]{envelope.provider}[/b] "
            f"in {envelope.duration_ms:.0f} ms"
        )

    def _apply_filter(self, needle: str) -> None:
        needle = needle.strip().lower()
        rows = self._all_rows
        if needle:
            rows = [
                r for r in rows
                if any(needle in _fmt(v).lower() for v in r.values())
            ]
        self._visible_rows = rows
        table = self.query_one(DataTable)
        table.clear()
        for i, row in enumerate(rows):
            table.add_row(*[_fmt(row.get(k)) for k, _label in self.spec.columns], key=str(i))

    @on(Input.Changed)
    def filter_changed(self, event: Input.Changed) -> None:
        self._apply_filter(event.value)

    def action_focus_search(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def search_submitted(self) -> None:
        self.query_one(DataTable).focus()

    # -- row helpers ---------------------------------------------------

    def _current_row(self) -> dict | None:
        table = self.query_one(DataTable)
        if not self._visible_rows or table.cursor_row is None or table.cursor_row < 0:
            return None
        try:
            return self._visible_rows[table.cursor_row]
        except IndexError:
            return None

    # -- detail & row operations ---------------------------------------

    @on(DataTable.RowSelected)
    def open_detail(self) -> None:
        if self.spec.detail_action and self.spec.detail_param:
            self.run_detail()

    @work
    async def run_detail(self) -> None:
        row = self._current_row()
        if row is None or not self.spec.detail_action or not self.spec.detail_param:
            return
        params = {self.spec.detail_param: row.get("id") or row.get(self.spec.detail_param)}
        envelope = await self.ctx.executor.read(self.spec.detail_action, params)
        if envelope.success:
            warning = self.spec.warning(row) if self.spec.warning else None
            await self.app.push_screen_wait(DetailModal(
                f"{self.spec.title}: {row.get('displayName', params)}",
                envelope.data,
                subtitle=warning or f"via {envelope.provider} — raw (redacted) object below",
            ))
        else:
            self.app.notify(envelope.error_summary(), severity="error")

    def on_key(self, event) -> None:
        # Row-operation keys apply only while the table itself is focused,
        # so typing in the search box can never trigger an action.
        if not isinstance(self.app.focused, DataTable):
            return
        for op in self.spec.row_ops:
            if event.key == op.key:
                row = self._current_row()
                if row is None:
                    self.app.notify("No row selected", severity="warning")
                    return
                event.stop()
                self.run_row_op(op, row)
                return

    @work
    async def run_row_op(self, op: RowOp, row: dict) -> None:
        params = {p: row.get(col) for p, col in op.params_from_row.items()}
        if op.kind in ("table", "detail"):
            envelope = await self.ctx.executor.read(op.action_id, params)
            if not envelope.success:
                self.app.notify(envelope.error_summary(), severity="error")
                return
            await self.app.push_screen_wait(DetailModal(
                f"{op.label}: {row.get('displayName', '')}",
                envelope.data,
                subtitle=f"{op.action_id} via {envelope.provider}",
            ))
            return
        # write flow
        prefill = dict(params)
        if op.prefill:
            prefill.update(op.prefill(row))
        await run_write_flow(self, self.ctx, op.action_id, prefill)
        self.action_refresh()

    # -- preview & export ------------------------------------------------

    @work
    async def action_show_preview(self) -> None:
        try:
            preview, selection = self.ctx.executor.preview(self.spec.list_action, {})
        except (GraphdeckError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        await self.app.push_screen_wait(DetailModal(
            f"Provider operation for {self.spec.list_action}",
            {
                "provider": selection.provider.name,
                "why": selection.reasons,
                "alternatives": selection.alternatives,
                "skipped": selection.skipped,
                "operation": preview.detail.split("\n"),
                "required_scopes": preview.required_scopes,
                "required_roles": preview.required_roles,
                "notes": preview.notes,
            },
        ))

    @work
    async def action_export(self) -> None:
        if not self._visible_rows:
            self.app.notify("Nothing to export", severity="warning")
            return
        fmt = await self.app.push_screen_wait(ExportModal())
        if fmt is None:
            return
        path = export_rows(
            self._visible_rows, fmt, self.ctx.config.exports_dir, self.spec.id
        )
        self.app.notify(f"Exported {len(self._visible_rows)} rows → {path}")


async def run_write_flow(view, ctx: AppContext, action_id: str, prefill: dict) -> None:
    """Shared guided write flow: form → plan → preview/confirm → commit."""
    action = ctx.actions.get(action_id)
    values = await view.app.push_screen_wait(WriteFormModal(action, prefill))
    if values is None:
        return
    try:
        plan = await ctx.executor.plan_write(action_id, values)
    except (GraphdeckError, ValueError) as exc:
        view.app.notify(f"Cannot plan {action_id}: {exc}", severity="error")
        return
    confirmed, reason = await view.app.push_screen_wait(
        PreviewConfirmModal(
            plan,
            mode_label=ctx.config.mode.value.upper(),
            tenant_label=f"{ctx.session.tenant_name or ctx.session.tenant_id}",
        )
    )
    if not confirmed:
        view.app.notify("Cancelled — nothing was executed.", severity="warning")
        return
    envelope = await ctx.executor.commit_write(plan, confirmed=True, reason=reason)
    if envelope.success:
        note = " (dry-run: not executed)" if any("Dry-run" in w for w in envelope.warnings) else ""
        rollback = " Rollback snapshot saved." if envelope.rollback_snapshot_id else ""
        view.app.notify(f"{action.name} succeeded{note}.{rollback}")
    else:
        err = envelope.errors[0] if envelope.errors else None
        view.app.notify(
            f"{action.name} FAILED: {err.message if err else 'unknown'}"
            + (f" — {err.guidance}" if err and err.guidance else ""),
            severity="error",
            timeout=10,
        )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardView(Vertical):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(id="view-dashboard", classes="dashboard")
        self.ctx = ctx

    def compose(self) -> ComposeResult:
        yield Label("Graphdeck — Microsoft 365 administration console", classes="view-title")
        yield Static("", id="dash-context", classes="dash-tile")
        with Horizontal(classes="dash-row"):
            yield Static("…", id="dash-users", classes="dash-tile")
            yield Static("…", id="dash-groups", classes="dash-tile")
            yield Static("…", id="dash-skus", classes="dash-tile")
        yield Static("", id="dash-audit", classes="dash-tile")
        yield Static(
            "[b]Getting around[/b]\n"
            "  Ctrl+P  command palette      /  filter any table\n"
            "  F1      help & safety model  Ctrl+L  logs panel\n"
            "  Navigate with the tree on the left. Every write action previews the exact\n"
            "  provider request, captures before-state and writes a hash-chained audit event.",
            classes="dash-tile",
        )

    def on_mount(self) -> None:
        session = self.ctx.session
        self.query_one("#dash-context", Static).update(
            f"[b]Tenant:[/b] {session.tenant_name or session.tenant_id}   "
            f"[b]Account:[/b] {session.actor}   "
            f"[b]Mode:[/b] {self.ctx.config.mode.value.upper()}   "
            f"[b]Auth:[/b] {session.auth_mode}"
        )
        self.load_counts()

    @work
    async def load_counts(self) -> None:
        async def count(action_id: str) -> str:
            try:
                env = await self.ctx.executor.read(action_id, {}, audit=False)
                return str(len(env.rows)) if env.success else "—"
            except (GraphdeckError, ValueError):
                return "—"

        self.query_one("#dash-users", Static).update(
            f"[b]Users[/b]\n{await count('users.list')}"
        )
        self.query_one("#dash-groups", Static).update(
            f"[b]Groups[/b]\n{await count('groups.list')}"
        )
        self.query_one("#dash-skus", Static).update(
            f"[b]Licence SKUs[/b]\n{await count('licenses.skus')}"
        )
        entries = self.ctx.audit_log.entries()
        recent = entries[-5:][::-1]
        lines = ["[b]Recent activity (local audit log)[/b]"]
        for e in recent:
            lines.append(
                f"  {e.get('timestamp', '')[:19]}  {e.get('event_type', ''):13} "
                f"{e.get('action_id', ''):28} {e.get('actor', '')}"
            )
        if not recent:
            lines.append("  (no activity yet)")
        self.query_one("#dash-audit", Static).update("\n".join(lines))


# ---------------------------------------------------------------------------
# Session / providers
# ---------------------------------------------------------------------------

class SessionView(Vertical):
    BINDINGS = [("r", "refresh", "Refresh")]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(id="view-session", classes="session-view")
        self.ctx = ctx

    def compose(self) -> ComposeResult:
        yield Label("Session & providers", classes="view-title")
        with VerticalScroll():
            yield Static("", id="session-info", classes="dash-tile")
            yield Static("", id="provider-info", classes="dash-tile")
            with Horizontal(classes="modal-buttons"):
                yield Button("Check PowerShell modules", id="check-modules")
                yield Button("Verify audit chain", id="verify-audit")
                yield Button("Export evidence pack", id="evidence")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        s = self.ctx.session
        cfg = self.ctx.config
        self.query_one("#session-info", Static).update(
            "[b]Session[/b]\n"
            f"  Mode:            {cfg.mode.value.upper()}\n"
            f"  Tenant ID:       {s.tenant_id}\n"
            f"  Tenant name:     {s.tenant_name or '—'}\n"
            f"  Signed-in as:    {s.actor}\n"
            f"  Auth mode:       {s.auth_mode}\n"
            f"  Default scopes:  {', '.join(cfg.default_scopes)}\n"
            f"  Beta endpoints:  {'allowed' if cfg.allow_beta else 'disabled'}\n"
            f"  Change reasons:  {'required' if cfg.require_change_reason else 'optional'}\n"
            f"  Audit log:       {self.ctx.audit_log.path}\n"
            f"  Snapshots:       {cfg.snapshots_dir}\n"
            f"  Exports:         {cfg.exports_dir}"
        )
        lines = ["[b]Providers[/b]"]
        for p in self.ctx.providers.all():
            mark = "🟢" if p.is_available() else "⚪"
            lines.append(f"  {mark} {p.name:24} {p.display_name}")
            lines.append(f"       {p.availability_detail()}")
        self.query_one("#provider-info", Static).update("\n".join(lines))

    @on(Button.Pressed, "#check-modules")
    @work
    async def check_modules(self) -> None:
        from ..providers.powershell import PowerShellProvider

        self.app.notify("Checking PowerShell module availability…")
        for p in self.ctx.providers.all():
            if isinstance(p, PowerShellProvider) and p.is_available():
                await p.check_modules()
        self.action_refresh()
        self.app.notify("Module check complete. Graphdeck never installs modules itself; "
                        "see each provider's guidance above.")

    @on(Button.Pressed, "#verify-audit")
    def verify_audit(self) -> None:
        result = self.ctx.audit_log.verify()
        if result.ok:
            self.app.notify(f"Audit chain intact ({result.entries} entries).")
        else:
            self.app.notify(
                f"AUDIT CHAIN BROKEN at entry {result.first_bad_line}: {result.detail}",
                severity="error", timeout=15,
            )

    @on(Button.Pressed, "#evidence")
    def evidence(self) -> None:
        pack = generate_evidence_pack(self.ctx.audit_log, self.ctx.config.exports_dir)
        self.app.notify(f"Evidence pack written → {pack}")


# ---------------------------------------------------------------------------
# Audit & change history
# ---------------------------------------------------------------------------

class AuditView(Vertical):
    """Full audit trail; change history with rollback lives in ChangesView."""

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("v", "verify", "Verify chain"),
        ("ctrl+e", "export", "Export"),
    ]

    changes_only = False

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(classes="browse-view", id=f"view-{'changes' if self.changes_only else 'audit'}")
        self.ctx = ctx
        self._entries: list[dict] = []

    def compose(self) -> ComposeResult:
        title = "Change history & rollback" if self.changes_only else "Audit log (hash-chained)"
        yield Label(title, classes="view-title")
        table: DataTable = DataTable()
        table.cursor_type = "row"
        yield table
        hint = "Enter: event detail  r: refresh  v: verify chain  Ctrl+E: export"
        if self.changes_only:
            hint += "  [b]b[/b]: roll back selected change"
        yield Static(hint, classes="hint-line")
        yield Static("", id="audit-status", classes="status-line")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Time (UTC)", "Type", "Action", "Risk", "OK", "Actor", "Object", "Rollback")
        self.action_refresh()

    def action_refresh(self) -> None:
        entries = [e for e in self.ctx.audit_log.entries() if "_corrupt" not in e]
        if self.changes_only:
            entries = [e for e in entries if e.get("event_type") in ("change", "rollback", "change_intent")]
        self._entries = entries[::-1]  # newest first
        table = self.query_one(DataTable)
        table.clear()
        for i, e in enumerate(self._entries):
            snap_id = e.get("rollback_snapshot_id")
            rollback_cell = "—"
            if snap_id:
                snap = self.ctx.rollback_store.load(snap_id)
                rollback_cell = "consumed" if (snap and snap.consumed) else "available"
            elif e.get("event_type") == "rollback":
                rollback_cell = f"undo of {str(e.get('rollback_of_operation', ''))[:8]}"
            table.add_row(
                e.get("timestamp", "")[:19],
                e.get("event_type", ""),
                e.get("action_id", ""),
                e.get("risk", ""),
                "✓" if e.get("result", {}).get("success") else "✗",
                e.get("actor", ""),
                e.get("object_id") or "",
                rollback_cell,
                key=str(i),
            )
        self.query_one("#audit-status", Static).update(f"{len(self._entries)} entries")

    def _current(self) -> dict | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or not self._entries:
            return None
        try:
            return self._entries[table.cursor_row]
        except IndexError:
            return None

    @on(DataTable.RowSelected)
    def open_detail(self) -> None:
        self.show_detail()

    @work
    async def show_detail(self) -> None:
        entry = self._current()
        if entry:
            await self.app.push_screen_wait(
                DetailModal(f"Audit event {entry.get('event_id', '')[:12]}…", entry)
            )

    def action_verify(self) -> None:
        result = self.ctx.audit_log.verify()
        severity: Literal["information", "error"] = "information" if result.ok else "error"
        self.app.notify(
            f"Audit chain: {'INTACT' if result.ok else 'BROKEN'} — {result.detail} "
            f"({result.entries} entries)",
            severity=severity, timeout=10,
        )

    @work
    async def action_export(self) -> None:
        if not self._entries:
            self.app.notify("Nothing to export", severity="warning")
            return
        fmt = await self.app.push_screen_wait(ExportModal())
        if fmt is None:
            return
        rows = self._entries
        if fmt in ("csv", "md", "txt"):
            from ..compliance.evidence import _flatten
            rows = _flatten(rows)
        path = export_rows(rows, fmt, self.ctx.config.exports_dir,
                           "changes" if self.changes_only else "audit")
        self.app.notify(f"Exported → {path}")


class ChangesView(AuditView):
    changes_only = True

    def on_key(self, event) -> None:
        if event.key == "b":
            event.stop()
            self.start_rollback()

    @work
    async def start_rollback(self) -> None:
        entry = self._current()
        if not entry:
            return
        snap_id = entry.get("rollback_snapshot_id")
        if not snap_id:
            self.app.notify(
                "No rollback snapshot for this change "
                "(read events, failed changes and rollbacks are not reversible).",
                severity="warning",
            )
            return
        try:
            plan = await self.ctx.executor.plan_rollback(snap_id)
        except (GraphdeckError, ValueError) as exc:
            self.app.notify(str(exc), severity="error")
            return
        confirmed, reason = await self.app.push_screen_wait(RollbackModal(plan))
        if not confirmed:
            return
        try:
            envelope = await self.ctx.executor.commit_rollback(plan, confirmed=True, reason=reason)
        except (GraphdeckError, ValueError) as exc:
            self.app.notify(f"Rollback refused: {exc}", severity="error")
            return
        if envelope.success:
            self.app.notify("Rollback executed and audited (linked to the original change).")
        else:
            self.app.notify(f"Rollback failed: {envelope.error_summary()}", severity="error")
        self.action_refresh()


# ---------------------------------------------------------------------------
# Logs panel
# ---------------------------------------------------------------------------

class LogsPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Label("Logs (redacted debug stream — audit log is separate)", classes="view-title")
        yield RichLog(id="app-log", markup=False, wrap=True, max_lines=2000)
