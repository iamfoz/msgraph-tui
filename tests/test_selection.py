"""Provider selection: preferences, beta gating, availability fallback."""

import pytest

from msgraph_tui.core.actions import ActionDefinition, GraphTemplate
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.core.errors import SelectionError
from msgraph_tui.core.providers import OperationPreview, Provider, ProviderRegistry
from msgraph_tui.core.selection import select_provider


class FakeProvider(Provider):
    def __init__(self, name: str, available: bool = True) -> None:
        self.name = name
        self.display_name = name
        self._available = available

    def is_available(self) -> bool:
        return self._available

    def preview(self, action, params):
        return OperationPreview(self.name, "x", "x")

    async def execute(self, action, params):
        raise NotImplementedError


def _action(**overrides) -> ActionDefinition:
    base = dict(
        id="t.a", name="t", description="d", service="Entra ID",
        preferred_provider="graph_rest",
        supported_providers=["graph_rest", "graph_powershell", "mock"],
        graph=GraphTemplate(method="GET", path="/x"),
    )
    base.update(overrides)
    return ActionDefinition(**base)


def _registry(*providers: Provider) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    return reg


def test_mock_mode_forces_mock():
    cfg = AppConfig(mode=Mode.MOCK)
    reg = _registry(FakeProvider("mock"), FakeProvider("graph_rest"))
    result = select_provider(_action(), cfg, reg)
    assert result.provider.name == "mock"
    assert any("mock" in r for r in result.reasons)


def test_live_mode_uses_preferred_when_available():
    cfg = AppConfig(mode=Mode.LIVE)
    reg = _registry(FakeProvider("graph_rest"), FakeProvider("graph_powershell"))
    result = select_provider(_action(), cfg, reg)
    assert result.provider.name == "graph_rest"


def test_falls_back_when_preferred_unavailable():
    cfg = AppConfig(mode=Mode.LIVE)
    reg = _registry(FakeProvider("graph_rest", available=False), FakeProvider("graph_powershell"))
    result = select_provider(_action(), cfg, reg)
    assert result.provider.name == "graph_powershell"
    assert any("graph_rest" in s for s in result.skipped)


def test_user_preference_wins():
    cfg = AppConfig(mode=Mode.LIVE, provider_preferences={"Entra ID": "graph_powershell"})
    reg = _registry(FakeProvider("graph_rest"), FakeProvider("graph_powershell"))
    result = select_provider(_action(), cfg, reg)
    assert result.provider.name == "graph_powershell"
    assert any("user preference" in r for r in result.reasons)


def test_per_action_preference_beats_service_preference():
    cfg = AppConfig(mode=Mode.LIVE, provider_preferences={
        "Entra ID": "graph_powershell", "t.a": "graph_rest",
    })
    reg = _registry(FakeProvider("graph_rest"), FakeProvider("graph_powershell"))
    assert select_provider(_action(), cfg, reg).provider.name == "graph_rest"


def test_beta_requiring_action_blocked_unless_enabled():
    action = _action(beta_required=True)
    reg = _registry(FakeProvider("graph_rest"), FakeProvider("graph_powershell"))
    cfg = AppConfig(mode=Mode.LIVE, allow_beta=False)
    result = select_provider(action, cfg, reg)
    assert result.provider.name == "graph_powershell"  # graph_rest skipped over beta
    cfg_beta = AppConfig(mode=Mode.LIVE, allow_beta=True)
    assert select_provider(action, cfg_beta, reg).provider.name == "graph_rest"


def test_no_provider_available_raises_with_reasons():
    cfg = AppConfig(mode=Mode.LIVE)
    reg = _registry(FakeProvider("graph_rest", available=False))
    with pytest.raises(SelectionError, match="graph_rest"):
        select_provider(_action(supported_providers=["graph_rest"]), cfg, reg)
