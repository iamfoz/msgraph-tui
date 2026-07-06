"""Rollback snapshots, sanity checks and drift blocking."""

from msgraph_tui.compliance.rollback import (
    RollbackSnapshot,
    RollbackStore,
    check_rollback_sanity,
    diff_states,
)
from msgraph_tui.core.actions import RollbackLevel


def _snapshot(**overrides) -> RollbackSnapshot:
    base = dict(
        tenant_id="t1",
        object_id="u-0001",
        object_type="Entra ID",
        provider="mock",
        operation_id="op-1",
        action_id="users.update",
        actor="admin@contoso.example",
        level=RollbackLevel.FULL.value,
        before_state={"department": "Engineering", "jobTitle": "Engineer"},
        after_state={"department": "Platform", "jobTitle": "Engineer"},
        tracked_fields=["department", "jobTitle"],
        inverse_action_id="users.update",
        inverse_params={"user_id": "u-0001", "department": "Engineering"},
    )
    base.update(overrides)
    return RollbackSnapshot(**base)


def test_clean_rollback_allowed():
    current = {"department": "Platform", "jobTitle": "Engineer"}
    result = check_rollback_sanity(_snapshot(), current)
    assert result.can_rollback and not result.blocked
    assert result.checks["no_drift"] is True


def test_drift_blocks_rollback():
    # a third party changed jobTitle after our change
    current = {"department": "Platform", "jobTitle": "Staff Engineer"}
    result = check_rollback_sanity(_snapshot(), current)
    assert result.blocked
    assert any(d.field == "jobTitle" for d in result.drift)
    assert "changed since the original operation" in result.summary


def test_missing_object_blocks_rollback():
    result = check_rollback_sanity(_snapshot(), None)
    assert result.blocked
    assert result.checks["object_exists"] is False


def test_consumed_snapshot_blocks_rollback():
    result = check_rollback_sanity(
        _snapshot(consumed=True), {"department": "Platform", "jobTitle": "Engineer"}
    )
    assert result.blocked


def test_no_rollback_level_blocks():
    snap = _snapshot(level=RollbackLevel.NONE.value)
    result = check_rollback_sanity(snap, {"department": "Platform", "jobTitle": "Engineer"})
    assert result.blocked


def test_blast_radius_check():
    result = check_rollback_sanity(
        _snapshot(),
        {"department": "Platform", "jobTitle": "Engineer"},
        rollback_object_count=10,
        original_object_count=1,
    )
    assert result.blocked
    assert result.checks["blast_radius_ok"] is False


def test_non_restorable_fields_warn_but_allow():
    snap = _snapshot(non_restorable_fields=["photo"])
    result = check_rollback_sanity(snap, {"department": "Platform", "jobTitle": "Engineer"})
    assert result.can_rollback
    assert any("photo" in w for w in result.warnings)


def test_store_roundtrip_and_consume(tmp_path):
    store = RollbackStore(tmp_path)
    snap = _snapshot()
    store.save(snap)
    loaded = store.load(snap.snapshot_id)
    assert loaded is not None
    assert loaded.before_state == snap.before_state
    assert loaded.inverse_params == snap.inverse_params
    store.mark_consumed(snap.snapshot_id)
    assert store.load(snap.snapshot_id).consumed is True
    assert store.load("does-not-exist") is None


def test_snapshot_serialisation_redacts_secrets(tmp_path):
    store = RollbackStore(tmp_path)
    snap = _snapshot(before_state={"password": "hunter2", "department": "Eng"})
    path = store.save(snap)
    text = path.read_text()
    assert "hunter2" not in text
    assert "Eng" in text


def test_diff_states():
    diff = diff_states({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert {"field": "b", "before": 2, "current": 3} in diff
    assert {"field": "c", "before": None, "current": 4} in diff
    assert all(d["field"] != "a" for d in diff)
