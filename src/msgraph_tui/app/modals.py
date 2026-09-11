"""Modal dialogs: detail view, write form, preview/confirm, rollback, export, help."""

from __future__ import annotations

import json
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ..compliance.audit import ChangeReason
from ..core.actions import ActionDefinition, Confirmation, RiskLevel
from ..core.export import FORMATS
from ..core.redaction import redact
from ..services.executor import RollbackPlan, WritePlan

RISK_LABEL = {
    RiskLevel.READ_ONLY: ("READ-ONLY", "risk-read"),
    RiskLevel.LOW: ("LOW RISK", "risk-low"),
    RiskLevel.MEDIUM: ("MEDIUM RISK", "risk-medium"),
    RiskLevel.HIGH: ("HIGH RISK", "risk-high"),
    RiskLevel.DESTRUCTIVE: ("DESTRUCTIVE", "risk-high"),
}


def _json_block(data: Any) -> str:
    try:
        return json.dumps(redact(data), indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)


class DetailModal(ModalScreen[None]):
    """Read-only object detail: key fields plus raw (redacted) JSON."""

    BINDINGS = [("escape", "dismiss_modal", "Close")]

    def __init__(self, title: str, data: Any, subtitle: str = "") -> None:
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._data = data

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-wide"):
            yield Label(self._title, classes="modal-title")
            if self._subtitle:
                yield Label(self._subtitle, classes="modal-subtitle")
            with VerticalScroll():
                yield Static(_json_block(self._data), classes="json-block")
            with Horizontal(classes="modal-buttons"):
                yield Button("Close (Esc)", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def action_dismiss_modal(self) -> None:
        self.dismiss(None)


class WriteFormModal(ModalScreen[dict | None]):
    """Guided form generated from an action's parameter specs."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, action: ActionDefinition, prefill: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.action = action
        self.prefill = prefill or {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label(f"{self.action.name}", classes="modal-title")
            yield Label(self.action.description, classes="modal-subtitle")
            risk_text, risk_class = RISK_LABEL[self.action.risk]
            yield Label(f" {risk_text} ", classes=f"risk-badge {risk_class}")
            with VerticalScroll(id="form-fields"):
                for spec in self.action.params:
                    yield Label(
                        f"{spec.name}{' *' if spec.required else ''}"
                        + (f" — {spec.description}" if spec.description else ""),
                        classes="field-label",
                    )
                    value = self.prefill.get(spec.name, spec.default)
                    if spec.type == "bool":
                        yield Checkbox(value=bool(value), id=f"field-{spec.name}")
                    else:
                        yield Input(
                            value="" if value is None else str(value),
                            placeholder=spec.type,
                            password=spec.secret,
                            id=f"field-{spec.name}",
                        )
            yield Static("", id="form-error", classes="error-text")
            with Horizontal(classes="modal-buttons"):
                yield Button("Continue to preview", id="submit", variant="primary")
                yield Button("Cancel (Esc)", id="cancel")

    def _collect(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for spec in self.action.params:
            widget = self.query_one(f"#field-{spec.name}")
            if isinstance(widget, Checkbox):
                values[spec.name] = widget.value
            elif isinstance(widget, Input):
                raw = widget.value.strip()
                if raw != "":
                    values[spec.name] = raw
        return self.action.validate_params(values)

    @on(Button.Pressed, "#submit")
    def submit(self) -> None:
        try:
            self.dismiss(self._collect())
        except ValueError as exc:
            self.query_one("#form-error", Static).update(str(exc))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PreviewConfirmModal(ModalScreen["tuple[bool, ChangeReason | None]"]):
    """The core safety gate: shows exactly what will run, provider reasoning,
    permissions, risk and rollback level; collects change reason; requires
    typed confirmation for high-risk actions."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        plan: WritePlan,
        mode_label: str,
        tenant_label: str,
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.plan = plan
        self.mode_label = mode_label
        self.tenant_label = tenant_label
        self.warnings = warnings or []

    def compose(self) -> ComposeResult:
        plan = self.plan
        action = plan.action
        risk_text, risk_class = RISK_LABEL[action.risk]
        with Vertical(classes="modal-box modal-wide"):
            yield Label(f"Confirm: {action.name}", classes="modal-title")
            yield Label(
                f"Tenant: {self.tenant_label}   Mode: {self.mode_label}   "
                f"Provider: {plan.selection.provider.display_name}",
                classes="modal-subtitle",
            )
            yield Label(f" {risk_text} ", classes=f"risk-badge {risk_class}")
            for warning in self.warnings:
                yield Static(f"⚠ {warning}", classes="warning-banner")
            with VerticalScroll():
                yield Static(
                    "[b]This exact operation will be executed:[/b]\n" + plan.preview.detail,
                    classes="preview-block",
                )
                meta = [
                    f"Provider selection: {'; '.join(plan.selection.reasons)}",
                    f"Required Graph scopes: {', '.join(plan.preview.required_scopes) or '—'}",
                    f"Typical admin roles: {', '.join(plan.preview.required_roles) or '—'}",
                    f"Rollback support: {action.rollback.level.value}"
                    + (" (snapshot captured)" if plan.snapshot else ""),
                    "Audit: hash-chained change record will be written",
                ]
                if plan.before_state_error:
                    meta.append(
                        f"[red]Before-state capture failed: {plan.before_state_error} — "
                        "rollback will NOT be available.[/red]"
                    )
                elif plan.before_state:
                    meta.append("Before-state captured: " + _json_block(plan.before_state))
                for note in plan.preview.notes:
                    meta.append(f"Note: {note}")
                yield Static("\n".join(meta), classes="meta-block")

            if plan.requires_reason:
                yield Label("Reason for change *", classes="field-label")
                yield Input(placeholder="Why is this change being made?", id="reason")
                with Horizontal(classes="reason-row"):
                    with Vertical():
                        yield Label("Ticket / change ref", classes="field-label")
                        yield Input(placeholder="e.g. CHG-1234", id="ticket")
                    with Vertical():
                        yield Label("Requestor", classes="field-label")
                        yield Input(placeholder="who asked for this", id="requestor")
                with Horizontal(classes="reason-row"):
                    with Vertical():
                        yield Label("Approval ref", classes="field-label")
                        yield Input(placeholder="e.g. APPR-7 (if required)", id="approval")
                    with Vertical():
                        yield Label("Expiry (temporary change)", classes="field-label")
                        yield Input(placeholder="ISO date, if temporary", id="expiry")
                yield Label("Notes", classes="field-label")
                yield Input(placeholder="optional notes for the record", id="notes")

            if plan.confirmation is Confirmation.TYPED:
                yield Label(
                    f"Type [b]{plan.typed_phrase}[/b] to enable execution:",
                    classes="field-label",
                )
                yield Input(placeholder=plan.typed_phrase, id="typed")

            yield Static("", id="confirm-error", classes="error-text")
            with Horizontal(classes="modal-buttons"):
                yield Button("Execute", id="execute", variant="error")
                yield Button("Cancel (Esc)", id="cancel")

    @on(Button.Pressed, "#execute")
    def execute(self) -> None:
        plan = self.plan
        error = self.query_one("#confirm-error", Static)
        reason = None
        if plan.requires_reason:
            reason_text = self.query_one("#reason", Input).value.strip()
            if not reason_text:
                error.update("A change reason is required by policy.")
                return
            reason = ChangeReason(
                reason=reason_text,
                ticket=self.query_one("#ticket", Input).value.strip(),
                requestor=self.query_one("#requestor", Input).value.strip(),
                approval_ref=self.query_one("#approval", Input).value.strip(),
                expiry=self.query_one("#expiry", Input).value.strip(),
                notes=self.query_one("#notes", Input).value.strip(),
            )
        if plan.confirmation is Confirmation.TYPED:
            typed = self.query_one("#typed", Input).value.strip()
            if typed != plan.typed_phrase:
                error.update(f"Confirmation text does not match {plan.typed_phrase!r}.")
                return
        self.dismiss((True, reason))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss((False, None))


class RollbackModal(ModalScreen["tuple[bool, ChangeReason | None]"]):
    """Rollback gate: original change, before/current diff, sanity checks."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, plan: RollbackPlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        snap = self.plan.snapshot
        sanity = self.plan.sanity
        with Vertical(classes="modal-box modal-wide"):
            yield Label("Rollback change", classes="modal-title")
            yield Label(
                f"Original: {snap.action_id} by {snap.actor} at {snap.created_at} "
                f"(op {snap.operation_id[:12]}…)",
                classes="modal-subtitle",
            )
            with VerticalScroll():
                status_class = "sanity-ok" if sanity.can_rollback else "sanity-blocked"
                yield Static(f"Sanity checks: {sanity.summary}", classes=status_class)
                yield Static(
                    "[b]Checks:[/b] "
                    + "  ".join(f"{k}={'✓' if v else '✗'}" for k, v in sanity.checks.items()),
                    classes="meta-block",
                )
                if self.plan.diff:
                    diff_lines = ["[b]Diff (before-state vs current):[/b]"]
                    for d in self.plan.diff:
                        diff_lines.append(
                            f"  {d['field']}: [red]{d['current']!r}[/red] → [green]{d['before']!r}[/green]"
                        )
                    yield Static("\n".join(diff_lines), classes="preview-block")
                else:
                    yield Static("No differences between before-state and current state.", classes="meta-block")
                yield Static(
                    "[b]Original request:[/b]\n" + snap.original_request, classes="meta-block"
                )
                if self.plan.write_plan is not None:
                    yield Static(
                        "[b]Rollback will execute (as a new audited change):[/b]\n"
                        + self.plan.write_plan.preview.detail,
                        classes="preview-block",
                    )
                if snap.non_restorable_fields:
                    yield Static(
                        "[red]Not automatically restorable: "
                        + ", ".join(snap.non_restorable_fields) + "[/red]",
                        classes="error-text",
                    )
            if self.plan.write_plan is not None and self.plan.write_plan.requires_reason:
                yield Label("Reason for rollback *", classes="field-label")
                yield Input(placeholder="Why is this being rolled back?", id="reason")
            yield Static("", id="confirm-error", classes="error-text")
            with Horizontal(classes="modal-buttons"):
                if sanity.can_rollback and self.plan.write_plan is not None:
                    yield Button("Execute rollback", id="execute", variant="error")
                yield Button("Close (Esc)", id="cancel")

    @on(Button.Pressed, "#execute")
    def execute(self) -> None:
        reason = None
        if self.plan.write_plan is not None and self.plan.write_plan.requires_reason:
            text = self.query_one("#reason", Input).value.strip()
            if not text:
                self.query_one("#confirm-error", Static).update("A rollback reason is required by policy.")
                return
            reason = ChangeReason(reason=text)
        self.dismiss((True, reason))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss((False, None))


class ExportModal(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box"):
            yield Label("Export current rows", classes="modal-title")
            yield Label("Data is redacted before writing. Files land in the exports directory.",
                        classes="modal-subtitle")
            with Horizontal(classes="modal-buttons"):
                for fmt in FORMATS:
                    yield Button(fmt.upper(), id=f"fmt-{fmt}", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("fmt-"):
            self.dismiss(bid.removeprefix("fmt-"))
        elif bid == "cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss_modal", "Close")]

    HELP = """[b]Graphdeck — keyboard reference[/b]

  Global
    Ctrl+P        Command palette (search every screen & action)
    F1 / ?        This help
    Ctrl+L        Toggle logs panel
    Ctrl+Q        Quit
    Escape        Close dialog / back

  Tables
    ↑/↓ PgUp/PgDn Navigate rows        /        Focus search box
    g / G         Jump top / bottom    Enter    Open detail
    s             Cycle sort column    Ctrl+E   Export rows
    y / Y         Copy row JSON / CSV  p        Show request preview
    r             Refresh

  Row operations are listed in each view's footer. Type Ctrl+P to search every
  screen AND every admin action; Ctrl+P also switches the colour theme.

[b]Safety model[/b]
  Every write shows the exact Graph request / PowerShell command first,
  captures before-state and a rollback snapshot where supported, requires
  a change reason (policy), and records a hash-chained audit event.
  High-risk actions require typed confirmation. Rollbacks re-check current
  state and are blocked if someone else changed the object since.
"""

    def compose(self) -> ComposeResult:
        with Vertical(classes="modal-box modal-wide"):
            with VerticalScroll():
                yield Static(self.HELP)
            with Horizontal(classes="modal-buttons"):
                yield Button("Close (Esc)", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def action_dismiss_modal(self) -> None:
        self.dismiss(None)
