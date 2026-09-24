"""Security & Compliance (Purview) module — read-only compliance posture reporting.

Retention and DLP policy configuration has no Graph parity, so these actions
prefer the Security & Compliance PowerShell provider (which runs on the
persistent PowerShell host so a single Connect-IPPSSession is reused).
Everything is also served by the mock engine for offline use and tests.

Deliberately read-only: creating/modifying retention or DLP policies is a
heavy, org-wide operation best done deliberately in the Purview portal or a
dedicated change process, not from a quick admin console action.
"""

from __future__ import annotations

from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    PowerShellTemplate,
)


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="scc.retention_policies.list",
        name="List retention policies",
        description="List Microsoft Purview retention compliance policies.",
        service="Security & Compliance",
        preferred_provider="scc_powershell",
        supported_providers=["scc_powershell", "mock"],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-RetentionCompliancePolicy",
            select=["Name", "Enabled", "Mode", "Workload", "Comment"],
        ),
        admin_roles=["Compliance Administrator"],
        ps_module="ExchangeOnlineManagement",
        output_schema={
            "name": "Name", "enabled": "Enabled", "mode": "Mode",
            "workload": "Workload", "comment": "Comment",
        },
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-retentioncompliancepolicy",
    ))

    registry.register(ActionDefinition(
        id="scc.dlp_policies.list",
        name="List DLP policies",
        description="List Microsoft Purview data loss prevention (DLP) policies.",
        service="Security & Compliance",
        preferred_provider="scc_powershell",
        supported_providers=["scc_powershell", "mock"],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-DlpCompliancePolicy",
            select=["Name", "Mode", "State", "Workload"],
        ),
        admin_roles=["Compliance Administrator"],
        ps_module="ExchangeOnlineManagement",
        output_schema={
            "name": "Name", "mode": "Mode", "state": "State", "workload": "Workload",
        },
        docs_url="https://learn.microsoft.com/powershell/module/exchange/get-dlpcompliancepolicy",
    ))
