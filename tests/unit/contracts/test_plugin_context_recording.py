"""PluginContext operation, validation-error and transform-error audit contracts.

Row calls belong to audited clients; these tests prove that contexts reject row
parentage before writing and that operation writes precede their telemetry.
"""

import logging
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from elspeth.contracts import CallStatus, CallType, FrameworkBugError
from elspeth.contracts.audit import Call, TokenRef
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.events import ExternalCallCompleted
from elspeth.contracts.plugin_context import PluginContext, TransformErrorToken, ValidationErrorToken
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.token_usage import UNKNOWN_TOKEN_USAGE, TokenUsage
from tests.fixtures.factories import make_source_context
from tests.fixtures.mock_audit import mock_audit_authority


class _FakePluginAuditWriter:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.operation_calls: list[dict[str, Any]] = []
        self.transform_error_calls: list[dict[str, Any]] = []

    def record_operation_call(
        self,
        *,
        operation_id: str,
        coordination_token: CoordinationToken,
        call_type: CallType,
        status: CallStatus,
        request_data: RawCallPayload,
        response_data: RawCallPayload | None = None,
        error: RawCallPayload | None = None,
        latency_ms: float | None = None,
        token_usage: TokenUsage = UNKNOWN_TOKEN_USAGE,
    ) -> Call:
        if self.failure is not None:
            raise self.failure
        call_index = len(self.operation_calls)
        self.operation_calls.append(
            {
                "operation_id": operation_id,
                "call_type": call_type,
                "status": status,
                "request_data": request_data,
                "response_data": response_data,
                "error": error,
                "latency_ms": latency_ms,
                "token_usage": token_usage,
            }
        )
        return Call(
            call_id=f"operation-call-{call_index}",
            call_index=call_index,
            call_type=call_type,
            status=status,
            request_hash="operation-request-hash",
            response_hash="operation-response-hash" if response_data is not None else None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            operation_id=operation_id,
            latency_ms=latency_ms,
        )

    def record_transform_error(
        self,
        *,
        ref: TokenRef,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
        transform_id: str,
        row_data: Any,
        error_details: Any,
        destination: str,
    ) -> str:
        error_id = f"terr-{len(self.transform_error_calls)}"
        self.transform_error_calls.append(
            {
                "ref": ref,
                "transform_id": transform_id,
                "row_data": row_data,
                "error_details": error_details,
                "destination": destination,
                "error_id": error_id,
            }
        )
        return error_id


class _FailingTransformErrorWriter:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure
        self.record_transform_error_kwargs: dict[str, Any] | None = None

    def record_transform_error(
        self,
        *,
        ref: TokenRef,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
        transform_id: str,
        row_data: Any,
        error_details: Any,
        destination: str,
    ) -> str:
        self.record_transform_error_kwargs = {
            "ref": ref,
            "transform_id": transform_id,
            "row_data": row_data,
            "error_details": error_details,
            "destination": destination,
        }
        raise self.failure


class TestRecordValidationErrorGuards:
    """record_validation_error() must crash on missing landscape or node_id."""

    def test_raises_when_landscape_is_none(self) -> None:
        ctx = PluginContext(run_id="run-1", config={}, landscape=None, node_id="source")
        with pytest.raises(FrameworkBugError, match=r"record_validation_error.*without landscape"):
            ctx.record_validation_error(
                row={"name": "test"},
                error="field X is NULL",
                schema_mode="fixed",
                destination="discard",
            )

    def test_raises_when_node_id_is_none(self) -> None:
        ctx = PluginContext(run_id="run-1", config={}, landscape=object(), node_id=None)
        with pytest.raises(FrameworkBugError, match=r"record_validation_error.*without node_id"):
            ctx.record_validation_error(
                row={"name": "test"},
                error="field X is NULL",
                schema_mode="fixed",
                destination="discard",
            )


