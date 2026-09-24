"""Read-only tenant smoke test (services/smoke.py)."""

from __future__ import annotations

import io
import json

import pytest

from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services import smoke
from msgraph_tui.services.context import build_context


def _cfg(tmp_path) -> AppConfig:
    return AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")


async def test_all_read_actions_ok_or_skipped_in_mock(tmp_path):
    ctx = build_context(_cfg(tmp_path))
    report = await smoke.run_smoke(ctx)

    assert report.failed == 0
    assert report.ok + report.skipped == len(report.results)
    # Every action in the registry (read and write) is accounted for; writes
    # are always reported "skipped", never executed.
    all_ids = {a.id for a in ctx.actions.all()}
    assert {r.action_id for r in report.results} == all_ids
    for r in report.results:
        assert r.status in ("ok", "skipped")
    read_ids = {a.id for a in ctx.actions.all() if not a.is_write}
    by_id = {r.action_id: r for r in report.results}
    for action_id in read_ids:
        assert by_id[action_id].status == "ok", by_id[action_id].detail


async def test_derived_param_actions_actually_ran(tmp_path):
    ctx = build_context(_cfg(tmp_path))
    report = await smoke.run_smoke(ctx)
    by_id = {r.action_id: r for r in report.results}

    for action_id in (
        "users.get",
        "users.licenses",
        "users.memberships",
        "groups.get",
        "groups.members",
        "groups.owners",
        "licenses.users_by_sku",
        "exchange.mailbox.get",
        "exchange.mailbox_permissions",
        "exchange.inbox_rules",
        "teams.user_policies.get",
        "sharepoint.site.get",
    ):
        result = by_id[action_id]
        assert result.status == "ok", f"{action_id}: {result.detail}"
        assert result.provider == "mock"


async def test_service_filter(tmp_path):
    ctx = build_context(_cfg(tmp_path))
    report = await smoke.run_smoke(ctx, services=["Licensing"])

    assert report.results
    for r in report.results:
        assert r.service == "Licensing"
    assert {r.action_id for r in report.results} == {
        a.id for a in ctx.actions.all() if a.service == "Licensing"
    }
    # licensing has no other-service actions leaking in, and read actions ran
    by_id = {r.action_id: r for r in report.results}
    assert by_id["licenses.skus"].status == "ok"


async def test_service_filter_is_case_insensitive(tmp_path):
    ctx = build_context(_cfg(tmp_path))
    report = await smoke.run_smoke(ctx, services=["licensing"])
    assert report.results
    assert all(r.service == "Licensing" for r in report.results)


async def test_writes_never_invoked(tmp_path, monkeypatch):
    ctx = build_context(_cfg(tmp_path))

    calls: list[str] = []

    async def _boom_plan_write(*args, **kwargs):
        calls.append("plan_write")
        raise AssertionError("plan_write must never be called by the smoke test")

    async def _boom_commit_write(*args, **kwargs):
        calls.append("commit_write")
        raise AssertionError("commit_write must never be called by the smoke test")

    monkeypatch.setattr(ctx.executor, "plan_write", _boom_plan_write)
    monkeypatch.setattr(ctx.executor, "commit_write", _boom_commit_write)

    report = await smoke.run_smoke(ctx)

    assert calls == []
    assert report.failed == 0
    write_ids = {a.id for a in ctx.actions.all() if a.is_write}
    by_id = {r.action_id: r for r in report.results}
    for action_id in write_ids:
        assert by_id[action_id].status == "skipped"
        assert "write action" in by_id[action_id].detail


async def test_write_action_guard_raises(tmp_path, monkeypatch):
    """Defensive guard: even if a write action reached the internal runner
    directly, it must be refused before any read/execute call is made."""
    ctx = build_context(_cfg(tmp_path))
    write_action = next(a for a in ctx.actions.all() if a.is_write)

    calls: list[str] = []
    real_read = ctx.executor.read

    async def _tracking_read(action_id, params=None, **kwargs):
        calls.append(action_id)
        return await real_read(action_id, params, **kwargs)

    monkeypatch.setattr(ctx.executor, "read", _tracking_read)

    with pytest.raises(RuntimeError):
        await smoke._run_one(ctx, write_action, {}, 5.0)
    assert calls == []


