"""SharePoint Online module — site administration via workload PowerShell.

SharePoint site sharing configuration has no Graph parity, so these actions
prefer the SharePoint Online PowerShell provider (which runs on the persistent
PowerShell host so a single Connect-SPOService is reused). Everything is also
served by the mock engine for offline use and tests.
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

SHARING_CAPABILITY_CHOICES = [
    "Disabled",
    "ExistingExternalUserSharingOnly",
    "ExternalUserSharingOnly",
    "ExternalUserAndGuestSharing",
]


def _inverse_set_sharing(params: dict, before: dict) -> tuple[str, dict]:
    return "sharepoint.set_sharing", {
        "site_url": params["site_url"],
        "sharing_capability": before.get("sharingCapability") or "Disabled",
    }


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="sharepoint.sites.list",
        name="List sites",
        description="List SharePoint Online sites and their storage/sharing configuration.",
        service="SharePoint Online",
        preferred_provider="sharepoint_powershell",
        supported_providers=["sharepoint_powershell", "mock"],
        powershell=PowerShellTemplate(
            module="Microsoft.Online.SharePoint.PowerShell",
            command="Get-SPOSite",
            select=["Url", "Title", "StorageQuota", "StorageUsedCurrent",
                    "SharingCapability", "LockState"],
        ),
        admin_roles=["SharePoint Administrator"],
        ps_module="Microsoft.Online.SharePoint.PowerShell",
        output_schema={
            "url": "Url", "title": "Title", "storageQuota": "Storage quota (MB)",
            "storageUsedCurrent": "Storage used (MB)", "sharingCapability": "Sharing",
            "lockState": "Lock state",
        },
        docs_url="https://learn.microsoft.com/powershell/module/sharepoint-online/get-sposite",
    ))

    registry.register(ActionDefinition(
        id="sharepoint.site.get",
        name="View site",
        description="Show a single SharePoint site's configuration.",
        service="SharePoint Online",
        preferred_provider="sharepoint_powershell",
        supported_providers=["sharepoint_powershell", "mock"],
        params=[ParamSpec("site_url", required=True, description="Site collection URL")],
        powershell=PowerShellTemplate(
            module="Microsoft.Online.SharePoint.PowerShell",
            command="Get-SPOSite",
            parameters=[PSParamSpec("Identity", source="site_url")],
        ),
        admin_roles=["SharePoint Administrator"],
        ps_module="Microsoft.Online.SharePoint.PowerShell",
        view=ViewType.DETAIL,
        docs_url="https://learn.microsoft.com/powershell/module/sharepoint-online/get-sposite",
    ))

    registry.register(ActionDefinition(
        id="sharepoint.set_sharing",
        name="Set site sharing capability",
        description=(
            "Change a site's external sharing capability. "
            "ExternalUserAndGuestSharing allows anonymous/anyone links — high exfiltration risk."
        ),
        service="SharePoint Online",
        preferred_provider="sharepoint_powershell",
        supported_providers=["sharepoint_powershell", "mock"],
        risk=RiskLevel.HIGH,
        tags={ActionTag.COMPLIANCE_SENSITIVE},
        params=[
            ParamSpec("site_url", required=True),
            ParamSpec("sharing_capability", type="choice", required=True,
                      choices=SHARING_CAPABILITY_CHOICES),
        ],
        powershell=PowerShellTemplate(
            module="Microsoft.Online.SharePoint.PowerShell",
            command="Set-SPOSite",
            parameters=[
                PSParamSpec("Identity", source="site_url"),
                PSParamSpec("SharingCapability", source="sharing_capability"),
            ],
        ),
        admin_roles=["SharePoint Administrator"],
        ps_module="Microsoft.Online.SharePoint.PowerShell",
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(
            level=RollbackLevel.FULL,
            read_action="sharepoint.site.get",
            read_param_map={"site_url": "site_url"},
            tracked_fields=["sharingCapability"],
            build_inverse=_inverse_set_sharing,
            notes=(
                "Restores the previous sharing capability. "
                "⚠ Setting ExternalUserAndGuestSharing allows anyone links to this site — "
                "verify this is intended before confirming."
            ),
        ),
        view=ViewType.FORM,
        docs_url="https://learn.microsoft.com/powershell/module/sharepoint-online/set-sposite",
    ))
