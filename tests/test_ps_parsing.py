"""PowerShell output parsing: JSON extraction from noisy real-world output."""

import pytest

from msgraph_tui.providers.ps_builder import (
    PSParseError,
    classify_ps_stderr,
    parse_ps_json_output,
)


def test_clean_json_array():
    assert parse_ps_json_output('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]


def test_clean_json_object():
    assert parse_ps_json_output('{"Id": "u1"}') == {"Id": "u1"}


def test_warning_lines_before_json():
    noisy = (
        "WARNING: The names of some imported commands include unapproved verbs\n"
        "Welcome to Microsoft Graph!\n"
        '[{"Id": "u1", "DisplayName": "Ada"}]'
    )
    assert parse_ps_json_output(noisy) == [{"Id": "u1", "DisplayName": "Ada"}]


def test_trailing_noise_after_json():
    noisy = '{"Id": "u1"}\nVERBOSE: done'
    assert parse_ps_json_output(noisy) == {"Id": "u1"}


def test_braces_in_warning_do_not_confuse_parser():
    noisy = 'WARNING: use {braces} carefully\n{"ok": true}'
    assert parse_ps_json_output(noisy) == {"ok": True}


def test_empty_output_is_none():
    assert parse_ps_json_output("") is None
    assert parse_ps_json_output("   \n  ") is None


def test_non_json_output_raises():
    with pytest.raises(PSParseError):
        parse_ps_json_output("Get-MgUser : One or more errors occurred.")


def test_stderr_classification():
    assert classify_ps_stderr(
        "The term 'Get-MgUser' is not recognized as the name of a cmdlet"
    ) == "missing_module"
    assert classify_ps_stderr("Authentication needed. Please call Connect-MgGraph.") == "expired_session"
    assert classify_ps_stderr("Access is denied") == "permission"
    assert classify_ps_stderr("The request was throttled") == "throttled"
    assert classify_ps_stderr("something else entirely") is None
