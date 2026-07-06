"""Action registry validation and parameter coercion."""

import pytest

from msgraph_tui.core.actions import (
    ActionDefinition,
    ActionRegistry,
    AuditRequirement,
    Confirmation,
    GraphTemplate,
    ParamSpec,
    RiskLevel,
    RollbackLevel,
    RollbackSpec,
)
from msgraph_tui.core.errors import RegistryError
from msgraph_tui.modules import build_registry


def _minimal(**overrides) -> ActionDefinition:
    base = dict(
        id="test.action",
        name="Test",
        description="d",
        service="Test",
        preferred_provider="mock",
        supported_providers=["mock"],
        graph=GraphTemplate(method="GET", path="/x"),
    )
    base.update(overrides)
    return ActionDefinition(**base)


def test_duplicate_id_rejected():
    reg = ActionRegistry()
    reg.register(_minimal())
    with pytest.raises(RegistryError, match="Duplicate"):
        reg.register(_minimal())


def test_preferred_must_be_supported():
    with pytest.raises(RegistryError, match="not in supported"):
        ActionRegistry().register(_minimal(preferred_provider="graph_rest"))


def test_write_requires_confirmation_and_change_log():
    with pytest.raises(RegistryError, match="confirmation"):
        ActionRegistry().register(_minimal(risk=RiskLevel.MEDIUM))
    with pytest.raises(RegistryError, match="CHANGE_LOG"):
        ActionRegistry().register(
            _minimal(risk=RiskLevel.MEDIUM, confirmation=Confirmation.CONFIRM)
        )


def test_high_risk_without_rollback_needs_notes():
    with pytest.raises(RegistryError, match="rollback"):
        ActionRegistry().register(_minimal(
            risk=RiskLevel.HIGH,
            confirmation=Confirmation.TYPED,
            audit=AuditRequirement.CHANGE_LOG,
        ))
    # with notes it is accepted
    ActionRegistry().register(_minimal(
        risk=RiskLevel.HIGH,
        confirmation=Confirmation.TYPED,
        audit=AuditRequirement.CHANGE_LOG,
        rollback=RollbackSpec(RollbackLevel.NONE, notes="irreversible by design"),
    ))


def test_action_without_any_template_rejected():
    with pytest.raises(RegistryError, match="template"):
        ActionRegistry().register(_minimal(graph=None))


def test_param_validation_and_coercion():
    action = _minimal(params=[
        ParamSpec("user_id", required=True),
        ParamSpec("top", type="int"),
        ParamSpec("enabled", type="bool"),
        ParamSpec("cols", type="string_array"),
    ])
    out = action.validate_params(
        {"user_id": "u1", "top": "5", "enabled": "true", "cols": "a, b"}
    )
    assert out == {"user_id": "u1", "top": 5, "enabled": True, "cols": ["a", "b"]}
    with pytest.raises(ValueError, match="required"):
        action.validate_params({})
    with pytest.raises(ValueError, match="Unknown parameter"):
        action.validate_params({"user_id": "u1", "bogus": 1})


def test_choice_param_enforced():
    action = _minimal(params=[ParamSpec("fmt", type="choice", choices=["json", "csv"])])
    assert action.validate_params({"fmt": "json"})["fmt"] == "json"
    with pytest.raises(ValueError):
        action.validate_params({"fmt": "xml"})


def test_shipped_registry_is_valid_and_complete():
    reg = build_registry()
    assert len(reg) >= 15
    # every write action carries the full safety contract
    for action in reg.all():
        if action.is_write:
            assert action.confirmation is not Confirmation.NONE
            assert action.audit is AuditRequirement.CHANGE_LOG
            assert action.rollback is not None
        assert "mock" in action.supported_providers  # everything testable offline
