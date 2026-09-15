"""Task I1 run adapter: a run's Landscape LLM calls become token-ledger entries on every terminal path."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.schema import calls_table
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.execution import service as execution_service
from elspeth.web.execution.service import _run_token_usage_entries
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db

T0 = datetime(2026, 9, 13, 23, 59, 58, tzinfo=UTC)


def test_run_entries_are_the_llm_calls_in_creation_order_with_their_node_model() -> None:
    """Completion audit timestamps retain their actual UTC day across midnight."""
    db = make_landscape_db()
    factory = make_factory(db)
    schema = SchemaConfig.from_dict({"mode": "observed"})
    run_id = "run-token-ledger"
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)
    coordination = leader_coordination_token(factory, run_id)
    source = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="inline_blob",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        schema_config=schema,
    )
    openai = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="llm",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={"model": "openai/run-model"},
        schema_config=schema,
    )
    azure = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="azure_llm",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={"model": None, "deployment_name": "azure-deployment"},
        schema_config=schema,
    )
    _row, token = factory.data_flow.create_row_with_token(
        coordination_token=coordination,
        source_node_id=source.node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"text": "x"},
    )
    openai_state = factory.execution.record_completed_node_state(
        token_id=token.token_id,
        node_id=openai.node_id,
        coordination_token=coordination,
        step_index=1,
        input_data={"text": "x"},
        output_data={"label": 1},
        duration_ms=1.0,
    )
    azure_state = factory.execution.record_completed_node_state(
        token_id=token.token_id,
        node_id=azure.node_id,
        coordination_token=coordination,
        step_index=2,
        input_data={"text": "x"},
        output_data={"label": 2},
        duration_ms=1.0,
    )
    operation = factory.execution.begin_operation(coordination_token=coordination, node_id=source.node_id, operation_type="source_load")

    def insert_call(
        call_id: str,
        *,
        offset: int,
        call_type: CallType = CallType.LLM,
        status: CallStatus = CallStatus.SUCCESS,
        state_id: str | None = None,
        operation_id: str | None = None,
        prompt: int | None = None,
        completion: int | None = None,
    ) -> None:
        with db.write_connection() as conn:
            conn.execute(
                calls_table.insert().values(
                    call_id=call_id,
                    state_id=state_id,
                    operation_id=operation_id,
                    call_index=offset,
                    call_type=call_type.value,
                    status=status.value,
                    request_hash=f"{call_id}-request",
                    created_at=T0 + timedelta(seconds=offset),
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )

    insert_call("c5-azure-reported-error", offset=5, status=CallStatus.ERROR, state_id=azure_state.state_id, prompt=3, completion=0)
    insert_call("c1-openai", offset=1, state_id=openai_state.state_id, prompt=10, completion=5)
    insert_call("c2-transport", offset=2, call_type=CallType.HTTP, state_id=openai_state.state_id, prompt=99, completion=99)
    insert_call("c3-openai-unreported-error", offset=3, status=CallStatus.ERROR, state_id=openai_state.state_id)
    insert_call("c4-operation-unreported", offset=4, operation_id=operation.operation_id)

    assert _run_token_usage_entries(db, landscape_run_id=run_id) == (
        TokenUsageEntry(
            model="openai/run-model",
            prompt_tokens=10,
            completion_tokens=5,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            call_id="c1-openai",
            recorded_at=T0 + timedelta(seconds=1),
        ),
        TokenUsageEntry(
            model="openai/run-model",
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            call_id="c3-openai-unreported-error",
            recorded_at=T0 + timedelta(seconds=3),
        ),
        TokenUsageEntry(
            model="inline_blob",
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            call_id="c4-operation-unreported",
            recorded_at=T0 + timedelta(seconds=4),
        ),
        TokenUsageEntry(
            model="azure-deployment",
            prompt_tokens=3,
            completion_tokens=0,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            call_id="c5-azure-reported-error",
            recorded_at=T0 + timedelta(seconds=5),
        ),
    )


def test_every_terminal_run_path_charges_the_run_token_ledger() -> None:
    """Result, graceful shutdown and exception recovery each charge the run's calls (sso-design.md:1371)."""
    tree = ast.parse(Path(execution_service.__file__).read_text(encoding="utf-8"))
    impl = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionServiceImpl")
    methods = {node.name: node for node in impl.body if isinstance(node, ast.FunctionDef)}

    def charges(name: str) -> int:
        return sum(
            1
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_record_run_token_usage"
        )

    assert (charges("_run_pipeline"), charges("_persist_failed_run_status")) == (2, 1)
