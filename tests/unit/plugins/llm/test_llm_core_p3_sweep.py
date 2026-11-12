"""Regression tests for the 20261111-plugins-review P3 LLM-core sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.llm.base import LLMConfig


def _raised_by(call: Callable[[], object]) -> BaseException | None:
    try:
        call()
    except Exception as exc:
        return exc
    return None


def _llm_config_dict(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "provider": "azure",
        "schema": {"mode": "observed"},
        "prompt_template": "hi",
    }
    config.update(overrides)
    return config


class TestFinding32ContradictoryPoolDelayKnobs:
    """Sweep finding #32: min_dispatch_delay_ms > max_dispatch_delay_ms must fail at config time."""

    @pytest.mark.parametrize("pool_size", [1, 4])
    def test_from_dict_refuses_min_delay_above_max_delay(self, pool_size: int) -> None:
        raised = _raised_by(
            lambda: LLMConfig.from_dict(
                _llm_config_dict(pool_size=pool_size, min_dispatch_delay_ms=9000, max_dispatch_delay_ms=10),
            )
        )
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError at from_dict, got {raised!r}"
        assert "min_dispatch_delay_ms" in str(raised)

    def test_pooled_config_refuses_the_other_pool_invariants_at_from_dict(self) -> None:
        raised = _raised_by(
            lambda: LLMConfig.from_dict(
                _llm_config_dict(pool_size=4, min_dispatch_delay_ms=0, recovery_step_ms=0),
            )
        )
        assert isinstance(raised, PluginConfigError), f"expected PluginConfigError at from_dict, got {raised!r}"
        assert "recovery_step_ms" in str(raised)

    def test_consistent_pool_knobs_still_build_a_pool_config(self) -> None:
        config = LLMConfig.from_dict(_llm_config_dict(pool_size=4, min_dispatch_delay_ms=10, max_dispatch_delay_ms=9000))
        pool = config.pool_config
        assert pool is not None
        assert pool.min_dispatch_delay_ms == 10
        assert pool.max_dispatch_delay_ms == 9000