class TestRecordValidationErrorHappyPath:
    """record_validation_error() delegates to landscape and returns token."""

    def test_returns_validation_error_token(self) -> None:
        """Happy path: row with id field -> token with that row_id."""
        ctx = make_source_context()
        token = ctx.record_validation_error(
            row={"id": "row-42", "name": "test"},
            error="field X is NULL",
            schema_mode="fixed",
            destination="discard",
        )
        assert isinstance(token, ValidationErrorToken)
        assert token.row_id == "row-42"
        assert token.node_id == "source"
        assert token.destination == "discard"
        assert token.error_id is not None  # Landscape assigns an error_id

    def test_row_without_id_uses_content_hash(self) -> None:
        """Row without 'id' field -> row_id derived from stable_hash."""
        ctx = make_source_context()
        token = ctx.record_validation_error(
            row={"name": "test"},
            error="missing required field",
            schema_mode="flexible",
            destination="quarantine_sink",
        )
        assert isinstance(token, ValidationErrorToken)
        assert len(token.row_id) == 16  # stable_hash[:16]
        assert token.destination == "quarantine_sink"

    def test_non_dict_row_uses_repr_hash(self) -> None:
        """Non-dict row (e.g., JSON primitive) -> row_id from repr_hash."""
        ctx = make_source_context()
        token = ctx.record_validation_error(
            row="not a dict",
            error="expected dict, got str",
            schema_mode="parse",
            destination="discard",
        )
        assert isinstance(token, ValidationErrorToken)
        assert len(token.row_id) == 16

    def test_non_canonical_row_does_not_leak_row_content_to_logger(self, caplog: pytest.LogCaptureFixture) -> None:
        """elspeth-05a5727489: when stable_hash() fails on non-canonical row data, the
        fallback warning must log only the error TYPE, never str(e). Hashing
        canonicalization errors embed `Got: {obj!r}` (raw row content), which must
        stay in Landscape, not cross a normal logging boundary."""
        ctx = make_source_context()
        secret = "ROW-SECRET-payload-42"
        with caplog.at_level(logging.WARNING, logger="elspeth.contracts.plugin_context"):
            token = ctx.record_validation_error(
                row={"data": frozenset({secret})},  # frozenset -> non-canonical -> repr_hash fallback
                error="non-serializable external data",
                schema_mode="flexible",
                destination="discard",
            )
        assert isinstance(token, ValidationErrorToken)
        assert len(token.row_id) == 16  # repr_hash fallback still produced an id
        log_text = "\n".join(r.getMessage() for r in caplog.records)
        assert secret not in log_text  # row content must not leak through the logger
        assert "TypeError" in log_text  # the diagnostic error type is still logged

    def test_custom_destination_propagated(self) -> None:
        """Destination string flows through to the returned token."""
        ctx = make_source_context()
        token = ctx.record_validation_error(
            row={"id": "row-1"},
            error="bad data",
            schema_mode="fixed",
            destination="error_sink",
        )
        assert token.destination == "error_sink"

    def test_quarantine_destinations_queue_error_for_row_linkage(self) -> None:
        """Non-discard validation errors should be available for quarantine row linking."""
        ctx = make_source_context()
        row = {"name": "test"}

        token = ctx.record_validation_error(
            row=row,
            error="missing required field",
            schema_mode="fixed",
            destination="quarantine_sink",
        )

        assert ctx.pop_pending_quarantine_validation_error_id(row) == token.error_id
        assert ctx.pop_pending_quarantine_validation_error_id(row) is None

    def test_discard_validation_errors_are_not_queued_for_row_linkage(self) -> None:
        """Discarded rows should not leave stale pending linkage entries behind."""
        ctx = make_source_context()
        row = {"name": "discard-me"}

        ctx.record_validation_error(
            row=row,
            error="bad data",
            schema_mode="fixed",
            destination="discard",
        )

        assert ctx.pop_pending_quarantine_validation_error_id(row) is None

    def test_noncanonical_quarantine_linkage_uses_repr_fallback(self) -> None:
        """Pending linkage must still match non-canonical raw rows like NaN payloads."""
        ctx = make_source_context()
        row = {"value": float("nan")}

        token = ctx.record_validation_error(
            row=row,
            error="Row contains NaN",
            schema_mode="observed",
            destination="quarantine_sink",
        )

        assert ctx.pop_pending_quarantine_validation_error_id({"value": float("nan")}) == token.error_id


