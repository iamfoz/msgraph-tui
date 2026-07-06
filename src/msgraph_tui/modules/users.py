"""Users module — Entra ID user administration actions."""

from __future__ import annotations

from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    ActionTag,
    AuditRequirement,
    Confirmation,
    GraphTemplate,
    ParamSpec,
    PowerShellTemplate,
    PSParamSpec,
    RiskLevel,
    RollbackLevel,
    RollbackSpec,
    ViewType,
)

USER_SELECT = [
    "id", "displayName", "userPrincipalName", "accountEnabled",
    "mail", "userType", "department", "jobTitle",
]

UPDATABLE_FIELDS = ("displayName", "department", "jobTitle", "officeLocation")


def _inverse_update(params: dict, before: dict) -> tuple[str, dict]:
    """Restore the previously captured values of the fields that were changed."""
    inverse = {"user_id": params["user_id"]}
    for field in UPDATABLE_FIELDS:
        if field in params and params[field] is not None:
            inverse[field] = before.get(field)
    return "users.update", inverse


def _inverse_set_enabled(params: dict, before: dict) -> tuple[str, dict]:
    return "users.set_account_enabled", {
        "user_id": params["user_id"],
        "enabled": bool(before.get("accountEnabled")),
    }


def _inverse_assign_license(params: dict, before: dict) -> tuple[str, dict]:
    return "users.remove_license", {"user_id": params["user_id"], "sku_id": params["sku_id"]}


