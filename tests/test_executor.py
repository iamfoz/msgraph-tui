"""End-to-end write pipeline and rollback flows against the mock tenant."""

import pytest

from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.errors import PipelineError

REASON = ChangeReason(reason="test change", ticket="CHG-1")


async def test_write_pipeline_full_cycle(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "users.update", {"user_id": "u-0001", "department": "Platform"}
    )
    # pipeline artefacts
    assert "PATCH https://graph.microsoft.com/v1.0/users/u-0001" in plan.preview.detail
    assert plan.before_state["department"] == "Engineering"
    assert plan.snapshot is not None and plan.snapshot.inverse_action_id == "users.update"

    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success and env.rollback_snapshot_id

    # state actually changed
    after = await ctx.executor.read("users.get", {"user_id": "u-0001"})
    assert after.data["department"] == "Platform"

    # audit event recorded with before/after and reason
    changes = [e for e in ctx.audit_log.entries() if e["event_type"] == "change"]
    assert changes[-1]["before_state"]["department"] == "Engineering"
    assert changes[-1]["after_state"]["department"] == "Platform"
    assert changes[-1]["reason"]["ticket"] == "CHG-1"
    assert ctx.audit_log.verify().ok


async def test_write_refused_without_confirmation(mock_ctx):
    plan = await mock_ctx.executor.plan_write(
        "users.update", {"user_id": "u-0001", "department": "X"}
    )
    with pytest.raises(PipelineError, match="confirmation"):
        await mock_ctx.executor.commit_write(plan, confirmed=False, reason=REASON)


async def test_write_refused_without_reason_when_policy_on(mock_ctx):
    assert mock_ctx.config.require_change_reason
    plan = await mock_ctx.executor.plan_write(
        "users.update", {"user_id": "u-0001", "department": "X"}
    )
    with pytest.raises(PipelineError, match="reason"):
        await mock_ctx.executor.commit_write(plan, confirmed=True, reason=None)


async def test_read_actions_cannot_use_write_path_and_vice_versa(mock_ctx):
    with pytest.raises(PipelineError):
        await mock_ctx.executor.plan_write("users.list", {})
    with pytest.raises(PipelineError):
        await mock_ctx.executor.read("users.update", {"user_id": "u-0001"})


async def test_rollback_restores_state(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "users.set_account_enabled", {"user_id": "u-0001", "enabled": False}
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.can_rollback
    renv = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert renv.success

    user = await ctx.executor.read("users.get", {"user_id": "u-0001"})
    assert user.data["accountEnabled"] is True
    # rollback audit event is linked to the original operation
    rollbacks = [e for e in ctx.audit_log.entries() if e["event_type"] == "rollback"]
    assert rollbacks[-1]["rollback_of_operation"] == plan.operation_id
    # snapshot consumed: second rollback attempt is blocked
    rplan2 = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan2.sanity.blocked


async def test_rollback_blocked_on_drift(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "users.update", {"user_id": "u-0002", "department": "Platform"}
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)

    # a "second administrator" changes the same field afterwards
    plan2 = await ctx.executor.plan_write(
        "users.update", {"user_id": "u-0002", "department": "Security"}
    )
    await ctx.executor.commit_write(plan2, confirmed=True, reason=REASON)

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.blocked
    assert any(d.field == "department" for d in rplan.sanity.drift)
    with pytest.raises(PipelineError, match="blocked"):
        await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)


async def test_group_membership_rollback(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "groups.member_add", {"group_id": "g-0001", "user_id": "u-0010"}
    )
    assert plan.before_state["members"] == ["u-0001", "u-0002", "u-0005", "u-0006"]
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success

    members = await ctx.executor.read("groups.members", {"group_id": "g-0001"})
    assert "u-0010" in {m["id"] for m in members.rows}

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.can_rollback
    renv = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert renv.success
    members = await ctx.executor.read("groups.members", {"group_id": "g-0001"})
    assert "u-0010" not in {m["id"] for m in members.rows}


async def test_failed_write_produces_no_snapshot_but_is_audited(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "users.assign_license", {"user_id": "u-0001", "sku_id": "sku-e5"}
    )  # u-0001 already has E5 -> conflict
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert not env.success
    assert env.rollback_snapshot_id is None
    last = ctx.audit_log.entries()[-1]
    assert last["event_type"] == "change"
    assert last["result"]["success"] is False


async def test_dry_run_mode_never_executes(dry_run_ctx):
    ctx = dry_run_ctx
    plan = await ctx.executor.plan_write(
        "users.set_account_enabled", {"user_id": "u-0001", "enabled": False}
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success
    assert any("NOT executed" in w for w in env.warnings)
    # state unchanged
    user = await ctx.executor.read("users.get", {"user_id": "u-0001"})
    assert user.data["accountEnabled"] is True
    # intent recorded for evidence (a verification read may follow it)
    intents = [e for e in ctx.audit_log.entries() if e["event_type"] == "change_intent"]
    assert len(intents) == 1
    assert intents[0]["action_id"] == "users.set_account_enabled"


async def test_license_assign_and_rollback(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "users.assign_license", {"user_id": "u-0010", "sku_id": "sku-e3"}
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success
    lic = await ctx.executor.read("users.licenses", {"user_id": "u-0010"})
    assert "sku-e3" in {l["skuId"] for l in lic.rows}

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    renv = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert renv.success
    lic = await ctx.executor.read("users.licenses", {"user_id": "u-0010"})
    assert "sku-e3" not in {l["skuId"] for l in lic.rows}