class TestRecordCallGuards:
    def test_raises_when_landscape_is_none(self) -> None:
        ctx = PluginContext(run_id="run-1", config={}, landscape=None, operation_id="operation-001")
        with pytest.raises(FrameworkBugError, match=r"record_call\(\) called without landscape"):
            ctx.record_call(CallType.HTTP, CallStatus.SUCCESS, {"url": "https://example.test"})

    @pytest.mark.parametrize(("state_id", "operation_id"), [("state-001", None), ("state-001", "operation-001"), (None, None)])
    def test_rejects_missing_or_row_parent_before_writing(self, state_id: str | None, operation_id: str | None) -> None:
        writer = _FakePluginAuditWriter()
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            state_id=state_id,
            operation_id=operation_id,
            **mock_audit_authority("run-1"),
        )
        with pytest.raises(FrameworkBugError, match="requires an operation parent"):
            ctx.record_call(CallType.HTTP, CallStatus.SUCCESS, {"url": "https://example.test"})
        assert writer.operation_calls == []

    def test_operation_without_leader_fails_before_writing(self) -> None:
        writer = _FakePluginAuditWriter()
        ctx = PluginContext(run_id="run-1", config={}, landscape=cast(Any, writer), operation_id="operation-001")
        with pytest.raises(FrameworkBugError, match="requires the executor's leader token"):
            ctx.record_call(CallType.HTTP, CallStatus.SUCCESS, {"url": "https://example.test"})
        assert writer.operation_calls == []

    def test_propagates_operation_write_failure_without_telemetry(self) -> None:
        writer = _FakePluginAuditWriter(RuntimeError("landscape call write failed"))
        emitted_events: list[ExternalCallCompleted] = []
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            operation_id="operation-001",
            telemetry_emit=emitted_events.append,
            **mock_audit_authority("run-1"),
        )
        with pytest.raises(RuntimeError, match="landscape call write failed"):
            ctx.record_call(CallType.HTTP, CallStatus.SUCCESS, {"url": "https://example.test"})
        assert emitted_events == []


class TestRecordCallHappyPath:
    @pytest.mark.parametrize(
        ("response", "expected_prompt", "expected_completion"),
        [
            ({}, None, None),
            ({"usage": None}, None, None),
            ({"usage": "unavailable"}, None, None),
            ({"usage": {"prompt_tokens": -1, "completion_tokens": True}}, None, None),
            ({"usage": {"prompt_tokens": 7}}, 7, None),
            ({"usage": {"prompt_tokens": 7, "completion_tokens": 3}}, 7, 3),
        ],
    )
    def test_llm_usage_observation_preserves_raw_response_and_unknown_counts(
        self, response: dict[str, Any], expected_prompt: int | None, expected_completion: int | None
    ) -> None:
        writer = _FakePluginAuditWriter()
        events: list[ExternalCallCompleted] = []
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            **mock_audit_authority("run-1"),
            operation_id="operation-001",
            telemetry_emit=events.append,
        )
        ctx.record_call(CallType.LLM, CallStatus.SUCCESS, {}, response_data=response)

        assert writer.operation_calls[0]["response_data"].to_dict() == response
        assert writer.operation_calls[0]["token_usage"] == TokenUsage.from_dict(response.get("usage"))
        assert len(events) == 1
        assert events[0].response_payload is not None
        assert events[0].response_payload.to_dict() == response
        usage = events[0].token_usage
        if expected_prompt is None and expected_completion is None:
            assert usage is None
        else:
            assert usage is not None
            assert usage.prompt_tokens == expected_prompt
            assert usage.completion_tokens == expected_completion

    def test_operation_context_records_operation_call_without_token_lookup(self) -> None:
        writer = _FakePluginAuditWriter()
        emitted_events: list[ExternalCallCompleted] = []
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            **mock_audit_authority("run-1"),
            operation_id="operation-001",
            telemetry_emit=emitted_events.append,
        )

        recorded = ctx.record_call(
            CallType.FILESYSTEM,
            CallStatus.ERROR,
            {"path": "/tmp/output.csv"},
            error={"type": "OSError", "message": "disk full"},
            provider="filesystem",
        )

        assert recorded is not None
        assert recorded.operation_id == "operation-001"
        assert recorded.state_id is None
        assert writer.operation_calls[0]["request_data"].to_dict() == {"path": "/tmp/output.csv"}
        assert writer.operation_calls[0]["error"].to_dict() == {"type": "OSError", "message": "disk full"}
        assert writer.operation_calls[0]["latency_ms"] is None

        assert len(emitted_events) == 1
        event = emitted_events[0]
        assert event.state_id is None
        assert event.operation_id == "operation-001"
        assert event.token_id is None
        assert event.provider == "filesystem"
        assert event.latency_ms is None
        assert event.to_dict()["latency_ms"] is None
        assert event.request_hash == recorded.request_hash


