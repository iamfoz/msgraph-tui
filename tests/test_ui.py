"""Pilot tests for the delight/UX additions: posture tiles, sorting, palette
actions, and the confirmation-gate safety wiring (warnings + full reason)."""

from textual.widgets import Static

from msgraph_tui.app.main import GraphdeckApp
from msgraph_tui.app.modals import PreviewConfirmModal
from msgraph_tui.app.views import BrowseView, SessionView
from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services.context import build_context


def _app(tmp_path) -> GraphdeckApp:
    cfg = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    return GraphdeckApp(build_context(cfg))


class _FakeTokenProvider:
    def get_token(self) -> str:
        return "fake-token"

    def account_label(self) -> str:
        return "admin@contoso.example"


async def test_sign_in_wires_graph_rest_in_live_mode(tmp_path):
    cfg = AppConfig(mode=Mode.LIVE, tenant_id="t", client_id="c", state_dir=tmp_path / "s")
    app = GraphdeckApp(build_context(cfg))
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        assert not app.ctx.graph_rest.is_available()  # no token yet
        app.switch_view("session")
        await pilot.pause(0.2)
        sv = app.query_one("#view-session", SessionView)
        sv._apply_sign_in(_FakeTokenProvider())
        await pilot.pause(0.1)
        assert app.ctx.graph_rest.is_available()
        assert app.ctx.session.actor == "admin@contoso.example"
        assert "device code" in app.ctx.session.auth_mode


async def test_sign_in_is_noop_in_mock_mode(tmp_path):
    app = _app(tmp_path)  # mock mode
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        app.switch_view("session")
        await pilot.pause(0.2)
        sv = app.query_one("#view-session", SessionView)
        sv.start_sign_in()  # should warn and not attach anything
        await pilot.pause(0.1)
        assert not app.ctx.graph_rest.is_available()


async def test_dashboard_posture_tiles(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.3)
        # audit chain intact on a fresh tenant
        assert app.query_one("#dash-integrity", Static).has_class("tile-ok")
        # fixtures have disabled users still holding licences -> hygiene warns
        assert app.query_one("#dash-hygiene", Static).has_class("tile-warn")


async def test_browse_sort_cycle(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.3)
        app.switch_view("users")
        await pilot.pause(0.3)
        view = app.query_one("#view-users", BrowseView)
        view.action_sort_cycle()  # column 0 ascending
        assert view._sort_index == 0 and view._sort_reverse is False
        names = [r["displayName"] for r in view._visible_rows]
        assert names == sorted(names, key=str.lower)
        view.action_sort_cycle()  # column 0 descending
        assert view._sort_reverse is True


