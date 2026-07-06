"""Graph REST request construction."""

import pytest

from msgraph_tui.core.actions import ActionDefinition, GraphTemplate
from msgraph_tui.providers.graph_request import MissingParamError, build_graph_request


def _action(template: GraphTemplate) -> ActionDefinition:
    return ActionDefinition(
        id="t.a", name="t", description="d", service="s",
        preferred_provider="graph_rest", supported_providers=["graph_rest"],
        graph=template,
    )


def test_select_and_top_query():
    action = _action(GraphTemplate(
        method="GET", path="/users",
        query={"$select": "id,displayName", "$top": "{top}"},
    ))
    built = build_graph_request(action, {"top": 50})
    assert built.method == "GET"
    assert built.url.startswith("https://graph.microsoft.com/v1.0/users?")
    assert "%24select=id%2CdisplayName" in built.url or "$select=id,displayName" in built.url
    assert "50" in built.url


def test_missing_optional_query_param_dropped():
    action = _action(GraphTemplate(method="GET", path="/users", query={"$top": "{top}"}))
    built = build_graph_request(action, {})
    assert built.url == "https://graph.microsoft.com/v1.0/users"


def test_path_params_are_url_quoted():
    action = _action(GraphTemplate(method="GET", path="/users/{user_id}"))
    built = build_graph_request(action, {"user_id": "a b/c?@x"})
    assert "/users/a%20b%2Fc%3F%40x" in built.url  # injection into path impossible


def test_missing_path_param_raises():
    action = _action(GraphTemplate(method="GET", path="/users/{user_id}"))
    with pytest.raises(MissingParamError):
        build_graph_request(action, {})


def test_beta_flag_changes_version():
    action = _action(GraphTemplate(method="GET", path="/users", beta=True))
    assert "/beta/users" in build_graph_request(action, {}).url


def test_body_placeholders_fill_and_drop():
    action = _action(GraphTemplate(
        method="PATCH", path="/users/{user_id}",
        body={"department": "{department}", "jobTitle": "{jobTitle}"},
    ))
    built = build_graph_request(action, {"user_id": "u1", "department": "Eng"})
    assert built.body == {"department": "Eng"}  # missing optional field dropped


def test_nested_body_substitution():
    action = _action(GraphTemplate(
        method="POST", path="/users/{user_id}/assignLicense",
        body={"addLicenses": [{"skuId": "{sku_id}"}], "removeLicenses": []},
    ))
    built = build_graph_request(action, {"user_id": "u1", "sku_id": "sku-e5"})
    assert built.body == {"addLicenses": [{"skuId": "sku-e5"}], "removeLicenses": []}


def test_pagination_only_for_get():
    patch = _action(GraphTemplate(method="PATCH", path="/users/{user_id}", paginate=True))
    assert build_graph_request(patch, {"user_id": "u1"}).paginate is False


def test_preview_contains_method_url_and_body():
    action = _action(GraphTemplate(method="PATCH", path="/users/{u}", body={"a": "{a}"}))
    text = build_graph_request(action, {"u": "u1", "a": "x"}).preview_text()
    assert text.startswith("PATCH https://graph.microsoft.com/v1.0/users/u1")
    assert '"a": "x"' in text
