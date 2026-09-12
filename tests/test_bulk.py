"""Bulk multi-select: executor plan/commit and the UI bulk flow."""

import pytest

from msgraph_tui.app.main import USERS_SPEC, GraphdeckApp
from msgraph_tui.app.views import BrowseView
from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.core.errors import PipelineError
from msgraph_tui.services.context import build_context

REASON = ChangeReason(reason="bulk offboard", ticket="CHG-7")


# --- executor -------------------------------------------------------------

async def test_plan_bulk_makes_per_object_plans_with_snapshots(mock_ctx):
    items = [{"user_id": "u-0001", "enabled": False}, {"user_id": "u-0002", "enabled": False}]
    plan = await mock_ctx.executor.plan_bulk("users.set_account_enabled", items)
    assert plan.count == 2
    assert plan.object_ids == ["u-0001", "u-0002"]
    assert all(p.snapshot is not None for p in plan.plans)  # per-object rollback
    assert plan.typed_phrase.endswith(" 2")


async def test_commit_bulk_executes_all_and_audits(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_bulk(
        "users.set_account_enabled",
        [{"user_id": "u-0001", "enabled": False}, {"user_id": "u-0002", "enabled": False}],
    )
    results = await ctx.executor.commit_bulk(plan, confirmed=True, reason=REASON)
    assert [r.success for r in results] == [True, True]
    for uid in ("u-0001", "u-0002"):
        env = await ctx.executor.read("users.get", {"user_id": uid})
        assert env.data["accountEnabled"] is False
    # each object produced its own rollback snapshot and change event
    assert all(r.envelope.rollback_snapshot_id for r in results)
    changes = [e for e in ctx.audit_log.entries()
               if e["event_type"] == "change" and e["interaction_mode"] == "bulk"]
    assert len(changes) == 2
    assert ctx.audit_log.verify().ok


async def test_commit_bulk_refuses_without_confirmation_or_reason(mock_ctx):
    plan = await mock_ctx.executor.plan_bulk(
        "users.set_account_enabled", [{"user_id": "u-0001", "enabled": False}]
    )
    with pytest.raises(PipelineError, match="confirmation"):
        await mock_ctx.executor.commit_bulk(plan, confirmed=False)
    with pytest.raises(PipelineError, match="reason"):
        await mock_ctx.executor.commit_bulk(plan, confirmed=True, reason=None)


async def test_bulk_partial_failure_is_reported(mock_ctx):
    ctx = mock_ctx
    # u-0004 already holds sku-e3 (conflict); u-0001 does not (ok)
    plan = await ctx.executor.plan_bulk(
        "users.assign_license",
        [{"user_id": "u-0001", "sku_id": "sku-e3"}, {"user_id": "u-0004", "sku_id": "sku-e3"}],
    )
    results = await ctx.executor.commit_bulk(plan, confirmed=True, reason=REASON)
    by_id = {r.object_id: r.success for r in results}
    assert by_id == {"u-0001": True, "u-0004": False}


async def test_plan_bulk_rejects_read_action_and_empty(mock_ctx):
    with pytest.raises(PipelineError, match="read-only"):
        await mock_ctx.executor.plan_bulk("users.list", [{}])
    with pytest.raises(PipelineError, match="at least one"):
        await mock_ctx.executor.plan_bulk("users.set_account_enabled", [])


# --- UI flow --------------------------------------------------------------

async def test_ui_bulk_flow_disables_selected_users(tmp_path):
    app = GraphdeckApp(build_context(AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "s")))
    async with app.run_test(size=(140, 46)) as pilot:
        await pilot.pause(0.3)
        app.switch_view("users")
        await pilot.pause(0.3)
        view = app.query_one("#view-users", BrowseView)
        view._selected_ids = {"u-0001", "u-0002"}

        # Drive the two modals the bulk flow opens without real interaction.
        from msgraph_tui.app import views as views_mod
        scripted = [
            {"enabled": False},          # WriteFormModal shared params
            (True, REASON),              # BulkConfirmModal result
        ]

        async def fake_wait(_modal):
            return scripted.pop(0)

        app.push_screen_wait = fake_wait  # type: ignore[assignment]

        # the 'd' RowOp on Users is enable/disable
        disable_op = next(o for o in USERS_SPEC.row_ops if o.action_id == "users.set_account_enabled")
        await views_mod.run_bulk_flow(view, app.ctx, disable_op, view._selected_rows())
        await pilot.pause(0.1)

    # both users disabled, selection cleared, audited as bulk
    for uid in ("u-0001", "u-0002"):
        env = await app.ctx.executor.read("users.get", {"user_id": uid})
        assert env.data["accountEnabled"] is False
    assert view._selected_ids == set()
