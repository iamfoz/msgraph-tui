"""Exchange Online workload module (PowerShell provider + mock coverage)."""


from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.errors import ErrorCategory
from msgraph_tui.modules import build_registry

REASON = ChangeReason(reason="IR containment", ticket="SEC-1")


def test_exchange_actions_registered_and_prefer_exchange_ps():
    reg = build_registry()
    exo = [a for a in reg.all() if a.id.startswith("exchange.")]
    assert {a.id for a in exo} >= {
        "exchange.mailboxes.list", "exchange.mailbox.get",
        "exchange.mailbox_permissions", "exchange.inbox_rules", "exchange.set_forwarding",
    }
    for a in exo:
        assert a.preferred_provider == "exchange_powershell"
        assert "mock" in a.supported_providers  # offline-testable
        assert a.graph is None and a.powershell is not None  # no Graph parity


async def test_mailbox_reads(mock_ctx):
    mbx = await mock_ctx.executor.read("exchange.mailboxes.list", {})
    assert mbx.success
    types = {m["recipientTypeDetails"] for m in mbx.rows}
    assert {"UserMailbox", "SharedMailbox", "RoomMailbox"} <= types

    perms = await mock_ctx.executor.read("exchange.mailbox_permissions", {"mailbox_id": "shared-helpdesk"})
    assert {p["user"] for p in perms.rows} == {
        "grace.hopper@contoso.example", "ada.lovelace@contoso.example"
    }


async def test_inbox_rule_hunt_surfaces_exfil(mock_ctx):
    rules = await mock_ctx.executor.read("exchange.inbox_rules", {"mailbox_id": "u-0003"})
    names = {r["name"] for r in rules.rows}
    assert "auto-forward-invoices" in names  # external forward
    assert any(r.get("forwardTo") for r in rules.rows)


async def test_unknown_mailbox_is_not_found(mock_ctx):
    env = await mock_ctx.executor.read("exchange.mailbox.get", {"mailbox_id": "nope@x"})
    assert not env.success and env.errors[0].category is ErrorCategory.NOT_FOUND


async def test_set_forwarding_preview_is_the_exact_ps_command(mock_ctx):
    plan = await mock_ctx.executor.plan_write(
        "exchange.set_forwarding",
        {"mailbox_id": "u-0003", "forwarding_address": "", "deliver_and_forward": False},
    )
    assert plan.preview.detail.startswith("Set-Mailbox -Identity 'u-0003'")
    assert "-ForwardingSmtpAddress ''" in plan.preview.detail
    assert plan.before_state["forwardingSmtpAddress"] == "external.actor@fabrikam.example"


async def test_set_forwarding_write_and_rollback(mock_ctx):
    ctx = mock_ctx
    plan = await ctx.executor.plan_write(
        "exchange.set_forwarding",
        {"mailbox_id": "u-0003", "forwarding_address": "", "deliver_and_forward": False},
    )
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success and env.rollback_snapshot_id
    after = await ctx.executor.read("exchange.mailbox.get", {"mailbox_id": "u-0003"})
    assert after.data["forwardingSmtpAddress"] is None  # exfil killed

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.can_rollback
    r2 = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert r2.success
    restored = await ctx.executor.read("exchange.mailbox.get", {"mailbox_id": "u-0003"})
    assert restored.data["forwardingSmtpAddress"] == "external.actor@fabrikam.example"


async def test_set_forwarding_requires_typed_confirmation():
    from msgraph_tui.core.actions import Confirmation
    action = build_registry().get("exchange.set_forwarding")
    assert action.confirmation is Confirmation.TYPED  # high-risk gate
