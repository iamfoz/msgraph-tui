"""Four-eyes approval workflow (F-COMP-1): core, executor gate, CLI, UI."""

import asyncio
import io
import json

import pytest

from msgraph_tui.compliance.approval import (
    APPLIED,
    APPROVED,
    PENDING,
    REJECTED,
    ApprovalStore,
    ChangeRequest,
    content_hash_for,
    make_approval,
    verify_approval,
)
from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.core.config import AppConfig, Mode, load_config
from msgraph_tui.core.errors import PipelineError
from msgraph_tui.services import commands
from msgraph_tui.services.context import build_context

R = ChangeReason(reason="leaver offboarding", ticket="HR-9")
DISABLE = ("users.set_account_enabled", {"user_id": "u-0001", "enabled": False})


def _ctx(tmp_path, **cfg):
    config = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state", **cfg)
    return build_context(config)


def _request(**overrides) -> ChangeRequest:
    base = dict(
        action_id="users.set_account_enabled", action_name="Disable", params={"user_id": "u1"},
        risk="high", requester="alice@x", tenant_id="t",
        preview="PATCH ...", content_hash=content_hash_for("users.set_account_enabled",
                                                           {"user_id": "u1"}, "t"),
    )
    base.update(overrides)
    return ChangeRequest(**base)


# --- core ------------------------------------------------------------------

def test_content_hash_is_stable_and_binds_params_and_tenant():
    h = content_hash_for("a.b", {"x": 1, "y": 2}, "t1")
    assert h == content_hash_for("a.b", {"y": 2, "x": 1}, "t1")      # order-independent
    assert h != content_hash_for("a.b", {"x": 1, "y": 3}, "t1")      # any param change
    assert h != content_hash_for("a.b", {"x": 1, "y": 2}, "t2")      # other tenant
    assert h == content_hash_for("a.b", {"x": 1, "y": 2, "_simulate": "z"}, "t1")  # control params ignored


def test_requester_cannot_approve_own_change():
    with pytest.raises(ValueError, match="Segregation of duties"):
        make_approval(_request(), "ALICE@x", approve=True)  # case-insensitive


def test_verify_rejects_rejected_mismatched_and_same_person():
    req = _request()
    good = make_approval(req, "bob@x", approve=True)
    ok, _ = verify_approval(good, content_hash=req.content_hash, requester="alice@x", executor="alice@x")
    assert ok

    rejected = make_approval(req, "bob@x", approve=False)
    assert verify_approval(rejected, content_hash=req.content_hash,
                           requester="alice@x", executor="alice@x") == (False, "request was rejected by bob@x")

    ok, why = verify_approval(good, content_hash="other", requester="alice@x", executor="alice@x")
    assert not ok and "exact change" in why

    # approver may not also be the one executing
    ok, why = verify_approval(good, content_hash=req.content_hash, requester="alice@x", executor="bob@x")
    assert not ok and "segregation" in why

    assert verify_approval(None, content_hash="h", requester="a", executor="a")[0] is False


def test_signed_approvals():
    req = _request()
    key = b"org-approval-key"
    signed = make_approval(req, "bob@x", approve=True, key=key)
    assert signed.signature
    common = dict(content_hash=req.content_hash, requester="alice@x", executor="alice@x")
    assert verify_approval(signed, key=key, **common)[0]
    assert not verify_approval(signed, key=b"wrong", **common)[0]           # wrong key
    unsigned = make_approval(req, "bob@x", approve=True)
    ok, why = verify_approval(unsigned, key=key, **common)                   # key but unsigned
    assert not ok and "unsigned" in why
    signed.approver = "mallory@x"                                            # tampered file
    assert not verify_approval(signed, key=key, **common)[0]


