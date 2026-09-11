"""Mock engine: a fixture-backed, mutable in-memory Microsoft 365 tenant.

Lets the entire product — including write pipeline, audit and rollback — run
end-to-end with no network, tenant or PowerShell. Error/throttle/permission
scenarios are simulated with the reserved ``_simulate`` parameter.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.resources
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.actions import ActionDefinition
from ..core.envelope import ResultEnvelope, failure, utc_now_iso
from ..core.errors import ErrorCategory, NormalizedError, with_guidance
from ..core.providers import MOCK, OperationPreview, Provider
from .graph_request import build_graph_request
from .ps_builder import build_ps_command

USER_COLUMNS = [
    "id", "displayName", "userPrincipalName", "accountEnabled",
    "mail", "userType", "department", "jobTitle",
]
GROUP_COLUMNS = [
    "id", "displayName", "description", "mail", "mailEnabled",
    "securityEnabled", "isAssignableToRole", "visibility",
]

_SIMULATIONS: dict[str, NormalizedError] = {
    "throttled": NormalizedError(
        ErrorCategory.THROTTLED, "Simulated throttling (429 TooManyRequests)", status=429,
        provider_code="TooManyRequests", retriable=True,
    ),
    "permission_denied": NormalizedError(
        ErrorCategory.PERMISSION, "Simulated: insufficient privileges to complete the operation",
        status=403, provider_code="Authorization_RequestDenied",
    ),
    "not_found": NormalizedError(
        ErrorCategory.NOT_FOUND, "Simulated: resource does not exist", status=404,
        provider_code="Request_ResourceNotFound",
    ),
    "expired_session": NormalizedError(
        ErrorCategory.EXPIRED_SESSION, "Simulated: access token has expired", status=401,
        provider_code="InvalidAuthenticationToken",
    ),
    "network": NormalizedError(
        ErrorCategory.NETWORK, "Simulated network failure", retriable=True,
    ),
}


def _load_fixture(name: str, override_dir: Path | None) -> Any:
    if override_dir is not None:
        path = override_dir / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))
    ref = importlib.resources.files("msgraph_tui").joinpath("fixtures", f"{name}.json")
    return json.loads(ref.read_text(encoding="utf-8"))


class MockTenant:
    """Mutable in-memory tenant state hydrated from fixtures."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self.users: list[dict] = _load_fixture("users", fixtures_dir)
        self.groups: list[dict] = _load_fixture("groups", fixtures_dir)
        self.memberships: dict[str, dict] = _load_fixture("memberships", fixtures_dir)
        self.skus: list[dict] = _load_fixture("skus", fixtures_dir)
        self.organization: dict = _load_fixture("organization", fixtures_dir)

    def user(self, user_id: str) -> dict | None:
        return next(
            (u for u in self.users
             if u["id"] == user_id or u["userPrincipalName"].lower() == str(user_id).lower()),
            None,
        )

    def group(self, group_id: str) -> dict | None:
        return next((g for g in self.groups if g["id"] == group_id), None)

    def sku(self, sku_id: str) -> dict | None:
        return next(
            (s for s in self.skus
             if s["skuId"] == sku_id or s["skuPartNumber"] == sku_id),
            None,
        )


def _project(row: dict, columns: list[str]) -> dict:
    return {c: row.get(c) for c in columns}


def _match(row: dict, needle: str, fields: tuple[str, ...]) -> bool:
    needle = needle.lower()
    return any(needle in str(row.get(f, "") or "").lower() for f in fields)


