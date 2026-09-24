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
from dataclasses import dataclass, field, replace
from typing import Any

from ..compliance.approval import (
    APPLIED,
    Approval,
    ApprovalStore,
    ChangeRequest,
    assert_requestable,
    bulk_content_hash,
    content_hash_for,
    verify_approval,
)
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

# Params that identify the object an action targets, in priority order — used to
# link audit events to the object they touched.
_OBJECT_ID_PARAMS = ("user_id", "group_id", "sku_id", "application_id", "device_id")


def _object_ref(params: dict[str, Any]) -> str | None:
    for name in _OBJECT_ID_PARAMS:
        value = params.get(name)
        if value:
            return str(value)
    return None


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
    before_etag: str | None = None
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


@dataclass
class BulkItemResult:
    object_id: str
    success: bool
    envelope: ResultEnvelope


@dataclass
class BulkPlan:
    """A bulk change: one fully-formed WritePlan per target object, each with
    its own before-state and rollback snapshot (preserving the per-object
    rollback guarantee the PRD requires for bulk actions)."""

    action: ActionDefinition
    plans: list[WritePlan]
    requires_reason: bool = False

    @property
    def count(self) -> int:
        return len(self.plans)

    @property
    def typed_phrase(self) -> str:
        verb = self.action.id.split(".")[-1].upper().replace("_", " ")
        return f"{verb} {self.count}"

    @property
    def object_ids(self) -> list[str]:
        return [
            str(p.params.get("user_id") or p.params.get("group_id")
                or (p.snapshot.object_id if p.snapshot else "?"))
            for p in self.plans
        ]


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
        self._approvals: ApprovalStore | None = None

    # ------------------------------------------------------------------
    # Four-eyes approval (F-COMP-1)
    # ------------------------------------------------------------------

    @property
    def approvals(self) -> ApprovalStore:
        if self._approvals is None:
            self._approvals = ApprovalStore(self.config.approvals_dir)
        return self._approvals

    def approval_required(self, action: ActionDefinition) -> bool:
        return action.is_write and self.config.approval_required_for(action.risk.value)

    def content_hash(self, plan: WritePlan) -> str:
        return content_hash_for(plan.action.id, plan.params, self.session.tenant_id)

    def create_change_request(
        self, plan: WritePlan, reason: ChangeReason | None = None
    ) -> ChangeRequest:
        """Queue a planned change for a second person's approval (not executed)."""
        assert_requestable(plan.action, plan.params)
        request = ChangeRequest(
            action_id=plan.action.id,
            action_name=plan.action.name,
            params=dict(plan.params),
            risk=plan.action.risk.value,
            requester=self.session.actor,
            tenant_id=self.session.tenant_id,
            preview=plan.preview.detail,
            content_hash=self.content_hash(plan),
            reason=(reason or ChangeReason()).to_dict(),
        )
        self.approvals.save_request(request)
        self._audit_approval_event("approval_requested", request, detail={"status": "pending"})
        return request

    def create_bulk_change_request(
        self, plan: BulkPlan, reason: ChangeReason | None = None
    ) -> ChangeRequest:
        items = [dict(p.params) for p in plan.plans]
        for item in items:
            assert_requestable(plan.action, item)
        request = ChangeRequest(
            action_id=plan.action.id,
            action_name=plan.action.name,
            params={},
            risk=plan.action.risk.value,
            requester=self.session.actor,
            tenant_id=self.session.tenant_id,
            preview=plan.plans[0].preview.detail if plan.plans else "",
            content_hash=bulk_content_hash(plan.action.id, items, self.session.tenant_id),
            reason=(reason or ChangeReason()).to_dict(),
            bulk_items=items,
        )
        self.approvals.save_request(request)
        self._audit_approval_event("approval_requested", request, detail={"objects": len(items)})
        return request

    def find_approval(self, plan: WritePlan) -> tuple[ChangeRequest, Approval] | None:
        """An approved request covering this exact change, if one exists."""
        return self.approvals.find_approved(self.content_hash(plan))

    def record_approval(self, request: ChangeRequest, approval: Approval) -> None:
        self.approvals.save_approval(approval)
        self.approvals.set_status(request.request_id, "approved" if approval.approved else "rejected")
        self._audit_approval_event(
            "approval_granted" if approval.approved else "approval_rejected",
            request,
            detail={"approver": approval.approver, "signed": bool(approval.signature),
                    "comment": approval.comment},
        )

    def _check_approval(
        self, *, content_hash: str, approval: Approval | None, request_id: str | None
    ) -> dict[str, Any]:
        """Enforce four-eyes. Returns audit metadata or raises PipelineError."""
        requester = self.session.actor
        if approval is not None and request_id:
            stored = self.approvals.load_request(request_id)
            if stored is not None:
                requester = stored.requester
        ok, why = verify_approval(
            approval,
            content_hash=content_hash,
            requester=requester,
            executor=self.session.actor,
            key=self.config.approval_signing_key(),
        )
        if not ok:
            raise PipelineError(
                f"Four-eyes approval required and not satisfied: {why}. "
                "Submit a change request and have a different person approve it "
                "(graphdeck approve <id> --as <name>)."
            )
        assert approval is not None
        return {"request_id": approval.request_id, "approver": approval.approver,
                "requester": requester, "signed": bool(approval.signature)}

    def _audit_approval_event(self, event_type: str, request: ChangeRequest, *, detail: dict) -> None:
        self.audit_log.append(AuditEvent(
            actor=self.session.actor,
            tenant_id=request.tenant_id,
            action_id=request.action_id,
            operation_id=request.request_id,
            provider="approval",
            event_type=event_type,
            risk=request.risk,
            request_preview=request.preview,
            reason=request.reason,
            result={"success": True, **detail},
            interaction_mode="bulk" if request.is_bulk else "interactive",
        ))

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
                object_id=_object_ref(validated),
                object_type=action.service,
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
        # Capture the concurrency marker (ETag) so the snapshot records the exact
        # object version the change was planned against (PRD §17).
        plan.before_etag = envelope.concurrency_marker

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
            concurrency_marker=plan.before_etag,
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
        approval: Approval | None = None,
        _preapproved: dict[str, Any] | None = None,
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

        # Four-eyes gate: real execution of a policy-covered risk level needs a
        # valid approval from a different person. Dry-run rehearsal (above) and
        # compensating rollbacks (already drift-checked and linked to an
        # approved original) are exempt so an urgent undo is never blocked.
        four_eyes: dict[str, Any] | None = None
        if self.approval_required(plan.action) and not rollback_of:
            if _preapproved is not None:
                four_eyes = _preapproved
            else:
                four_eyes = self._check_approval(
                    content_hash=self.content_hash(plan),
                    approval=approval,
                    request_id=approval.request_id if approval else None,
                )

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

        validation: dict[str, Any] = {
            "after_state_captured": bool(after_state),
            "result_matches_intent": envelope.success,
        }
        if four_eyes is not None:
            validation["four_eyes"] = four_eyes
            if reason is not None and not reason.approval_ref:
                reason = replace(reason, approval_ref=four_eyes["request_id"])
            if envelope.success and _preapproved is None:
                self.approvals.set_status(four_eyes["request_id"], APPLIED)
        self._audit_write(
            plan, envelope, reason,
            event_type=event_type,
            after_state=after_state,
            snapshot_id=snapshot_id,
            rollback_of=rollback_of,
            validation=validation,
        )
        log.info("write %s via %s -> success=%s op=%s",
                 plan.action.id, envelope.provider, envelope.success, plan.operation_id)
        return envelope

    # ------------------------------------------------------------------
    # Bulk pipeline (PRD W2 / F-UX-1): each object gets its own full plan,
    # snapshot and audit event, so per-object rollback still holds.
    # ------------------------------------------------------------------

    async def plan_bulk(
        self,
        action_id: str,
        items: list[dict[str, Any]],
    ) -> BulkPlan:
        action = self.actions.get(action_id)
        if not action.is_write:
            raise PipelineError(f"{action_id} is read-only; bulk applies to write actions")
        if not items:
            raise PipelineError("Bulk plan requires at least one target object")
        plans = [
            await self.plan_write(action_id, item, interaction_mode="bulk")
            for item in items
        ]
        return BulkPlan(action=action, plans=plans, requires_reason=self.config.require_change_reason)

    def bulk_content_hash(self, plan: BulkPlan) -> str:
        return bulk_content_hash(
            plan.action.id, [dict(p.params) for p in plan.plans], self.session.tenant_id
        )

    def find_bulk_approval(self, plan: BulkPlan) -> tuple[ChangeRequest, Approval] | None:
        return self.approvals.find_approved(self.bulk_content_hash(plan))

    async def commit_bulk(
        self,
        plan: BulkPlan,
        *,
        confirmed: bool,
        reason: ChangeReason | None = None,
        approval: Approval | None = None,
    ) -> list[BulkItemResult]:
        if not confirmed:
            raise PipelineError("Bulk write refused: confirmation not given")
        if plan.requires_reason and (reason is None or not reason.reason.strip()):
            raise PipelineError("Bulk write refused: change reason required by policy")
        # One approval covers the whole bulk change (bound to every object's
        # params), verified once, then passed to each item as pre-approved.
        preapproved: dict[str, Any] | None = None
        if self.approval_required(plan.action) and self.config.mode is not Mode.DRY_RUN:
            preapproved = self._check_approval(
                content_hash=self.bulk_content_hash(plan),
                approval=approval,
                request_id=approval.request_id if approval else None,
            )
        results: list[BulkItemResult] = []
        for wp, object_id in zip(plan.plans, plan.object_ids, strict=True):
            envelope = await self.commit_write(
                wp, confirmed=True, reason=reason, _preapproved=preapproved
            )
            results.append(BulkItemResult(object_id, envelope.success, envelope))
        if preapproved is not None:
            self.approvals.set_status(preapproved["request_id"], APPLIED)
        succeeded = sum(1 for r in results if r.success)
        log.info("bulk %s -> %d/%d succeeded", plan.action.id, succeeded, len(results))
        return results

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
