"""SharePoint Online workload module (PowerShell provider + mock coverage)."""


from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.errors import ErrorCategory
from msgraph_tui.modules import build_registry

REASON = ChangeReason(reason="Sharing lockdown", ticket="SEC-3")


def test_sharepoint_actions_registered_and_prefer_spo_ps():
    reg = build_registry()
    spo = [a for a in reg.all() if a.id.startswith("sharepoint.")]
    assert {a.id for a in spo} >= {
        "sharepoint.sites.list", "sharepoint.site.get", "sharepoint.set_sharing",
    }
    for a in spo:
        assert a.preferred_provider == "sharepoint_powershell"
        assert "mock" in a.supported_providers  # offline-testable
        assert a.graph is None and a.powershell is not None  # no Graph parity


async def test_site_reads(mock_ctx):
    sites = await mock_ctx.executor.read("sharepoint.sites.list", {})
    assert sites.success
    capabilities = {s["sharingCapability"] for s in sites.rows}
    assert "ExternalUserAndGuestSharing" in capabilities

    site = await mock_ctx.executor.read(
        "sharepoint.site.get", {"site_url": "https://contoso.sharepoint.com/sites/finance"},
    )
    assert site.success
    assert site.data["title"] == "Finance"


async def test_unknown_site_is_not_found(mock_ctx):
    env = await mock_ctx.executor.read("sharepoint.site.get", {"site_url": "https://nope.example/sites/x"})
    assert not env.success and env.errors[0].category is ErrorCategory.NOT_FOUND


async def test_set_sharing_preview_is_the_exact_ps_command(mock_ctx):
    plan = await mock_ctx.executor.plan_write(
        "sharepoint.set_sharing",
        {"site_url": "https://contoso.sharepoint.com/sites/finance", "sharing_capability": "Disabled"},
    )
    assert plan.preview.detail.startswith("Set-SPOSite -Identity 'https://contoso.sharepoint.com/sites/finance'")
    assert "-SharingCapability 'Disabled'" in plan.preview.detail
    assert plan.before_state["sharingCapability"] == "Disabled"


async def test_set_sharing_write_and_rollback(mock_ctx):
    ctx = mock_ctx
    site_url = "https://contoso.sharepoint.com/sites/marketing-external"
    plan = await ctx.executor.plan_write(
        "sharepoint.set_sharing", {"site_url": site_url, "sharing_capability": "Disabled"},
    )
    assert plan.before_state["sharingCapability"] == "ExternalUserAndGuestSharing"
    env = await ctx.executor.commit_write(plan, confirmed=True, reason=REASON)
    assert env.success and env.rollback_snapshot_id
    after = await ctx.executor.read("sharepoint.site.get", {"site_url": site_url})
    assert after.data["sharingCapability"] == "Disabled"

    rplan = await ctx.executor.plan_rollback(env.rollback_snapshot_id)
    assert rplan.sanity.can_rollback
    r2 = await ctx.executor.commit_rollback(rplan, confirmed=True, reason=REASON)
    assert r2.success
    restored = await ctx.executor.read("sharepoint.site.get", {"site_url": site_url})
    assert restored.data["sharingCapability"] == "ExternalUserAndGuestSharing"


async def test_set_sharing_requires_typed_confirmation():
    from msgraph_tui.core.actions import Confirmation
    action = build_registry().get("sharepoint.set_sharing")
    assert action.confirmation is Confirmation.TYPED  # high-risk gate
