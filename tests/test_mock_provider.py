"""Mock engine behaviour: fixtures, mutations, error simulation."""

import pytest

from msgraph_tui.core.errors import ErrorCategory


async def test_users_list_returns_fixture_rows(mock_ctx):
    env = await mock_ctx.executor.read("users.list", {})
    assert env.success and env.provider == "mock"
    assert len(env.rows) >= 10
    assert {"id", "displayName", "userPrincipalName"} <= set(env.rows[0])


async def test_users_list_search_filters(mock_ctx):
    env = await mock_ctx.executor.read("users.list", {"search": "lovelace"})
    assert [r["id"] for r in env.rows] == ["u-0001"]


async def test_user_get_by_upn(mock_ctx):
    env = await mock_ctx.executor.read("users.get", {"user_id": "ada.lovelace@contoso.example"})
    assert env.success and env.data["id"] == "u-0001"


async def test_unknown_user_is_not_found(mock_ctx):
    env = await mock_ctx.executor.read("users.get", {"user_id": "nope"})
    assert not env.success
    assert env.errors[0].category is ErrorCategory.NOT_FOUND
    assert env.errors[0].guidance  # recovery guidance always attached


@pytest.mark.parametrize("simulate,category", [
    ("throttled", ErrorCategory.THROTTLED),
    ("permission_denied", ErrorCategory.PERMISSION),
    ("expired_session", ErrorCategory.EXPIRED_SESSION),
    ("network", ErrorCategory.NETWORK),
    ("malformed", ErrorCategory.PARSE),
])
async def test_error_simulation(mock_ctx, simulate, category):
    env = await mock_ctx.executor.read("users.list", {"_simulate": simulate})
    assert not env.success
    assert env.errors[0].category is category


async def test_group_membership_reads(mock_ctx):
    members = await mock_ctx.executor.read("groups.members", {"group_id": "g-0001"})
    owners = await mock_ctx.executor.read("groups.owners", {"group_id": "g-0001"})
    assert {m["id"] for m in members.rows} == {"u-0001", "u-0002", "u-0005", "u-0006"}
    assert [o["id"] for o in owners.rows] == ["u-0002"]


async def test_sku_report_shape(mock_ctx):
    env = await mock_ctx.executor.read("licenses.skus", {})
    row = next(r for r in env.rows if r["skuPartNumber"] == "SPE_E5")
    assert row["available"] == row["enabled"] - row["consumed"]


async def test_disabled_with_license_report(mock_ctx):
    env = await mock_ctx.executor.read("licenses.disabled_with_license", {})
    ids = {r["id"] for r in env.rows}
    assert ids == {"u-0003", "u-0009"}  # disabled AND licensed only


async def test_mock_preview_shows_real_graph_request(mock_ctx):
    preview, selection = mock_ctx.executor.preview("users.get", {"user_id": "u-0001"})
    assert selection.provider.name == "mock"
    assert "GET https://graph.microsoft.com/v1.0/users/u-0001" in preview.detail
