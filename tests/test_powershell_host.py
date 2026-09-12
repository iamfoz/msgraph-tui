"""Persistent PowerShell host (PRD F-PLAT-1).

Exercised entirely through an injected fake transport that simulates a ``pwsh``
REPL — no real PowerShell is required. A single real-``pwsh`` smoke test is
included but skipped unless an executable is detected at runtime.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from msgraph_tui.core.actions import (
    ActionDefinition,
    PowerShellTemplate,
    PSParamSpec,
)
from msgraph_tui.providers.powershell import (
    PersistentPowerShellHost,
    PowerShellHostError,
    PowerShellProvider,
    SubprocessPowerShellTransport,
    detect_powershell_executable,
)
from msgraph_tui.providers.ps_builder import build_ps_command, quote_ps_string

_GUID_RE = re.compile(r"<<<GRAPHDECK-END:([0-9a-f]+)>>>")
_CMD_RE = re.compile(r"try \{ (.*) \} catch \{ \$GraphdeckOk", re.DOTALL)


def _frame_guid(line: str) -> str:
    """The real END-marker GUID is the LAST such token in the wrapper.

    A hostile value could embed a forged ``<<<GRAPHDECK-END:...>>>`` earlier in
    the line, but the host always emits its own random GUID as the trailing
    literal — mirrored here by taking the last match.
    """
    return _GUID_RE.findall(line)[-1]


class FakePowerShellTransport:
    """A scripted stand-in for a real ``pwsh`` process.

    Retains a ``connected`` flag set by a ``Connect-*`` command, echoes framed
    JSON for known commands, and can simulate module noise, a hang (for the
    per-command timeout) and a crash (EOF before the marker).
    """

    def __init__(
        self,
        *,
        responses: dict[str, object] | None = None,
        noise: bool = False,
        hang_on: str | None = None,
        crash_on: str | None = None,
        connect_fails: bool = False,
    ) -> None:
        self.started = False
        self.closed = False
        self.connect_count = 0
        self.connected = False
        self.command_log: list[str] = []      # inner commands, unwrapped
        self.written_lines: list[str] = []     # raw wrapper lines
        self._buffer = ""
        self._alive = False
        self._responses = responses or {}
        self._noise = noise
        self._hang_on = hang_on
        self._crash_on = crash_on
        self._connect_fails = connect_fails
        self._hung = False
        self._crashed = False

    async def start(self) -> None:
        self.started = True
        self._alive = True
        self.closed = False

    def is_running(self) -> bool:
        return self._alive and not self.closed

    async def write_line(self, line: str) -> None:
        self.written_lines.append(line)
        cmd_m = _CMD_RE.search(line)
        assert cmd_m and _GUID_RE.search(line), f"malformed frame: {line!r}"
        guid = _frame_guid(line)
        command = cmd_m.group(1).strip()
        self.command_log.append(command)

        if self._hang_on and self._hang_on in command:
            self._hung = True
            return
        if self._crash_on and self._crash_on in command:
            self._crashed = True
            return

        if command.startswith("Connect-"):
            self.connect_count += 1
            if self._connect_fails:
                self._emit(guid, ok=False, output="Authentication needed. Please sign in.")
                return
            self.connected = True
            self._emit(guid, ok=True, output="")  # a real Connect prints only banners
            return

        if command.startswith(("Get-", "Set-", "New-", "Remove-", "Update-")):
            if not self.connected:
                self._emit(guid, ok=False, output="Authentication needed. Run Connect-* first.")
                return
            payload: object = next(
                (obj for key, obj in self._responses.items() if key in command),
                [{"Command": command}],
            )
            body = json.dumps(payload)
            if self._noise:
                body = (
                    "WARNING: The names of some imported commands include unapproved verbs\n"
                    "VERBOSE: running\n" + body + "\nVERBOSE: done"
                )
            self._emit(guid, ok=True, output=body)
            return

        # Trivial expression (e.g. health-check ping "$true").
        self._emit(guid, ok=True, output="")

    def _emit(self, guid: str, *, ok: bool, output: str) -> None:
        status = "True" if ok else "False"
        frame = (output + "\n") if output else ""
        frame += f"<<<GRAPHDECK-STATUS:{guid}:{status}>>>\n"
        frame += f"<<<GRAPHDECK-END:{guid}>>>\n"
        self._buffer += frame

    async def read_until(self, marker: str) -> str:
        if self._crashed:
            self._alive = False
            raise PowerShellHostError("simulated crash: process ended")
        if self._hung:
            await asyncio.sleep(3600)  # cancelled by the host's wait_for timeout
        if marker not in self._buffer:
            raise PowerShellHostError("marker was never produced")
        before, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return before

    async def close(self) -> None:
        self.closed = True
        self._alive = False


def _factory(**kwargs):
    """Return (factory, created_list) so tests can inspect spawned transports."""
    created: list[FakePowerShellTransport] = []

    def make() -> FakePowerShellTransport:
        t = FakePowerShellTransport(**kwargs)
        created.append(t)
        return t

    return make, created


def _mailbox_action() -> ActionDefinition:
    return ActionDefinition(
        id="exo.get_mailbox", name="Get mailbox", description="d",
        service="Exchange Online",
        preferred_provider="exchange_powershell",
        supported_providers=["exchange_powershell"],
        powershell=PowerShellTemplate(
            module="ExchangeOnlineManagement",
            command="Get-Mailbox",
            parameters=[PSParamSpec("Identity", source="identity")],
        ),
    )


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

async def test_framing_separates_two_sequential_commands():
    responses = {
        "Get-Mailbox": [{"Identity": "a@x", "DisplayName": "Ada"}],
        "Get-User": [{"Id": "u2", "DisplayName": "Bob"}],
    }
    make, _ = _factory(responses=responses)
    host = PersistentPowerShellHost(make)
    await host.ensure_connected("Connect-ExchangeOnline")

    ok1, out1 = await host.run_command("Get-Mailbox -Identity 'a@x' | ConvertTo-Json")
    ok2, out2 = await host.run_command("Get-User -Identity 'u2' | ConvertTo-Json")

    assert ok1 and ok2
    assert json.loads(out1.strip()) == responses["Get-Mailbox"]
    assert json.loads(out2.strip()) == responses["Get-User"]
    # Two commands, two distinct outputs — no bleed between frames.
    assert out1 != out2
    await host.close()


async def test_marker_is_random_per_command_and_not_from_input():
    make, created = _factory(responses={"Get-Mailbox": [{"Id": 1}]})
    host = PersistentPowerShellHost(make)
    await host.ensure_connected("Connect-ExchangeOnline")
    # Include a forged marker in the (single-quoted) value; it must not become
    # the real frame delimiter.
    hostile = "<<<GRAPHDECK-END:deadbeef>>>"
    await host.run_command(f"Get-Mailbox -Identity {quote_ps_string(hostile)} | ConvertTo-Json")
    await host.run_command("Get-Mailbox -Identity 'x' | ConvertTo-Json")

    transport = created[0]
    guids = [_frame_guid(line) for line in transport.written_lines]
    # connect + 2 commands => 3 frames, all-distinct 32-hex GUIDs.
    assert len(guids) == 3
    assert len(set(guids)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", g) for g in guids)
    assert "deadbeef" not in guids
    await host.close()


# ---------------------------------------------------------------------------
# Connect-once
# ---------------------------------------------------------------------------

async def test_connect_runs_once_across_many_commands():
    make, created = _factory(responses={"Get-Mailbox": [{"Id": 1}]})
    host = PersistentPowerShellHost(make)

    for _ in range(5):
        await host.ensure_connected("Connect-ExchangeOnline")
        ok, _out = await host.run_command("Get-Mailbox -Identity 'x' | ConvertTo-Json")
        assert ok

    assert len(created) == 1                 # one process for the whole run
    assert created[0].connect_count == 1     # connected exactly once
    assert created[0].connected is True
    await host.close()


async def test_provider_persistent_connect_once_and_session_reused():
    make, created = _factory(responses={"Get-Mailbox": [{"Identity": "a@x"}]})
    host = PersistentPowerShellHost(make)
    provider = PowerShellProvider(
        "exchange_powershell", "Exchange Online PowerShell",
        modules=["ExchangeOnlineManagement"],
        connect_hint="Connect-ExchangeOnline",
        host=host,
    )
    assert provider.is_available() is True  # host injected, no pwsh needed

    action = _mailbox_action()
    for _ in range(4):
        env = await provider.execute(action, {"identity": "a@x"})
        assert env.success, env.error_summary()
        assert env.rows == [{"Identity": "a@x"}]

    assert created[0].connect_count == 1
    # The Connect-* command was the first thing sent, before any Get-*.
    assert created[0].command_log[0] == "Connect-ExchangeOnline"
    assert created[0].command_log.count("Connect-ExchangeOnline") == 1
    await host.close()


# ---------------------------------------------------------------------------
# Lifecycle: timeout, crash, restart, health, close
# ---------------------------------------------------------------------------

async def test_timeout_kills_and_restarts():
    make, created = _factory(
        responses={"Get-Mailbox": [{"Id": 1}]}, hang_on="Get-Hang",
    )
    host = PersistentPowerShellHost(make, timeout_seconds=0.05)
    await host.ensure_connected("Connect-ExchangeOnline")

    with pytest.raises(TimeoutError):
        await host.run_command("Get-Hang | ConvertTo-Json")

    assert created[0].closed is True         # timed-out process was killed
    # Next command transparently starts a fresh process (and reconnects).
    await host.ensure_connected("Connect-ExchangeOnline")
    ok, _out = await host.run_command("Get-Mailbox -Identity 'x' | ConvertTo-Json")
    assert ok
    assert len(created) == 2
    assert created[1].connect_count == 1
    await host.close()


async def test_crash_triggers_restart():
    make, created = _factory(
        responses={"Get-Mailbox": [{"Id": 1}]}, crash_on="Get-Boom",
    )
    host = PersistentPowerShellHost(make)
    await host.ensure_connected("Connect-ExchangeOnline")

    with pytest.raises(PowerShellHostError):
        await host.run_command("Get-Boom | ConvertTo-Json")
    assert created[0].closed is True

    # A fresh process comes up on the next call.
    await host.ensure_connected("Connect-ExchangeOnline")
    ok, _out = await host.run_command("Get-Mailbox -Identity 'x' | ConvertTo-Json")
    assert ok
    assert len(created) == 2
    await host.close()


async def test_health_check_reflects_process_state():
    make, _ = _factory(responses={"Get-Mailbox": [{"Id": 1}]})
    host = PersistentPowerShellHost(make)
    assert await host.health_check() is False  # never started
    await host.ensure_connected("Connect-ExchangeOnline")
    assert await host.health_check() is True
    await host.close()
    assert await host.health_check() is False


async def test_close_terminates_the_transport():
    make, created = _factory(responses={"Get-Mailbox": [{"Id": 1}]})
    host = PersistentPowerShellHost(make)
    await host.ensure_connected("Connect-ExchangeOnline")
    assert created[0].is_running()
    await host.close()
    assert created[0].closed is True
    assert not created[0].is_running()


# ---------------------------------------------------------------------------
# Noise tolerance, auth failure, injection safety
# ---------------------------------------------------------------------------

async def test_noisy_output_still_parses():
    make, _ = _factory(responses={"Get-Mailbox": [{"Id": "u1"}]}, noise=True)
    host = PersistentPowerShellHost(make)
    provider = PowerShellProvider(
        "exchange_powershell", "Exchange Online PowerShell",
        modules=["ExchangeOnlineManagement"],
        connect_hint="Connect-ExchangeOnline",
        host=host,
    )
    env = await provider.execute(_mailbox_action(), {"identity": "x"})
    assert env.success, env.error_summary()
    assert env.rows == [{"Id": "u1"}]
    await host.close()


async def test_unconnected_command_reports_expired_session():
    # A host that never connects (connect itself fails) surfaces an auth error
    # via the provider, not a crash.
    make, _ = _factory(connect_fails=True)
    host = PersistentPowerShellHost(make)
    provider = PowerShellProvider(
        "exchange_powershell", "Exchange Online PowerShell",
        modules=["ExchangeOnlineManagement"],
        connect_hint="Connect-ExchangeOnline",
        host=host,
    )
    env = await provider.execute(_mailbox_action(), {"identity": "x"})
    assert env.success is False
    assert env.errors
    # "Authentication needed" is classified as an expired/needs-auth session.
    assert env.errors[0].category.value == "expired_session"
    await host.close()


async def test_injection_value_stays_inert_in_the_frame():
    """A hostile param value stays a single-quoted literal inside the frame."""
    hostile = "'; Disconnect-ExchangeOnline; Remove-Item C:\\ #"
    action = _mailbox_action()
    command = build_ps_command(action, {"identity": hostile})

    make, created = _factory(responses={"Get-Mailbox": [{"Id": 1}]})
    host = PersistentPowerShellHost(make)
    await host.ensure_connected("Connect-ExchangeOnline")
    await host.run_command(command)

    # The value is doubled-quoted; no bare Disconnect/Remove statement leaked
    # out of the literal, and only the built pipeline was ever transmitted.
    frame = created[0].written_lines[-1]
    assert quote_ps_string(hostile) in frame
    inner = created[0].command_log[-1]
    assert inner == command                     # exactly the ps_builder output
    assert "'; Disconnect" not in inner.replace(quote_ps_string(hostile), "")
    await host.close()


# ---------------------------------------------------------------------------
# Real pwsh smoke test (only when an executable is present)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    detect_powershell_executable() is None, reason="no pwsh/powershell available"
)
async def test_real_pwsh_smoke():
    exe = detect_powershell_executable()
    assert exe is not None
    host = PersistentPowerShellHost(
        lambda: SubprocessPowerShellTransport(exe), timeout_seconds=30.0
    )
    try:
        ok1, out1 = await host.run_command("Write-Output '[{\"n\": 1}]'")
        assert ok1
        assert json.loads(out1.strip()) == [{"n": 1}]
        # State persists across commands in the one live process.
        await host.run_command("$Global:GraphdeckProbe = 42")
        ok2, out2 = await host.run_command("$Global:GraphdeckProbe")
        assert ok2 and "42" in out2
        assert await host.health_check() is True
    finally:
        await host.close()