def test_store_round_trip_and_find_approved(tmp_path):
    store = ApprovalStore(tmp_path)
    req = _request()
    store.save_request(req)
    assert store.load_request(req.request_id).content_hash == req.content_hash
    assert store.find_approved(req.content_hash) is None            # still pending
    appr = make_approval(req, "bob@x", approve=True)
    store.save_approval(appr)
    store.set_status(req.request_id, APPROVED)
    found = store.find_approved(req.content_hash)
    assert found and found[1].approver == "bob@x"
    store.set_status(req.request_id, APPLIED)
    assert store.find_approved(req.content_hash) is None            # no replay once applied
    assert store.load_request("../../etc/passwd") is None           # id sanitised


# --- executor gate --------------------------------------------------------

async def test_policy_off_means_no_gate(tmp_path):
    ex = _ctx(tmp_path).executor
    plan = await ex.plan_write(*DISABLE)
    assert not ex.approval_required(plan.action)
    assert (await ex.commit_write(plan, confirmed=True, reason=R)).success


async def test_gate_refuses_then_allows_with_valid_approval(tmp_path):
    ex = _ctx(tmp_path, require_approval_for_risk=["high"]).executor
    plan = await ex.plan_write(*DISABLE)
    with pytest.raises(PipelineError, match="Four-eyes"):
        await ex.commit_write(plan, confirmed=True, reason=R)

    req = ex.create_change_request(plan, R)
    assert req.status == PENDING and req.requester == ex.session.actor
    ex.record_approval(req, make_approval(req, "bob@contoso.example", approve=True))

    plan2 = await ex.plan_write(*DISABLE)
    _, approval = ex.find_approval(plan2)
    env = await ex.commit_write(plan2, confirmed=True, reason=R, approval=approval)
    assert env.success
    assert ex.approvals.load_request(req.request_id).status == APPLIED

    change = [e for e in ex.audit_log.entries() if e["event_type"] == "change"][-1]
    assert change["validation"]["four_eyes"]["approver"] == "bob@contoso.example"
    assert change["reason"]["approval_ref"] == req.request_id
    kinds = [e["event_type"] for e in ex.audit_log.entries()]
    assert "approval_requested" in kinds and "approval_granted" in kinds
    assert ex.audit_log.verify().ok


async def test_approval_cannot_be_reused_for_a_different_object(tmp_path):
    ex = _ctx(tmp_path, require_approval_for_risk=["high"]).executor
    plan = await ex.plan_write(*DISABLE)
    req = ex.create_change_request(plan, R)
    approval = make_approval(req, "bob@contoso.example", approve=True)
    ex.record_approval(req, approval)
    other = await ex.plan_write("users.set_account_enabled", {"user_id": "u-0002", "enabled": False})
    with pytest.raises(PipelineError, match="exact change"):
        await ex.commit_write(other, confirmed=True, reason=R, approval=approval)


async def test_lower_risk_dry_run_and_rollback_are_not_gated(tmp_path):
    ex = _ctx(tmp_path, require_approval_for_risk=["high"]).executor
    medium = await ex.plan_write("users.update", {"user_id": "u-0002", "department": "X"})
    assert not ex.approval_required(medium.action)            # MEDIUM not in policy

    # rollback of an approved change is exempt (never block an urgent undo)
    plan = await ex.plan_write(*DISABLE)
    req = ex.create_change_request(plan, R)
    ex.record_approval(req, make_approval(req, "bob@contoso.example", approve=True))
    plan2 = await ex.plan_write(*DISABLE)
    env = await ex.commit_write(plan2, confirmed=True, reason=R, approval=ex.find_approval(plan2)[1])
    rplan = await ex.plan_rollback(env.rollback_snapshot_id)
    assert (await ex.commit_rollback(rplan, confirmed=True, reason=R)).success

    dry = _ctx(tmp_path / "dry", require_approval_for_risk=["high"])
    dry.config.mode = Mode.DRY_RUN
    dplan = await dry.executor.plan_write(*DISABLE)
    assert (await dry.executor.commit_write(dplan, confirmed=True, reason=R)).success