async def test_command_palette_includes_registry_actions(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        titles = [c.title for c in app.get_system_commands(app.screen)]
        action_cmds = [t for t in titles if t.startswith("Action:")]
        assert len(action_cmds) >= 15  # every registry action is discoverable
        assert any("users.update" in t for t in action_cmds)


async def test_confirm_gate_shows_warning_banner(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        plan = await app.ctx.executor.plan_write(
            "groups.member_add", {"group_id": "g-0004", "user_id": "u-0001"}
        )
        modal = PreviewConfirmModal(
            plan, "MOCK", "Contoso", warnings=["⚠ ROLE-ASSIGNABLE GROUP — privileged"]
        )
        app.push_screen(modal)
        await pilot.pause(0.3)
        banners = modal.query(".warning-banner")
        assert len(banners) == 1


async def test_confirm_gate_collects_full_change_reason(tmp_path):
    app = _app(tmp_path)
    captured: list = []
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause(0.2)
        plan = await app.ctx.executor.plan_write(
            "users.update", {"user_id": "u-0001", "department": "Platform"}
        )
        modal = PreviewConfirmModal(plan, "MOCK", "Contoso")
        app.push_screen(modal, lambda result: captured.append(result))
        await pilot.pause(0.3)
        modal.query_one("#reason").value = "team move"
        modal.query_one("#ticket").value = "CHG-9"
        modal.query_one("#requestor").value = "alice"
        modal.query_one("#approval").value = "APPR-3"
        modal.query_one("#notes").value = "note"
        modal.execute()  # invoke the confirm handler directly (button may be off-screen)
        await pilot.pause(0.2)
    assert captured, "modal did not return a result"
    confirmed, reason = captured[0]
    assert confirmed and isinstance(reason, ChangeReason)
    assert reason.requestor == "alice" and reason.approval_ref == "APPR-3"
    assert reason.ticket == "CHG-9" and reason.notes == "note"


# --- four-eyes UI flow + app-only toggle ------------------------------------

class _FakeApp:
    """Stands in for the Textual app: returns canned modal results in order."""

    def __init__(self, results):
        self._results = list(results)
        self.notes: list[tuple[str, str]] = []
        self.screens: list = []

    async def push_screen_wait(self, screen):
        self.screens.append(screen)
        return self._results.pop(0)

    def notify(self, message, severity="information", timeout=None):
        self.notes.append((severity, message))


class _FakeView:
    def __init__(self, results):
        self.app = _FakeApp(results)


async def test_write_flow_submits_then_executes_with_approval(tmp_path):
    from msgraph_tui.app.views import run_write_flow
    from msgraph_tui.compliance.approval import APPLIED, PENDING, make_approval

    cfg = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state",
                    require_approval_for_risk=["high"])
    ctx = build_context(cfg)
    values = {"user_id": "u-0001", "enabled": False}
    reason = ChangeReason(reason="leaver", ticket="HR-1")

    # 1st pass: no approval yet → confirming only SUBMITS a request
    view = _FakeView([values, (True, reason)])
    await run_write_flow(view, ctx, "users.set_account_enabled", {})
    banners = view.app.screens[1].warnings
    assert any("FOUR-EYES APPROVAL REQUIRED" in b for b in banners)
    assert any("Submitted for four-eyes approval" in m for _, m in view.app.notes)
    [req] = ctx.executor.approvals.list_requests()
    assert req.status == PENDING
    assert not [e for e in ctx.audit_log.entries() if e["event_type"] == "change"]

    # approver signs off; 2nd pass executes and marks the request applied
    ctx.executor.record_approval(req, make_approval(req, "bob@contoso.example", approve=True))
    view = _FakeView([values, (True, reason)])
    await run_write_flow(view, ctx, "users.set_account_enabled", {})
    assert any("approved by bob@contoso.example" in b for b in view.app.screens[1].warnings)
    assert any("succeeded" in m for _, m in view.app.notes)
    assert ctx.executor.approvals.load_request(req.request_id).status == APPLIED


async def test_app_only_toggle_live_success_and_failure(tmp_path, monkeypatch):
    import msgraph_tui.app.views as views

    cfg = AppConfig(mode=Mode.LIVE, tenant_id="t", client_id="c", state_dir=tmp_path / "s")
    app = GraphdeckApp(build_context(cfg))
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        app.switch_view("session")
        await pilot.pause(0.2)
        sv = app.query_one("#view-session", SessionView)

        def broken(_cfg):
            raise RuntimeError("certificate not found: client_secret=hunter2")

        notes: list[str] = []
        monkeypatch.setattr(app, "notify", lambda msg, **_kw: notes.append(msg))
        monkeypatch.setattr(views, "app_only_factory", broken)
        sv.use_app_only()
        await pilot.pause(0.1)
        assert cfg.auth_mode == "delegated"              # reverted
        assert notes and "hunter2" not in notes[-1]      # secrets never reach the UI
        assert not app.ctx.graph_rest.is_available()

        monkeypatch.setattr(views, "app_only_factory", lambda _cfg: _FakeTokenProvider())
        sv.use_app_only()
        await pilot.pause(0.1)
        assert cfg.auth_mode == "app-only"
        assert app.ctx.graph_rest.is_available()
        assert "app-only" in app.ctx.session.auth_mode


async def test_app_only_toggle_is_noop_outside_live(tmp_path, monkeypatch):
    import msgraph_tui.app.views as views

    called = []
    monkeypatch.setattr(views, "app_only_factory", lambda c: called.append(c))
    app = _app(tmp_path)
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.2)
        app.switch_view("session")
        await pilot.pause(0.2)
        app.query_one("#view-session", SessionView).use_app_only()
        await pilot.pause(0.1)
    assert not called and app.ctx.config.auth_mode == "delegated"