class MockProvider(Provider):
    name = MOCK
    display_name = "Mock tenant (fixtures)"

    def __init__(self, fixtures_dir: Path | None = None, latency_seconds: float = 0.0) -> None:
        self.tenant = MockTenant(fixtures_dir)
        self._latency = latency_seconds
        self._handlers: dict[str, Callable[[dict], Any]] = {
            "users.list": self._users_list,
            "users.get": self._users_get,
            "users.update": self._users_update,
            "users.set_account_enabled": self._users_set_enabled,
            "users.revoke_sessions": self._users_revoke_sessions,
            "users.licenses": self._users_licenses,
            "users.memberships": self._users_memberships,
            "users.assign_license": self._assign_license,
            "users.remove_license": self._remove_license,
            "groups.list": self._groups_list,
            "groups.get": self._groups_get,
            "groups.members": self._groups_members,
            "groups.owners": self._groups_owners,
            "groups.member_add": self._member_add,
            "groups.member_remove": self._member_remove,
            "licenses.skus": self._skus,
            "licenses.users_by_sku": self._users_by_sku,
            "licenses.disabled_with_license": self._disabled_with_license,
            "session.organization": lambda p: dict(self.tenant.organization),
        }

    def is_available(self) -> bool:
        return True

    def availability_detail(self) -> str:
        return f"mock tenant '{self.tenant.organization['displayName']}'"

    def supports(self, action: ActionDefinition) -> bool:
        return action.id in self._handlers

    def preview(self, action: ActionDefinition, params: dict[str, Any]) -> OperationPreview:
        """Preview shows the operation the *preferred real provider* would run,
        so mock mode is honest about what live mode would do."""
        detail = summary = f"mock://{action.id}"
        notes = ["Mock mode: executed against fixture data, not a live tenant."]
        try:
            if action.graph is not None:
                built = build_graph_request(action, params)
                summary = f"{built.method} {built.url}"
                detail = built.preview_text()
                notes.append("Shown request is what graph_rest would execute in live mode.")
            elif action.powershell is not None:
                detail = build_ps_command(action, params)
                summary = detail.split(" | ")[0]
                notes.append("Shown command is what the PowerShell provider would run in live mode.")
        except ValueError as exc:
            notes.append(f"(could not render live preview: {exc})")
        return OperationPreview(
            provider=self.name,
            summary=summary,
            detail=detail,
            required_scopes=action.graph_scopes,
            required_roles=action.admin_roles,
            notes=notes,
        )

    async def execute(self, action: ActionDefinition, params: dict[str, Any]) -> ResultEnvelope:
        started = time.monotonic()
        if self._latency:
            await asyncio.sleep(self._latency)
        preview = self.preview(action, params)
        simulate = params.get("_simulate")
        if simulate:
            if simulate == "malformed":
                env = failure(
                    self.name, action.id,
                    with_guidance(NormalizedError(
                        ErrorCategory.PARSE, "Simulated malformed provider output",
                    )),
                    request_preview=preview.detail,
                )
                env.raw = "WARNING: something\x00{not-json"
                return env
            err = _SIMULATIONS.get(str(simulate))
            if err is None:
                err = NormalizedError(ErrorCategory.UNKNOWN, f"Unknown simulation {simulate!r}")
            return failure(
                self.name, action.id, with_guidance(copy.deepcopy(err)),
                request_preview=preview.detail,
            )

        handler = self._handlers.get(action.id)
        if handler is None:
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    f"Mock provider has no handler for {action.id}",
                )),
                request_preview=preview.detail,
            )
        try:
            data = handler(params)
        except _MockError as exc:
            return failure(
                self.name, action.id, with_guidance(exc.error),
                request_preview=preview.detail,
            )
        envelope = ResultEnvelope(
            success=True,
            provider=self.name,
            action_id=action.id,
            request_preview=preview.detail,
            data=copy.deepcopy(data),
            raw=copy.deepcopy(data),
            started_at=utc_now_iso(),
            correlation_id="mock-correlation-id",
        )
        envelope.duration_ms = (time.monotonic() - started) * 1000
        return envelope

    # ------------------------------------------------------------------
    # Handlers (operate on live mutable state)
    # ------------------------------------------------------------------

    def _users_list(self, p: dict) -> list[dict]:
        rows = self.tenant.users
        search = p.get("search")
        if search:
            rows = [u for u in rows if _match(u, search, ("displayName", "userPrincipalName", "mail"))]
        top = p.get("top")
        if top:
            rows = rows[: int(top)]
        return [_project(u, USER_COLUMNS) for u in rows]

    def _require_user(self, p: dict) -> dict:
        user = self.tenant.user(p.get("user_id", ""))
        if user is None:
            raise _MockError(NormalizedError(
                ErrorCategory.NOT_FOUND, f"User {p.get('user_id')!r} not found",
                status=404, provider_code="Request_ResourceNotFound",
            ))
        return user

    def _require_group(self, p: dict) -> dict:
        group = self.tenant.group(p.get("group_id", ""))
        if group is None:
            raise _MockError(NormalizedError(
                ErrorCategory.NOT_FOUND, f"Group {p.get('group_id')!r} not found",
                status=404, provider_code="Request_ResourceNotFound",
            ))
        return group

    def _users_get(self, p: dict) -> dict:
        return dict(self._require_user(p))

    def _users_update(self, p: dict) -> dict:
        user = self._require_user(p)
        for field_name in ("department", "jobTitle", "officeLocation", "displayName"):
            if field_name in p and p[field_name] is not None:
                user[field_name] = p[field_name]
        return dict(user)

    def _users_set_enabled(self, p: dict) -> dict:
        user = self._require_user(p)
        user["accountEnabled"] = bool(p["enabled"])
        return dict(user)

    def _users_revoke_sessions(self, p: dict) -> dict:
        user = self._require_user(p)
        user["signInSessionsValidFromDateTime"] = utc_now_iso()
        return {"value": True, "id": user["id"]}

    def _users_licenses(self, p: dict) -> list[dict]:
        user = self._require_user(p)
        out = []
        for assignment in user.get("assignedLicenses", []):
            sku = self.tenant.sku(assignment["skuId"]) or {}
            out.append({
                "skuId": assignment["skuId"],
                "skuPartNumber": sku.get("skuPartNumber"),
                "displayName": sku.get("displayName"),
            })
        return out

    def _users_memberships(self, p: dict) -> list[dict]:
        user = self._require_user(p)
        rows = []
        for group_id, membership in self.tenant.memberships.items():
            if user["id"] in membership["members"]:
                group = self.tenant.group(group_id) or {"id": group_id}
                rows.append(_project(group, GROUP_COLUMNS))
        return rows

    def _assign_license(self, p: dict) -> dict:
        user = self._require_user(p)
        sku = self.tenant.sku(p["sku_id"])
        if sku is None:
            raise _MockError(NormalizedError(
                ErrorCategory.INVALID_INPUT, f"SKU {p['sku_id']!r} does not exist in this tenant",
            ))
        if any(a["skuId"] == sku["skuId"] for a in user.setdefault("assignedLicenses", [])):
            raise _MockError(NormalizedError(
                ErrorCategory.CONFLICT, f"User already has licence {sku['skuPartNumber']}",
            ))
        user["assignedLicenses"].append({"skuId": sku["skuId"]})
        sku["consumedUnits"] = sku.get("consumedUnits", 0) + 1
        return dict(user)

    def _remove_license(self, p: dict) -> dict:
        user = self._require_user(p)
        sku = self.tenant.sku(p["sku_id"])
        sku_id = sku["skuId"] if sku else p["sku_id"]
        assignments = user.get("assignedLicenses", [])
        if not any(a["skuId"] == sku_id for a in assignments):
            raise _MockError(NormalizedError(
                ErrorCategory.CONFLICT, f"User does not hold licence {p['sku_id']!r}",
            ))
        user["assignedLicenses"] = [a for a in assignments if a["skuId"] != sku_id]
        if sku:
            sku["consumedUnits"] = max(0, sku.get("consumedUnits", 1) - 1)
        return dict(user)

    def _groups_list(self, p: dict) -> list[dict]:
        rows = self.tenant.groups
        search = p.get("search")
        if search:
            rows = [g for g in rows if _match(g, search, ("displayName", "description", "mail"))]
        top = p.get("top")
        if top:
            rows = rows[: int(top)]
        return [_project(g, GROUP_COLUMNS) for g in rows]

    def _groups_get(self, p: dict) -> dict:
        return dict(self._require_group(p))

    def _member_rows(self, group_id: str, key: str) -> list[dict]:
        ids = self.tenant.memberships.get(group_id, {}).get(key, [])
        return [
            _project(self.tenant.user(uid) or {"id": uid, "displayName": "(unknown)"}, USER_COLUMNS)
            for uid in ids
        ]

    def _groups_members(self, p: dict) -> list[dict]:
        self._require_group(p)
        return self._member_rows(p["group_id"], "members")

    def _groups_owners(self, p: dict) -> list[dict]:
        self._require_group(p)
        return self._member_rows(p["group_id"], "owners")

    def _member_add(self, p: dict) -> dict:
        group = self._require_group(p)
        user = self._require_user(p)
        members = self.tenant.memberships.setdefault(group["id"], {"owners": [], "members": []})["members"]
        if user["id"] in members:
            raise _MockError(NormalizedError(
                ErrorCategory.CONFLICT,
                "One or more added object references already exist for the following modified properties: 'members'.",
                status=400, provider_code="Request_BadRequest",
            ))
        members.append(user["id"])
        return {"group_id": group["id"], "user_id": user["id"], "members": list(members)}

    def _member_remove(self, p: dict) -> dict:
        group = self._require_group(p)
        user = self._require_user(p)
        members = self.tenant.memberships.setdefault(group["id"], {"owners": [], "members": []})["members"]
        if user["id"] not in members:
            raise _MockError(NormalizedError(
                ErrorCategory.NOT_FOUND, "User is not a member of this group", status=404,
            ))
        members.remove(user["id"])
        return {"group_id": group["id"], "user_id": user["id"], "members": list(members)}

    def _skus(self, p: dict) -> list[dict]:
        return [
            {
                "skuId": s["skuId"],
                "skuPartNumber": s["skuPartNumber"],
                "displayName": s.get("displayName"),
                "enabled": s["prepaidUnits"]["enabled"],
                "consumed": s["consumedUnits"],
                "available": s["prepaidUnits"]["enabled"] - s["consumedUnits"],
                "capabilityStatus": s.get("capabilityStatus"),
            }
            for s in self.tenant.skus
        ]

    def _users_by_sku(self, p: dict) -> list[dict]:
        sku = self.tenant.sku(p["sku_id"])
        if sku is None:
            raise _MockError(NormalizedError(
                ErrorCategory.NOT_FOUND, f"SKU {p['sku_id']!r} not found", status=404,
            ))
        return [
            _project(u, USER_COLUMNS)
            for u in self.tenant.users
            if any(a["skuId"] == sku["skuId"] for a in u.get("assignedLicenses", []))
        ]

    def _disabled_with_license(self, p: dict) -> list[dict]:
        out = []
        for u in self.tenant.users:
            if not u.get("accountEnabled") and u.get("assignedLicenses"):
                row = _project(u, USER_COLUMNS)
                row["licences"] = ", ".join(
                    (self.tenant.sku(a["skuId"]) or {}).get("skuPartNumber", a["skuId"])
                    for a in u["assignedLicenses"]
                )
                out.append(row)
        return out


class _MockError(Exception):
    def __init__(self, error: NormalizedError) -> None:
        super().__init__(error.message)
        self.error = error
