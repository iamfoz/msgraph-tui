"""Graph REST engine against a mocked transport: pagination, throttling, errors."""

import httpx

from msgraph_tui.core.actions import ActionDefinition, GraphTemplate, ParamSpec
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.core.errors import ErrorCategory
from msgraph_tui.providers.graph_rest import GraphRestProvider, normalize_graph_error


class FakeToken:
    def get_token(self) -> str:
        return "fake-token-value"

    def account_label(self) -> str:
        return "admin@contoso.example"


def _action(**overrides) -> ActionDefinition:
    base = dict(
        id="users.list", name="n", description="d", service="Entra ID",
        preferred_provider="graph_rest", supported_providers=["graph_rest"],
        graph=GraphTemplate(method="GET", path="/users"),
        params=[ParamSpec("top", type="int")],
        graph_scopes=["User.Read.All"],
    )
    base.update(overrides)
    return ActionDefinition(**base)


def _provider(handler, **config_kwargs) -> GraphRestProvider:
    config = AppConfig(mode=Mode.LIVE, **config_kwargs)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GraphRestProvider(config, token_provider=FakeToken(), client=client)


async def test_pagination_follows_next_link():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "page2" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": "u3"}]})
        return httpx.Response(200, json={
            "value": [{"id": "u1"}, {"id": "u2"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?page2=1",
        })

    env = await _provider(handler).execute(_action(), {})
    assert env.success
    assert [r["id"] for r in env.rows] == ["u1", "u2", "u3"]
    assert env.page_count == 2 and len(calls) == 2


async def test_page_cap_truncates_with_warning():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "value": [{"id": "x"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?again=1",
        })

    env = await _provider(handler, max_pages=3).execute(_action(), {})
    assert env.success and env.truncated
    assert env.page_count == 3
    assert any("truncated" in w.lower() for w in env.warnings)


async def test_max_pages_one_stops_after_first_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={
            "value": [{"id": "only"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/users?again=1",
        })

    env = await _provider(handler, max_pages=1).execute(_action(), {})
    assert env.success and env.truncated
    assert env.page_count == 1 and len(calls) == 1  # nextLink not followed
    assert [r["id"] for r in env.rows] == ["only"]


async def test_throttling_retries_with_retry_after():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"},
                                  json={"error": {"code": "TooManyRequests", "message": "slow down"}})
        return httpx.Response(200, json={"value": [{"id": "u1"}]})

    env = await _provider(handler).execute(_action(), {})
    assert env.success
    assert env.retry.attempts == 3 and env.retry.throttled


async def test_permission_error_normalised():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"request-id": "corr-123"},
            json={"error": {"code": "Authorization_RequestDenied",
                            "message": "Insufficient privileges"}},
        )

    env = await _provider(handler).execute(_action(), {})
    assert not env.success
    err = env.errors[0]
    assert err.category is ErrorCategory.PERMISSION
    assert err.provider_code == "Authorization_RequestDenied"
    assert env.correlation_id == "corr-123"
    assert err.guidance  # actionable recovery hint


async def test_expired_token_normalised():
    err = normalize_graph_error(
        401, {"error": {"code": "InvalidAuthenticationToken", "message": "Lifetime expired"}}, None
    )
    assert err.category is ErrorCategory.EXPIRED_SESSION


async def test_non_json_response_is_parse_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>gateway</html>")

    env = await _provider(handler).execute(_action(), {})
    assert not env.success
    assert env.errors[0].category is ErrorCategory.PARSE


async def test_authorization_header_never_in_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fake-token-value"
        return httpx.Response(200, json={"value": []})

    env = await _provider(handler).execute(_action(), {})
    assert "fake-token-value" not in env.request_preview
    assert "fake-token-value" not in str(env.raw)


async def test_not_signed_in_is_auth_error():
    config = AppConfig(mode=Mode.LIVE)
    provider = GraphRestProvider(config, token_provider=None)
    assert not provider.is_available()
    env = await provider.execute(_action(), {})
    assert env.errors[0].category is ErrorCategory.AUTH
