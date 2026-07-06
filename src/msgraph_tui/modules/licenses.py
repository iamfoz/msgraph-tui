"""Licences module — subscribed SKUs, consumption and licence reports."""

from __future__ import annotations

from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    GraphTemplate,
    ParamSpec,
    PowerShellTemplate,
    PSParamSpec,
)


def _shape_skus(data):
    """Normalise Graph subscribedSkus rows into the report shape."""
    out = []
    for s in data or []:
        prepaid = s.get("prepaidUnits", {}) or {}
        enabled = prepaid.get("enabled", s.get("enabled", 0)) or 0
        consumed = s.get("consumedUnits", s.get("consumed", 0)) or 0
        out.append({
            "skuId": s.get("skuId"),
            "skuPartNumber": s.get("skuPartNumber"),
            "displayName": s.get("displayName") or s.get("skuPartNumber"),
            "enabled": enabled,
            "consumed": consumed,
            "available": enabled - consumed,
            "capabilityStatus": s.get("capabilityStatus"),
        })
    return out


def register(registry: ActionRegistry) -> None:
    registry.register(ActionDefinition(
        id="licenses.skus",
        name="Subscribed SKUs",
        description="Tenant licence SKUs with consumption and availability.",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "graph_powershell", "mock"],
        graph=GraphTemplate(method="GET", path="/subscribedSkus"),
        powershell=PowerShellTemplate(
            module="Microsoft.Graph.Identity.DirectoryManagement",
            command="Get-MgSubscribedSku",
            parameters=[PSParamSpec("All", switch=True)],
        ),
        graph_scopes=["Organization.Read.All"],
        ps_module="Microsoft.Graph.Identity.DirectoryManagement",
        output_schema={
            "skuPartNumber": "SKU", "displayName": "Product", "enabled": "Purchased",
            "consumed": "Assigned", "available": "Available", "capabilityStatus": "Status",
        },
        output_transform=_shape_skus,
        docs_url="https://learn.microsoft.com/graph/api/subscribedsku-list",
    ))

    registry.register(ActionDefinition(
        id="licenses.users_by_sku",
        name="Users by licence",
        description="All users holding a given SKU.",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "mock"],
        params=[ParamSpec("sku_id", required=True, description="SKU id or part number")],
        graph=GraphTemplate(
            method="GET",
            path="/users",
            query={
                "$select": "id,displayName,userPrincipalName,accountEnabled,mail",
                "$filter": "assignedLicenses/any(x:x/skuId eq {sku_id})",
            },
        ),
        graph_scopes=["User.Read.All"],
        output_schema={"displayName": "Name", "userPrincipalName": "UPN",
                       "accountEnabled": "Enabled", "mail": "Mail"},
        docs_url="https://learn.microsoft.com/graph/api/user-list",
    ))

    registry.register(ActionDefinition(
        id="licenses.disabled_with_license",
        name="Disabled users with licences",
        description="Cost/hygiene report: disabled accounts still consuming licences.",
        service="Licensing",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_sdk", "mock"],
        graph=GraphTemplate(
            method="GET",
            path="/users",
            query={
                "$select": "id,displayName,userPrincipalName,accountEnabled,assignedLicenses",
                "$filter": "accountEnabled eq false",
            },
        ),
        graph_scopes=["User.Read.All"],
        output_schema={"displayName": "Name", "userPrincipalName": "UPN", "licences": "Licences"},
        output_transform=lambda data: [
            u for u in (data or []) if u.get("assignedLicenses") or u.get("licences")
        ],
        docs_url="https://learn.microsoft.com/graph/api/user-list",
    ))
