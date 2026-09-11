"""Graph REST engine: token acquisition, pagination, throttling, normalisation.

Authentication uses MSAL (device code for delegated, client credential for
app-only) when the optional `msal` dependency is installed. Tokens live in
memory only. The Authorization header is never logged, previewed or audited.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import httpx

from ..core.actions import ActionDefinition
from ..core.config import AppConfig
from ..core.envelope import ResultEnvelope, RetryInfo, failure, utc_now_iso
from ..core.errors import ErrorCategory, NormalizedError, with_guidance
from ..core.providers import GRAPH_REST, OperationPreview, Provider
from ..core.redaction import redact, redact_text
from .graph_request import BuiltRequest, MissingParamError, build_graph_request

MAX_RETRIES = 3


class TokenProvider(Protocol):
    def get_token(self) -> str: ...
    def account_label(self) -> str: ...


class MsalDeviceCodeTokenProvider:
    """Delegated auth via OAuth 2.0 device code flow (requires `msal`)."""

    def __init__(self, config: AppConfig, message_callback=None) -> None:
        try:
            import msal  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "Live mode needs the optional 'msal' dependency: pip install 'msgraph-tui[live]'"
            ) from exc
        import msal

        if not config.client_id or not config.tenant_id:
            raise RuntimeError("Set tenant_id and client_id in the config file for live mode.")
        self._config = config
        self._app = msal.PublicClientApplication(
            config.client_id,
            authority=config.authority.format(tenant=config.tenant_id),
        )
        self._message_callback = message_callback
        self._result: dict[str, Any] | None = None

    def get_token(self) -> str:
        import msal  # noqa: F401

        scopes = self._config.default_scopes
        accounts = self._app.get_accounts()
        if accounts:
            silent = self._app.acquire_token_silent(scopes, account=accounts[0])
            if silent and "access_token" in silent:
                self._result = silent
                return silent["access_token"]
        flow = self._app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown')}")
        if self._message_callback:
            self._message_callback(flow["message"])
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Auth failed: {result.get('error_description', 'unknown')}")
        self._result = result
        return result["access_token"]

    def account_label(self) -> str:
        claims = (self._result or {}).get("id_token_claims", {})
        return claims.get("preferred_username", "signed-in user")


def normalize_graph_error(status: int, payload: Any, correlation_id: str | None) -> NormalizedError:
    code, message = None, f"Graph request failed with HTTP {status}"
    if isinstance(payload, dict):
        err = payload.get("error", {})
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message", message)
    category = {
        400: ErrorCategory.INVALID_INPUT,
        401: ErrorCategory.AUTH,
        403: ErrorCategory.PERMISSION,
        404: ErrorCategory.NOT_FOUND,
        409: ErrorCategory.CONFLICT,
        412: ErrorCategory.CONFLICT,
        429: ErrorCategory.THROTTLED,
    }.get(status, ErrorCategory.NETWORK if status >= 500 else ErrorCategory.UNKNOWN)
    if code == "InvalidAuthenticationToken":
        category = ErrorCategory.EXPIRED_SESSION
    return with_guidance(
        NormalizedError(
            category=category,
            message=redact_text(str(message)),
            provider_code=code,
            status=status,
            correlation_id=correlation_id,
            retriable=status in (429, 502, 503, 504),
            detail=redact_text(str(payload)[:2000]) if payload else None,
        )
    )


class GraphRestProvider(Provider):
    name = GRAPH_REST
    display_name = "Microsoft Graph REST API"

    def __init__(
        self,
        config: AppConfig,
        token_provider: TokenProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._client = client  # injectable for tests

    def is_available(self) -> bool:
        return self._token_provider is not None

    def attach_token_provider(self, token_provider: TokenProvider) -> None:
        """Wire a signed-in token provider (called by the Session sign-in flow)."""
        self._token_provider = token_provider

    def availability_detail(self) -> str:
        if self._token_provider is None:
            return "not connected (sign in from the Session screen)"
        return f"connected as {self._token_provider.account_label()}"

    def preview(self, action: ActionDefinition, params: dict[str, Any]) -> OperationPreview:
        built = build_graph_request(action, params, base_url=self._config.graph_base)
        notes = []
        if built.paginate:
            notes.append(f"Paginated (follows @odata.nextLink, max {self._config.max_pages} pages).")
        if built.beta:
            notes.append("Uses the BETA Graph endpoint — subject to change by Microsoft.")
        return OperationPreview(
            provider=self.name,
            summary=f"{built.method} {built.url}",
            detail=redact_text(built.preview_text()),
            required_scopes=action.graph_scopes,
            required_roles=action.admin_roles,
            beta=built.beta,
            notes=notes,
        )

    async def execute(self, action: ActionDefinition, params: dict[str, Any]) -> ResultEnvelope:
        started = time.monotonic()
        try:
            built = build_graph_request(action, params, base_url=self._config.graph_base)
        except (MissingParamError, ValueError) as exc:
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(ErrorCategory.INVALID_INPUT, str(exc))),
            )
        preview_text = redact_text(built.preview_text())
        if self._token_provider is None:
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(ErrorCategory.AUTH, "Not signed in to Microsoft Graph")),
                request_preview=preview_text,
            )
        try:
            token = await asyncio.to_thread(self._token_provider.get_token)
        except Exception as exc:  # auth library errors are user-facing
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(ErrorCategory.AUTH, redact_text(str(exc)))),
                request_preview=preview_text,
            )

        envelope = ResultEnvelope(
            success=False,
            provider=self.name,
            action_id=action.id,
            request_preview=preview_text,
            started_at=utc_now_iso(),
        )
        client = self._client or httpx.AsyncClient(timeout=30.0)
        owns_client = self._client is None
        try:
            data, raw_pages = await self._run(client, built, token, envelope)
            if envelope.errors:
                return envelope
            if action.output_transform:
                data = action.output_transform(data)
            envelope.success = True
            envelope.data = data
            envelope.raw = redact(raw_pages[0] if len(raw_pages) == 1 else raw_pages)
            return envelope
        except httpx.HTTPError as exc:
            envelope.errors.append(
                with_guidance(NormalizedError(ErrorCategory.NETWORK, redact_text(str(exc)), retriable=True))
            )
            return envelope
        finally:
            envelope.duration_ms = (time.monotonic() - started) * 1000
            if owns_client:
                await client.aclose()

    async def _run(
        self,
        client: httpx.AsyncClient,
        built: BuiltRequest,
        token: str,
        envelope: ResultEnvelope,
    ) -> tuple[Any, list[Any]]:
        headers = {
            "Authorization": f"Bearer {token}",  # never logged — see redaction tests
            "Accept": "application/json",
            **built.headers,
        }
        collected: list[Any] = []
        raw_pages: list[Any] = []
        url: str | None = built.url
        retry = RetryInfo()
        is_get = built.method == "GET"

        while url:
            response = await self._request_with_retry(
                client, built.method, url, headers, built.body, retry, idempotent=is_get
            )
            envelope.retry = retry
            envelope.correlation_id = response.headers.get("request-id") or envelope.correlation_id
            etag = response.headers.get("ETag")
            if etag:
                envelope.concurrency_marker = etag
            payload: Any = None
            if response.content:
                try:
                    payload = response.json()
                except ValueError:
                    envelope.errors.append(
                        with_guidance(NormalizedError(
                            ErrorCategory.PARSE,
                            "Graph returned a non-JSON response",
                            status=response.status_code,
                        ))
                    )
                    return None, raw_pages
            if response.status_code >= 400:
                envelope.errors.append(
                    normalize_graph_error(response.status_code, payload, envelope.correlation_id)
                )
                return None, raw_pages

            raw_pages.append(payload)
            envelope.page_count = len(raw_pages)
            if isinstance(payload, dict) and "value" in payload:
                collected.extend(payload["value"])
                url = payload.get("@odata.nextLink") if built.paginate else None
                if url and envelope.page_count >= self._config.max_pages:
                    envelope.truncated = True
                    envelope.warnings.append(
                        f"Result truncated at {self._config.max_pages} pages "
                        f"({len(collected)} rows). Narrow the query or raise max_pages."
                    )
                    url = None
            else:
                if not collected and len(raw_pages) == 1:
                    return payload, raw_pages
                url = None
        return collected, raw_pages

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        body: Any,
        retry: RetryInfo,
        *,
        idempotent: bool,
    ) -> httpx.Response:
        attempt = 0
        while True:
            response = await client.request(method, url, headers=headers, json=body)
            retry.attempts = attempt + 1
            if response.status_code in (429, 503, 504) and idempotent and attempt < MAX_RETRIES:
                retry.throttled = retry.throttled or response.status_code == 429
                delay = float(response.headers.get("Retry-After", 2 ** attempt))
                retry.retry_after_seconds = delay
                await asyncio.sleep(min(delay, 30.0))
                attempt += 1
                continue
            return response
