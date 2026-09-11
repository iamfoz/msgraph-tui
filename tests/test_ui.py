"""Pilot tests for the delight/UX additions: posture tiles, sorting, palette
actions, and the confirmation-gate safety wiring (warnings + full reason)."""

from textual.widgets import Static

from msgraph_tui.app.main import GraphdeckApp
from msgraph_tui.app.modals import PreviewConfirmModal
from msgraph_tui.app.views import BrowseView
from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services.context import build_context


def _app(tmp_path) -> GraphdeckApp:
    cfg = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    return GraphdeckApp(build_context(cfg))


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
