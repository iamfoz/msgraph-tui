"""Declarative action registry (PRD §13).

Every administrative capability in Graphdeck is an ActionDefinition registered
here. UI screens, provider engines, the audit service and the rollback service
all consume this metadata — nothing about an action's behaviour is hard-coded
into a screen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import RegistryError


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"

    @property
    def is_write(self) -> bool:
        return self is not RiskLevel.READ_ONLY


class ActionTag(str, Enum):
    PRIVILEGED = "privileged"
    BULK = "bulk"
    COMPLIANCE_SENSITIVE = "compliance_sensitive"


class Confirmation(str, Enum):
    NONE = "none"          # read-only actions
    CONFIRM = "confirm"    # single confirmation dialog
    TYPED = "typed"        # user must type a confirmation phrase


class RollbackLevel(str, Enum):
    NONE = "none"
    MANUAL_GUIDANCE = "manual_guidance"
    PARTIAL = "partial"
    FULL = "full"
    SNAPSHOT_RESTORE = "snapshot_restore"
    RECREATE = "recreate"


class AuditRequirement(str, Enum):
    READ_LOG = "read_log"      # logged as a read event
    CHANGE_LOG = "change_log"  # full change record with before/after


class ViewType(str, Enum):
    TABLE = "table"
    DETAIL = "detail"
    FORM = "form"


@dataclass
class ParamSpec:
    name: str
    type: str = "string"  # string | int | bool | string_array | choice
    required: bool = False
    default: Any = None
    description: str = ""
    choices: list[str] | None = None
    secret: bool = False  # never echoed, never logged

    def coerce(self, value: Any) -> Any:
        if value is None:
            return self.default
        if self.type == "int":
            return int(value)
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if self.type == "string_array":
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return [str(v) for v in value]
        if self.type == "choice":
            value = str(value)
            if self.choices and value not in self.choices:
                raise ValueError(
                    f"{self.name!r} must be one of {self.choices}, got {value!r}"
                )
            return value
        return str(value)


@dataclass
class GraphTemplate:
    """Template for a Microsoft Graph REST call.

    ``path`` may contain ``{param}`` placeholders; each segment value is
    URL-quoted at build time. ``query``/``body`` values may reference params
    with ``"{param}"`` string placeholders (whole-value substitution).
    """

    method: str
    path: str
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    beta: bool = False
    paginate: bool = True


@dataclass
class PSParamSpec:
    name: str                 # PowerShell parameter name, e.g. "UserId"
    source: str | None = None  # action param that supplies the value (None = literal)
    literal: Any = None
    switch: bool = False       # emit as -Name with no value


@dataclass
class PowerShellTemplate:
    module: str
    command: str
    parameters: list[PSParamSpec] = field(default_factory=list)
    select: list[str] | None = None      # piped through Select-Object
    supports_whatif: bool = False
    json_depth: int = 5


@dataclass
class RollbackSpec:
    """How to capture before-state and build a compensating change."""

    level: RollbackLevel
    # Read-only action used to capture before/current state of the object.
    read_action: str | None = None
    # Maps read-action param name -> write-action param name supplying the value.
    read_param_map: dict[str, str] = field(default_factory=dict)
    # Fields of the object considered "owned" by this change (drift detection).
    tracked_fields_from: str | None = None  # param whose keys list changed fields
    tracked_fields: list[str] = field(default_factory=list)
    # Builds (action_id, params) that reverses the change. Receives the original
    # write params and captured before-state.
    build_inverse: Callable[[dict, dict], tuple[str, dict]] | None = None
    # Fields that can never be restored automatically.
    non_restorable_fields: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class ActionDefinition:
    id: str
    name: str
    description: str
    service: str                                  # e.g. "Entra ID", "Exchange Online"
    preferred_provider: str
    supported_providers: list[str]
    risk: RiskLevel = RiskLevel.READ_ONLY
    tags: set[ActionTag] = field(default_factory=set)
    params: list[ParamSpec] = field(default_factory=list)
    graph: GraphTemplate | None = None
    powershell: PowerShellTemplate | None = None
    sdk_operation: str | None = None
    graph_scopes: list[str] = field(default_factory=list)
    admin_roles: list[str] = field(default_factory=list)
    ps_module: str | None = None
    app_only_supported: bool = True
    delegated_required: bool = False
    beta_required: bool = False
    supports_dry_run: bool = True
    rollback: RollbackSpec = field(default_factory=lambda: RollbackSpec(RollbackLevel.NONE))
    audit: AuditRequirement = AuditRequirement.READ_LOG
    confirmation: Confirmation = Confirmation.NONE
    selection_rule: str = "prefer_preferred_then_available"
    view: ViewType = ViewType.TABLE
    output_schema: dict[str, str] = field(default_factory=dict)  # column -> label
    docs_url: str = "https://learn.microsoft.com/graph/"  # placeholder ok
    # Optional post-parse hook: (data) -> data
    output_transform: Callable[[Any], Any] | None = None
    # Optional hook adding derived params after validation (e.g. member $ref
    # URLs computed from a user_id). Receives validated params, returns extras.
    derive_params: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def is_write(self) -> bool:
        return self.risk.is_write

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.params if p.name == name), None)

    def validate_params(self, values: dict[str, Any]) -> dict[str, Any]:
        """Coerce and validate user-supplied params against the specs."""
        known = {p.name for p in self.params}
        internal = {k for k in values if k.startswith("_")}  # e.g. _simulate
        unknown = set(values) - known - internal
        if unknown:
            raise ValueError(f"Unknown parameter(s) for {self.id}: {sorted(unknown)}")
        out: dict[str, Any] = {k: values[k] for k in internal}
        for spec in self.params:
            raw = values.get(spec.name, None)
            coerced = spec.coerce(raw)
            if spec.required and coerced in (None, "", []):
                raise ValueError(f"Parameter {spec.name!r} is required for {self.id}")
            if coerced is not None:
                out[spec.name] = coerced
        return out


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, ActionDefinition] = {}

    def register(self, action: ActionDefinition) -> ActionDefinition:
        self._validate(action)
        self._actions[action.id] = action
        return action

    def _validate(self, action: ActionDefinition) -> None:
        if action.id in self._actions:
            raise RegistryError(f"Duplicate action id: {action.id}")
        if not action.supported_providers:
            raise RegistryError(f"{action.id}: no supported providers declared")
        if action.preferred_provider not in action.supported_providers:
            raise RegistryError(
                f"{action.id}: preferred provider {action.preferred_provider!r} "
                f"not in supported providers {action.supported_providers}"
            )
        if action.is_write:
            if action.confirmation is Confirmation.NONE:
                raise RegistryError(
                    f"{action.id}: write actions must require confirmation"
                )
            if action.audit is not AuditRequirement.CHANGE_LOG:
                raise RegistryError(
                    f"{action.id}: write actions must use CHANGE_LOG auditing"
                )
            # rollback level must be an explicit decision, even if NONE — the
            # default RollbackSpec(NONE) with no notes on a destructive action
            # is suspicious, so require a note explaining why.
            if (
                action.risk in (RiskLevel.HIGH, RiskLevel.DESTRUCTIVE)
                and action.rollback.level is RollbackLevel.NONE
                and not action.rollback.notes
            ):
                raise RegistryError(
                    f"{action.id}: high-risk/destructive actions must document why "
                    "rollback is unavailable (RollbackSpec.notes)"
                )
        if action.graph is None and action.powershell is None and action.sdk_operation is None:
            raise RegistryError(f"{action.id}: no provider template declared")
        seen: set[str] = set()
        for p in action.params:
            if p.name in seen:
                raise RegistryError(f"{action.id}: duplicate param {p.name!r}")
            seen.add(p.name)
            if p.type not in ("string", "int", "bool", "string_array", "choice"):
                raise RegistryError(f"{action.id}: param {p.name!r} has unknown type {p.type!r}")
            if p.type == "choice" and not p.choices:
                raise RegistryError(f"{action.id}: choice param {p.name!r} needs choices")

    def get(self, action_id: str) -> ActionDefinition:
        try:
            return self._actions[action_id]
        except KeyError:
            raise RegistryError(f"Unknown action: {action_id}") from None

    def all(self) -> list[ActionDefinition]:
        return list(self._actions.values())

    def by_service(self, service: str) -> list[ActionDefinition]:
        return [a for a in self._actions.values() if a.service == service]

    def __contains__(self, action_id: str) -> bool:
        return action_id in self._actions

    def __len__(self) -> int:
        return len(self._actions)
