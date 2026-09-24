"""Per-approver Ed25519 signatures, offline approval, import and the git channel end to end."""

import asyncio
import io
import json
import os
import shutil
import stat
import subprocess

import pytest

pytest.importorskip("cryptography")

from msgraph_tui.compliance.approval import (
    APPLIED,
    APPROVED,
    PENDING,
    ChangeRequest,
    content_hash_for,
    make_approval,
    verify_approval,
    verify_signature_only,
)
from msgraph_tui.compliance.audit import ChangeReason
from msgraph_tui.compliance.signing import (
    TrustStore,
    generate_keypair,
    key_id,
    load_private_key,
    write_private_key,
)
from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.core.errors import PipelineError
from msgraph_tui.services import commands
from msgraph_tui.services.context import build_context

R = ChangeReason(reason="leaver offboarding", ticket="HR-9")
DISABLE = ("users.set_account_enabled", {"user_id": "u-0001", "enabled": False})
REQUESTER = "mock-admin@contoso.example"  # the mock session's actor


def _key(tmp_path, name="bob", passphrase=None):
    pem, public = generate_keypair(passphrase)
    path = tmp_path / f"{name}.pem"
    write_private_key(path, pem)
    return path, public


def _trust(tmp_path, approvers: dict) -> os.PathLike:
    path = tmp_path / "trust.json"
    path.write_text(json.dumps({"approvers": approvers}))
    return path


def _request(**overrides) -> ChangeRequest:
    base = dict(
        action_id="users.set_account_enabled", action_name="Disable", params={"user_id": "u1"},
        risk="high", requester="alice@x", tenant_id="t", preview="PATCH ...",
        content_hash=content_hash_for("users.set_account_enabled", {"user_id": "u1"}, "t"),
    )
    base.update(overrides)
    return ChangeRequest(**base)


# --- key handling ------------------------------------------------------------

def test_private_key_file_is_owner_only_and_never_overwritten(tmp_path):
    path, public = _key(tmp_path)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private_key(path, b"x")
    loaded = load_private_key(path)
    assert loaded.public_b64 == public and loaded.key_id == key_id(public)
    assert "PRIVATE" not in repr(loaded)  # key material never in repr


def test_encrypted_key_needs_the_passphrase(tmp_path):
    path, _ = _key(tmp_path, passphrase=b"correct horse")
    with pytest.raises(ValueError, match="passphrase"):
        load_private_key(path)
    with pytest.raises(ValueError):
        load_private_key(path, b"wrong")
    assert load_private_key(path, b"correct horse").key_id


def test_trust_store_validation(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"approvers": {"bob": "not-a-list"}}))
    with pytest.raises(ValueError, match="must look like"):
        TrustStore.load(bad)
    store = TrustStore({"Bob@X": ["k1"]})
    assert store.keys_for("bob@x") == ["k1"]  # case-insensitive


# --- signatures ----------------------------------------------------------------

def test_ed25519_approval_binds_the_person(tmp_path):
    bob_path, bob_pub = _key(tmp_path, "bob")
    carol_path, carol_pub = _key(tmp_path, "carol")
    store = TrustStore({"bob@x": [bob_pub], "carol@x": [carol_pub]})
    bob = load_private_key(bob_path)
    req = _request()

    good = make_approval(req, "bob@x", approve=True, private_key=bob)
    assert good.algorithm == "ed25519" and good.key_id == bob.key_id
    ok, _ = verify_approval(good, content_hash=req.content_hash, requester="alice@x",
                            executor="alice@x", trust_store=store)
    assert ok

    # Bob signs but claims to be Carol: Carol's registered key doesn't match.
    forged = make_approval(req, "carol@x", approve=True, private_key=bob)
    ok, why = verify_signature_only(forged, trust_store=store)
    assert not ok and "not registered" in why

    # Any edit after signing breaks the signature (comment, decision, hash).
    for field, value in (("comment", "edited"), ("decision", "reject"), ("content_hash", "x")):
        tampered = make_approval(req, "bob@x", approve=True, private_key=bob)
        setattr(tampered, field, value)
        assert not verify_signature_only(tampered, trust_store=store)[0]

    # A trust store demands personal keys: HMAC and unsigned approvals are refused.
    hmac_signed = make_approval(req, "bob@x", approve=True, key=b"shared")
    assert not verify_signature_only(hmac_signed, trust_store=store)[0]
    unsigned = make_approval(req, "bob@x", approve=True)
    assert "personal" in verify_signature_only(unsigned, trust_store=store)[1]


