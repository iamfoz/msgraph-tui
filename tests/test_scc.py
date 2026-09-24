"""Security & Compliance (Purview) workload module (PowerShell provider + mock coverage)."""


from msgraph_tui.modules import build_registry


def test_scc_actions_registered_and_prefer_scc_ps():
    reg = build_registry()
    scc = [a for a in reg.all() if a.id.startswith("scc.")]
    assert {a.id for a in scc} >= {"scc.retention_policies.list", "scc.dlp_policies.list"}
    for a in scc:
        assert a.preferred_provider == "scc_powershell"
        assert "mock" in a.supported_providers  # offline-testable
        assert a.graph is None and a.powershell is not None  # no Graph parity
        assert not a.is_write  # SCC surface is read-only in this product


async def test_retention_policy_reads(mock_ctx):
    env = await mock_ctx.executor.read("scc.retention_policies.list", {})
    assert env.success
    names = {p["name"] for p in env.rows}
    assert "Finance 7-Year Retention" in names


async def test_dlp_policy_reads_flag_test_mode(mock_ctx):
    env = await mock_ctx.executor.read("scc.dlp_policies.list", {})
    assert env.success
    test_mode = [p for p in env.rows if str(p.get("mode", "")).startswith("Test")]
    assert test_mode  # at least one policy still in Test mode
    always_on = {p["name"] for p in env.rows if p["mode"] == "Enable"}
    assert "Credit Card Number Protection" in always_on
