"""Microsoft Teams module — meeting/messaging policy administration via workload PowerShell.

Teams meeting/messaging policy assignment has no Graph parity, so these
actions prefer the Teams PowerShell provider (which runs on the persistent
PowerShell host so a single Connect-MicrosoftTeams is reused). Everything is
also served by the mock engine for offline use and tests.
"""

from __future__ import annotations

from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    AuditRequirement,
    Confirmation,
    ParamSpec,
    PowerShellTemplate,
    PSParamSpec,
    RiskLevel,
    RollbackLevel,
    RollbackSpec,
    ViewType,
)


def _inverse_grant_meeting_policy(params: dict, before: dict) -> tuple[str, dict]:
    return "teams.grant_meeting_policy", {
        "user_id": params["user_id"],
        "policy_name": before.get("teamsMeetingPolicy") or "Global",
    }


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="teams.meeting_policies.list",
        name="List meeting policies",
        description="List Teams meeting policies configured in the tenant.",
        service="Microsoft Teams",
        preferred_provider="teams_powershell",
        supported_providers=["teams_powershell", "mock"],
        powershell=PowerShellTemplate(
            module="MicrosoftTeams",
            command="Get-CsTeamsMeetingPolicy",
            select=["Identity", "Description", "AllowMeetNow", "AllowCloudRecording",
                    "AllowPSTNUsersToBypassLobby", "DesignatedPresenterRoleMode"],
        ),
        admin_roles=["Teams Administrator"],
        ps_module="MicrosoftTeams",
        output_schema={
            "identity": "Policy", "description": "Description",
            "allowMeetNow": "Meet now", "allowCloudRecording": "Cloud recording",
            "allowPSTNUsersToBypassLobby": "PSTN bypass lobby",
            "designatedPresenterRoleMode": "Presenter role mode",
        },
        docs_url="https://learn.microsoft.com/powershell/module/teams/get-csteamsmeetingpolicy",
    ))

    registry.register(ActionDefinition(
        id="teams.messaging_policies.list",
        name="List messaging policies",
        description="List Teams messaging policies configured in the tenant.",
        service="Microsoft Teams",
        preferred_provider="teams_powershell",
        supported_providers=["teams_powershell", "mock"],
        powershell=PowerShellTemplate(
            module="MicrosoftTeams",
            command="Get-CsTeamsMessagingPolicy",
            select=["Identity", "Description", "AllowUserEditMessage",
                    "AllowUserDeleteMessage", "AllowGiphy", "AllowMemes"],
        ),
        admin_roles=["Teams Administrator"],
        ps_module="MicrosoftTeams",
        output_schema={
            "identity": "Policy", "description": "Description",
            "allowUserEditMessage": "Edit messages", "allowUserDeleteMessage": "Delete messages",
            "allowGiphy": "Giphy", "allowMemes": "Memes",
        },
        docs_url="https://learn.microsoft.com/powershell/module/teams/get-csteamsmessagingpolicy",
    ))

    registry.register(ActionDefinition(
        id="teams.user_policies.get",
        name="View user's assigned Teams policies",
        description="Show which meeting/messaging policies are assigned to a user.",
        service="Microsoft Teams",
        preferred_provider="teams_powershell",
        supported_providers=["teams_powershell", "mock"],
        params=[ParamSpec("user_id", required=True, description="User id or UPN")],
        powershell=PowerShellTemplate(
            module="MicrosoftTeams",
            command="Get-CsOnlineUser",
            parameters=[PSParamSpec("Identity", source="user_id")],
            select=["Identity", "UserPrincipalName", "TeamsMeetingPolicy", "TeamsMessagingPolicy"],
        ),
        admin_roles=["Teams Administrator"],
        ps_module="MicrosoftTeams",
        view=ViewType.DETAIL,
        docs_url="https://learn.microsoft.com/powershell/module/teams/get-csonlineuser",
    ))

    registry.register(ActionDefinition(
        id="teams.grant_meeting_policy",
        name="Grant meeting policy",
        description="Assign a Teams meeting policy to a user.",
        service="Microsoft Teams",
        preferred_provider="teams_powershell",
        supported_providers=["teams_powershell", "mock"],
        risk=RiskLevel.MEDIUM,
        params=[
            ParamSpec("user_id", required=True),
            ParamSpec("policy_name", required=True, description="Meeting policy identity, e.g. 'Global'"),
        ],
        powershell=PowerShellTemplate(
            module="MicrosoftTeams",
            command="Grant-CsTeamsMeetingPolicy",
            parameters=[
                PSParamSpec("Identity", source="user_id"),
                PSParamSpec("PolicyName", source="policy_name"),
            ],
        ),
        admin_roles=["Teams Administrator"],
        ps_module="MicrosoftTeams",
        confirmation=Confirmation.CONFIRM,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="teams.user_policies.get",
            read_param_map={"user_id": "user_id"},
            tracked_fields=["teamsMeetingPolicy"],
            build_inverse=_inverse_grant_meeting_policy,
            notes="Re-grants the previously assigned meeting policy.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/powershell/module/teams/grant-csteamsmeetingpolicy",
    ))