async def test_failing_read_is_reported_failed(tmp_path, monkeypatch):
    ctx = build_context(_cfg(tmp_path))

    real_read = ctx.executor.read

    async def _flaky_read(action_id, params=None, **kwargs):
        if action_id == "groups.list":
            raise RuntimeError("simulated provider outage")
        return await real_read(action_id, params, **kwargs)

    monkeypatch.setattr(ctx.executor, "read", _flaky_read)

    report = await smoke.run_smoke(ctx)
    by_id = {r.action_id: r for r in report.results}

    assert by_id["groups.list"].status == "failed"
    assert "simulated provider outage" in by_id["groups.list"].detail
    assert report.failed >= 1


async def test_timeout_is_reported_as_failed_with_timeout_detail(tmp_path, monkeypatch):
    ctx = build_context(_cfg(tmp_path))

    real_read = ctx.executor.read

    async def _slow_read(action_id, params=None, **kwargs):
        if action_id == "users.list":
            import asyncio

            await asyncio.sleep(1.0)
        return await real_read(action_id, params, **kwargs)

    monkeypatch.setattr(ctx.executor, "read", _slow_read)

    report = await smoke.run_smoke(ctx, timeout_s=0.01)
    by_id = {r.action_id: r for r in report.results}

    assert by_id["users.list"].status == "failed"
    assert "timeout" in by_id["users.list"].detail.lower()


def test_smoke_test_cli_json_output_and_report_file(tmp_path):
    cfg = _cfg(tmp_path)
    out = io.StringIO()

    exit_code = smoke.smoke_test(cfg, as_json=True, out=out)

    assert exit_code == 0
    payload = json.loads(out.getvalue())
    assert payload["mode"] == "mock"
    assert payload["failed"] == 0
    assert isinstance(payload["results"], list) and payload["results"]

    files = list(cfg.exports_dir.glob("smoke-*.json"))
    assert len(files) == 1
    on_disk = json.loads(files[0].read_text())
    assert on_disk["mode"] == "mock"


def test_smoke_test_cli_text_output_has_banner_and_summary(tmp_path):
    cfg = _cfg(tmp_path)
    out = io.StringIO()

    exit_code = smoke.smoke_test(cfg, out=out)

    text = out.getvalue()
    assert exit_code == 0
    assert "mode=mock" in text
    assert "mock mode" in text.lower()
    assert "Summary:" in text
    assert "Report written:" in text


def test_smoke_test_exit_code_1_on_failure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    # Patch the module-level run_smoke used by smoke_test so a failure surfaces
    # without needing a live executor failure.
    import msgraph_tui.services.context as context_module

    real_build_context = context_module.build_context

    def _build_and_break(config):
        ctx = real_build_context(config)

        async def _broken_read(action_id, params=None, **kwargs):
            from msgraph_tui.core.envelope import ResultEnvelope
            from msgraph_tui.core.errors import ErrorCategory, NormalizedError

            return ResultEnvelope(
                success=False,
                provider="mock",
                action_id=action_id,
                errors=[NormalizedError(ErrorCategory.UNKNOWN, "forced failure")],
            )

        ctx.executor.read = _broken_read  # type: ignore[method-assign]
        return ctx

    monkeypatch.setattr(context_module, "build_context", _build_and_break)

    out = io.StringIO()
    exit_code = smoke.smoke_test(cfg, out=out)

    assert exit_code == 1
    assert "failed" in out.getvalue().lower()


async def test_service_filter_accepts_action_prefix(tmp_path):
    ctx = build_context(AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state"))
    report = await smoke.run_smoke(ctx, services=["teams"])
    assert report.results and all(r.action_id.startswith("teams.") for r in report.results)


def test_live_delegated_signs_in_with_device_code_first(tmp_path, monkeypatch):
    import msgraph_tui.providers.graph_rest as graph_rest

    class Fake:
        def __init__(self, config, message_callback=None):
            message_callback("To sign in, visit https://microsoft.com/devicelogin and enter ABC")

        def get_token(self):
            raise RuntimeError("AADSTS700016: app not found; client_secret=hunter2")

    monkeypatch.setattr(graph_rest, "MsalDeviceCodeTokenProvider", Fake)
    cfg = AppConfig(mode=Mode.LIVE, tenant_id="t", client_id="c", state_dir=tmp_path / "s")
    out = io.StringIO()
    assert smoke.smoke_test(cfg, out=out) == 2          # never runs reads unauthenticated
    text = out.getvalue()
    assert "devicelogin" in text and "Sign-in failed" in text and "hunter2" not in text
