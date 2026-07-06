"""Graph SDK engine — interface-complete stub (implementation post-MVP).

The engine exists so that the provider abstraction, capability matrix and
selection logic treat graph_sdk as a first-class provider id today. It reports
itself unavailable, so selection falls through to graph_rest (which covers the
same surface). See docs/capability-matrix.md.
"""

from __future__ import annotations

from typing import Any

from ..core.actions import ActionDefinition
from ..core.envelope import ResultEnvelope, failure
from ..core.errors import ErrorCategory, NormalizedError, with_guidance
from ..core.providers import GRAPH_SDK, OperationPreview, Provider


class GraphSdkProvider(Provider):
    name = GRAPH_SDK
    display_name = "Microsoft Graph SDK (Python)"

    def is_available(self) -> bool:
        return False

    def availability_detail(self) -> str:
        return "not implemented in this release — graph_rest provides the same coverage"

    def preview(self, action: ActionDefinition, params: dict[str, Any]) -> OperationPreview:
        op = action.sdk_operation or f"graph_client.{action.id.replace('.', '_')}(...)"
        return OperationPreview(
            provider=self.name,
            summary=op,
            detail=op,
            required_scopes=action.graph_scopes,
            required_roles=action.admin_roles,
            notes=["Graph SDK engine is a stub in this release."],
        )

    async def execute(self, action: ActionDefinition, params: dict[str, Any]) -> ResultEnvelope:
        return failure(
            self.name,
            action.id,
            with_guidance(NormalizedError(
                ErrorCategory.PROVIDER_UNAVAILABLE,
                "The Graph SDK engine is not implemented yet; use graph_rest.",
            )),
        )
