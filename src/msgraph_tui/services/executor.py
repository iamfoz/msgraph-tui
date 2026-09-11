"""Executor: the single gateway for running actions.

Reads go through ``read()``. Writes MUST go through ``plan_write()`` →
``commit_write()``, which enforces the PRD §16 pipeline: provider resolution,
preview, risk, before-state capture, rollback snapshot, change reason,
confirmation, execution, after-state capture, hash-chained audit event, result
validation. Rollbacks are planned with ``plan_rollback()`` and executed as new
audited changes linked to the original operation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..compliance.audit import AuditEvent, AuditLog, ChangeReason
from ..compliance.rollback import (
    RollbackSnapshot,
    RollbackStore,
    SanityCheckResult,
    check_rollback_sanity,
    diff_states,
)
from ..core.actions import (
    ActionDefinition,
    ActionRegistry,
    Confirmation,
    RiskLevel,
    RollbackLevel,
)
from ..core.config import AppConfig, Mode
from ..core.envelope import ResultEnvelope, new_operation_id
from ..core.errors import PipelineError
from ..core.providers import OperationPreview, ProviderRegistry
from ..core.redaction import redact
from ..core.selection import SelectionResult, select_provider

log = logging.getLogger("graphdeck.executor")


@dataclass
class SessionInfo:
    actor: str = "unknown@local"
    tenant_id: str = "unknown-tenant"
    tenant_name: str = ""
    auth_mode: str = "none"  # delegated | app-only | mock | none


@dataclass
class WritePlan:
    action: ActionDefinition
    params: dict[str, Any]
    selection: SelectionResult
    preview: OperationPreview
    operation_id: str = field(default_factory=new_operation_id)
    before_state: dict[str, Any] = field(default_factory=dict)
    before_state_error: str | None = None
    snapshot: RollbackSnapshot | None = None
    requires_reason: bool = False
    confirmation: Confirmation = Confirmation.CONFIRM
    interaction_mode: str = "interactive"

    @property
    def risk(self) -> RiskLevel:
        return self.action.risk

    @property
    def typed_phrase(self) -> str:
        """Phrase the user must type for TYPED confirmation."""
        return self.action.id.split(".")[-1].upper().replace("_", " ")


@dataclass
class RollbackPlan:
    snapshot: RollbackSnapshot
    sanity: SanityCheckResult
    current_state: dict[str, Any] | None
    diff: list[dict[str, Any]]
    write_plan: WritePlan | None  # None when rollback is blocked


class Executor:
    def __init__(
        self,
        config: AppConfig,
        actions: ActionRegistry,
        providers: ProviderRegistry,
        audit_log: AuditLog,
        rollback_store: RollbackStore,
        session: SessionInfo,
    ) -> None:
        self.config = config
        self.actions = actions
        self.providers = providers
        self.audit_log = audit_log
        self.rollback_store = rollback_store
        self.session = session

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _prepare(self, action_id: str, params: dict[str, Any]) -> tuple[ActionDefinition, dict, SelectionResult]:
        action = self.actions.get(action_id)
        validated = action.validate_params(params)
        if action.derive_params:
            validated.update(action.derive_params(validated))
        selection = select_provider(action, self.config, self.providers)
        return action, validated, selection

    def preview(self, action_id: str, params: dict[str, Any]) -> tuple[OperationPreview, SelectionResult]:
        action, validated, selection = self._prepare(action_id, params)
        return selection.provider.preview(action, validated), selection

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def read(self, action_id: str, params: dict[str, Any] | None = None, *, audit: bool = True) -> ResultEnvelope:
        action, validated, selection = self._prepare(action_id, params or {})
        if action.is_write:
            raise PipelineError(f"{action_id} is a write action; use plan_write/commit_write")
        envelope = await selection.provider.execute(action, validated)
        log.info("read %s via %s -> success=%s rows=%s",
                 action_id, envelope.provider, envelope.success, len(envelope.rows))
        if audit and self.config.log_reads_to_audit:
            self.audit_log.append(AuditEvent(
                actor=self.session.actor,
                tenant_id=self.session.tenant_id,
                action_id=action_id,
                operation_id=envelope.operation_id,
                provider=envelope.provider,
                event_type="read",
                risk=action.risk.value,
                request_preview=envelope.request_preview,
                result=envelope.to_audit_dict(),
            ))
        return envelope

    # ------------------------------------------------------------------
    # Write pipeline
    # ------------------------------------------------------------------

    async def plan_write(
        self,
        action_id: str,
        params: dict[str, Any],
        *,
        interaction_mode: str = "interactive",
    ) -> WritePlan:
        action, validated, selection = self._prepare(action_id, params)
        if not action.is_write:
            raise PipelineError(f"{action_id} is read-only; use read()")
        preview = selection.provider.preview(action, validated)
        plan = WritePlan(
            action=action,
            params=validated,
            selection=selection,
            preview=preview,
            requires_reason=self.config.require_change_reason,
            confirmation=action.confirmation,
            interaction_mode=interaction_mode,
        )
        await self._capture_before_state(plan)
        self._draft_snapshot(plan)
        return plan

    async def _capture_before_state(self, plan: WritePlan) -> None:
        spec = plan.action.rollback
        if spec.read_action is None:
            return
        read_params = {
            rp: plan.params.get(source) for rp, source in spec.read_param_map.items()
        }
        envelope = await self.read(spec.read_action, read_params, audit=False)
        if not envelope.success:
            plan.before_state_error = envelope.error_summary() or "before-state capture failed"
            return
        plan.before_state = self._state_from_envelope(envelope, spec.tracked_fields)

    @staticmethod
    def _state_from_envelope(envelope: ResultEnvelope, tracked_fields: list[str]) -> dict[str, Any]:
        data = envelope.data
        if isinstance(data, list):
            # collection state (e.g. group members) keyed by first tracked field
            key = tracked_fields[0] if tracked_fields else "items"
            ids = sorted(
                str(row.get("id", row)) if isinstance(row, dict) else str(row) for row in data
            )
            return {key: ids}
        if isinstance(data, dict):
            fields = tracked_fields or list(data.keys())
            state = {f: data.get(f) for f in fields}
            state["id"] = data.get("id")
            return state
        return {"value": data}

    def _draft_snapshot(self, plan: WritePlan) -> None:
        spec = plan.action.rollback
        if spec.level is RollbackLevel.NONE:
            return
        if plan.before_state_error is not None:
            # PRD §17: no captured before-state -> no rollback offer
            plan.snapshot = None
            return
        inverse_action_id: str | None = None
        inverse_params: dict[str, Any] = {}
        inverse_preview = ""
        if spec.build_inverse is not None:
            inverse_action_id, inverse_params = spec.build_inverse(plan.params, plan.before_state)
            try:
                p, _sel = self.preview(inverse_action_id, inverse_params)
                inverse_preview = p.detail
            except Exception as exc:  # inverse may be unbuildable; degrade gracefully
                inverse_preview = f"(inverse preview unavailable: {exc})"
        object_id = str(
            plan.params.get("user_id") or plan.params.get("group_id")
            or plan.before_state.get("id") or "unknown"
        )
        plan.snapshot = RollbackSnapshot(
            tenant_id=self.session.tenant_id,
            object_id=object_id,
            object_type=plan.action.service,
            provider=plan.selection.provider.name,
            operation_id=plan.operation_id,
            action_id=plan.action.id,
            actor=self.session.actor,
            level=spec.level.value,
            before_state=plan.before_state,
            original_params=redact(dict(plan.params)),
            original_request=plan.preview.detail,
            inverse_action_id=inverse_action_id,
            inverse_params=inverse_params,
            inverse_preview=inverse_preview,
            tracked_fields=list(spec.tracked_fields),
            non_restorable_fields=list(spec.non_restorable_fields),
            risk=plan.action.risk.value,
        )

    async def commit_write(
        self,
        plan: WritePlan,
        *,
        confirmed: bool,
        reason: ChangeReason | None = None,
        rollback_of: str | None = None,
    ) -> ResultEnvelope:
        if not confirmed:
            raise PipelineError("Write refused: confirmation not given")
        if plan.requires_reason and (reason is None or not reason.reason.strip()):
            raise PipelineError("Write refused: change reason required by policy")

        event_type = "rollback" if rollback_of else "change"
        if self.config.mode is Mode.DRY_RUN:
            envelope = ResultEnvelope(
                success=True,
                provider=plan.selection.provider.name,
                action_id=plan.action.id,
                operation_id=plan.operation_id,
                request_preview=plan.preview.detail,
                data={"dry_run": True, "note": "No change was executed (dry-run mode)."},
            )
            envelope.warnings.append("Dry-run mode: the operation was previewed but NOT executed.")
            self._audit_write(plan, envelope, reason, event_type="change_intent", rollback_of=rollback_of)
            return envelope

        envelope = await plan.selection.provider.execute(plan.action, plan.params)
        envelope.operation_id = plan.operation_id

        after_state: dict[str, Any] = {}
        read_action = plan.action.rollback.read_action
        if envelope.success and read_action:
            spec = plan.action.rollback
            read_params = {rp: plan.params.get(src) for rp, src in spec.read_param_map.items()}
            after_env = await self.read(read_action, read_params, audit=False)
            if after_env.success:
                after_state = self._state_from_envelope(after_env, spec.tracked_fields)

        snapshot_id = None
        if envelope.success and plan.snapshot is not None:
            plan.snapshot.after_state = after_state
            self.rollback_store.save(plan.snapshot)
            snapshot_id = plan.snapshot.snapshot_id
            envelope.rollback_snapshot_id = snapshot_id

        validation = {
            "after_state_captured": bool(after_state),
            "result_matches_intent": envelope.success,
        }
        self._audit_write(
            plan, envelope, reason,
            event_type=event_type,
            after_state=after_state,
            snapshot_id=snapshot_id,
            rollback_of=rollback_of,
            validation=validation,
        )
        if rollback_of and envelope.success and plan.snapshot is None:
            pass  # rollback of a rollback is not chained further without a snapshot
        log.info("write %s via %s -> success=%s op=%s",
                 plan.action.id, envelope.provider, envelope.success, plan.operation_id)
        return envelope

    def _audit_write(
        self,
        plan: WritePlan,
        envelope: ResultEnvelope,
        reason: ChangeReason | None,
        *,
        event_type: str,
        after_state: dict | None = None,
        snapshot_id: str | None = None,
        rollback_of: str | None = None,
        validation: dict | None = None,
    ) -> None:
        self.audit_log.append(AuditEvent(
            actor=self.session.actor,
            tenant_id=self.session.tenant_id,
            action_id=plan.action.id,
            operation_id=plan.operation_id,
            provider=plan.selection.provider.name,
            event_type=event_type,
            object_id=plan.snapshot.object_id if plan.snapshot else (
                str(plan.params.get("user_id") or plan.params.get("group_id") or "")
                or None
            ),
            object_type=plan.action.service,
            risk=plan.action.risk.value,
            request_preview=envelope.request_preview or plan.preview.detail,
            before_state=plan.before_state or None,
            after_state=after_state or None,
            reason=(reason or ChangeReason()).to_dict(),
            result=envelope.to_audit_dict(),
            rollback_snapshot_id=snapshot_id,
            rollback_of_operation=rollback_of,
            interaction_mode=plan.interaction_mode,
            validation=validation or {},
        ))

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def plan_rollback(self, snapshot_id: str) -> RollbackPlan:
        snapshot = self.rollback_store.load(snapshot_id)
        if snapshot is None:
            raise PipelineError(f"Rollback snapshot {snapshot_id} not found")

        current_state: dict[str, Any] | None = None
        spec_action = self.actions.get(snapshot.action_id)
        spec = spec_action.rollback
        if spec.read_action:
            read_params = {
                rp: snapshot.original_params.get(src) for rp, src in spec.read_param_map.items()
            }
            env = await self.read(spec.read_action, read_params, audit=False)
            if env.success:
                current_state = self._state_from_envelope(env, snapshot.tracked_fields)

        sanity = check_rollback_sanity(
            snapshot,
            current_state,
            after_state=snapshot.after_state or None,
        )
        diff = diff_states(snapshot.before_state, current_state or {})

        write_plan: WritePlan | None = None
        if sanity.can_rollback and snapshot.inverse_action_id:
            write_plan = await self.plan_write(
                snapshot.inverse_action_id, snapshot.inverse_params
            )
        return RollbackPlan(
            snapshot=snapshot,
            sanity=sanity,
            current_state=current_state,
            diff=diff,
            write_plan=write_plan,
        )

    async def commit_rollback(
        self,
        plan: RollbackPlan,
        *,
        confirmed: bool,
        reason: ChangeReason | None = None,
    ) -> ResultEnvelope:
        if plan.write_plan is None or not plan.sanity.can_rollback:
            raise PipelineError(f"Rollback blocked: {plan.sanity.summary}")
        envelope = await self.commit_write(
            plan.write_plan,
            confirmed=confirmed,
            reason=reason,
            rollback_of=plan.snapshot.operation_id,
        )
        if envelope.success:
            self.rollback_store.mark_consumed(plan.snapshot.snapshot_id)
        return envelope
