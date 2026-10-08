"""Transform execution, retry routing, and telemetry tests for RowProcessor.

Shared processor construction and durable token helpers retain their authority
in test_processor; importing those bindings preserves their object identity.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, create_autospec, patch

import pytest

from elspeth.contracts import NodeType, RowResult, TokenInfo, TransformResult
from elspeth.contracts.enums import FrameKind, NodeStateStatus, RoutingMode, RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import (
    CapacityError,
    ExecutionError,
    MaxRetriesExceeded,
    OrchestrationInvariantError,
    PluginContractViolation,
    SinkTransactionalInvariantError,
)
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import BranchName, CoalesceName, NodeID, RowUnionName
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.coalesce_executor import CoalesceExecutor
from elspeth.engine.executors.state_guard import stamp_node_state_id
from elspeth.engine.orchestrator.ports import TelemetryManagerProtocol
from elspeth.engine.processor import RowProcessor, _TransformTerminal
from elspeth.engine.retry import RetryManager
from elspeth.engine.row_union_executor import RowUnionExecutor, RowUnionOutcome
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.testing import make_row, make_token_info
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run
from tests.unit.engine.test_processor import (
    _DYNAMIC_SCHEMA,
    _TEST_LEADER_WORKER_ID,
    _assert_outcome_pair,
    _claim_processor_token,
    _make_claimed_work_item,
    _make_factory,
    _make_mock_transform,
    _make_processor,
    _make_source_row,
    _persist_token_for_scheduler,
    _record_error_sink_outcome,
)

# =============================================================================
# _execute_transform_with_retry: No retry manager
# =============================================================================


class TestExecuteTransformNoRetry:
    """Tests for _execute_transform_with_retry when no retry_manager is set."""

    def _setup(self) -> tuple[LandscapeDB, RecorderFactory, RowProcessor]:
        db, factory = _make_factory()

        processor = _make_processor(factory, retry_manager=None)
        return db, factory, processor

    def test_single_attempt_success(self) -> None:
        """Without retry_manager, executes single attempt."""
        _, factory, processor = self._setup()
        transform = _make_mock_transform()
        token = make_token_info(data={"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        expected_result = TransformResult.success(
            make_row({"value": 42}),
            success_reason={"action": "test"},
        )

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            return_value=(expected_result, token, None),
        ) as mock_exec:
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )
            mock_exec.assert_called_once()
            assert result.status == "success"
            assert error_sink is None

    def test_retryable_llm_error_with_on_error_discard(self) -> None:
        """Retryable LLMClientError with on_error='discard' returns error result (no re-raise)."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        llm_error = LLMClientError("rate limited", retryable=True)
        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=llm_error,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_retryable_llm_error_on_error_is_always_set(self) -> None:
        """on_error is now required at config time — None no longer reaches runtime.

        Previously on_error=None would raise RuntimeError. Now TransformSettings
        requires on_error, so every transform has a valid error route. This test
        documents the invariant by verifying 'discard' (minimum valid value) works.
        """
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        llm_error = LLMClientError("rate limited", retryable=True)
        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=llm_error,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_non_retryable_llm_error_produces_error_result(self) -> None:
        """Regression: elspeth-27a8298a24 — non-retryable PluginRetryableError
        must produce an error result, not crash the run."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        llm_error = LLMClientError("content policy", retryable=False)
        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=llm_error,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_interrupted_error_converts_to_shutdown_requested_result(self) -> None:
        """InterruptedError from a transform (e.g. a shutdown-cancelled batch
        waiter, elspeth-14571961a6) converts to the row-scoped
        shutdown_requested error result at the PROCESSOR layer — end-state
        parity with the sync path's retry.py InterruptedError convention."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=InterruptedError("Shutdown requested while waiting for token tok-1 (state st-1)"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "shutdown_requested"
        assert result.retryable is False
        assert error_sink == "discard"

    def test_transient_connection_error_with_on_error(self) -> None:
        """ConnectionError with on_error returns error result (no re-raise)."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=ConnectionError("connection reset"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_transient_timeout_error_with_on_error(self) -> None:
        """TimeoutError with on_error returns error result."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=TimeoutError("timed out"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_capacity_error_with_on_error_returns_row_scoped_error(self) -> None:
        """CapacityError with no retry manager returns retryable row error."""
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=CapacityError(429, "rate limited"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert result.retryable is True
        assert error_sink == "discard"

    def test_transient_error_on_error_is_always_set(self) -> None:
        """on_error is now required at config time — None no longer reaches runtime.

        Previously on_error=None would raise RuntimeError on transient errors.
        Now TransformSettings requires on_error, so every transform has a valid
        error route. This test documents the invariant.
        """
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=ConnectionError("connection reset"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "discard"

    def test_retryable_llm_error_with_named_error_sink_returns_error_sink(self) -> None:
        """Retryable LLMClientError with on_error pointing to real sink returns that sink."""
        _, factory, processor = self._setup()
        # Set up error edge so the routing path is reachable
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}

        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        ctx = make_context(state_id="state-123")

        llm_error = LLMClientError("rate limited", retryable=True)
        # The real executor stamps the failed state id on the exception via
        # NodeStateGuard; simulate that for the patched executor.
        stamp_node_state_id(llm_error, "state-123")
        # Mock record_routing_event since we don't have a real state_id in DB
        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=llm_error),
            patch.object(factory.execution, "record_routing_event"),
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "error-sink"

    def test_retryable_exception_text_is_scrubbed_before_audit_routing_and_export(self, tmp_path: Any) -> None:
        """Retryable exception conversion must not persist raw provider secrets."""
        from elspeth.core.landscape.exporter import LandscapeExporter
        from elspeth.core.payload_store import FilesystemPayloadStore
        from tests.fixtures.landscape import register_test_node

        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        setup = make_recorder_with_run(
            run_id="run-secret-scrub",
            source_node_id="source-0",
            source_plugin_name="csv",
            payload_store=payload_store,
            leader_worker_id=_TEST_LEADER_WORKER_ID,
        )
        factory = setup.factory
        processor = _make_processor(
            factory,
            run_id=setup.run_id,
            retry_manager=None,
            node_step_map={NodeID("t1"): 1},
        )
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        register_test_node(factory.data_flow, setup.run_id, "error-sink", node_type=NodeType.SINK, plugin_name="csv")
        factory.data_flow.register_edge(
            from_node_id="t1",
            to_node_id="error-sink",
            label="__error_0__",
            mode=RoutingMode.DIVERT,
            edge_id="error-edge-1",
            coordination_token=leader_coordination_token(factory, setup.run_id),
        )
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(
            factory,
            token,
            run_id=setup.run_id,
            source_node_id=setup.source_node_id,
        )
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id="t1",
            step_index=0,
            input_data=token.row_data.to_dict(),
            state_id="state-retryable-secret",
            member_token=leader_coordination_token(factory, setup.run_id).membership,
        )
        ctx = make_context(
            run_id=setup.run_id,
            state_id="state-retryable-secret",
            token=token,
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, setup.run_id),
            work_item=_claim_processor_token(factory, token, node_id="t1", run_id=setup.run_id),
        )
        raw_secret = "https://blob.example/path?sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"

        # The real executor stamps the failed state id on the exception via
        # NodeStateGuard; simulate that for the patched executor.
        retryable_error = ConnectionError(f"provider retry failed: {raw_secret}")
        stamp_node_state_id(retryable_error, "state-retryable-secret")
        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=retryable_error,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["error"] == "<redacted-secret>"
        assert error_sink == "error-sink"

        transform_errors = factory.data_flow.get_transform_errors_for_run(setup.run_id)
        assert len(transform_errors) == 1
        transform_error_payload = json.loads(transform_errors[0].error_details_json)
        assert transform_error_payload["error"] == "<redacted-secret>"

        routing_events = factory.query.get_all_routing_events_for_run(setup.run_id)
        assert len(routing_events) == 1
        assert routing_events[0].reason_ref is not None
        routing_reason_payload = json.loads(payload_store.retrieve(routing_events[0].reason_ref).decode("utf-8"))
        assert routing_reason_payload["error"] == "<redacted-secret>"

        # Settle the direct executor's claim before taking an immutable export.
        # The row was routed to its error sink, which this harness never
        # runs; record its outcome and settle the claim TERMINAL as that sink
        # write would (the sibling scheduler test does the same).
        _record_error_sink_outcome(factory, token_id=ctx.require_work_item().token_id, run_id=setup.run_id)
        factory.scheduler.mark_terminal(
            member_token=ctx.require_member_token(),
            work_item_id=ctx.require_work_item().work_item_id,
            expected_lease_owner=ctx.require_member_token().worker_id,
        )
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_coordination_token(factory, setup.run_id))
        export_records = list(LandscapeExporter(setup.db, compartment_id="test-compartment").export_run(setup.run_id))
        transform_error_export = next(record for record in export_records if record["record_type"] == "transform_error")
        exported_error_payload = json.loads(transform_error_export["error_details_json"])
        assert exported_error_payload["error"] == "<redacted-secret>"

        durable_text = json.dumps(
            {
                "result": result.reason,
                "transform_error": transform_error_payload,
                "routing_reason": routing_reason_payload,
                "transform_error_export": transform_error_export,
            },
            sort_keys=True,
        )
        assert raw_secret not in durable_text

    def test_returned_transform_error_text_is_scrubbed_before_audit_routing_scheduler_and_export(self, tmp_path: Any) -> None:
        """TransformResult.error(reason['error']) must not persist raw provider secrets."""
        from elspeth.core.landscape.exporter import LandscapeExporter
        from elspeth.core.payload_store import FilesystemPayloadStore
        from tests.fixtures.landscape import register_test_node

        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        setup = make_recorder_with_run(
            run_id="run-returned-error-scrub",
            source_node_id="source-0",
            source_plugin_name="csv",
            payload_store=payload_store,
            leader_worker_id=_TEST_LEADER_WORKER_ID,
        )
        factory = setup.factory
        raw_secret = "https://blob.example/path?sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        transform = _make_mock_transform(
            node_id="t1",
            name="llm",
            on_error="error-sink",
            result=TransformResult.error({"reason": "api_error", "error": f"provider failed: {raw_secret}"}),
        )
        transform._on_start_called = True
        register_test_node(factory.data_flow, setup.run_id, "t1", node_type=NodeType.TRANSFORM, plugin_name="llm")
        register_test_node(factory.data_flow, setup.run_id, "error-sink", node_type=NodeType.SINK, plugin_name="csv")
        factory.data_flow.register_edge(
            from_node_id="t1",
            to_node_id="error-sink",
            label="__error_0__",
            mode=RoutingMode.DIVERT,
            edge_id="error-edge-1",
            coordination_token=leader_coordination_token(factory, setup.run_id),
        )
        processor = _make_processor(
            factory,
            run_id=setup.run_id,
            retry_manager=None,
            edge_map={(NodeID("t1"), "__error_0__"): "error-edge-1"},
            node_step_map={NodeID("source-0"): 0, NodeID("t1"): 1},
            node_to_next={NodeID("source-0"): NodeID("t1"), NodeID("t1"): None},
            node_to_plugin={NodeID("t1"): transform},
            sink_names=frozenset({"error-sink"}),
        )

        results = processor.process_row(
            row_index=0,
            source_row=_make_source_row(),
            transforms=[transform],
            ctx=make_context(
                run_id=setup.run_id,
                landscape=factory.plugin_audit_writer(),
                coordination_token=leader_coordination_token(factory, setup.run_id),
            ),
            source_row_index=0,
            ingest_sequence=0,
        )

        assert len(results) == 1
        result = results[0]
        _assert_outcome_pair(result, TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED)
        assert result.error is not None
        assert "<redacted-secret>" in result.error.message
        assert raw_secret not in result.error.message
        assert processor._sink_emission_from_result(result).error_message == result.error.message

        transform_errors = factory.data_flow.get_transform_errors_for_run(setup.run_id)
        assert len(transform_errors) == 1
        transform_error_payload = json.loads(transform_errors[0].error_details_json)
        assert transform_error_payload == {"reason": "api_error", "error": "<redacted-secret>"}

        routing_events = factory.query.get_all_routing_events_for_run(setup.run_id)
        assert len(routing_events) == 1
        assert routing_events[0].reason_ref is not None
        routing_reason_payload = json.loads(payload_store.retrieve(routing_events[0].reason_ref).decode("utf-8"))
        assert routing_reason_payload == transform_error_payload

        from sqlalchemy import update

        from elspeth.core.landscape.schema import token_work_items_table

        _record_error_sink_outcome(factory, token_id=result.token.token_id, run_id=setup.run_id)
        with setup.db.engine.begin() as conn:
            conn.execute(update(token_work_items_table).where(token_work_items_table.c.run_id == setup.run_id).values(status="terminal"))
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_coordination_token(factory, setup.run_id))
        export_records = list(LandscapeExporter(setup.db, compartment_id="test-compartment").export_run(setup.run_id))
        transform_error_export = next(record for record in export_records if record["record_type"] == "transform_error")
        exported_error_payload = json.loads(transform_error_export["error_details_json"])
        assert exported_error_payload == transform_error_payload

        durable_text = json.dumps(
            {
                "result": result.error.message,
                "pending_error_message": processor._sink_emission_from_result(result).error_message,
                "transform_error": transform_error_payload,
                "routing_reason": routing_reason_payload,
                "transform_error_export": transform_error_export,
            },
            sort_keys=True,
        )
        assert raw_secret not in durable_text

    def test_returned_error_reason_is_scrubbed_before_audit_routing_and_export(self, tmp_path: Any) -> None:
        """TransformResult.error reasons must not persist raw provider secrets."""
        from elspeth.core.landscape.exporter import LandscapeExporter
        from elspeth.core.payload_store import FilesystemPayloadStore
        from tests.fixtures.landscape import register_test_node

        payload_store = FilesystemPayloadStore(tmp_path / "payloads")
        setup = make_recorder_with_run(
            run_id="run-returned-error-scrub",
            source_node_id="source-0",
            source_plugin_name="csv",
            payload_store=payload_store,
            leader_worker_id=_TEST_LEADER_WORKER_ID,
        )
        factory = setup.factory
        register_test_node(factory.data_flow, setup.run_id, "t1", node_type=NodeType.TRANSFORM, plugin_name="llm")
        register_test_node(factory.data_flow, setup.run_id, "error-sink", node_type=NodeType.SINK, plugin_name="csv")
        factory.data_flow.register_edge(
            from_node_id="t1",
            to_node_id="error-sink",
            label="__error_0__",
            mode=RoutingMode.DIVERT,
            edge_id="error-edge-1",
            coordination_token=leader_coordination_token(factory, setup.run_id),
        )
        raw_secret = "https://blob.example/path?sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        api_key = "FAKE_TOKEN_PLACEHOLDER"
        transform = _make_mock_transform(
            node_id="t1",
            on_error="error-sink",
            result=TransformResult.error(
                {
                    "reason": "api_error",
                    "error": f"provider returned {raw_secret}",
                    "message": f"last provider message had {raw_secret}",
                    "provider": "azure",
                    "response": {
                        "headers": {"authorization": f"Bearer {api_key}"},
                        "events": [{"message": f"retry target {raw_secret}"}],
                    },
                }
            ),
        )
        transform._on_start_called = True
        processor = _make_processor(
            factory,
            run_id=setup.run_id,
            retry_manager=None,
            edge_map={(NodeID("t1"), "__error_0__"): "error-edge-1"},
            node_step_map={NodeID("source-0"): 0, NodeID("t1"): 1},
            node_to_next={NodeID("source-0"): NodeID("t1"), NodeID("t1"): None},
            node_to_plugin={NodeID("t1"): transform},
            sink_names=frozenset({"error-sink"}),
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(
            factory,
            token,
            run_id=setup.run_id,
            source_node_id=setup.source_node_id,
        )
        ctx = make_context(
            run_id=setup.run_id,
            token=token,
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, setup.run_id),
            work_item=_claim_processor_token(factory, token, node_id="t1", run_id=setup.run_id),
        )

        result, _out_token, error_sink = processor._execute_transform_with_retry(
            transform=transform,
            token=token,
            ctx=ctx,
            attempt_offset=0,
        )

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "api_error"
        assert result.reason["error"] == "<redacted-secret>"
        assert result.reason["message"] == "<redacted-secret>"
        assert result.reason["provider"] == "azure"
        assert result.reason["response"] == {
            "headers": {"authorization": "<redacted-secret>"},
            "events": [{"message": "<redacted-secret>"}],
        }
        assert error_sink == "error-sink"

        transform_errors = factory.data_flow.get_transform_errors_for_run(setup.run_id)
        assert len(transform_errors) == 1
        transform_error_payload = json.loads(transform_errors[0].error_details_json)
        assert transform_error_payload == result.reason

        from sqlalchemy import select

        from elspeth.core.landscape.schema import node_states_table

        with setup.db.connection() as conn:
            node_state_error_json = conn.execute(
                select(node_states_table.c.error_json)
                .where(node_states_table.c.run_id == setup.run_id)
                .where(node_states_table.c.node_id == "t1")
            ).scalar_one()
        assert node_state_error_json is not None
        node_state_error_payload = json.loads(node_state_error_json)
        assert node_state_error_payload == result.reason

        routing_events = factory.query.get_all_routing_events_for_run(setup.run_id)
        assert len(routing_events) == 1
        assert routing_events[0].reason_ref is not None
        routing_reason_payload = json.loads(payload_store.retrieve(routing_events[0].reason_ref).decode("utf-8"))
        assert routing_reason_payload == result.reason

        # Settle the direct executor's claim before taking an immutable export.
        # The row was routed to its error sink, which this harness never
        # runs; record its outcome and settle the claim TERMINAL as that sink
        # write would (the sibling scheduler test does the same).
        _record_error_sink_outcome(factory, token_id=ctx.require_work_item().token_id, run_id=setup.run_id)
        factory.scheduler.mark_terminal(
            member_token=ctx.require_member_token(),
            work_item_id=ctx.require_work_item().work_item_id,
            expected_lease_owner=ctx.require_member_token().worker_id,
        )
        factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_coordination_token(factory, setup.run_id))
        export_records = list(LandscapeExporter(setup.db, compartment_id="test-compartment").export_run(setup.run_id))
        transform_error_export = next(record for record in export_records if record["record_type"] == "transform_error")
        exported_error_payload = json.loads(transform_error_export["error_details_json"])
        assert exported_error_payload == result.reason

        durable_text = json.dumps(
            {
                "result": result.reason,
                "node_state": node_state_error_payload,
                "transform_error": transform_error_payload,
                "routing_reason": routing_reason_payload,
                "transform_error_export": transform_error_export,
            },
            sort_keys=True,
        )
        assert raw_secret not in durable_text
        assert api_key not in durable_text

    def test_retryable_llm_error_with_missing_error_edge_raises(self) -> None:
        """Retryable error with named sink but no DIVERT edge → OrchestrationInvariantError."""
        _, _factory, processor = self._setup()
        # No error edges configured
        processor._error_edge_ids = {}

        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        ctx = make_context(state_id="state-123")

        llm_error = LLMClientError("rate limited", retryable=True)
        # Stamp the failed state id (as the real executor's guard would) so
        # the conversion reaches the missing-edge invariant under test.
        stamp_node_state_id(llm_error, "state-123")
        with (
            patch.object(
                processor._transform_executor,
                "execute_transform",
                side_effect=llm_error,
            ),
            pytest.raises(OrchestrationInvariantError, match="no DIVERT edge"),
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

    def test_named_sink_divert_attributes_to_failed_state_not_ctx(self) -> None:
        """The DIVERT routing_event must use the failed attempt's node-state id.

        ctx.state_id is scope-restored by the executor before the exception
        reaches the retryable-error conversion, so it holds a stale previous
        value (or None) here — the id must be read from the exception stamp.
        """
        _, factory, processor = self._setup()
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}

        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        ctx = make_context(state_id="stale-previous-state")

        error = ConnectionError("connection reset")
        stamp_node_state_id(error, "state-attempt-0")

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=error),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "error-sink"
        record_event.assert_called_once()
        assert record_event.call_args.kwargs["state_id"] == "state-attempt-0"

    def test_tier_2_contract_violation_returns_row_scoped_error(self) -> None:
        """A Tier-2 PluginContractViolation is routed, not propagated.

        Regression for elspeth-181db83da7. ADR-008 §"TIER_1 registration is
        load-bearing" states the registration is what stops ``on_error`` from
        absorbing a violation — so the deliberately UNREGISTERED base class
        must reach ``on_error``. It used to escape every conversion clause
        here and abort the whole run with a raw traceback, leaving the row
        uncounted and the error sink unwritten.
        """
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        violation = PluginContractViolation("transform emitted a colliding field")
        stamp_node_state_id(violation, "state-123")
        with patch.object(
            processor._transform_executor,
            "execute_transform",
            side_effect=violation,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert result.retryable is False
        assert error_sink == "discard"
        assert result.reason is not None
        assert result.reason["reason"] == "contract_violation"

    def test_tier_2_contract_violation_reaches_named_error_sink(self) -> None:
        """The routed violation diverts to the configured sink, not to discard."""
        _, factory, processor = self._setup()
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}

        transform = _make_mock_transform(node_id="t1", on_error="quarantine")
        token = make_token_info(data={"value": 42})
        ctx = make_context(state_id="stale-previous-state")

        violation = PluginContractViolation("input validation failed")
        stamp_node_state_id(violation, "state-attempt-0")
        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=violation),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert error_sink == "quarantine"
        record_event.assert_called_once()
        assert record_event.call_args.kwargs["state_id"] == "state-attempt-0"

    def test_tier_1_contract_violation_subclass_still_propagates(self) -> None:
        """The Tier-1 registration must survive the Tier-2 conversion clause.

        ``SinkTransactionalInvariantError`` inherits ``PluginContractViolation``
        but is ``@tier_1_error``-registered precisely so it "must crash, never
        be absorbed by on_error". A conversion keyed on the class rather than
        on tier membership would route it to a quarantine sink — the exact
        failure ADR-008 says the registry exists to prevent.
        """
        _, _factory, processor = self._setup()
        transform = _make_mock_transform(node_id="t1", on_error="quarantine")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with (
            patch.object(
                processor._transform_executor,
                "execute_transform",
                side_effect=SinkTransactionalInvariantError("commit boundary diverged"),
            ),
            pytest.raises(SinkTransactionalInvariantError),
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )


