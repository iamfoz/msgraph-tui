"""Textual pilot smoke tests: the app boots in mock mode and navigates."""

from textual.widgets import ContentSwitcher, DataTable

from msgraph_tui.app.main import GraphdeckApp
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services.context import build_context


def _app(tmp_path) -> GraphdeckApp:
    config = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    return GraphdeckApp(build_context(config))


async def test_app_boots_to_dashboard(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        switcher = app.query_one("#content", ContentSwitcher)
        assert switcher.current == "view-dashboard"
        status = app.query_one("#status-bar").render()
        assert "MOCK" in str(status)
        assert "Contoso" in str(status)


async def test_users_view_loads_rows(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.switch_view("users")
        await pilot.pause(0.2)
        table = app.query_one("#table-users", DataTable)
        assert table.row_count >= 10


async def test_audit_view_renders(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.switch_view("users")
        await pilot.pause(0.2)  # generates read audit events
        app.switch_view("audit")
        await pilot.pause(0.2)
        view = app.query_one("#view-audit")
        table = view.query_one(DataTable)
        assert table.row_count >= 1
