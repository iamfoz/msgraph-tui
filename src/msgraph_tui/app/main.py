"""Graphdeck application shell: navigation, status bar, command palette, logs."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical
from textual.widgets import ContentSwitcher, Footer, RichLog, Static, Tree

from .. import PRODUCT_NAME, __version__
from ..core.config import AppConfig, Mode, load_config, write_default_config
from ..core.redaction import redact_text
from ..services.context import AppContext, build_context
from .modals import HelpModal
from .views import (
    AuditView,
    BrowseSpec,
    BrowseView,
    ChangesView,
    DashboardView,
    LogsPanel,
    RowOp,
    SessionView,
)

# ---------------------------------------------------------------------------
# Browse specifications (declarative; consume registry actions)
# ---------------------------------------------------------------------------

USERS_SPEC = BrowseSpec(
    id="users",
    title="Users — Entra ID",
    list_action="users.list",
    columns=[
        ("displayName", "Name"),
        ("userPrincipalName", "UPN"),
        ("accountEnabled", "Enabled"),
        ("mail", "Mail"),
        ("userType", "Type"),
        ("department", "Department"),
        ("jobTitle", "Job title"),
    ],
    detail_action="users.get",
    detail_param="user_id",
    row_ops=[
        RowOp("l", "Licences", "users.licenses", "table", {"user_id": "id"}),
        RowOp("m", "Memberships", "users.memberships", "table", {"user_id": "id"}),
        RowOp("u", "Update", "users.update", "write", {"user_id": "id"},
              prefill=lambda row: {k: row.get(k) for k in
                                   ("displayName", "department", "jobTitle") if row.get(k)}),
        RowOp("d", "Enable/Disable", "users.set_account_enabled", "write", {"user_id": "id"},
              prefill=lambda row: {"enabled": not row.get("accountEnabled")}),
        RowOp("a", "Assign licence", "users.assign_license", "write", {"user_id": "id"}),
        RowOp("x", "Remove licence", "users.remove_license", "write", {"user_id": "id"}),
        RowOp("k", "Revoke sessions", "users.revoke_sessions", "write", {"user_id": "id"}),
    ],
)

GROUPS_SPEC = BrowseSpec(
    id="groups",
    title="Groups — Entra ID",
    list_action="groups.list",
    columns=[
        ("displayName", "Name"),
        ("description", "Description"),
        ("mail", "Mail"),
        ("securityEnabled", "Security"),
        ("isAssignableToRole", "Role-assignable"),
        ("visibility", "Visibility"),
    ],
    detail_action="groups.get",
    detail_param="group_id",
    row_ops=[
        RowOp("m", "Members", "groups.members", "table", {"group_id": "id"}),
        RowOp("o", "Owners", "groups.owners", "table", {"group_id": "id"}),
        RowOp("a", "Add member", "groups.member_add", "write", {"group_id": "id"}),
        RowOp("x", "Remove member", "groups.member_remove", "write", {"group_id": "id"}),
    ],
    warning=lambda row: (
        "⚠ ROLE-ASSIGNABLE GROUP — membership changes grant directory roles. Treat as privileged."
        if row.get("isAssignableToRole") in (True, "✓") else None
    ),
)

SKUS_SPEC = BrowseSpec(
    id="skus",
    title="Licences — subscribed SKUs",
    list_action="licenses.skus",
    columns=[
        ("skuPartNumber", "SKU"),
        ("displayName", "Product"),
        ("enabled", "Purchased"),
        ("consumed", "Assigned"),
        ("available", "Available"),
        ("capabilityStatus", "Status"),
    ],
    row_ops=[
        RowOp("u", "Users with licence", "licenses.users_by_sku", "table", {"sku_id": "skuId"}),
    ],
)

DISABLED_LICENSED_SPEC = BrowseSpec(
    id="disabled-licensed",
    title="Report — disabled users still holding licences",
    list_action="licenses.disabled_with_license",
    columns=[
        ("displayName", "Name"),
        ("userPrincipalName", "UPN"),
        ("licences", "Licences"),
    ],
    detail_action="users.get",
    detail_param="user_id",
    row_ops=[
        RowOp("x", "Remove licence", "users.remove_license", "write", {"user_id": "id"}),
    ],
)


MAILBOXES_SPEC = BrowseSpec(
    id="mailboxes",
    title="Mailboxes — Exchange Online",
    list_action="exchange.mailboxes.list",
    columns=[
        ("displayName", "Name"),
        ("primarySmtpAddress", "Primary SMTP"),
        ("recipientTypeDetails", "Type"),
        ("forwardingSmtpAddress", "Forwarding"),
        ("litigationHoldEnabled", "Litigation hold"),
    ],
    detail_action="exchange.mailbox.get",
    detail_param="mailbox_id",
    row_ops=[
        RowOp("e", "Permissions", "exchange.mailbox_permissions", "table", {"mailbox_id": "id"}),
        RowOp("i", "Inbox rules", "exchange.inbox_rules", "table", {"mailbox_id": "id"}),
        RowOp("f", "Set/clear forwarding", "exchange.set_forwarding", "write", {"mailbox_id": "id"}),
    ],
    warning=lambda row: (
        "⚠ EXTERNAL FORWARDING SET — verify this is legitimate (BEC persistence vector)."
        if row.get("forwardingSmtpAddress") else None
    ),
)


TEAMS_MEETING_POLICIES_SPEC = BrowseSpec(
    id="teams-meeting-policies",
    title="Meeting policies — Microsoft Teams",
    list_action="teams.meeting_policies.list",
    columns=[
        ("identity", "Policy"),
        ("description", "Description"),
        ("allowMeetNow", "Meet now"),
        ("allowCloudRecording", "Cloud recording"),
        ("allowPSTNUsersToBypassLobby", "PSTN bypass lobby"),
        ("designatedPresenterRoleMode", "Presenter role mode"),
    ],
    row_ops=[
        RowOp("g", "Grant to user", "teams.grant_meeting_policy", "write", {"policy_name": "identity"}),
    ],
)

TEAMS_MESSAGING_POLICIES_SPEC = BrowseSpec(
    id="teams-messaging-policies",
    title="Messaging policies — Microsoft Teams",
    list_action="teams.messaging_policies.list",
    columns=[
        ("identity", "Policy"),
        ("description", "Description"),
        ("allowUserEditMessage", "Edit messages"),
        ("allowUserDeleteMessage", "Delete messages"),
        ("allowGiphy", "Giphy"),
        ("allowMemes", "Memes"),
    ],
)

SPO_SITES_SPEC = BrowseSpec(
    id="spo-sites",
    title="Sites — SharePoint Online",
    list_action="sharepoint.sites.list",
    columns=[
        ("url", "Url"),
        ("title", "Title"),
        ("storageQuota", "Storage quota (MB)"),
        ("storageUsedCurrent", "Storage used (MB)"),
        ("sharingCapability", "Sharing"),
        ("lockState", "Lock state"),
    ],
    detail_action="sharepoint.site.get",
    detail_param="site_url",
    row_ops=[
        RowOp("s", "Set sharing capability", "sharepoint.set_sharing", "write", {"site_url": "url"},
              prefill=lambda row: {"sharing_capability": row.get("sharingCapability")}),
    ],
    warning=lambda row: (
        "⚠ EXTERNAL USER AND GUEST SHARING — anyone links are allowed on this site."
        if row.get("sharingCapability") == "ExternalUserAndGuestSharing" else None
    ),
)

RETENTION_POLICIES_SPEC = BrowseSpec(
    id="retention-policies",
    title="Retention policies — Security & Compliance",
    list_action="scc.retention_policies.list",
    columns=[
        ("name", "Name"),
        ("enabled", "Enabled"),
        ("mode", "Mode"),
        ("workload", "Workload"),
        ("comment", "Comment"),
    ],
)

DLP_POLICIES_SPEC = BrowseSpec(
    id="dlp-policies",
    title="DLP policies — Security & Compliance",
    list_action="scc.dlp_policies.list",
    columns=[
        ("name", "Name"),
        ("mode", "Mode"),
        ("state", "State"),
        ("workload", "Workload"),
    ],
    warning=lambda row: (
        "⚠ POLICY STILL IN TEST MODE — not actively enforcing/blocking."
        if str(row.get("mode", "")).startswith("Test") else None
    ),
)


# Which module view owns each action id prefix (for palette navigation).
_ACTION_VIEW = {
    "users": "users", "groups": "groups", "licenses": "skus", "exchange": "mailboxes",
    "teams": "teams-meeting-policies", "sharepoint": "spo-sites", "scc": "retention-policies",
}


class UILogHandler(logging.Handler):
    """Streams redacted debug log lines into the in-app logs panel."""

    def __init__(self, app: GraphdeckApp) -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            widget = self._app.query_one("#app-log", RichLog)
            widget.write(redact_text(self.format(record)))
        except Exception:
            pass  # UI not ready — file handler still captures the record


class GraphdeckApp(App):
    TITLE = PRODUCT_NAME
    SUB_TITLE = "Microsoft 365 administration console"

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("f1,question_mark", "help", "Help"),
        ("ctrl+l", "toggle_logs", "Logs"),
    ]

    CSS = """
    #layout { height: 1fr; }
    #nav-tree { width: 30; border-right: solid $primary-darken-2; }
    #content { padding: 0 1; }
    #status-bar {
        height: 1; background: $primary-darken-3; color: $text;
        padding: 0 1;
    }
    #logs-panel { height: 12; border-top: heavy $primary; display: none; }
    #logs-panel.visible { display: block; }

    .view-title { text-style: bold; color: $accent; padding: 0 0 1 0; }
    .search-box { margin: 0 0 1 0; }
    .hint-line { color: $text-muted; height: auto; }
    .status-line { color: $text-muted; height: 1; }
    .browse-view DataTable { height: 1fr; }
    .browse-view { height: 1fr; }

    .dashboard { padding: 1; }
    .dash-row { height: auto; }
    .dash-tile {
        border: round $primary-darken-2; padding: 1; margin: 0 1 1 0;
        height: auto; width: 1fr;
    }
    .session-view { padding: 1; }

    .modal-box {
        width: 70; height: auto; max-height: 90%; background: $surface;
        border: thick $primary; padding: 1 2;
    }
    .modal-wide { width: 100; }
    .modal-box VerticalScroll { height: auto; max-height: 24; }
    ModalScreen { align: center middle; }
    .modal-title { text-style: bold; color: $accent; }
    .modal-subtitle { color: $text-muted; margin: 0 0 1 0; }
    .modal-buttons { height: auto; align-horizontal: right; padding: 1 0 0 0; }
    .modal-buttons Button { margin-left: 2; }
    .field-label { margin: 1 0 0 0; color: $text; }
    .error-text { color: $error; height: auto; }
    .json-block, .preview-block { background: $surface-darken-1; padding: 1; margin: 0 0 1 0; }
    .meta-block { color: $text-muted; margin: 0 0 1 0; height: auto; }
    .sanity-ok { color: $success; text-style: bold; }
    .sanity-blocked { color: $error; text-style: bold; }

    .risk-badge { width: auto; padding: 0 1; margin: 0 0 1 0; text-style: bold; }
    .risk-read { background: $success-darken-2; }
    .risk-low { background: $success-darken-1; }
    .risk-medium { background: $warning-darken-1; color: $text; }
    .risk-high { background: $error-darken-1; }

    .warning-banner {
        background: $warning-darken-2; color: $text; text-style: bold;
        padding: 0 1; margin: 0 0 1 0; height: auto;
    }
    .reason-row { height: auto; }
    .reason-row > Vertical { width: 1fr; }
    .reason-row > Vertical:last-of-type { margin-left: 2; }
    .empty-state { color: $text-muted; text-style: italic; padding: 1 0; }
    .tile-ok { color: $success; }
    .tile-warn { color: $warning; }
    .tile-bad { color: $error; }
    """

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._views: dict[str, object] = {}
        self._log_handler: UILogHandler | None = None

    # -- layout ---------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            with Horizontal(id="layout"):
                tree: Tree = Tree("Graphdeck", id="nav-tree")
                tree.root.expand()
                tree.root.add_leaf("🏠 Dashboard", data="dashboard")
                tree.root.add_leaf("🔑 Session & providers", data="session")
                users = tree.root.add("👤 Users", expand=True)
                users.add_leaf("All users", data="users")
                groups = tree.root.add("👥 Groups", expand=True)
                groups.add_leaf("All groups", data="groups")
                lic = tree.root.add("🎫 Licences", expand=True)
                lic.add_leaf("Subscribed SKUs", data="skus")
                lic.add_leaf("Disabled users w/ licences", data="disabled-licensed")
                exo = tree.root.add("📧 Exchange", expand=True)
                exo.add_leaf("Mailboxes", data="mailboxes")
                teams = tree.root.add("🟣 Teams", expand=True)
                teams.add_leaf("Meeting policies", data="teams-meeting-policies")
                teams.add_leaf("Messaging policies", data="teams-messaging-policies")
                spo = tree.root.add("🔷 SharePoint", expand=True)
                spo.add_leaf("Sites", data="spo-sites")
                comp = tree.root.add("🧾 Compliance", expand=True)
                comp.add_leaf("Audit log", data="audit")
                comp.add_leaf("Change history & rollback", data="changes")
                comp.add_leaf("Retention policies", data="retention-policies")
                comp.add_leaf("DLP policies", data="dlp-policies")
                yield tree
                yield ContentSwitcher(id="content", initial=None)
            yield LogsPanel(id="logs-panel")
            yield Static("", id="status-bar")
            yield Footer()

    def on_mount(self) -> None:
        self._log_handler = UILogHandler(self)
        self._log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                                         datefmt="%H:%M:%S"))
        logging.getLogger("graphdeck").addHandler(self._log_handler)
        self._update_status_bar()
        self.switch_view("dashboard")
        logging.getLogger("graphdeck").info(
            "%s %s started in %s mode", PRODUCT_NAME, __version__, self.ctx.config.mode.value
        )

    def _update_status_bar(self) -> None:
        s = self.ctx.session
        mode = self.ctx.config.mode.value.upper()
        mode_icon = {"MOCK": "🧪", "LIVE": "🟢", "DRY-RUN": "🔍"}.get(mode, "")
        self.query_one("#status-bar", Static).update(
            f"{mode_icon} [b]{mode}[/b] │ Tenant: [b]{s.tenant_name or s.tenant_id}[/b] "
            f"│ Account: {s.actor} │ Auth: {s.auth_mode} │ F1 help · Ctrl+P palette"
        )

    # -- navigation -------------------------------------------------------

    def _make_view(self, key: str):
        ctx = self.ctx
        factories = {
            "dashboard": lambda: DashboardView(ctx),
            "session": lambda: SessionView(ctx),
            "users": lambda: BrowseView(ctx, USERS_SPEC),
            "groups": lambda: BrowseView(ctx, GROUPS_SPEC),
            "skus": lambda: BrowseView(ctx, SKUS_SPEC),
            "disabled-licensed": lambda: BrowseView(ctx, DISABLED_LICENSED_SPEC),
            "mailboxes": lambda: BrowseView(ctx, MAILBOXES_SPEC),
            "teams-meeting-policies": lambda: BrowseView(ctx, TEAMS_MEETING_POLICIES_SPEC),
            "teams-messaging-policies": lambda: BrowseView(ctx, TEAMS_MESSAGING_POLICIES_SPEC),
            "spo-sites": lambda: BrowseView(ctx, SPO_SITES_SPEC),
            "retention-policies": lambda: BrowseView(ctx, RETENTION_POLICIES_SPEC),
            "dlp-policies": lambda: BrowseView(ctx, DLP_POLICIES_SPEC),
            "audit": lambda: AuditView(ctx),
            "changes": lambda: ChangesView(ctx),
        }
        return factories[key]()

    def switch_view(self, key: str) -> None:
        switcher = self.query_one("#content", ContentSwitcher)
        view_id = {
            "dashboard": "view-dashboard", "session": "view-session",
            "users": "view-users", "groups": "view-groups", "skus": "view-skus",
            "disabled-licensed": "view-disabled-licensed", "mailboxes": "view-mailboxes",
            "teams-meeting-policies": "view-teams-meeting-policies",
            "teams-messaging-policies": "view-teams-messaging-policies",
            "spo-sites": "view-spo-sites",
            "retention-policies": "view-retention-policies",
            "dlp-policies": "view-dlp-policies",
            "audit": "view-audit", "changes": "view-changes",
        }[key]
        if key not in self._views:
            view = self._make_view(key)
            self._views[key] = view
            switcher.mount(view)
        switcher.current = view_id

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data:
            self.switch_view(str(event.node.data))

    # -- command palette ---------------------------------------------------

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        for key, title, help_text in [
            ("dashboard", "Go: Dashboard", "Home screen with tenant context"),
            ("session", "Go: Session & providers", "Connection state, providers, module checks"),
            ("users", "Go: Users", "Browse and manage Entra ID users"),
            ("groups", "Go: Groups", "Browse groups, members and owners"),
            ("skus", "Go: Licences (SKUs)", "Subscribed SKUs and consumption"),
            ("disabled-licensed", "Go: Report — disabled users with licences", "Licence hygiene"),
            ("audit", "Go: Audit log", "Hash-chained local audit trail"),
            ("changes", "Go: Change history & rollback", "Review and roll back changes"),
        ]:
            yield SystemCommand(title, help_text, lambda k=key: self.switch_view(k))
        yield SystemCommand("Help: keyboard & safety model", "Open help", self.action_help)
        yield SystemCommand("Toggle logs panel", "Show/hide the debug log stream",
                            self.action_toggle_logs)
        # Every registry action is searchable from the palette — the expert
        # path. Selecting one jumps to its module view (where it runs against a
        # selected row through the full preview/confirm pipeline).
        for action in sorted(self.ctx.actions.all(), key=lambda a: a.id):
            view_key = _ACTION_VIEW.get(action.id.split(".")[0])
            if view_key is None:
                continue
            verb = "write" if action.is_write else "read"
            yield SystemCommand(
                f"Action: {action.name}  ({action.id})",
                f"{verb} · {action.service} · risk {action.risk.value} → opens {view_key}",
                lambda k=view_key, aid=action.id: self._go_to_action(k, aid),
            )

    def _go_to_action(self, view_key: str, action_id: str) -> None:
        self.switch_view(view_key)
        self.notify(f"{action_id}: pick a row and use the listed key to run it.")

    # -- actions -----------------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_toggle_logs(self) -> None:
        self.query_one("#logs-panel").toggle_class("visible")

    def on_unmount(self) -> None:
        if self._log_handler:
            logging.getLogger("graphdeck").removeHandler(self._log_handler)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="graphdeck",
        description=f"{PRODUCT_NAME} — Microsoft 365 administration TUI",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="run against fixture data (no tenant needed)")
    mode.add_argument("--live", action="store_true", help="run against a real tenant")
    mode.add_argument("--dry-run", action="store_true",
                      help="full pipeline including previews and audit-of-intent, but never executes")
    parser.add_argument("--config", type=Path, help="path to config.json")
    parser.add_argument("--fixtures", type=Path, help="override bundled mock fixtures directory")
    parser.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {__version__}")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("verify-audit", help="verify the audit hash chain and exit")

    ev = sub.add_parser("evidence-pack", help="export an ISO-27001-aligned evidence pack")
    ev.add_argument("--since", help="period start (ISO timestamp)")
    ev.add_argument("--until", help="period end (ISO timestamp)")
    ev.add_argument("--zip", action="store_true", help="write a single .zip instead of a directory")

    pg = sub.add_parser("purge", help="delete local logs/exports/audit state")
    pg.add_argument("--logs", action="store_true", help="debug logs (default target)")
    pg.add_argument("--exports", action="store_true", help="exported reports and evidence packs")
    pg.add_argument("--audit", action="store_true", help="audit log + snapshots (EVIDENCE; needs --yes)")
    pg.add_argument("--all", action="store_true", help="logs + exports + audit")
    pg.add_argument("--yes", action="store_true", help="confirm deletion")

    ac = sub.add_parser("actions", help="print the action catalog (registry as data)")
    ac.add_argument("--json", action="store_true", help="emit JSON")

    return parser.parse_args(argv)


def build_config_from_args(args: argparse.Namespace) -> AppConfig:
    write_default_config(args.config)
    config = load_config(args.config)
    if args.mock:
        config.mode = Mode.MOCK
    elif args.live:
        config.mode = Mode.LIVE
    elif args.dry_run:
        config.mode = Mode.DRY_RUN
    if args.fixtures:
        config.fixtures_dir = args.fixtures
    return config


def _dispatch_command(args: argparse.Namespace, config: AppConfig) -> int:
    """Run a non-TUI subcommand. Returns a process exit code."""
    from ..services import commands

    if args.command == "verify-audit":
        return commands.verify_audit(config)
    if args.command == "evidence-pack":
        return commands.evidence_pack(config, since=args.since, until=args.until, as_zip=args.zip)
    if args.command == "purge":
        want_logs = args.logs or args.all or not (args.exports or args.audit)
        return commands.purge(
            config,
            logs=want_logs,
            exports=args.exports or args.all,
            audit=args.audit or args.all,
            assume_yes=args.yes,
        )
    if args.command == "actions":
        return commands.list_actions(as_json=args.json)
    raise ValueError(f"Unknown command: {args.command}")


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config = build_config_from_args(args)
    if args.command:
        config.ensure_dirs()
        return _dispatch_command(args, config)
    ctx = build_context(config)
    GraphdeckApp(ctx).run()
    return 0


if __name__ == "__main__":
    run()
