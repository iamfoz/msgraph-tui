"""Microsoft Teams workload module (PowerShell provider + mock coverage)."""


from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.errors import ErrorCategory
from msgraph_tui.modules import build_registry

REASON = ChangeReason(reason="Policy hardening", ticket="SEC-2")


def test_teams_actions_registered_and_prefer_teams_ps():
    reg = build_registry()
    teams = [a for a in reg.all() if a.id.startswith("teams.")]
    assert {a.id for a in teams} >= {
        "teams.meeting_policies.list", "teams.messaging_policies.list",
        "teams.user_policies.get", "teams.grant_meeting_policy",
    }
    for a in teams:
        assert a.preferred_provider == "teams_powershell"
        assert "mock" in a.supported_providers  # offline-testable
        assert a.graph is None and a.powershell is not None  # no Graph parity


async def test_policy_reads(mock_ctx):
    meeting = await mock_ctx.executor.read("teams.meeting_policies.list", {})
    assert meeting.success
    identities = {p["identity"] for p in meeting.rows}
    assert "Global" in identities and "Tag:RestrictedMeetings" in identities

    messaging = await mock_ctx.executor.read("teams.messaging_policies.list", {})
    assert messaging.success
    assert {"Global", "Tag:NoFun"} <= {p["identity"] for p in messaging.rows}


async def test_user_policies_get(mock_ctx):
    env = await mock_ctx.executor.read("teams.user_policies.get", {"user_id": "u-0002"})
    assert env.success
    assert env.data["teamsMeetingPolicy"] == "Tag:RestrictedMeetings"


async def test_unknown_user_is_not_found(mock_ctx):
    env = await mock_ctx.executor.read("teams.user_policies.get", {"user_id": "nope@x"})
    assert not env.success and env.errors[0].category is ErrorCategory.NOT_FOUND


async def test_grant_meeting_policy_preview_is_the_exact_ps_command(mock_ctx):
    plan = await mock_ctx.executor.plan_write(
        "teams.grant_meeting_policy", {"user_id": "u-0002", "policy_name": "Global"},
    )
    assert plan.preview.detail.startswith("Grant-CsTeamsMeetingPolicy -Identity 'u-0002'")
    assert "-PolicyName 'Global'" in plan.preview.detail
    assert plan.before_state["teamsMeetingPolicy"] == "Tag:RestrictedMeetings"


async def test_grant_meeting_policy_write_and_rollback(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "teams.grant_meeting_policy", {"user_id": "u-0002", "policy_name": "Global"},
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success and env.rollback_snapshot_id
    after = await ctx.executor.read("teams.user_policies.get", {"user_id": "u-0002"})
    assert after.data["teamsMeetingPolicy"] == "Global"

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.can_rollback
    r2 = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert r2.success
    restored = await ctx.executor.read("teams.user_policies.get", {"user_id": "u-0002"})
    assert restored.data["teamsMeetingPolicy"] == "Tag:RestrictedMeetings"


async def test_grant_meeting_policy_requires_confirmation():
    from msgraph_tui.core.actions import Confirmation
    action = build_registry().get("teams.grant_meeting_policy")
    assert action.confirmation is not Confirmation.NONE
