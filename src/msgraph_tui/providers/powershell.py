"""PowerShell engine.

Serves every *_powershell provider id (Graph PS, Exchange Online, Teams,
SharePoint, SCC), parameterised by module. Commands run out-of-process via
``pwsh``/``powershell`` with -NoProfile -NonInteractive and **no shell**, so
the only injection surface is the command text itself — which is built by the
injection-safe ps_builder from registry templates.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import time
from typing import Any

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
    ) -> None:
        self.name = provider_id
        self.display_name = display_name
        self.modules = modules
        self.connect_hint = connect_hint
        self._executable = detect_powershell_executable(executable)
        self._timeout = timeout_seconds
        self._module_cache: dict[str, bool] | None = None

    def is_available(self) -> bool:
        return self._executable is not None

    def availability_detail(self) -> str:
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
            notes.append("Supports -WhatIf (used automatically in dry-run mode).")
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
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError from None
        return proc.returncode or 0, stdout_b.decode("utf-8", "replace"), stderr_b.decode("utf-8", "replace")


def make_powershell_providers(executable: str | None = None) -> list[PowerShellProvider]:
    """Factory for all workload PowerShell providers."""
    return [
        PowerShellProvider(
            "graph_powershell", "Microsoft Graph PowerShell",
            modules=["Microsoft.Graph.Users", "Microsoft.Graph.Groups",
                     "Microsoft.Graph.Identity.DirectoryManagement"],
            connect_hint="Connect-MgGraph -Scopes 'User.Read.All','Group.Read.All'",
            executable=executable,
        ),
        PowerShellProvider(
            "exchange_powershell", "Exchange Online PowerShell",
            modules=["ExchangeOnlineManagement"],
            connect_hint="Connect-ExchangeOnline",
            executable=executable,
        ),
        PowerShellProvider(
            "teams_powershell", "Microsoft Teams PowerShell",
            modules=["MicrosoftTeams"],
            connect_hint="Connect-MicrosoftTeams",
            executable=executable,
        ),
        PowerShellProvider(
            "sharepoint_powershell", "SharePoint Online PowerShell",
            modules=["Microsoft.Online.SharePoint.PowerShell"],
            connect_hint="Connect-SPOService -Url https://<tenant>-admin.sharepoint.com",
            executable=executable,
        ),
    ]