# =============================================================================
# _execute_transform_with_retry: With retry manager
# =============================================================================


class TestExecuteTransformWithRetry:
    """Tests for _execute_transform_with_retry when retry_manager IS configured."""

    def test_exhaustion_returns_named_error_route_with_final_attempt_evidence(self) -> None:
        """Exhausted retryable attempts retain the transform's on_error route.

        Regression for elspeth-454892147c: MaxRetriesExceeded used to escape
        this processor seam before ``on_error`` was returned, so traversal
        could only terminalize the row as FAILURE/UNROUTED.
        """
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        processor = _make_processor(factory, retry_manager=retry_manager)
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        ctx = make_context()
        seen_attempts: list[int] = []

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempt = kwargs["attempt"]
            seen_attempts.append(attempt)
            error = ConnectionError(f"connection reset on attempt {attempt}")
            stamp_node_state_id(error, f"state-attempt-{attempt}")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert seen_attempts == [0, 1]
        assert out_token is token
        assert result.status == "error"
        assert result.reason == {
            "reason": "retry_exhausted",
            "error": "connection reset on attempt 1",
            "attempts": 2,
        }
        assert result.retryable is False
        assert error_sink == "error-sink"
        record_event.assert_called_once()
        assert record_event.call_args.kwargs["state_id"] == "state-attempt-1"

    def test_exhaustion_scrubs_final_error_before_result_and_audit(self) -> None:
        """The underlying final exception is scrubbed before any durable use."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        processor = _make_processor(factory, retry_manager=retry_manager)
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        ctx = make_context()
        raw_secret = "https://blob.example/path?sig=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempt = kwargs["attempt"]
            error = ConnectionError(f"provider failed: {raw_secret}")
            stamp_node_state_id(error, f"state-attempt-{attempt}")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.reason == {
            "reason": "retry_exhausted",
            "error": "<redacted-secret>",
            "attempts": 2,
        }
        assert error_sink == "error-sink"
        assert raw_secret not in str(result.reason)
        assert raw_secret not in str(record_event.call_args.kwargs["reason"])

    def test_exhaustion_with_named_sink_and_missing_state_fails_closed(self) -> None:
        """A named route may not fabricate DIVERT attribution without a state."""
        _, factory = _make_factory()
        retry_manager = Mock(spec=RetryManager)
        retry_manager.execute_with_retry.side_effect = MaxRetriesExceeded(2, ConnectionError("connection reset"))
        processor = _make_processor(factory, retry_manager=retry_manager)
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")

        with pytest.raises(OrchestrationInvariantError, match="state_id is required"):
            processor._execute_transform_with_retry(
                transform=transform,
                token=make_token_info(data={"value": 42}),
                ctx=make_context(),
                attempt_offset=0,
            )

    def test_exhaustion_does_not_misattribute_unstamped_final_attempt(self) -> None:
        """A prior attempt's state cannot stand in for the final failure."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        processor = _make_processor(
            factory,
            retry_manager=RetryManager(
                RuntimeRetryConfig(
                    max_attempts=2,
                    base_delay=0.01,
                    max_delay=0.1,
                    jitter=0.0,
                    exponential_base=2.0,
                )
            ),
        )
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempt = kwargs["attempt"]
            error = ConnectionError(f"connection reset on attempt {attempt}")
            if attempt == 0:
                stamp_node_state_id(error, "state-attempt-0")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            pytest.raises(OrchestrationInvariantError, match="state_id is required"),
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=make_token_info(data={"value": 42}),
                ctx=make_context(),
                attempt_offset=0,
            )

    def test_exhaustion_with_named_sink_and_missing_edge_fails_closed(self) -> None:
        """A named route may not emit half of its required audit pair."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        processor = _make_processor(factory, retry_manager=retry_manager)
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempt = kwargs["attempt"]
            error = ConnectionError("connection reset")
            stamp_node_state_id(error, f"state-attempt-{attempt}")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            pytest.raises(OrchestrationInvariantError, match="no DIVERT edge"),
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=make_token_info(data={"value": 42}),
                ctx=make_context(),
                attempt_offset=0,
            )

    @pytest.mark.parametrize(
        ("on_error", "expected_path", "expected_sink"),
        [
            ("error-sink", TerminalPath.ON_ERROR_ROUTED, "error-sink"),
            ("discard", TerminalPath.QUARANTINED_AT_SOURCE, None),
        ],
    )
    def test_exhaustion_routes_fork_branch_through_existing_loss_seam(
        self,
        on_error: str,
        expected_path: TerminalPath,
        expected_sink: str | None,
    ) -> None:
        """Every exhausted terminal route notifies coalesce instead of hanging."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        coalesce = create_autospec(CoalesceExecutor, instance=True)
        coalesce.notify_branch_lost.return_value = None
        coalesce_name = CoalesceName("merge")
        processor = _make_processor(
            factory,
            retry_manager=retry_manager,
            coalesce_executor=coalesce,
            branch_to_coalesce={BranchName("path_a"): coalesce_name},
            coalesce_node_ids={coalesce_name: NodeID("coalesce::merge")},
        )
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error=on_error)
        factory.data_flow.register_node(
            coordination_token=leader_coordination_token(factory, "test-run"),
            node_id="t1",
            plugin_name="transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=_DYNAMIC_SCHEMA,
        )
        token = make_token_info(
            data={"value": 42}, lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-path_a", member_key="path_a"),)
        )
        _persist_token_for_scheduler(factory, token)

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempt = kwargs["attempt"]
            error = ConnectionError("connection reset")
            stamp_node_state_id(error, f"state-attempt-{attempt}")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            patch.object(factory.execution, "record_routing_event"),
        ):
            outcome = processor._handle_transform_node(
                transform=transform,
                current_token=token,
                ctx=make_context(
                    work_item=_claim_processor_token(factory, token, node_id="t1", run_id="test-run"),
                    landscape=factory.plugin_audit_writer(),
                    coordination_token=leader_coordination_token(factory, "test-run"),
                ),
                node_id=NodeID("t1"),
                child_items=[],
                coalesce_node_id=NodeID("coalesce::merge"),
                coalesce_name=coalesce_name,
                current_on_success_sink="default",
                attempt_offset=0,
            )

        assert isinstance(outcome, _TransformTerminal)
        assert isinstance(outcome.result, RowResult)
        assert (outcome.result.outcome, outcome.result.path, outcome.result.sink_name) == (
            TerminalOutcome.FAILURE,
            expected_path,
            expected_sink,
        )
        coalesce.notify_branch_lost.assert_called_once_with(
            coalesce_name="merge",
            fork_group_id=token.fork_group_id,
            lost_branch=token.branch_name,
            reason="max_retries_exceeded",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        assert len(processor._pending_group_losses) == 1

    def test_retry_configuration_does_not_change_named_error_destination(self) -> None:
        """Retry-off and exhausted-retry paths produce the same route triple."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, no_retry_factory = _make_factory(run_id="run-no-retry")
        no_retry = _make_processor(
            no_retry_factory,
            run_id="run-no-retry",
            retry_manager=None,
        )
        _, retry_factory = _make_factory(run_id="run-with-retry")
        with_retry = _make_processor(
            retry_factory,
            run_id="run-with-retry",
            retry_manager=RetryManager(
                RuntimeRetryConfig(
                    max_attempts=2,
                    base_delay=0.01,
                    max_delay=0.1,
                    jitter=0.0,
                    exponential_base=2.0,
                )
            ),
        )
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})

        def route_triple(processor: RowProcessor, factory: RecorderFactory) -> tuple[TerminalOutcome | None, TerminalPath, str | None]:
            processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}

            def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
                attempt = kwargs["attempt"]
                error = ConnectionError("same connection failure")
                stamp_node_state_id(error, f"state-attempt-{attempt}")
                raise error

            with (
                patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
                patch.object(factory.execution, "record_routing_event"),
            ):
                outcome = processor._handle_transform_node(
                    transform=transform,
                    current_token=token,
                    ctx=make_context(run_id=processor.run_id),
                    node_id=NodeID("t1"),
                    child_items=[],
                    coalesce_node_id=None,
                    coalesce_name=None,
                    current_on_success_sink="default",
                    attempt_offset=0,
                )
            assert isinstance(outcome, _TransformTerminal)
            assert isinstance(outcome.result, RowResult)
            return outcome.result.outcome, outcome.result.path, outcome.result.sink_name

        assert (
            route_triple(no_retry, no_retry_factory)
            == route_triple(with_retry, retry_factory)
            == (
                TerminalOutcome.FAILURE,
                TerminalPath.ON_ERROR_ROUTED,
                "error-sink",
            )
        )

    def test_non_retryable_exception_still_propagates_unchanged(self) -> None:
        """The new exhaustion arm does not intercept non-retryable failures."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        processor = _make_processor(
            factory,
            retry_manager=RetryManager(
                RuntimeRetryConfig(
                    max_attempts=2,
                    base_delay=0.01,
                    max_delay=0.1,
                    jitter=0.0,
                    exponential_base=2.0,
                )
            ),
        )
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        non_retryable = TypeError("plugin contract bug")

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=non_retryable),
            pytest.raises(TypeError, match="plugin contract bug") as exc_info,
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=make_token_info(data={"value": 42}),
                ctx=make_context(),
                attempt_offset=0,
            )

        assert exc_info.value is non_retryable

    def test_delegates_to_retry_manager(self) -> None:
        """With retry_manager, delegates to execute_with_retry."""
        _, factory = _make_factory()

        retry_manager = Mock(spec=RetryManager)
        processor = _make_processor(factory, retry_manager=retry_manager)

        expected = (
            TransformResult.success(make_row({"value": 42}), success_reason={"action": "test"}),
            make_token_info(data={"value": 42}),
            None,
        )
        retry_manager.execute_with_retry.return_value = expected

        transform = _make_mock_transform()
        token = make_token_info(data={"value": 42})
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        result = processor._execute_transform_with_retry(
            transform=transform,
            token=token,
            ctx=ctx,
            attempt_offset=0,
        )

        retry_manager.execute_with_retry.assert_called_once()
        assert result == expected

    def test_is_retryable_accepts_retryable_llm_error(self) -> None:
        """is_retryable callback returns True for retryable LLMClientError."""
        _, factory = _make_factory()

        retry_manager = Mock(spec=RetryManager)
        processor = _make_processor(factory, retry_manager=retry_manager)

        # Capture the is_retryable callback
        retry_manager.execute_with_retry.return_value = (
            TransformResult.success(make_row({}), success_reason={"action": "t"}),
            make_token_info(),
            None,
        )

        transform = _make_mock_transform()
        token = make_token_info()
        ctx = make_context(
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
        )

        processor._execute_transform_with_retry(
            transform=transform,
            token=token,
            ctx=ctx,
            attempt_offset=0,
        )

        # Extract the is_retryable callback from the call
        call_kwargs = retry_manager.execute_with_retry.call_args
        is_retryable = call_kwargs.kwargs.get("is_retryable") or call_kwargs[1].get("is_retryable")
        if is_retryable is None:
            # Might be positional
            is_retryable = call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs.kwargs["is_retryable"]

        # Verify retryable logic
        assert is_retryable(LLMClientError("rate limit", retryable=True)) is True
        assert is_retryable(LLMClientError("content policy", retryable=False)) is False
        assert is_retryable(ConnectionError("conn reset")) is True
        assert is_retryable(TimeoutError("timeout")) is True
        assert is_retryable(CapacityError(429, "rate limited")) is True
        assert is_retryable(AttributeError("bug")) is False
        assert is_retryable(TypeError("bug")) is False
        # Bare OSError is a plugin bug-class (FileNotFoundError, PermissionError)
        # and must NOT be engine-classified retryable — only the canonical
        # transport signals above are (see PluginRetryableError's contract).
        assert is_retryable(FileNotFoundError("missing input")) is False
        assert is_retryable(PermissionError("denied")) is False

    def test_shutdown_during_backoff_diverts_with_last_attempt_state_id(self) -> None:
        """InterruptedError born in RetryManager backoff carries no stamp; the
        shutdown conversion must attribute the DIVERT routing_event to the
        last failed attempt's node state, not ctx.state_id (which is
        scope-restored and stale by then)."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(RuntimeRetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0, exponential_base=2.0))
        processor = _make_processor(factory, retry_manager=retry_manager)
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}

        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        token = make_token_info(data={"value": 42})
        shutdown_event = threading.Event()
        ctx = make_context(state_id="stale-previous-state")
        ctx.shutdown_event = shutdown_event

        def fail_and_request_shutdown(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            shutdown_event.set()
            error = ConnectionError("connection reset")
            stamp_node_state_id(error, "state-attempt-0")
            raise error

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_and_request_shutdown),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "shutdown_requested"
        assert error_sink == "error-sink"
        record_event.assert_called_once()
        assert record_event.call_args.kwargs["state_id"] == "state-attempt-0"

    def test_pre_attempt_shutdown_records_state_backed_divert_without_partial_audit(self) -> None:
        """A pre-set shutdown must not write transform_error without its DIVERT."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(RuntimeRetryConfig(max_attempts=3, base_delay=0.01, max_delay=0.1, jitter=0.0, exponential_base=2.0))
        transform = _make_mock_transform(node_id="t1", on_error="error-sink")
        processor = _make_processor(
            factory,
            retry_manager=retry_manager,
            node_step_map={NodeID("t1"): 1},
            node_to_plugin={NodeID("t1"): transform},
        )
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        factory.data_flow.register_node(
            plugin_name="csv",
            node_type=NodeType.SINK,
            plugin_version="1.0",
            config={},
            node_id="error-sink",
            schema_config=_DYNAMIC_SCHEMA,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.data_flow.register_edge(
            from_node_id="t1",
            to_node_id="error-sink",
            label="__error_0__",
            mode=RoutingMode.DIVERT,
            edge_id="error-edge-1",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)
        shutdown_event = threading.Event()
        shutdown_event.set()
        ctx = make_context(
            run_id="test-run",
            token=token,
            landscape=factory.plugin_audit_writer(),
            coordination_token=leader_coordination_token(factory, "test-run"),
            work_item=_claim_processor_token(factory, token, node_id="t1", run_id="test-run"),
        )
        ctx.shutdown_event = shutdown_event

        with patch.object(processor._transform_executor, "execute_transform") as execute_transform:
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        execute_transform.assert_not_called()
        assert result.status == "error"
        assert result.reason == {"reason": "shutdown_requested", "error": "shutdown requested before retry attempt"}
        assert result.retryable is False
        assert error_sink == "error-sink"

        transform_errors = factory.data_flow.get_transform_errors_for_run("test-run")
        routing_events = factory.query.get_all_routing_events_for_run("test-run")
        assert len(transform_errors) == 1
        assert len(routing_events) == 1
        assert routing_events[0].state_id is not None
        state = factory.execution.get_node_state(routing_events[0].state_id)
        assert state is not None
        assert state.status is NodeStateStatus.FAILED
        assert state.node_id == "t1"
        assert state.attempt == 0
        assert state.error_json is not None
        assert json.loads(state.error_json)["phase"] == "retry_pre_attempt_shutdown"

    def test_pre_attempt_shutdown_auto_fail_keeps_shutdown_phase(self) -> None:
        """A failure while building the explicit shutdown error keeps site attribution."""
        _, factory = _make_factory()
        transform = _make_mock_transform(node_id="t1", on_error="discard")
        processor = _make_processor(
            factory,
            node_step_map={NodeID("t1"): 1},
            node_to_plugin={NodeID("t1"): transform},
        )
        token = make_token_info(data={"value": 42})
        _persist_token_for_scheduler(factory, token)

        with (
            patch("elspeth.engine.processor.scrub_text_for_audit", side_effect=RuntimeError("shutdown audit preparation failed")),
            pytest.raises(RuntimeError, match="shutdown audit preparation failed"),
        ):
            processor._record_pre_attempt_shutdown_state(
                exc=InterruptedError("shutdown requested before retry attempt"),
                transform=transform,
                token=token,
                attempt=0,
            )

        states = factory.query.get_node_states_for_token(token.token_id)
        assert len(states) == 1
        assert states[0].status is NodeStateStatus.FAILED
        assert states[0].error_json is not None
        error = json.loads(states[0].error_json)
        assert error["type"] == "RuntimeError"
        assert error["phase"] == "retry_pre_attempt_shutdown"

    def test_tier_2_contract_violation_is_routed_without_retrying(self) -> None:
        """The RetryManager branch needs the same Tier-2 conversion.

        Regression for elspeth-181db83da7. ``is_retryable`` rejects a
        PluginContractViolation, so tenacity re-raises the ORIGINAL exception
        rather than wrapping it in MaxRetriesExceeded — it slipped past both
        clauses guarding this call and aborted the run. A contract violation
        is deterministic, so it must be converted after exactly one attempt.
        """
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=3,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        processor = _make_processor(factory, retry_manager=retry_manager)
        processor._error_edge_ids = {NodeID("t1"): "error-edge-1"}
        transform = _make_mock_transform(node_id="t1", on_error="quarantine")
        token = make_token_info(data={"value": 42})
        ctx = make_context()
        attempts: list[int] = []

        def fail_attempt(**kwargs: Any) -> tuple[TransformResult, TokenInfo, str | None]:
            attempts.append(kwargs["attempt"])
            violation = PluginContractViolation("would overwrite existing input fields {'notes'}")
            stamp_node_state_id(violation, f"state-attempt-{kwargs['attempt']}")
            raise violation

        with (
            patch.object(processor._transform_executor, "execute_transform", side_effect=fail_attempt),
            patch.object(factory.execution, "record_routing_event") as record_event,
        ):
            result, _out_token, error_sink = processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )

        assert attempts == [0], "a deterministic contract violation must not be retried"
        assert result.status == "error"
        assert result.retryable is False
        assert error_sink == "quarantine"
        record_event.assert_called_once()
        assert record_event.call_args.kwargs["state_id"] == "state-attempt-0"

    def test_tier_1_contract_violation_subclass_still_propagates_with_retry(self) -> None:
        """Tier-1 registration outranks the Tier-2 conversion on this branch too."""
        from elspeth.contracts.config import RuntimeRetryConfig

        _, factory = _make_factory()
        retry_manager = RetryManager(
            RuntimeRetryConfig(
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.1,
                jitter=0.0,
                exponential_base=2.0,
            )
        )
        processor = _make_processor(factory, retry_manager=retry_manager)
        transform = _make_mock_transform(node_id="t1", on_error="quarantine")
        token = make_token_info(data={"value": 42})
        ctx = make_context()

        with (
            patch.object(
                processor._transform_executor,
                "execute_transform",
                side_effect=SinkTransactionalInvariantError("commit boundary diverged"),
            ),
            pytest.raises(SinkTransactionalInvariantError),
        ):
            processor._execute_transform_with_retry(
                transform=transform,
                token=token,
                ctx=ctx,
                attempt_offset=0,
            )


