"""Provider engine interface and registry.

An engine is one execution backend (Graph REST, PowerShell, Graph SDK, Mock).
A *provider id* is the logical provider an action names (e.g. graph_rest,
graph_powershell, exchange_powershell). One engine may serve several provider
ids — the PowerShell engine serves every *_powershell provider, parameterised
by module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .actions import ActionDefinition
from .envelope import ResultEnvelope

# Canonical provider ids
GRAPH_REST = "graph_rest"
GRAPH_SDK = "graph_sdk"
GRAPH_POWERSHELL = "graph_powershell"
EXCHANGE_POWERSHELL = "exchange_powershell"
TEAMS_POWERSHELL = "teams_powershell"
SHAREPOINT_POWERSHELL = "sharepoint_powershell"
SCC_POWERSHELL = "scc_powershell"
MOCK = "mock"

POWERSHELL_PROVIDERS = {
    GRAPH_POWERSHELL,
    EXCHANGE_POWERSHELL,
    TEAMS_POWERSHELL,
    SHAREPOINT_POWERSHELL,
    SCC_POWERSHELL,
}


@dataclass
class OperationPreview:
    """What exactly would run — shown to the user before execution."""

    provider: str
    summary: str            # one-line, e.g. "GET https://graph.microsoft.com/v1.0/users"
    detail: str             # full redacted request / command text
    required_scopes: list[str] = field(default_factory=list)
    required_roles: list[str] = field(default_factory=list)
    beta: bool = False
    notes: list[str] = field(default_factory=list)


class Provider(ABC):
    """One logical provider backend."""

    #: provider id, e.g. "graph_rest"
    name: str = "abstract"
    #: human display name
    display_name: str = "Abstract provider"

    @abstractmethod
    def is_available(self) -> bool:
        """Can this provider execute right now (module installed, engine on)?"""

    def availability_detail(self) -> str:
        return "available" if self.is_available() else "unavailable"

    def supports(self, action: ActionDefinition) -> bool:
        return self.name in action.supported_providers

    @abstractmethod
    def preview(self, action: ActionDefinition, params: dict[str, Any]) -> OperationPreview:
        """Build the redacted request/command preview without executing."""

    @abstractmethod
    async def execute(self, action: ActionDefinition, params: dict[str, Any]) -> ResultEnvelope:
        """Execute the action and return the common envelope. Must not raise
        for provider-side failures — normalise them into the envelope."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> Provider | None:
        return self._providers.get(name)

    def available(self) -> list[Provider]:
        return [p for p in self._providers.values() if p.is_available()]

    def all(self) -> list[Provider]:
        return list(self._providers.values())
