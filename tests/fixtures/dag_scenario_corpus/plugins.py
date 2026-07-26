"""Plugin registry and deterministic recovery transform for the DAG corpus."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from elspeth.contracts import Determinism, PipelineRow, PluginSchema
from elspeth.contracts.errors import TransformSuccessReason
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sinks._diversion_attribution import build_diversion_attribution
from elspeth.plugins.sinks._local_file_effects import prepare_local_effect
from elspeth.plugins.sinks.json_sink import JSONSink


class CorpusInputSchema(PluginSchema):
    id: int
    value: int


class CorpusOutputSchema(PluginSchema):
    value: int
    count: int


class CorpusAlwaysErrorTransform(BaseTransform):
    name = "dag_corpus_always_error"
    determinism = Determinism.DETERMINISTIC
    input_schema = CorpusInputSchema
    output_schema = CorpusInputSchema
    on_error = "discard"

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        del row, ctx
        return TransformResult.error(
            {
                "reason": "invalid_input",
                "error": "injected DAG corpus routed error",
            },
            retryable=False,
        )


class CorpusAlwaysFailSink(JSONSink):
    """Corpus JSON sink that deterministically diverts every member."""

    name = "dag_corpus_always_fail_sink"
    determinism = Determinism.IO_WRITE

    def prepare_effect(self, request: SinkEffectPrepareRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectPlan:
        if type(request.effect_input) is not SinkEffectPipelineMembersInput:
            return super().prepare_effect(request, ctx)
        del ctx
        reason = "injected DAG corpus terminal sink failure"
        members = request.effect_input.members
        diverted = tuple(member.ordinal for member in members)
        for member in members:
            self._divert_row(deep_thaw(member.row), row_index=member.ordinal, reason=reason)
        return prepare_local_effect(
            effect_id=request.effect_id,
            input_kind=request.input_kind,
            inspection=request.inspection,
            chunks=(),
            row_count=len(members),
            accepted_ordinals=(),
            diverted_ordinals=diverted,
            encoding="utf-8",
            format_name="jsonl",
            stream_sequence=0,
            diversion_attribution=tuple(build_diversion_attribution(ordinal=ordinal, reason=reason) for ordinal in diverted),
        )


class CorpusFailOnceEOFBatchTransform(BaseTransform):
    name = "dag_corpus_fail_once_eof_batch"
    determinism = Determinism.DETERMINISTIC
    input_schema = CorpusInputSchema
    output_schema = CorpusOutputSchema
    is_batch_aware = True
    on_error = "discard"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        marker = config.get("fault_marker_path")
        if not isinstance(marker, str) or not marker:
            raise ValueError("fault_marker_path must be a non-empty string")
        self._fault_marker = Path(marker)

    def process(self, row: PipelineRow | list[PipelineRow], ctx: Any) -> TransformResult:
        del ctx
        if not isinstance(row, list):
            return TransformResult.success(
                row,
                success_reason=cast(TransformSuccessReason, {"action": "buffer"}),
            )

        self._fault_marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fault_marker.touch(exist_ok=False)
        except FileExistsError:
            pass
        else:
            raise RuntimeError("injected DAG corpus EOF flush crash")

        total = sum(int(member["value"]) for member in row)
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="value",
                    original_name="value",
                    python_type=int,
                    required=False,
                    source="inferred",
                ),
                FieldContract(
                    normalized_name="count",
                    original_name="count",
                    python_type=int,
                    required=False,
                    source="inferred",
                ),
            ),
            locked=True,
        )
        return TransformResult.success(
            PipelineRow({"value": total, "count": len(row)}, contract),
            success_reason=cast(TransformSuccessReason, {"action": "batch_sum"}),
        )


def make_corpus_plugin_manager() -> PluginManager:
    manager = PluginManager()
    manager.register_builtin_plugins()
    manager.register(
        create_dynamic_hookimpl(
            [CorpusAlwaysErrorTransform, CorpusFailOnceEOFBatchTransform],
            "elspeth_get_transforms",
        )
    )
    manager.register(
        create_dynamic_hookimpl(
            [CorpusAlwaysFailSink],
            "elspeth_get_sinks",
        )
    )
    return manager


def install_corpus_plugin_manager(monkeypatch: pytest.MonkeyPatch) -> PluginManager:
    manager = make_corpus_plugin_manager()
    monkeypatch.setattr(
        "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
        lambda: manager,
    )
    return manager