class TestRecordTransformErrorGuards:
    """record_transform_error() must crash on missing landscape."""

    def test_raises_when_landscape_is_none(self) -> None:
        ctx = PluginContext(run_id="run-1", config={}, landscape=None, node_id="transform-1")
        with pytest.raises(FrameworkBugError, match=r"record_transform_error.*without landscape"):
            ctx.record_transform_error(
                token_id="tok-1",
                transform_id="transform-1",
                row={"data": "test"},
                error_details={"reason": "api_error", "error": "API returned 500"},
                destination="discard",
            )


class TestRecordTransformErrorHappyPath:
    """record_transform_error() delegates to landscape and returns token."""

    def test_returns_transform_error_token(self) -> None:
        """Happy path: landscape.record_transform_error is called and token fields are populated.

        record_transform_error requires a pre-existing token FK in the DB.
        Use a small fake landscape to test the delegation and return-value logic
        without needing to build the full token/row/node FK chain — that
        belongs in integration tests (test_recorder_errors.py).
        """
        writer = _FakePluginAuditWriter()
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            **mock_audit_authority("run-1"),
            node_id="transform-1",
        )
        token = ctx.record_transform_error(
            token_id="tok-1",
            transform_id="transform-1",
            row={"data": "test"},
            error_details={"reason": "api_error", "error": "API returned 500"},
            destination="error_sink",
        )
        assert isinstance(token, TransformErrorToken)
        assert token.token_id == "tok-1"
        assert token.transform_id == "transform-1"
        assert token.destination == "error_sink"
        assert token.error_id == "terr-0"
        assert writer.transform_error_calls == [
            {
                "ref": TokenRef(token_id="tok-1", run_id="run-1"),
                "transform_id": "transform-1",
                "row_data": {"data": "test"},
                "error_details": {"reason": "api_error", "error": "API returned 500"},
                "destination": "error_sink",
                "error_id": "terr-0",
            }
        ]

    def test_propagates_landscape_write_failure(self) -> None:
        writer = _FailingTransformErrorWriter(RuntimeError("transform recorder failed"))
        ctx = PluginContext(
            run_id="run-1",
            config={},
            landscape=cast(Any, writer),
            **mock_audit_authority("run-1"),
            node_id="transform-1",
        )

        with pytest.raises(RuntimeError, match="transform recorder failed"):
            ctx.record_transform_error(
                token_id="tok-1",
                transform_id="transform-1",
                row={"data": "test"},
                error_details={"reason": "api_error", "error": "API returned 500"},
                destination="discard",
            )

        assert writer.record_transform_error_kwargs == {
            "ref": TokenRef(token_id="tok-1", run_id="run-1"),
            "transform_id": "transform-1",
            "row_data": {"data": "test"},
            "error_details": {"reason": "api_error", "error": "API returned 500"},
            "destination": "discard",
        }