# =============================================================================
# Telemetry emission (optional)
# =============================================================================


class TestTelemetryEmission:
    """Tests for telemetry emission behavior."""

    def test_no_telemetry_manager_does_not_crash(self) -> None:
        """Without telemetry_manager, _emit_telemetry is a no-op."""
        _, factory = _make_factory()
        processor = _make_processor(factory)
        # Should not raise
        processor._emit_telemetry(SimpleNamespace())

    def test_telemetry_manager_receives_events(self) -> None:
        """With telemetry_manager, events are forwarded."""
        _, factory = _make_factory()

        telemetry = create_autospec(TelemetryManagerProtocol, instance=True)
        processor = _make_processor(factory, telemetry_manager=telemetry)

        event = SimpleNamespace()
        processor._emit_telemetry(event)
        telemetry.handle_event.assert_called_once_with(event)


class TestRowUnionBranchLossTelemetry:
    def test_follower_stages_durable_loss_without_executor(self) -> None:
        _, factory = _make_factory()
        lost_token = make_token_info(
            row_id="row-1",
            token_id="lost-token",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-control", member_key="control"),),
        )
        processor = _make_processor(
            factory,
            row_union_executor=None,
            branch_to_row_union={BranchName("control"): RowUnionName("variants")},
        )

        assert processor._settle_member_losses(lost_token, "error_routed", []) == []

        claimed = _make_claimed_work_item(token_id="lost-token", lineage_path=lost_token.lineage_path)
        losses = processor._take_claim_group_losses(claimed)
        assert len(losses) == 1
        (loss,) = losses
        assert loss.closer_name == "variants"
        assert loss.group_id == "fg-control"
        assert loss.member_key == "control"
        assert loss.reason == "error_routed"

    def test_failed_siblings_emit_token_completed_after_audit(self) -> None:
        _, factory = _make_factory()
        held_sibling = make_token_info(
            row_id="row-1",
            token_id="held-token",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-treatment", member_key="treatment"),),
        )
        lost_token = make_token_info(
            row_id="row-1",
            token_id="lost-token",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-control", member_key="control"),),
        )
        row_union_executor = create_autospec(RowUnionExecutor, instance=True)
        row_union_executor.notify_branch_lost.return_value = RowUnionOutcome(
            held=False,
            consumed_tokens=(held_sibling,),
            failure_reason="row_union_branch_lost",
            row_union_name="variants",
            outcomes_recorded=True,
        )
        telemetry = create_autospec(TelemetryManagerProtocol, instance=True)
        processor = _make_processor(
            factory,
            row_union_executor=row_union_executor,
            branch_to_row_union={BranchName("control"): RowUnionName("variants")},
            telemetry_manager=telemetry,
        )

        with patch.object(processor, "_complete_row_union_fire"):
            results = processor._settle_member_losses(lost_token, "error_routed", [])

        assert [result.token.token_id for result in results] == ["held-token"]
        telemetry.handle_event.assert_called_once()
        event = telemetry.handle_event.call_args.args[0]
        assert event.token_id == "held-token"
        assert event.outcome is TerminalOutcome.FAILURE
        assert event.path is TerminalPath.UNROUTED

    def test_released_group_settles_nothing_once_its_fork_frame_is_popped(self) -> None:
        """WS3 retires `_row_union_group_released` (spec :240) as structurally
        unneeded, not merely redundant: ruling 27 pops a released group's FORK
        frame off every released token
        (`RowUnionExecutor._pop_released_group`, pinned directly by
        `tests/unit/engine/test_token_lineage_path.py`'s
        `TestRowUnionReleasePop` — a real post-release token's lineage_path
        never carries this union's FORK frame again; that class's
        `test_pops_the_fork_frame_from_beneath_a_surviving_expand_frame`
        pins the exact shape used here: a mid-branch expand's EXPAND frame
        survives the pop, stacked where the popped FORK frame used to sit).
        Once popped, `binding_for` cannot resolve the walk back to this
        union at all, so a post-release terminal settles nothing —
        structurally, with no explicit release check, and identically
        whether or not this worker holds a coalesce/row_union executor.

        A non-empty lineage_path is load-bearing here, not decorative — but
        honestly scoped (2026-08-24 re-review R1). Every assertion below is
        negative (`== []`, `assert_not_called()`, `== ()`), so this test
        alone still passes against a `_settle_member_losses` that returns
        `[]` unconditionally — no purely negative test rules that out. What
        the surviving EXPAND frame actually kills is a walk that resolves a
        frame it should not (a "return the first binding regardless"
        mutant stages a spec, tripping `notify_branch_lost.assert_not_called()`
        and `_take_claim_group_losses(...) == ()`); an empty path could
        never exercise that kill at all, since it has nothing to resolve.
        The deleted-walk mutant this test cannot kill alone is covered by
        the seam suite's positive tests and, in THIS class, by the
        immediately adjacent
        `test_failure_closed_group_still_stages_durable_loss_follower`,
        which drives the same shape of registry and a FORK frame that DOES
        stage — together the pair supplies the differential a single
        negative test cannot.
        """
        _, factory = _make_factory()
        released_token = make_token_info(
            row_id="row-1",
            token_id="released-token",
            # The union's own FORK frame is popped by release; a frame from
            # a mid-branch expand the token passed through BEFORE reaching
            # the union survives above it (test_pops_the_fork_frame_from_beneath_a_surviving_expand_frame's
            # shape) — an ordinary unbound EXPAND frame, not registered
            # against any closer here.
            lineage_path=(LineageFrame(kind=FrameKind.EXPAND, group_id="eg-survivor", member_key="tok-child-1"),),
        )
        row_union_executor = create_autospec(RowUnionExecutor, instance=True)
        processor = _make_processor(
            factory,
            row_union_executor=row_union_executor,
            branch_to_row_union={BranchName("control"): RowUnionName("variants")},
        )

        assert processor._settle_member_losses(released_token, "routed_to_sink", []) == []

        row_union_executor.notify_branch_lost.assert_not_called()
        claimed = _make_claimed_work_item(token_id="released-token", lineage_path=released_token.lineage_path)
        assert processor._take_claim_group_losses(claimed) == ()

    def test_failure_closed_group_still_stages_durable_loss_follower(self) -> None:
        # A FAILED closure at the union node is not a release: a later
        # pre-barrier loss for the same group is still a true loss record.
        _, factory = _make_factory()
        union_node = NodeID("row_union::variants")
        processor = _make_processor(
            factory,
            row_union_executor=None,
            row_union_node_ids={RowUnionName("variants"): union_node},
            branch_to_row_union={BranchName("control"): RowUnionName("variants")},
        )
        factory.data_flow.create_row_with_token(
            "source-0",
            0,
            {"a": 1},
            row_id="row-1",
            source_row_index=0,
            ingest_sequence=0,
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        factory.data_flow.create_token(
            "row-1",
            token_id="failed-token",
            coordination_token=leader_coordination_token(factory, "test-run"),
        )
        state = factory.execution.begin_node_state(
            "failed-token",
            str(union_node),
            1,
            {"a": 1},
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        factory.execution.complete_node_state(
            state.state_id,
            NodeStateStatus.FAILED,
            error=ExecutionError(exception="row_union_timeout", exception_type="RowUnionFailureReason"),
            duration_ms=1.0,
            member_token=leader_coordination_token(factory, "test-run").membership,
        )
        lost_token = make_token_info(
            row_id="row-1",
            token_id="failed-token",
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fg-control", member_key="control"),),
        )

        assert processor._settle_member_losses(lost_token, "error_routed", []) == []

        claimed = _make_claimed_work_item(token_id="failed-token", lineage_path=lost_token.lineage_path)
        losses = processor._take_claim_group_losses(claimed)
        assert len(losses) == 1
        assert losses[0].member_key == "control"


# =============================================================================
# _handle_transform_error_status: ROUTED_ON_ERROR producer-site invariants
# =============================================================================


class TestHandleTransformErrorStatusRoutedOnError:
    """Pin the no-fabricated-audit-data rule on the ROUTED_ON_ERROR producer site.

    Plan elspeth-5069612f3c, Task 4 Step 1: when transform_result.reason is
    falsy and the row is routed to an error sink (not "discard"), the producer
    must crash with OrchestrationInvariantError BEFORE constructing FailureInfo
    or the RowResult. Refusing to fabricate "unknown_error" is required to
    avoid a deterministic error_hash collision across unrelated falsy-error
    failures (Tier-1 audit fabrication rule).
    """

    def _setup(self) -> tuple[LandscapeDB, RecorderFactory, RowProcessor]:
        db, factory = _make_factory()
        processor = _make_processor(factory, retry_manager=None)
        return db, factory, processor

    def test_falsy_reason_with_error_sink_raises_invariant_error(self) -> None:
        """Empty reason on ROUTED_ON_ERROR path must raise before any RowResult is built.

        Construct a TransformResult with a valid reason to satisfy
        __post_init__, then mutate `reason` to an empty dict (the producer-side
        invariant must catch this even though TransformResult itself rejects
        empty reasons at construction time — defense in depth at the routing
        boundary).
        """
        _, _factory, processor = self._setup()
        token = make_token_info(data={"value": 42})

        # Build a valid error TransformResult, then forge a falsy reason to
        # exercise the producer-side guard. TransformResult is not frozen, so
        # direct attribute mutation is sufficient; we cannot use the factory
        # method with an empty dict because TransformResult.__post_init__
        # rejects it.
        tr = TransformResult(
            status="error",
            row=None,
            reason={"reason": "needs forging"},
            rows=None,
        )
        tr.reason = {}  # falsy — triggers the offensive guard

        with pytest.raises(
            OrchestrationInvariantError,
            match=r"ROUTED_ON_ERROR requires transform_result\.reason",
        ):
            processor._handle_transform_error_status(
                transform_result=tr,
                current_token=token,
                error_sink="error-sink",
                child_items=[],
                ctx=make_context(
                    landscape=_factory.plugin_audit_writer(), coordination_token=leader_coordination_token(_factory, "test-run")
                ),
            )

    def test_none_reason_with_error_sink_raises_invariant_error(self) -> None:
        """`reason=None` on the ROUTED_ON_ERROR path must also raise.

        TransformResult.__post_init__ rejects status='error' with reason=None,
        so we forge it post-construction. The producer-side guard must still
        catch the case before any FailureInfo is created.
        """
        _, _factory, processor = self._setup()
        token = make_token_info(data={"value": 42})

        tr = TransformResult(
            status="error",
            row=None,
            reason={"reason": "needs forging"},
            rows=None,
        )
        tr.reason = None  # falsy — triggers the offensive guard

        with pytest.raises(
            OrchestrationInvariantError,
            match=r"ROUTED_ON_ERROR requires transform_result\.reason",
        ):
            processor._handle_transform_error_status(
                transform_result=tr,
                current_token=token,
                error_sink="error-sink",
                child_items=[],
                ctx=make_context(
                    landscape=_factory.plugin_audit_writer(), coordination_token=leader_coordination_token(_factory, "test-run")
                ),
            )

    def test_valid_reason_routed_on_error_emits_routed_on_error_with_failure(
        self,
    ) -> None:
        """Non-falsy reason produces ROUTED_ON_ERROR with FailureInfo populated.

        Verifies the new shape: outcome is ROUTED_ON_ERROR, sink_name is set,
        and error.message is the str() of transform_result.reason. Pins that
        we never use 'unknown_error' as message — error_hash must be derived
        from the real upstream error.
        """
        from elspeth.contracts.results import FailureInfo

        _, _factory, processor = self._setup()
        token = make_token_info(data={"value": 42})
        tr = TransformResult(
            status="error",
            row=None,
            reason={"reason": "transform_failed_for_reasons"},
            rows=None,
        )

        terminal = processor._handle_transform_error_status(
            transform_result=tr,
            current_token=token,
            error_sink="error-sink",
            child_items=[],
            ctx=make_context(landscape=_factory.plugin_audit_writer(), coordination_token=leader_coordination_token(_factory, "test-run")),
        )
        result = terminal.result
        assert not isinstance(result, tuple)
        _assert_outcome_pair(result, TerminalOutcome.FAILURE, TerminalPath.ON_ERROR_ROUTED)
        assert result.sink_name == "error-sink"
        assert isinstance(result.error, FailureInfo)
        assert result.error.exception_type == "TransformError"
        # str({'reason': 'transform_failed_for_reasons'}) — the message is the
        # str() of the whole reason dict, not literal "unknown_error".
        assert "transform_failed_for_reasons" in result.error.message
        assert result.error.message != "unknown_error"