def test_tampered_request_cannot_be_approved():
    req = _request()
    req.params = {"user_id": "someone-harmless"}  # shows other params than the hash covers
    with pytest.raises(ValueError, match="inconsistent"):
        make_approval(req, "bob@x", approve=True)


# --- executor + import ---------------------------------------------------------

def _config(tmp_path, **extra) -> AppConfig:
    return AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state",
                     require_approval_for_risk=["high"], **extra)


async def test_gate_requires_personal_key_when_trust_store_configured(tmp_path):
    bob_path, bob_pub = _key(tmp_path)
    cfg = _config(tmp_path, approver_trust_store_path=_trust(tmp_path, {"bob@x": [bob_pub]}))
    ex = build_context(cfg).executor
    plan = await ex.plan_write(*DISABLE)
    req = ex.create_change_request(plan, R)

    weak = make_approval(req, "bob@x", approve=True)  # typed name only
    ex.record_approval(req, weak)
    with pytest.raises(PipelineError, match="personal"):
        await ex.commit_write(await ex.plan_write(*DISABLE), confirmed=True, reason=R, approval=weak)

    strong = make_approval(req, "bob@x", approve=True, private_key=load_private_key(bob_path))
    env = await ex.commit_write(await ex.plan_write(*DISABLE), confirmed=True, reason=R,
                                approval=strong)
    assert env.success
    change = [e for e in ex.audit_log.entries() if e["event_type"] == "change"][-1]
    assert change["validation"]["four_eyes"]["signature_alg"] == "ed25519"
    assert change["validation"]["four_eyes"]["key_id"] == strong.key_id


async def test_import_accepts_only_verifiable_signed_approvals(tmp_path):
    bob_path, bob_pub = _key(tmp_path)
    bob = load_private_key(bob_path)

    # No verification policy configured → even a signed import is refused.
    ex = build_context(_config(tmp_path / "a")).executor
    req = ex.create_change_request(await ex.plan_write(*DISABLE), R)
    with pytest.raises(ValueError, match="configure approver_trust_store_path"):
        ex.import_approval(make_approval(req, "bob@x", approve=True, private_key=bob))

    cfg = _config(tmp_path / "b", approver_trust_store_path=_trust(tmp_path, {"bob@x": [bob_pub]}))
    ex = build_context(cfg).executor
    req = ex.create_change_request(await ex.plan_write(*DISABLE), R)
    with pytest.raises(ValueError, match="unsigned"):
        ex.import_approval(make_approval(req, "bob@x", approve=True))
    other = _request(request_id=req.request_id, requester=req.requester)  # different change
    with pytest.raises(ValueError, match="content hash"):
        ex.import_approval(make_approval(other, "bob@x", approve=True, private_key=bob))
    ex.import_approval(make_approval(req, "bob@x", approve=True, private_key=bob))
    assert ex.approvals.load_request(req.request_id).status == APPROVED
    with pytest.raises(ValueError, match="already approved"):
        ex.import_approval(make_approval(req, "bob@x", approve=True, private_key=bob))


# --- CLI: keygen, offline sign, import, apply ------------------------------------

