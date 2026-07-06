"""PowerShell command construction: escaping and injection resistance."""

import pytest

from msgraph_tui.core.actions import (
    ActionDefinition,
    PowerShellTemplate,
    PSParamSpec,
)
from msgraph_tui.providers.ps_builder import (
    build_ps_command,
    format_ps_value,
    quote_ps_string,
)


def _action(template: PowerShellTemplate) -> ActionDefinition:
    return ActionDefinition(
        id="t.a", name="t", description="d", service="s",
        preferred_provider="graph_powershell", supported_providers=["graph_powershell"],
        powershell=template,
    )


def test_basic_command_with_switch_select_and_json():
    action = _action(PowerShellTemplate(
        module="Microsoft.Graph.Users",
        command="Get-MgUser",
        parameters=[PSParamSpec("All", switch=True)],
        select=["Id", "DisplayName"],
    ))
    cmd = build_ps_command(action, {})
    assert cmd == "Get-MgUser -All | Select-Object Id, DisplayName | ConvertTo-Json -Depth 5 -Compress"


def test_string_values_single_quoted():
    action = _action(PowerShellTemplate(
        module="m", command="Update-MgUser",
        parameters=[PSParamSpec("UserId", source="user_id")],
    ))
    cmd = build_ps_command(action, {"user_id": "user@contoso.com"})
    assert "-UserId 'user@contoso.com'" in cmd


@pytest.mark.parametrize("hostile", [
    "'; Remove-Item -Recurse C:\\ #",
    "$(Get-Secret)",
    "`whoami`",
    "x'; Disconnect-MgGraph; '",
    'a" | Out-File pwned "',
])
def test_injection_attempts_are_inert(hostile):
    """Hostile values must stay inside a single-quoted literal."""
    action = _action(PowerShellTemplate(
        module="m", command="Get-MgUser",
        parameters=[PSParamSpec("UserId", source="user_id")],
    ))
    cmd = build_ps_command(action, {"user_id": hostile})
    quoted = quote_ps_string(hostile)
    assert f"-UserId {quoted}" in cmd
    # inside single quotes, every original ' is doubled => no way to close the literal
    inner = quoted[1:-1]
    assert "''" in inner or "'" not in hostile


def test_bool_int_and_array_formatting():
    assert format_ps_value(True) == "$true"
    assert format_ps_value(False) == "$false"
    assert format_ps_value(7) == "7"
    assert format_ps_value(["a", "b'c"]) == "@('a', 'b''c')"
    assert format_ps_value(None) == "$null"


def test_illegal_command_or_param_names_rejected():
    bad_cmd = _action(PowerShellTemplate(module="m", command="Get-MgUser; whoami"))
    with pytest.raises(ValueError, match="Illegal"):
        build_ps_command(bad_cmd, {})
    bad_param = _action(PowerShellTemplate(
        module="m", command="Get-MgUser",
        parameters=[PSParamSpec("User Id; rm")],
    ))
    with pytest.raises(ValueError, match="Illegal"):
        build_ps_command(bad_param, {})


def test_whatif_only_when_supported():
    no_whatif = _action(PowerShellTemplate(module="m", command="Get-MgUser"))
    with pytest.raises(ValueError, match="WhatIf"):
        build_ps_command(no_whatif, {}, whatif=True)
    yes = _action(PowerShellTemplate(module="m", command="Update-MgUser", supports_whatif=True))
    assert "-WhatIf" in build_ps_command(yes, {}, whatif=True)


def test_none_valued_params_omitted():
    action = _action(PowerShellTemplate(
        module="m", command="Update-MgUser",
        parameters=[
            PSParamSpec("UserId", source="user_id"),
            PSParamSpec("Department", source="department"),
        ],
    ))
    cmd = build_ps_command(action, {"user_id": "u1"})
    assert "-Department" not in cmd
