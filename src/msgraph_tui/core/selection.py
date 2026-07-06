"""Provider selection (PRD §9).

Returns not only the chosen provider but the reasoning trail, which the UI
surfaces in the preview panel so administrators always know *why* a provider
was picked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions import ActionDefinition
from .config import AppConfig, Mode
from .errors import SelectionError
from .providers import MOCK, Provider, ProviderRegistry


@dataclass
class SelectionResult:
    provider: Provider
    reasons: list[str] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "provider: reason"


def select_provider(
    action: ActionDefinition,
    config: AppConfig,
    providers: ProviderRegistry,
) -> SelectionResult:
    reasons: list[str] = []
    skipped: list[str] = []

    # Mock/dry-run modes force the mock engine — the pipeline still runs fully.
    if config.mode in (Mode.MOCK, Mode.DRY_RUN):
        mock = providers.get(MOCK)
        if mock is None or not mock.is_available():
            raise SelectionError("Mock provider unavailable in mock/dry-run mode")
        reasons.append(f"{config.mode.value} mode forces the mock provider")
        return SelectionResult(mock, reasons, alternatives=action.supported_providers, skipped=skipped)

    candidates: list[str] = []
    preference = config.provider_preference_for(action.id, action.service)
    if preference:
        if preference in action.supported_providers:
            candidates.append(preference)
            reasons.append(f"user preference ({preference}) for {action.id}/{action.service}")
        else:
            skipped.append(f"{preference}: user-preferred but not supported by this action")
    candidates.append(action.preferred_provider)
    candidates.extend(p for p in action.supported_providers if p not in candidates)

    for name in candidates:
        provider = providers.get(name)
        if provider is None:
            skipped.append(f"{name}: no engine registered")
            continue
        if action.beta_required and not config.allow_beta and name in ("graph_rest", "graph_sdk"):
            skipped.append(f"{name}: requires beta Graph endpoints (disabled in config)")
            continue
        if not provider.is_available():
            skipped.append(f"{name}: {provider.availability_detail()}")
            continue
        if name == action.preferred_provider and not reasons:
            reasons.append(f"action's preferred provider ({name}) is available")
        elif not reasons or reasons[-1].startswith("user preference") is False:
            reasons.append(f"selected {name} (first available supported provider)")
        alternatives = [p for p in action.supported_providers if p != name]
        return SelectionResult(provider, reasons, alternatives, skipped)

    raise SelectionError(
        f"No usable provider for {action.id}. "
        + ("; ".join(skipped) if skipped else "No providers registered.")
    )
