"""Admin modules: each module contributes ActionDefinitions to the registry."""

from __future__ import annotations

from ..core.actions import ActionRegistry
from . import groups, licenses, users


def build_registry() -> ActionRegistry:
    registry = ActionRegistry()
    for module in (users, groups, licenses):
        module.register(registry)
    return registry