def test_cli_keygen_offline_approval_import_and_apply(tmp_path):
    out = io.StringIO()
    key_path = tmp_path / "keys" / "bob.pem"
    assert commands.keygen(AppConfig(), "bob@contoso.example", out_path=key_path,
                           passphrase=False, out=out) == 0
    public = next(line.split(": ", 1)[1] for line in out.getvalue().splitlines()
                  if line.startswith("Public key: "))
    assert commands.keygen(AppConfig(), "bob@contoso.example", out_path=key_path,
                           passphrase=False, out=io.StringIO()) == 2  # no overwrite

    trust = _trust(tmp_path, {"bob@contoso.example": [public]})
    requester_cfg = _config(tmp_path / "requester", approver_trust_store_path=trust)
    ex = build_context(requester_cfg).executor
    rid = ex.create_change_request(asyncio.run(ex.plan_write(*DISABLE)), R).request_id

    req_file = tmp_path / "req.json"
    assert commands.export_request(requester_cfg, rid, out_file=req_file, out=io.StringIO()) == 0

    # The approver works on another machine: no tenant, no shared state.
    approver_cfg = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "approver",
                             approver_trust_store_path=trust)
    appr_file = tmp_path / "approval.json"
    assert commands.approve(approver_cfg, None, "bob@contoso.example", request_file=req_file,
                            out_file=appr_file, out=io.StringIO()) == 2  # key required
    out = io.StringIO()
    assert commands.approve(approver_cfg, None, "bob@contoso.example", key_path=key_path,
                            request_file=req_file, out_file=appr_file, out=out) == 0
    assert "Parameters:" in out.getvalue()  # approver sees what the hash covers

    # A request file edited to look harmless is refused before signing.
    evil = json.loads(req_file.read_text())
    evil["params"]["user_id"] = "u-harmless"
    evil_file = tmp_path / "evil.json"
    evil_file.write_text(json.dumps(evil))
    assert commands.approve(approver_cfg, None, "bob@contoso.example", key_path=key_path,
                            request_file=evil_file, out_file=tmp_path / "x.json",
                            out=io.StringIO()) == 2

    assert commands.import_approval(requester_cfg, appr_file, out=io.StringIO()) == 0
    assert commands.apply_request(requester_cfg, rid, out=io.StringIO()) == 0
    assert build_context(requester_cfg).executor.approvals.load_request(rid).status == APPLIED


def test_cli_approve_refuses_key_missing_from_trust_store(tmp_path):
    key_path, _ = _key(tmp_path, "mallory")
    _, bob_pub = _key(tmp_path, "bob")
    cfg = _config(tmp_path, approver_trust_store_path=_trust(tmp_path, {"bob@x": [bob_pub]}))
    ex = build_context(cfg).executor
    rid = ex.create_change_request(asyncio.run(ex.plan_write(*DISABLE)), R).request_id
    out = io.StringIO()
    assert commands.approve(cfg, rid, "bob@x", key_path=key_path, out=out) == 2
    assert "trust store" in out.getvalue()
    assert ex.approvals.load_request(rid).status == PENDING


# --- git channel end to end: two workstations, one shared repo ---------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(*args, cwd=None) -> str:
    cmd = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def _clone(origin, dest, name):
    _git("clone", "-q", str(origin), str(dest))
    _git("config", "user.name", name, cwd=dest)
    _git("config", "user.email", f"{name}@example.test", cwd=dest)
    _git("config", "commit.gpgsign", "false", cwd=dest)


@needs_git
def test_git_channel_round_trip_between_two_workstations(tmp_path, monkeypatch):
    origin = tmp_path / "approvals.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _clone(origin, seed, "seed")
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    (seed / "README.md").write_text("approvals\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    _clone(origin, tmp_path / "alice-clone", "alice")
    _clone(origin, tmp_path / "bob-clone", "bob")

    sent: list[str] = []
    import msgraph_tui.compliance.channels as channels
    monkeypatch.setattr(channels.WebhookNotifier, "notify",
                        lambda self, event, request, approval=None: sent.append(event) or True)

    key_path, public = _key(tmp_path, "bob")
    trust = _trust(tmp_path, {"bob@contoso.example": [public]})
    common = dict(approver_trust_store_path=trust, approval_channel="git",
                  approval_webhook_url="https://hooks.example.test/x")
    alice = _config(tmp_path / "alice", approval_git_repo=tmp_path / "alice-clone", **common)
    bob = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "bob", approval_git_repo=tmp_path / "bob-clone",
                    **common)

    ex = build_context(alice).executor
    rid = ex.create_change_request(asyncio.run(ex.plan_write(*DISABLE)), R).request_id
    assert any("graphdeck/cr-" in n for n in ex.last_channel_notes)
    assert f"graphdeck/cr-{rid}" in _git("ls-remote", "--heads", str(origin))

    # Bob never saw the request locally: approve fetches it from the repo.
    out = io.StringIO()
    assert commands.approve(bob, rid, "bob@contoso.example", key_path=key_path, out=out) == 0
    assert "pushed to the approvals repo" in out.getvalue()

    # Alice's apply pulls the decision, verifies Bob's signature, then executes.
    out = io.StringIO()
    assert commands.apply_request(alice, rid, out=out) == 0, out.getvalue()
    assert "imported" in out.getvalue()
    assert build_context(alice).executor.approvals.load_request(rid).status == APPLIED
    assert sent[0] == "approval_requested" and "approval_granted" in sent and "change_applied" in sent
