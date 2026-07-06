"""Groups module — Microsoft 365 / security / distribution group actions."""

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

GROUP_SELECT = [
    "id", "displayName", "description", "mail", "mailEnabled",
    "securityEnabled", "isAssignableToRole", "visibility", "groupTypes",
]


def _inverse_member_add(params: dict, before: dict) -> tuple[str, dict]:
    return "groups.member_remove", {"group_id": params["group_id"], "user_id": params["user_id"]}


def _inverse_member_remove(params: dict, before: dict) -> tuple[str, dict]:
    return "groups.member_add", {"group_id": params["group_id"], "user_id": params["user_id"]}


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="groups.list",
        name="List groups",
        description="List all groups (M365, security, mail-enabled, distribution).",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[
            ParamSpec("search", description="Filter by name, description or mail (contains)"),
            ParamSpec("top", type="int"),
        ],
        graph=GraphTemplate(
            method="GET",
            path="/groups",
            query={"$select": ",".join(GROUP_SELECT), "$top": "{top}"},
        ),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Groups",
            command="Get-MgGroup",
            parameters=[PSParamSpec("All", switch=True)],
            select=["Id", "DisplayName", "Description", "Mail", "MailEnabled",
                    "SecurityEnabled", "IsAssignableToRole", "Visibility"],
        ),
        graph_scopes=["Group.Read.All"],
        ps_module="Microsoft.Graph.Groups",
        output_schema={
            "displayName": "Name", "description": "Description", "mail": "Mail",
            "securityEnabled": "Security", "isAssignableToRole": "Role-assignable",
            "visibility": "Visibility",
        },
        docs_url="https://learn.microsoft.com/graph/api/group-list",
    ))

    registry.register(ActionDefinition(
        id="groups.get",
        name="View group",
        description="Show full detail for a single group.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("group_id", required=True)],
        graph=GraphTemplate(method="GET", path="/groups/{group_id}", paginate=False),
        graph_scopes=["Group.Read.All"],
        view=ViewType.DETAIL,
        docs_url="https://learn.microsoft.com/graph/api/group-get",
    ))

    registry.register(ActionDefinition(
        id="groups.members",
        name="View group members",
        description="List the members of a group.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("group_id", required=True)],
        graph=GraphTemplate(
            method="GET",
            path="/groups/{group_id}/members",
            query={"$select": "id,displayName,userPrincipalName,mail,accountEnabled"},
        ),
        graph_scopes=["Group.Read.All"],
        output_schema={"displayName": "Name", "userPrincipalName": "UPN",
                       "mail": "Mail", "accountEnabled": "Enabled"},
        docs_url="https://learn.microsoft.com/graph/api/group-list-members",
    ))

    registry.register(ActionDefinition(
        id="groups.owners",
        name="View group owners",
        description="List the owners of a group.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        params=[ParamSpec("group_id", required=True)],
        graph=GraphTemplate(
            method="GET",
            path="/groups/{group_id}/owners",
            query={"$select": "id,displayName,userPrincipalName,mail"},
        ),
        graph_scopes=["Group.Read.All"],
        output_schema={"displayName": "Name", "userPrincipalName": "UPN", "mail": "Mail"},
        docs_url="https://learn.microsoft.com/graph/api/group-list-owners",
    ))

    registry.register(ActionDefinition(
        id="groups.member_add",
        name="Add group member",
        description="Add a user to a group. Adding to role-assignable groups grants directory roles.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.MEDIUM,
        tags={ActionTag.PRIVILEGED},  # may touch role-assignable groups; UI escalates warnings
        params=[
            ParamSpec("group_id", required=True),
            ParamSpec("user_id", required=True),
        ],
        graph=GraphTemplate(
            method="POST",
            path="/groups/{group_id}/members/$ref",
            body={"@odata.id": "{member_ref}"},
            paginate=False,
        ),
        derive_params=lambda p: {
            "member_ref": f"https://graph.microsoft.com/v1.0/directoryObjects/{p['user_id']}"
        },
        graph_scopes=["GroupMember.ReadWrite.All"],
        admin_roles=["Groups Administrator"],
        confirmation=Confirmation.CONFIRM,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="groups.members",
            read_param_map={"group_id": "group_id"},
            tracked_fields=["members"],
            build_inverse=_inverse_member_add,
            notes="Membership re-checked at rollback time; intervening changes block auto-rollback.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/group-post-members",
    ))

    registry.register(ActionDefinition(
        id="groups.member_remove",
        name="Remove group member",
        description="Remove a user from a group. Access granted via the group is lost immediately.",
        service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        risk=RiskLevel.HIGH,
        tags={ActionTag.PRIVILEGED},
        params=[
            ParamSpec("group_id", required=True),
            ParamSpec("user_id", required=True),
        ],
        graph=GraphTemplate(
            method="DELETE",
            path="/groups/{group_id}/members/{user_id}/$ref",
            paginate=False,
        ),
        graph_scopes=["GroupMember.ReadWrite.All"],
        admin_roles=["Groups Administrator"],
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="groups.members",
            read_param_map={"group_id": "group_id"},
            tracked_fields=["members"],
            build_inverse=_inverse_member_remove,
            notes="Membership re-checked at rollback time; intervening changes block auto-rollback.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/graph/api/group-delete-members",
    ))
