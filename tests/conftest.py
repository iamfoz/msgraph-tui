from __future__ import annotations

import pytest

from msgraph_tui.core.config import AppConfig, Mode
from msgraph_tui.services.context import AppContext, build_context


@pytest.fixture
def mock_ctx(tmp_path) -> AppContext:
    """A fully wired application context in mock mode with isolated state."""
    config = AppConfig(mode=Mode.MOCK, state_dir=tmp_path / "state")
    return build_context(config)


@pytest.fixture
def dry_run_ctx(tmp_path) -> AppContext:
    config = AppConfig(mode=Mode.DRY_RUN, state_dir=tmp_path / "state")
    return build_context(config)