def _inverse_remove_license(params: dict, before: dict) -> tuple[str, dict]:
    return "users.assign_license", {"user_id": params["user_id"], "sku_id": params["sku_id"]}


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="users.list",
        name="List users",
        description="List Entra ID users with common identity fields.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[
            ParamSpec("search", description="Filter by name, UPN or mail (contains)"),
            ParamSpec("top", type="int", description="Max rows to return"),
        ],
        graph=GraphTemplate(
            method="GET",
            path="/users",
            query={"$select": ",".join(USER_SELECT), "$top": "{top}"},
        ),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Users",
            command="Get-MgUser",
            parameters=[
                PSParamSpec("All", switch=True),
                PSParamSpec("Property", literal=USER_SELECT),
            ],
            select=["Id", "DisplayName", "UserPrincipalName", "AccountEnabled",
                    "Mail", "UserType", "Department", "JobTitle"],
        ),
        graph_scopes=["User.Read.All"],
        ps_module="Microsoft.Graph.Users",
        output_schema={
            "displayName": "Name", "userPrincipalName": "UPN", "accountEnabled": "Enabled",
            "mail": "Mail", "userType": "Type", "department": "Department", "jobTitle": "Job title",
        },
        docs_url="https://learn.microsoft.com/graph/api/user-list",
    ))

    registry.register(ActionDefinition(
        id="users.get",
        name="View user",
        description="Show full detail for a single user.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("user_id", required=True, description="User id or UPN")],
        graph=GraphTemplate(method="GET", path="/users/{user_id}", paginate=False),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Users",
            command="Get-MgUser",
            parameters=[PSParamSpec("UserId", source="user_id")],
        ),
        graph_scopes=["User.Read.All"],
        ps_module="Microsoft.Graph.Users",
        view=ViewType.DETAIL,
        docs_url="https://learn.microsoft.com/graph/api/user-get",
    ))

    registry.register(ActionDefinition(
        id="users.update",
        name="Update user properties",
        description="Update editable identity properties (display name, department, job title, office).",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.MEDIUM,
        params=[
            ParamSpec("user_id", required=True),
            ParamSpec("displayName", description="New display name"),
            ParamSpec("department", description="New department"),
            ParamSpec("jobTitle", description="New job title"),
            ParamSpec("officeLocation", description="New office location"),
        ],
        graph=GraphTemplate(
            method="PATCH",
            path="/users/{user_id}",
            body={
                "displayName": "{displayName}",
                "department": "{department}",
                "jobTitle": "{jobTitle}",
                "officeLocation": "{officeLocation}",
            },
            paginate=False,
        ),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Users",
            command="Update-MgUser",
            parameters=[
                PSParamSpec("UserId", source="user_id"),
                PSParamSpec("DisplayName", source="displayName"),
                PSParamSpec("Department", source="department"),
                PSParamSpec("JobTitle", source="jobTitle"),
                PSParamSpec("OfficeLocation", source="officeLocation"),
            ],
            supports_whatif=True,
        ),
        graph_scopes=["User.ReadWrite.All"],
        admin_roles=["User Administrator"],
        ps_module="Microsoft.Graph.Users",
        confirmation=Confirmation.CONFIRM,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="users.get",
            read_param_map={"user_id": "user_id"},
            tracked_fields=list(UPDATABLE_FIELDS),
            build_inverse=_inverse_update,
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/user-update",
    ))

    registry.register(ActionDefinition(
        id="users.set_account_enabled",
        name="Enable / disable account",
        description="Enable or disable a user's sign-in. Disabling blocks all new sign-ins.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.HIGH,
        params=[
            ParamSpec("user_id", required=True),
            ParamSpec("enabled", type="bool", required=True,
                      description="true = enable sign-in, false = disable"),
        ],
        graph=GraphTemplate(
            method="PATCH",
            path="/users/{user_id}",
            body={"accountEnabled": "{enabled}"},
            paginate=False,
        ),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Users",
            command="Update-MgUser",
            parameters=[
                PSParamSpec("UserId", source="user_id"),
                PSParamSpec("AccountEnabled", source="enabled"),
            ],
            supports_whatif=True,
        ),
        graph_scopes=["User.ReadWrite.All"],
        admin_roles=["User Administrator"],
        ps_module="Microsoft.Graph.Users",
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="users.get",
            read_param_map={"user_id": "user_id"},
            tracked_fields=["accountEnabled"],
            build_inverse=_inverse_set_enabled,
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/user-update",
    ))

    registry.register(ActionDefinition(
        id="users.revoke_sessions",
        name="Revoke sign-in sessions",
        description="Invalidate all refresh tokens; the user must sign in again everywhere.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.HIGH,
        tags={ActionTag.COMPLIANCE_SENSITIVE},
        params=[ParamSpec("user_id", required=True)],
        graph=GraphTemplate(method="POST", path="/users/{user_id}/revokeSignInSessions", paginate=False),
        graph_scopes=["User.RevokeSessions.All"],
        admin_roles=["User Administrator"],
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.NONE,
            notes="Revocation cannot be undone; sessions must be re-established by the user.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/user-revokesigninsessions",
    ))

    registry.register(ActionDefinition(
        id="users.licenses",
        name="View user licences",
        description="Licences assigned to a user.",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("user_id", required=True)],
        graph=GraphTemplate(method="GET", path="/users/{user_id}/licenseDetails"),
        graph_scopes=["User.Read.All"],
        output_schema={"skuPartNumber": "SKU", "displayName": "Product", "skuId": "SKU id"},
        docs_url="https://learn.microsoft.com/graph/api/user-list-licensedetails",
    ))

    registry.register(ActionDefinition(
        id="users.memberships",
        name="View user group memberships",
        description="Groups the user is a member of.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("user_id", required=True)],
        graph=GraphTemplate(
            method="GET",
            path="/users/{user_id}/memberOf",
            query={"$select": "id,displayName,mail,securityEnabled,isAssignableToRole"},
        ),
        graph_scopes=["User.Read.All", "Group.Read.All"],
        output_schema={"displayName": "Group", "mail": "Mail",
                       "securityEnabled": "Security", "isAssignableToRole": "Role-assignable"},
        docs_url="https://learn.microsoft.com/graph/api/user-list-memberof",
    ))

    registry.register(ActionDefinition(
        id="users.assign_license",
        name="Assign licence",
        description="Assign a licence SKU to a user (user must have a usage location set).",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.MEDIUM,
        params=[
            ParamSpec("user_id", required=True),
            ParamSpec("sku_id", required=True, description="SKU id or part number"),
        ],
        graph=GraphTemplate(
            method="POST",
            path="/users/{user_id}/assignLicense",
            body={"addLicenses": [{"skuId": "{sku_id}"}], "removeLicenses": []},
            paginate=False,
        ),
        graph_scopes=["User.ReadWrite.All"],
        admin_roles=["License Administrator", "User Administrator"],
        confirmation=Confirmation.CONFIRM,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="users.get",
            read_param_map={"user_id": "user_id"},
            tracked_fields=["assignedLicenses"],
            build_inverse=_inverse_assign_license,
            notes="Warns if SKU availability or group-based licensing changed since the snapshot.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/user-assignlicense",
    ))

    registry.register(ActionDefinition(
        id="users.remove_license",
        name="Remove licence",
        description="Remove a licence SKU from a user. Service data may become inaccessible.",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.HIGH,
        params=[
            ParamSpec("user_id", required=True),
            ParamSpec("sku_id", required=True, description="SKU id or part number"),
        ],
        graph=GraphTemplate(
            method="POST",
            path="/users/{user_id}/assignLicense",
            body={"addLicenses": [], "removeLicenses": ["{sku_id}"]},
            paginate=False,
        ),
        graph_scopes=["User.ReadWrite.All"],
        admin_roles=["License Administrator", "User Administrator"],
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="users.get",
            read_param_map={"user_id": "user_id"},
            tracked_fields=["assignedLicenses"],
            build_inverse=_inverse_remove_license,
            notes="Re-assignment restores the licence but user data retention depends on the workload.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/user-assignlicense",
    ))
