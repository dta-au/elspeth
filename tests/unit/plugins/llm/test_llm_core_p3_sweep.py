"""Regression tests for the 20261111-plugins-review P3 LLM-core sweep findings."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Any
from unittest.mock import Mock

import pytest

from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.results import TransformResult
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.token_usage import TokenUsage
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.templates import ALL_FIELDS
from elspeth.plugins.transforms.llm.base import LLMConfig
from elspeth.plugins.transforms.llm.multi_query import OutputFieldConfig, QuerySpec
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.templates import PromptTemplate
from elspeth.plugins.transforms.llm.transform import MultiQueryStrategy, SingleQueryStrategy
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


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


class _SilentTracer:
    def record_success(self, **kwargs: Any) -> None:
        return None

    def record_error(self, **kwargs: Any) -> None:
        return None


def _identity_contract(contract: SchemaContract) -> SchemaContract:
    return contract


def _identity_row(row: PipelineRow) -> PipelineRow:
    return row


def _served_model_provider(content: str) -> Mock:
    provider = Mock(spec=LLMProvider)
    provider.execute_query.return_value = LLMQueryResult(
        content=content,
        usage=TokenUsage.known(prompt_tokens=10, completion_tokens=5),
        model="served-model",
        finish_reason=FinishReason.STOP,
    )
    return provider


def _strategy_context() -> PluginContext:
    return make_context(
        state_id="state-45",
        run_id="run-45",
        token=TokenInfo(row_id="row-1", token_id="token-1", row_data=make_pipeline_row({})),
    )


def _fields_written(result: TransformResult, input_row: dict[str, Any]) -> set[str]:
    assert result.row is not None
    return set(result.row.to_dict()) - set(input_row)


class TestFinding45SuccessReasonFieldInventoryAndModel:
    """Sweep finding #45: fields_added must list every written field; model keys must mean the same in both modes."""

    def _single_result(self) -> TransformResult:
        strategy = SingleQueryStrategy(
            template=PromptTemplate("Classify: {{ row.text }}"),
            row_projection=ALL_FIELDS,
            system_prompt=None,
            system_prompt_source=None,
            model="configured-model",
            temperature=0.0,
            max_tokens=None,
            response_field="llm_response",
            align_output_contract=_identity_contract,
            apply_declared_output_field_contracts=_identity_contract,
            output_fields=(OutputFieldConfig(suffix="score", type="integer"),),
        )
        return strategy.execute(
            make_pipeline_row({"text": "hello"}),
            _strategy_context(),
            provider=_served_model_provider('{"score": 7}'),
            tracer=_SilentTracer(),
        )

    def _multi_result(self) -> TransformResult:
        strategy = MultiQueryStrategy(
            query_specs=[QuerySpec(name="sentiment", input_fields=MappingProxyType({"text": "text"}))],
            template=PromptTemplate("Analyze: {{ row.text }}"),
            row_projection=ALL_FIELDS,
            system_prompt=None,
            system_prompt_source=None,
            model="configured-model",
            temperature=0.0,
            max_tokens=None,
            response_field="llm_response",
            align_output_contract=_identity_contract,
            align_output_row_contract=_identity_row,
            apply_declared_output_field_contracts=_identity_contract,
            executor=None,
        )
        return strategy.execute(
            make_pipeline_row({"text": "hello"}),
            _strategy_context(),
            provider=_served_model_provider("positive"),
            tracer=_SilentTracer(),
        )

    def test_single_mode_fields_added_lists_every_written_field(self) -> None:
        result = self._single_result()
        assert result.success_reason is not None
        written = _fields_written(result, {"text": "hello"})
        assert written == {"llm_response", "llm_response_usage", "llm_response_model", "score"}
        assert set(result.success_reason["fields_added"]) == written

    def test_single_mode_metadata_model_is_the_configured_model_and_served_model_is_separate(self) -> None:
        result = self._single_result()
        assert result.success_reason is not None
        metadata = result.success_reason["metadata"]
        assert metadata["model"] == "configured-model"
        assert "served_model" in metadata, sorted(metadata)
        assert metadata["served_model"] == "served-model"

    def test_multi_mode_metadata_records_the_served_model_per_query(self) -> None:
        result = self._multi_result()
        assert result.success_reason is not None
        metadata = result.success_reason["metadata"]
        assert metadata["model"] == "configured-model"
        assert "sentiment_served_model" in metadata, sorted(metadata)
        assert metadata["sentiment_served_model"] == "served-model"
        assert set(result.success_reason["fields_added"]) == _fields_written(result, {"text": "hello"})
