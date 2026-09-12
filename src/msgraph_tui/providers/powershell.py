"""PowerShell engine.

Serves every *_powershell provider id (Graph PS, Exchange Online, Teams,
SharePoint, SCC), parameterised by module. Commands run out-of-process via
``pwsh``/``powershell`` with -NoProfile -NonInteractive and **no shell**, so
the only injection surface is the command text itself — which is built by the
injection-safe ps_builder from registry templates.

Two execution models coexist:

* **One-shot** (``PowerShellProvider._run_script``) — a fresh ``pwsh`` process
  per command. Used by ``graph_powershell`` (Graph PS caches its own token, so
  a fresh process still finds the session) and as the fallback everywhere.

* **Persistent** (``PersistentPowerShellHost``) — one long-lived ``pwsh``
  process driven over a framed stdin/stdout protocol, so a stateful
  ``Connect-*`` (Exchange/Teams/SharePoint/SCC) runs *once* and the
  authenticated runspace is reused by every later command. This is the
  platform prerequisite (PRD F-PLAT-1) for the workload modules. It is
  **opt-in** (a host must be injected) so unit tests never spawn a process.

The persistent host talks to the process through a small
:class:`PowerShellTransport` seam, so tests inject a fake REPL and no real
``pwsh`` is required.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from ..core.actions import ActionDefinition
from ..core.envelope import ResultEnvelope, failure, utc_now_iso
from ..core.errors import ErrorCategory, NormalizedError, with_guidance
from ..core.providers import OperationPreview, Provider
from ..core.redaction import redact, redact_text
from .ps_builder import PSParseError, build_ps_command, classify_ps_stderr, parse_ps_json_output

_STDERR_CATEGORY = {
    "missing_module": ErrorCategory.PROVIDER_UNAVAILABLE,
    "expired_session": ErrorCategory.EXPIRED_SESSION,
    "permission": ErrorCategory.PERMISSION,
    "throttled": ErrorCategory.THROTTLED,
}


def detect_powershell_executable(configured: str | None = None) -> str | None:
    """Prefer PowerShell 7 (pwsh); fall back to Windows PowerShell on Windows."""
    if configured:
        return configured if shutil.which(configured) else None
    for candidate in ("pwsh", "powershell") if sys.platform == "win32" else ("pwsh",):
        if shutil.which(candidate):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Persistent host: framed protocol
# ---------------------------------------------------------------------------
#
# Each command sent to the long-lived process is wrapped so that the process
# emits, on its stdout, a STATUS marker (carrying the success flag) followed by
# an END marker. Both markers embed a per-command random GUID so a command's
# output can never be confused with the next one's, and so the marker can never
# be forged from user-controlled data (the GUID is uuid4, never derived from
# input). The host reads stdout until it sees the END marker.

_STATUS_PREFIX = "<<<GRAPHDECK-STATUS:"
_END_PREFIX = "<<<GRAPHDECK-END:"


class PowerShellHostError(RuntimeError):
    """The persistent PowerShell process crashed, ended, or misbehaved.

    Distinct from :class:`TimeoutError` (a per-command timeout). Both cause the
    host to kill and drop the process so the next command starts a fresh one.
    """


class PowerShellTransport(Protocol):
    """The seam between the host and an actual OS process.

    Implemented by :class:`SubprocessPowerShellTransport` in production and by a
    fake REPL in tests, so the host is fully exercisable without ``pwsh``.
    """

    async def start(self) -> None:
        """Spawn (or otherwise ready) the process."""
        ...

    def is_running(self) -> bool:
        """True while the process is alive and usable."""
        ...

    async def write_line(self, line: str) -> None:
        """Write one line (a newline is appended) to the process stdin."""
        ...

    async def read_until(self, marker: str) -> str:
        """Read stdout until ``marker`` is seen; return everything before it.

        Must raise :class:`PowerShellHostError` if the process ends (EOF)
        before the marker arrives — that is how a crash is detected.
        """
        ...

    async def close(self) -> None:
        """Kill/terminate the process and release its handles. Idempotent."""
        ...


class SubprocessPowerShellTransport:
    """A real ``pwsh``/``powershell`` process, driven over stdin/stdout.

    Started with ``-NoProfile -NonInteractive -NoLogo`` and **no shell**
    (``create_subprocess_exec``). stderr is merged into stdout so module banners
    and error records ride the same stream we already frame and parse (noise is
    tolerated by :func:`parse_ps_json_output`).
    """

    def __init__(self, executable: str) -> None:
        self._executable = executable
        self._proc: asyncio.subprocess.Process | None = None
        self._buffer = ""

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self._executable,
            "-NoProfile",
            "-NonInteractive",
            "-NoLogo",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._buffer = ""

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def write_line(self, line: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise PowerShellHostError("PowerShell process is not started")
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def read_until(self, marker: str) -> str:
        if self._proc is None or self._proc.stdout is None:
            raise PowerShellHostError("PowerShell process is not started")
        while marker not in self._buffer:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                raise PowerShellHostError(
                    "PowerShell process ended before the command completed"
                )
            self._buffer += chunk.decode("utf-8", "replace")
        before, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return before

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self._buffer = ""
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass


def _wrap_command(command: str, guid: str) -> str:
    """Frame one command so the process emits STATUS + END markers after it.

    ``command`` is trusted, pre-built pipeline text (from ps_builder, or a
    provider's fixed ``connect_hint`` constant) — never raw user input. The
    GUID is random per call, so the emitted markers cannot be spoofed by data
    the command prints.
    """
    return (
        "$GraphdeckOk=$true; "
        f"try {{ {command} }} catch {{ $GraphdeckOk=$false; $_.Exception.Message }}; "
        f'"{_STATUS_PREFIX}{guid}:$GraphdeckOk>>>"; '
        f'"{_END_PREFIX}{guid}>>>"'
    )


def _parse_frame(text_before_end: str, guid: str) -> tuple[bool, str]:
    """Split framed output into ``(ok, command_output)``.

    ``text_before_end`` is everything the process printed before the END marker
    (module noise, the command's own output, then the STATUS marker line). We
    locate the STATUS marker for this GUID; everything before it is the command
    output handed to :func:`parse_ps_json_output`.
    """
    status_token = f"{_STATUS_PREFIX}{guid}:"
    idx = text_before_end.rfind(status_token)
    if idx == -1:
        # No status marker seen (shouldn't happen); treat all of it as output.
        return True, text_before_end
    output = text_before_end[:idx]
    tail = text_before_end[idx + len(status_token):]
    status = tail.split(">>>", 1)[0].strip()
    return status == "True", output


class PersistentPowerShellHost:
    """One long-lived PowerShell process shared by the workload providers.

    A single runspace can hold several independent module connections
    (``Connect-ExchangeOnline`` *and* ``Connect-MicrosoftTeams`` *and* ...), so
    the workload providers share one host and each runs its own ``connect_hint``
    exactly once (:meth:`ensure_connected` de-duplicates per connect command for
    the life of the process). If the process is killed on timeout or crashes,
    the connected set is cleared and the next call transparently starts a fresh
    process (and each provider re-connects on its next action).

    All access is serialised by an :class:`asyncio.Lock`: the framed protocol is
    a single shared stdin/stdout pair, so commands must not interleave.
    """

    def __init__(
        self,
        transport_factory: Callable[[], PowerShellTransport],
        *,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._transport_factory = transport_factory
        self._timeout = timeout_seconds
        self._transport: PowerShellTransport | None = None
        self._connected: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    async def ensure_connected(self, connect_command: str) -> None:
        """Run ``connect_command`` once per process lifetime (lazy).

        ``connect_command`` is a trusted provider constant (e.g. the
        ``connect_hint``), not user input. Raises :class:`PowerShellHostError`
        if the connect fails, without caching it, so a later action retries.
        """
        async with self._lock:
            await self._ensure_started_locked()
            if connect_command in self._connected:
                return
            ok, output = await self._run_locked(connect_command)
            if not ok:
                raise PowerShellHostError(
                    f"connect failed: {output.strip()[:200] or connect_command}"
                )
            self._connected.add(connect_command)

    async def run_command(self, command: str) -> tuple[bool, str]:
        """Run one framed command; return ``(ok, raw_command_output)``.

        Lazily (re)starts the process. On timeout raises :class:`TimeoutError`;
        on process crash raises :class:`PowerShellHostError`. In both cases the
        process is killed and dropped so the next call starts fresh.
        """
        async with self._lock:
            await self._ensure_started_locked()
            return await self._run_locked(command)

    async def health_check(self) -> bool:
        """True if the process is alive and answers a trivial ping.

        Never starts a process; a host that has never run returns False.
        """
        async with self._lock:
            if self._transport is None or not self._transport.is_running():
                return False
            try:
                ok, _ = await self._run_locked("$true")
            except (TimeoutError, PowerShellHostError):
                await self._reset_locked()
                return False
            return ok

    async def close(self) -> None:
        """Terminate the process and reset state. Idempotent."""
        async with self._lock:
            await self._reset_locked()

    # -- internals (lock held) ---------------------------------------------

    async def _ensure_started_locked(self) -> None:
        if self._transport is not None and self._transport.is_running():
            return
        if self._transport is not None:
            await self._reset_locked()
        transport = self._transport_factory()
        await transport.start()
        self._transport = transport
        self._connected.clear()

    async def _run_locked(self, command: str) -> tuple[bool, str]:
        transport = self._transport
        assert transport is not None  # _ensure_started_locked ran first
        guid = uuid.uuid4().hex
        end_marker = f"{_END_PREFIX}{guid}>>>"
        wrapper = _wrap_command(command, guid)
        try:
            await transport.write_line(wrapper)
            raw = await asyncio.wait_for(transport.read_until(end_marker), self._timeout)
        except (TimeoutError, PowerShellHostError, asyncio.CancelledError):
            await self._reset_locked()
            raise
        return _parse_frame(raw, guid)

    async def _reset_locked(self) -> None:
        transport, self._transport = self._transport, None
        self._connected.clear()
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass


class PowerShellProvider(Provider):
    def __init__(
        self,
        provider_id: str,
        display_name: str,
        *,
        modules: list[str],
        connect_hint: str,
        executable: str | None = None,
        timeout_seconds: float = 120.0,
        host: PersistentPowerShellHost | None = None,
    ) -> None:
        self.name = provider_id
        self.display_name = display_name
        self.modules = modules
        self.connect_hint = connect_hint
        self._executable = detect_powershell_executable(executable)
        self._timeout = timeout_seconds
        self._module_cache: dict[str, bool] | None = None
        # When a persistent host is injected, execute() runs the stateful
        # long-lived path (connect-once + reuse); otherwise the one-shot path.
        # Opt-in: nothing injects a host unless explicitly asked, so unit tests
        # never spawn a process.
        self._host = host

    def is_available(self) -> bool:
        return self._host is not None or self._executable is not None

    def availability_detail(self) -> str:
        if self._host is not None and self._executable is None:
            return "ready (persistent PowerShell host; connect-once session reuse)"
        if self._executable is None:
            return (
                "PowerShell not found. Install PowerShell 7 (pwsh) — "
                "https://learn.microsoft.com/powershell/scripting/install/installing-powershell"
            )
        missing = self.missing_modules()
        if missing is None:
            return f"using {self._executable} (module availability not yet checked)"
        if missing:
            return f"missing modules: {', '.join(missing)} — {self.install_guidance()}"
        return f"ready ({self._executable}; modules: {', '.join(self.modules)})"

    def install_guidance(self) -> str:
        """Guidance only — Graphdeck never installs modules without approval."""
        return "; ".join(
            f"Install-Module {m} -Scope CurrentUser" for m in (self.missing_modules() or [])
        )

    def missing_modules(self) -> list[str] | None:
        """None = not checked yet (check is an explicit, user-visible step)."""
        if self._module_cache is None:
            return None
        return [m for m, present in self._module_cache.items() if not present]

    async def check_modules(self) -> dict[str, bool]:
        if self._executable is None:
            self._module_cache = {m: False for m in self.modules}
            return self._module_cache
        names = ", ".join(f"'{m}'" for m in self.modules)
        script = (
            f"Get-Module -ListAvailable -Name {names} | "
            "Select-Object -ExpandProperty Name -Unique | ConvertTo-Json -Compress"
        )
        code, stdout, _stderr = await self._run_script(script)
        found: set[str] = set()
        if code == 0 and stdout.strip():
            try:
                value = parse_ps_json_output(stdout)
                found = {value} if isinstance(value, str) else set(value or [])
            except PSParseError:
                pass
        self._module_cache = {m: m in found for m in self.modules}
        return self._module_cache

    def preview(self, action: ActionDefinition, params: dict[str, Any]) -> OperationPreview:
        command = build_ps_command(action, params)
        notes = [f"Runs via {self._executable or 'pwsh'} -NoProfile -NonInteractive"]
        if action.ps_module:
            notes.append(f"Requires module: {action.ps_module}")
        if action.powershell and action.powershell.supports_whatif:
            notes.append("Supports -WhatIf (declared; dry-run mode uses the mock engine).")
        return OperationPreview(
            provider=self.name,
            summary=command.split(" | ")[0],
            detail=redact_text(command),
            required_scopes=action.graph_scopes,
            required_roles=action.admin_roles,
            notes=notes + [f"If disconnected: {self.connect_hint}"],
        )

    async def execute(self, action: ActionDefinition, params: dict[str, Any]) -> ResultEnvelope:
        started = time.monotonic()
        try:
            command = build_ps_command(action, params)
        except ValueError as exc:
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(ErrorCategory.INVALID_INPUT, str(exc))),
            )
        preview_text = redact_text(command)
        if self._host is not None:
            return await self._execute_persistent(action, command, preview_text, started)
        if self._executable is None:
            return failure(
                self.name, action.id,
                with_guidance(NormalizedError(
                    ErrorCategory.PROVIDER_UNAVAILABLE,
                    "No PowerShell executable found (install PowerShell 7 / pwsh)",
                )),
                request_preview=preview_text,
            )

        envelope = ResultEnvelope(
            success=False,
            provider=self.name,
            action_id=action.id,
            request_preview=preview_text,
            started_at=utc_now_iso(),
        )
        try:
            code, stdout, stderr = await self._run_script(command)
        except TimeoutError:
            envelope.errors.append(
                with_guidance(NormalizedError(
                    ErrorCategory.NETWORK,
                    f"PowerShell command timed out after {self._timeout}s",
                    retriable=True,
                ))
            )
            envelope.duration_ms = (time.monotonic() - started) * 1000
            return envelope

        envelope.duration_ms = (time.monotonic() - started) * 1000
        stderr_redacted = redact_text(stderr.strip())
        if code != 0 or (not stdout.strip() and stderr.strip()):
            kind = classify_ps_stderr(stderr)
            category = _STDERR_CATEGORY.get(kind or "", ErrorCategory.UNKNOWN)
            err = NormalizedError(
                category=category,
                message=(stderr_redacted.splitlines() or [f"PowerShell exited with code {code}"])[0][:300],
                provider_code=kind,
                retriable=category is ErrorCategory.THROTTLED,
                detail=stderr_redacted[:2000] or None,
            )
            if kind == "missing_module":
                err.guidance = f"Module missing. Suggested (requires your approval): {self.install_guidance() or 'Install-Module ' + (action.ps_module or '')}"
            elif kind == "expired_session":
                err.guidance = f"Reconnect first: {self.connect_hint}"
            envelope.errors.append(with_guidance(err))
            return envelope

        if stderr_redacted:
            envelope.warnings.append(stderr_redacted[:500])
        try:
            data = parse_ps_json_output(stdout)
        except PSParseError as exc:
            envelope.errors.append(
                with_guidance(NormalizedError(ErrorCategory.PARSE, str(exc)))
            )
            envelope.raw = redact_text(stdout[:5000])
            return envelope
        if isinstance(data, dict):
            data = [data] if action.view.value == "table" else data
        if action.output_transform:
            data = action.output_transform(data)
        envelope.success = True
        envelope.data = data
        envelope.raw = redact(parse_ps_json_output(stdout)) if stdout.strip() else None
        return envelope

    async def _execute_persistent(
        self,
        action: ActionDefinition,
        command: str,
        preview_text: str,
        started: float,
    ) -> ResultEnvelope:
        """Run one action through the shared persistent host.

        Connect-once: the provider's ``connect_hint`` runs a single time per
        process lifetime (before the first action), then every command reuses
        the authenticated runspace. stderr is merged into stdout by the
        transport, so the framed output carries any error text too.
        """
        assert self._host is not None
        envelope = ResultEnvelope(
            success=False,
            provider=self.name,
            action_id=action.id,
            request_preview=preview_text,
            started_at=utc_now_iso(),
        )
        try:
            if self.connect_hint:
                await self._host.ensure_connected(self.connect_hint)
            ok, stdout = await self._host.run_command(command)
        except TimeoutError:
            envelope.errors.append(
                with_guidance(NormalizedError(
                    ErrorCategory.NETWORK,
                    f"PowerShell command timed out after {self._host.timeout_seconds}s "
                    "(persistent host restarted)",
                    retriable=True,
                ))
            )
            envelope.duration_ms = (time.monotonic() - started) * 1000
            return envelope
        except PowerShellHostError as exc:
            text = str(exc)
            kind = classify_ps_stderr(text)
            category = _STDERR_CATEGORY.get(kind or "", ErrorCategory.PROVIDER_UNAVAILABLE)
            err = NormalizedError(
                category=category,
                message=redact_text(text)[:300],
                provider_code=kind,
                retriable=True,
            )
            if kind == "expired_session":
                err.guidance = f"Reconnect first: {self.connect_hint}"
            envelope.errors.append(with_guidance(err))
            envelope.duration_ms = (time.monotonic() - started) * 1000
            return envelope

        envelope.duration_ms = (time.monotonic() - started) * 1000
        stdout_redacted = redact_text(stdout.strip())
        if not ok:
            kind = classify_ps_stderr(stdout)
            category = _STDERR_CATEGORY.get(kind or "", ErrorCategory.UNKNOWN)
            err = NormalizedError(
                category=category,
                message=(stdout_redacted.splitlines() or ["PowerShell command failed"])[0][:300],
                provider_code=kind,
                retriable=category is ErrorCategory.THROTTLED,
                detail=stdout_redacted[:2000] or None,
            )
            if kind == "missing_module":
                err.guidance = f"Module missing. Suggested (requires your approval): {self.install_guidance() or 'Install-Module ' + (action.ps_module or '')}"
            elif kind == "expired_session":
                err.guidance = f"Reconnect first: {self.connect_hint}"
            envelope.errors.append(with_guidance(err))
            return envelope

        try:
            data = parse_ps_json_output(stdout)
        except PSParseError as exc:
            envelope.errors.append(
                with_guidance(NormalizedError(ErrorCategory.PARSE, str(exc)))
            )
            envelope.raw = redact_text(stdout[:5000])
            return envelope
        if isinstance(data, dict):
            data = [data] if action.view.value == "table" else data
        if action.output_transform:
            data = action.output_transform(data)
        envelope.success = True
        envelope.data = data
        envelope.raw = redact(parse_ps_json_output(stdout)) if stdout.strip() else None
        return envelope

    async def _run_script(self, script: str) -> tuple[int, str, str]:
        assert self._executable is not None
        proc = await asyncio.create_subprocess_exec(
            self._executable,
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat", "Text",
            "-Command", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            raise TimeoutError from None
        return proc.returncode or 0, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace")


def make_powershell_providers(
    executable: str | None = None,
    *,
    persistent: bool = False,
    timeout_seconds: float = 120.0,
) -> list[PowerShellProvider]:
    """Factory for all workload PowerShell providers.

    ``persistent`` is **opt-in** (default off, so existing callers and unit
    tests are unaffected). When enabled *and* a ``pwsh``/``powershell`` is
    detected, the stateful workload providers (Exchange, Teams, SharePoint,
    SCC) share ONE :class:`PersistentPowerShellHost`: a single runspace holds
    all their independent ``Connect-*`` sessions, each run once on first use.
    ``graph_powershell`` deliberately stays on the one-shot ``_run_script``
    path (Graph PS caches its own token, so a fresh process still finds the
    session) so persistent-mode bugs cannot regress it.
    """
    resolved = detect_powershell_executable(executable)
    host: PersistentPowerShellHost | None = None
    if persistent and resolved is not None:
        exe = resolved
        host = PersistentPowerShellHost(
            lambda: SubprocessPowerShellTransport(exe),
            timeout_seconds=timeout_seconds,
        )
    return [
        PowerShellProvider(
            "graph_powershell", "Microsoft Graph PowerShell",
            modules=["Microsoft.Graph.Users", "Microsoft.Graph.Groups",
                     "Microsoft.Graph.Identity.DirectoryManagement"],
            connect_hint="Connect-MgGraph -Scopes 'User.Read.All','Group.Read.All'",
            executable=executable,
            timeout_seconds=timeout_seconds,
        ),
        PowerShellProvider(
            "exchange_powershell", "Exchange Online PowerShell",
            modules=["ExchangeOnlineManagement"],
            connect_hint="Connect-ExchangeOnline",
            executable=executable,
            timeout_seconds=timeout_seconds,
            host=host,
        ),
        PowerShellProvider(
            "teams_powershell", "Microsoft Teams PowerShell",
            modules=["MicrosoftTeams"],
            connect_hint="Connect-MicrosoftTeams",
            executable=executable,
            timeout_seconds=timeout_seconds,
            host=host,
        ),
        PowerShellProvider(
            "sharepoint_powershell", "SharePoint Online PowerShell",
            modules=["Microsoft.Online.SharePoint.PowerShell"],
            connect_hint="Connect-SPOService -Url https://<tenant>-admin.sharepoint.com",
            executable=executable,
            timeout_seconds=timeout_seconds,
            host=host,
        ),
        PowerShellProvider(
            "scc_powershell", "Security & Compliance PowerShell",
            modules=["ExchangeOnlineManagement"],  # IPPSSession ships in the EXO module
            connect_hint="Connect-IPPSSession",
            executable=executable,
            timeout_seconds=timeout_seconds,
            host=host,
        ),
    ]