async def test_bulk_needs_one_approval_covering_all_objects(tmp_path):
    ex = _ctx(tmp_path, require_approval_for_risk=["high"]).executor
    items = [{"user_id": "u-0001", "enabled": False}, {"user_id": "u-0002", "enabled": False}]
    bulk = await ex.plan_bulk("users.set_account_enabled", items)
    with pytest.raises(PipelineError, match="Four-eyes"):
        await ex.commit_bulk(bulk, confirmed=True, reason=R)
    req = ex.create_bulk_change_request(bulk, R)
    assert req.is_bulk and len(req.bulk_items) == 2
    ex.record_approval(req, make_approval(req, "bob@contoso.example", approve=True))
    bulk2 = await ex.plan_bulk("users.set_account_enabled", items)
    results = await ex.commit_bulk(bulk2, confirmed=True, reason=R,
                                   approval=ex.find_bulk_approval(bulk2)[1])
    assert all(r.success for r in results)
    assert ex.approvals.load_request(req.request_id).status == APPLIED


# --- CLI --------------------------------------------------------------------

def _submit(tmp_path) -> tuple[AppConfig, str]:
    config = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state",
                       require_approval_for_risk=["high"])
    ex = build_context(config).executor
    plan = asyncio.run(ex.plan_write(*DISABLE))
    return config, ex.create_change_request(plan, R).request_id


def test_cli_approve_and_apply_loop(tmp_path):
    config, rid = _submit(tmp_path)
    out = io.StringIO()
    assert commands.list_approvals(config, out=out) == 0 and rid in out.getvalue()
    assert commands.apply_request(config, rid, out=io.StringIO()) == 2          # not approved yet
    assert commands.approve(config, rid, "mock-admin@contoso.example", out=io.StringIO()) == 2  # self
    assert commands.approve(config, rid, "bob@contoso.example", out=io.StringIO()) == 0
    assert commands.apply_request(config, rid, out=io.StringIO()) == 0
    assert commands.apply_request(config, rid, out=io.StringIO()) == 2          # no replay
    assert commands.approve(config, "missing", "bob@x", out=io.StringIO()) == 1


def test_cli_reject_blocks_apply(tmp_path):
    config, rid = _submit(tmp_path)
    assert commands.approve(config, rid, "bob@contoso.example", reject=True, out=io.StringIO()) == 0
    assert ApprovalStore(config.approvals_dir).load_request(rid).status == REJECTED
    assert commands.apply_request(config, rid, out=io.StringIO()) == 2


def test_secret_params_cannot_be_queued(tmp_path):
    from msgraph_tui.compliance.approval import assert_requestable
    from msgraph_tui.core.actions import ActionDefinition, GraphTemplate, ParamSpec
    action = ActionDefinition(id="t.x", name="t", description="d", service="s",
                              preferred_provider="mock", supported_providers=["mock"],
                              params=[ParamSpec("pw", secret=True)],
                              graph=GraphTemplate(method="GET", path="/x"))
    with pytest.raises(ValueError, match="secret"):
        assert_requestable(action, {"pw": "x"})


# --- config -----------------------------------------------------------------

def test_env_toggles_for_auth_mode_and_approval(tmp_path, monkeypatch):
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({}))
    monkeypatch.setenv("GRAPHDECK_AUTH_MODE", "App-Only")
    monkeypatch.setenv("GRAPHDECK_REQUIRE_APPROVAL", "High, Destructive")
    cfg = load_config(cfg_file)
    assert cfg.auth_mode == "app-only"
    assert cfg.require_approval_for_risk == ["high", "destructive"]
    assert cfg.approval_required_for("HIGH") and not cfg.approval_required_for("medium")
    monkeypatch.setenv("GRAPHDECK_AUTH_MODE", "sideways")
    with pytest.raises(ValueError, match="auth_mode"):
        load_config(cfg_file)
