"""Exchange Online module — mailbox administration via workload PowerShell.

Exchange mailbox permissions, forwarding and inbox rules have no Graph parity,
so these actions prefer the Exchange Online PowerShell provider (which runs on
the persistent PowerShell host so a single Connect-ExchangeOnline is reused).
Everything is also served by the mock engine for offline use and tests.
"""

from __future__ import annotations

from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    ActionTag,
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

MAILBOX_SELECT = [
    "id", "displayName", "userPrincipalName", "primarySmtpAddress",
    "recipientTypeDetails", "forwardingSmtpAddress", "litigationHoldEnabled",
]


def _inverse_set_forwarding(params: dict, before: dict) -> tuple[str, dict]:
    return "exchange.set_forwarding", {
        "mailbox_id": params["mailbox_id"],
        "forwarding_address": before.get("forwardingSmtpAddress") or "",
        "deliver_and_forward": bool(before.get("deliverToMailboxAndForward")),
    }


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="exchange.mailboxes.list",
        name="List mailboxes",
        description="List Exchange Online mailboxes (user, shared, room, equipment).",
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell", "mock"],
        params=[ParamSpec("search", description="Filter by name / address (contains)")],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-Mailbox",
            parameters=[PSParamSpec("ResultSize", literal="Unlimited")],
            select=["Identity", "DisplayName", "UserPrincipalName", "PrimarySmtpAddress",
                    "RecipientTypeDetails", "ForwardingSmtpAddress", "LitigationHoldEnabled"],
        ),
        graph_scopes=[],
        admin_roles=["Exchange Administrator"],
        ps_module="ExchangeOnlineManagement",
        app_only_supported=True,
        output_schema={
            "displayName": "Name", "primarySmtpAddress": "Primary SMTP",
            "recipientTypeDetails": "Type", "forwardingSmtpAddress": "Forwarding",
            "litigationHoldEnabled": "Litigation hold",
        },
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-mailbox",
    ))

    registry.register(ActionDefinition(
        id="exchange.mailbox.get",
        name="View mailbox",
        description="Show a single mailbox's configuration.",
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell", "mock"],
        params=[ParamSpec("mailbox_id", required=True, description="Mailbox identity / UPN")],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-Mailbox",
            parameters=[PSParamSpec("Identity", source="mailbox_id")],
        ),
        admin_roles=["Exchange Administrator"],
        ps_module="ExchangeOnlineManagement",
        view=ViewType.DETAIL,
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-mailbox",
    ))

    registry.register(ActionDefinition(
        id="exchange.mailbox_permissions",
        name="View mailbox permissions",
        description="Who has FullAccess/SendAs on this mailbox.",
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell", "mock"],
        params=[ParamSpec("mailbox_id", required=True)],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-MailboxPermission",
            parameters=[PSParamSpec("Identity", source="mailbox_id")],
        ),
        admin_roles=["Exchange Administrator"],
        ps_module="ExchangeOnlineManagement",
        output_schema={"user": "Trustee", "accessRights": "Rights", "isInherited": "Inherited"},
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-mailboxpermission",
    ))

    registry.register(ActionDefinition(
        id="exchange.inbox_rules",
        name="View inbox rules",
        description="Inbox rules on a mailbox — key surface for BEC / forwarding hunts.",
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell", "mock"],
        tags={ActionTag.COMPLIANCE_SENSITIVE},
        params=[ParamSpec("mailbox_id", required=True)],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-InboxRule",
            parameters=[PSParamSpec("Mailbox", source="mailbox_id")],
        ),
        admin_roles=["Exchange Administrator"],
        ps_module="ExchangeOnlineManagement",
        output_schema={"name": "Rule", "enabled": "Enabled", "forwardTo": "Forwards to",
                       "deleteMessage": "Deletes", "moveToFolder": "Moves to"},
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-inboxrule",
    ))

    registry.register(ActionDefinition(
        id="exchange.set_forwarding",
        name="Set / clear mailbox forwarding",
        description=(
            "Set or clear SMTP forwarding on a mailbox. Exfiltration risk — "
            "forwarding to an external address is a classic BEC persistence."
        ),
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell", "mock"],
        risk=RiskLevel.HIGH,
        tags={ActionTag.COMPLIANCE_SENSITIVE},
        params=[
            ParamSpec("mailbox_id", required=True),
            ParamSpec("forwarding_address",
                      description="External/internal SMTP address; empty clears forwarding"),
            ParamSpec("deliver_and_forward", type="bool", default=False,
                      description="Also keep a copy in the mailbox"),
        ],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Set-Mailbox",
            parameters=[
                PSParamSpec("Identity", source="mailbox_id"),
                PSParamSpec("ForwardingSmtpAddress", source="forwarding_address"),
                PSParamSpec("DeliverToMailboxAndForward", source="deliver_and_forward"),
            ],
            supports_whatif=True,
        ),
        admin_roles=["Exchange Administrator"],
        ps_module="ExchangeOnlineManagement",
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="exchange.mailbox.get",
            read_param_map={"mailbox_id": "mailbox_id"},
            tracked_fields=["forwardingSmtpAddress", "deliverToMailboxAndForward"],
            build_inverse=_inverse_set_forwarding,
            notes="Restores the previous forwarding address after checking it has not drifted.",
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/powershell/module/exchange/set-mailbox",
    ))
