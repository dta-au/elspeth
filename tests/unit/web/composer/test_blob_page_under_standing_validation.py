"""A blob must be readable whatever the saved graph's validation looks like.

``get_blob_content`` caps the WHOLE serialized result at 4,096 bytes and
``read_attachment`` at the complete-attachment limit. Both results used to carry
the saved graph's full ``validation`` block, so the block spent the bound: with
enough standing errors no page could hold one character and every read was
refused, and a prompt file could not be read until an unrelated graph error was
repaired. A successful read now carries no ``validation`` block at all.

Real: the dispatcher, the handler, a session store with a stored blob, and the
provider serializer the handler itself measures with. Faked: the catalog.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool

from elspeth.web.blobs.service import content_hash
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.redaction import READ_ATTACHMENT_MAX_UTF8_BYTES, redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolResult
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import blobs_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.composer.test_tools import _blob_operation, _mock_catalog, execute_tool

_PAGE_CAP_BYTES = 4_096
_PROMPT_FILE_TEXT = "Classify the record as urgent or routine and give one reason.\n"
_LONG_FILE_TEXT = "".join(f"{row:05d} weigh the evidence\n" for row in range(800))


def _source_only() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).with_source(
        SourceSpec(
            plugin="csv",
            on_success="shared",
            on_validation_failure="discard",
            options={"path": "/data/in.csv", "schema": {"mode": "fixed", "fields": ["text: str"]}},
        )
    )


def _node(node_id: str, plugin: str, input_name: str, on_success: str, options: dict[str, Any]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=input_name,
        on_success=on_success,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _valid_graph() -> CompositionState:
    state = _source_only().with_node(_node("only", "passthrough", "shared", "out", {"schema": {"mode": "observed"}}))
    return state.with_output(OutputSpec(name="out", plugin="csv", options={"path": "outputs/out.csv"}, on_write_failure="discard"))


def _two_llm_nodes_on_one_connection() -> CompositionState:
    """Two ordinary LLM nodes (a 5 KB prompt each) reading the same connection."""
    state = _source_only()
    for name in ("judge_a", "judge_b"):
        options = {
            "provider": "openrouter",
            "model": "openai/gpt-4o",
            "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
            "prompt_template": f"Rubric for {name}. " + "Weigh the stated evidence and give one grounded finding. " * 90,
            "schema": {"mode": "observed"},
        }
        state = state.with_node(_node(name, "llm", "shared", f"out_{name}", options))
        state = state.with_output(
            OutputSpec(name=f"out_{name}", plugin="csv", options={"path": f"outputs/{name}.csv"}, on_write_failure="discard")
        )
    return state


def _many_plain_errors(nodes: int = 26) -> CompositionState:
    """A chain whose every node publishes to a connection nothing reads: many errors, no shared connection."""
    state = _source_only()
    previous = "shared"
    for index in range(nodes):
        state = state.with_node(
            _node(f"step_{index:02d}", "passthrough", previous, f"dangling_{index:02d}", {"schema": {"mode": "observed"}})
        )
        previous = f"link_{index:02d}"
    return state


def _valid_graph_with_warnings() -> CompositionState:
    """A valid fan-out: one fork gate, five branches, five sinks. Every branch draws a 'contract check skipped' warning."""
    branches = [f"branch_{index:02d}" for index in range(5)]
    gate = NodeSpec(
        id="fan_out",
        node_type="gate",
        plugin=None,
        input="shared",
        on_success=None,
        on_error=None,
        options={},
        condition="True",
        routes={"true": "fork", "false": "fork"},
        fork_to=tuple(branches),
        branches=None,
        policy=None,
        merge=None,
    )
    state = _source_only().with_node(gate)
    for index, branch in enumerate(branches):
        options = {"schema": {"mode": "observed"}, "required_input_fields": ["text"]}
        state = state.with_node(_node(f"keep_{index:02d}", "passthrough", branch, f"out_{index:02d}", options))
        state = state.with_output(
            OutputSpec(name=f"out_{index:02d}", plugin="csv", options={"path": f"outputs/{index:02d}.csv"}, on_write_failure="discard")
        )
    return state


_GRAPH_STATES = {
    "valid": _valid_graph,
    "valid_with_warnings": _valid_graph_with_warnings,
    "many_plain_errors": _many_plain_errors,
    "duplicate_consumer_over_llm_nodes": _two_llm_nodes_on_one_connection,
}


class TestBlobPageUnderStandingValidation:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.engine = create_session_engine("sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False})
        initialize_session_schema(self.engine)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")
        self.data_dir = tmp_path
        self.session_id = str(uuid4())
        with self.engine.begin() as conn:
            now = datetime.now(UTC)
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id, user_id="test-user", auth_provider_type="local", title="Test", created_at=now, updated_at=now
                )
            )
        self.blob_id = self._store(_PROMPT_FILE_TEXT)

    def _store(self, text: str) -> str:
        blob_id = str(uuid4())
        storage_dir = self.data_dir / "blobs" / self.session_id
        storage_dir.mkdir(parents=True, exist_ok=True)
        storage_path = storage_dir / f"{blob_id}_prompt.txt"
        content = text.encode("utf-8")
        storage_path.write_bytes(content)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=blob_id,
                    session_id=self.session_id,
                    filename="prompt.txt",
                    mime_type="text/plain",
                    size_bytes=len(content),
                    content_hash=content_hash(content),
                    storage_path=str(storage_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )
        return blob_id

    def _call(self, tool_name: str, arguments: dict[str, Any], state: CompositionState) -> ToolResult:
        with _blob_operation(self.engine, self.session_id) as (authority, context):
            return execute_tool(
                tool_name,
                arguments,
                state,
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=context,
            )

    def _read_first_page(self, state: CompositionState) -> ToolResult:
        return self._call("get_blob_content", {"blob_id": self.blob_id}, state)

    @staticmethod
    def _wire(tool_name: str, result: ToolResult) -> dict[str, Any]:
        return json.loads(serialize_admitted_discovery_result(admit_discovery_result(tool_name, result)))

    @staticmethod
    def _wire_bytes(tool_name: str, result: ToolResult) -> int:
        return len(serialize_admitted_discovery_result(admit_discovery_result(tool_name, result)).encode("utf-8"))

    @staticmethod
    def _failure_message(result: ToolResult) -> str:
        return result.validation.errors[0].message if result.validation.errors else ""

    def test_a_valid_graph_returns_the_page(self) -> None:
        result = self._read_first_page(_valid_graph())

        assert result.success, self._failure_message(result)
        assert result.data["content"] == _PROMPT_FILE_TEXT
        assert self._wire_bytes("get_blob_content", result) <= _PAGE_CAP_BYTES

    def test_a_standing_duplicate_consumer_error_does_not_block_the_page(self) -> None:
        state = _two_llm_nodes_on_one_connection()
        assert "duplicate_connection_consumer" in {entry.error_code for entry in state.validate().errors}

        result = self._read_first_page(state)

        assert result.success, self._failure_message(result)
        assert result.data["content"] == _PROMPT_FILE_TEXT

    def test_many_plain_errors_do_not_block_the_page(self) -> None:
        state = _many_plain_errors()
        validation = state.validate()
        assert "duplicate_connection_consumer" not in {entry.error_code for entry in validation.errors}
        assert len(validation.errors) >= 26

        result = self._read_first_page(state)

        assert result.success, self._failure_message(result)
        assert result.data["content"] == _PROMPT_FILE_TEXT

    def test_warnings_on_a_valid_graph_take_no_room_from_the_page(self) -> None:
        """While the block rode the page, this graph's five warnings left 2,091 characters of content on the first page."""
        long_blob = self._store(_LONG_FILE_TEXT)
        state = _valid_graph_with_warnings()
        validation = state.validate()
        assert validation.is_valid, [entry.message for entry in validation.errors]
        assert len(validation.warnings) >= 3

        with_warnings = self._call("get_blob_content", {"blob_id": long_blob}, state)
        without = self._call("get_blob_content", {"blob_id": long_blob}, _valid_graph())

        assert with_warnings.success and without.success
        held = len(with_warnings.data["content"])
        assert held >= 3_200, held
        # The two envelopes differ only in the digits of ``version``.
        version_digits = abs(len(str(state.version)) - len(str(_valid_graph().version)))
        assert abs(held - len(without.data["content"])) <= version_digits

    @pytest.mark.parametrize("graph", sorted(_GRAPH_STATES))
    @pytest.mark.parametrize("text", ["x", _PROMPT_FILE_TEXT, _LONG_FILE_TEXT], ids=["one_char", "short", "long"])
    def test_a_page_always_makes_progress(self, graph: str, text: str) -> None:
        """No saved-graph state can leave a non-empty blob with a page of nothing."""
        blob_id = self._store(text)

        result = self._call("get_blob_content", {"blob_id": blob_id}, _GRAPH_STATES[graph]())

        assert result.success, f"{graph}: {self._failure_message(result)}"
        assert len(result.data["content"]) >= 1
        assert result.data["content"] == text[: result.data["end_char"]]
        assert self._wire_bytes("get_blob_content", result) <= _PAGE_CAP_BYTES

    @pytest.mark.parametrize("graph", sorted(_GRAPH_STATES))
    def test_a_successful_page_carries_no_validation_block(self, graph: str) -> None:
        wire = self._wire("get_blob_content", self._read_first_page(_GRAPH_STATES[graph]()))

        assert list(wire) == ["success", "affected_nodes", "version", "data"]

    def test_page_boundaries_follow_the_content_when_the_graph_changes_between_pages(self) -> None:
        long_blob = self._store(_LONG_FILE_TEXT)
        first = self._call("get_blob_content", {"blob_id": long_blob}, _valid_graph())
        assert first.success and first.data["truncated"]
        continuation = {
            "blob_id": long_blob,
            "offset_chars": first.data["next_offset_chars"],
            "expected_content_hash": first.data["content_hash"],
        }

        second_unchanged = self._call("get_blob_content", continuation, _valid_graph())
        second_after_errors = self._call("get_blob_content", continuation, _many_plain_errors())

        assert second_after_errors.success, self._failure_message(second_after_errors)
        assert second_after_errors.data["start_char"] == first.data["end_char"]
        end = second_after_errors.data["end_char"]
        assert first.data["content"] + second_after_errors.data["content"] == _LONG_FILE_TEXT[:end]
        # The two envelopes differ only in the digits of ``version``.
        version_digits = abs(len(str(_many_plain_errors().version)) - len(str(_valid_graph().version)))
        assert abs(end - second_unchanged.data["end_char"]) <= version_digits

    def test_a_refused_read_keeps_its_own_rejection_in_full(self) -> None:
        missing = str(uuid4())

        result = self._call("get_blob_content", {"blob_id": missing}, _many_plain_errors())
        wire = self._wire("get_blob_content", result)

        assert result.success is False
        assert wire["validation"]["errors"][0]["component"] == "rejected_mutation"
        assert wire["validation"]["errors"][0]["message"] == f"Blob '{missing}' not found."

    def test_a_complete_attachment_read_is_not_refused_for_the_graphs_validation(self) -> None:
        """The attachment fits with room to spare; only the saved graph's validation block pushed it over the limit."""
        state = _two_llm_nodes_on_one_connection()
        block = len(json.dumps(execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()["validation"]))
        text = "a" * (READ_ATTACHMENT_MAX_UTF8_BYTES - 2_000)
        assert block > 4_000, block
        blob_id = self._store(text)

        result = self._call("read_attachment", {"blob_id": blob_id}, state)

        assert result.success, self._failure_message(result)
        assert result.data["content"] == text
        wire = self._wire("read_attachment", result)
        assert "validation" not in wire
        assert self._wire_bytes("read_attachment", result) <= READ_ATTACHMENT_MAX_UTF8_BYTES

    @pytest.mark.parametrize("tool_name", ["get_blob_content", "read_attachment"])
    def test_the_audit_record_of_a_successful_read_has_no_validation_block(self, tool_name: str) -> None:
        wire = self._wire(tool_name, self._call(tool_name, {"blob_id": self.blob_id}, _many_plain_errors()))

        stored = redact_tool_call_response(tool_name, wire, telemetry=NoopRedactionTelemetry())

        assert "validation" not in stored
        assert stored["success"] is True
        assert _PROMPT_FILE_TEXT not in json.dumps(stored)

    @pytest.mark.parametrize("tool_name", ["get_blob_content", "read_attachment"])
    def test_the_audit_record_refuses_a_refusal_without_its_validation(self, tool_name: str) -> None:
        wire = self._wire(tool_name, self._call(tool_name, {"blob_id": str(uuid4())}, _valid_graph()))
        assert wire["success"] is False
        del wire["validation"]

        with pytest.raises(ValidationError, match="requires validation"):
            redact_tool_call_response(tool_name, wire, telemetry=NoopRedactionTelemetry())

    @pytest.mark.parametrize("tool_name", ["get_blob_content", "read_attachment"])
    def test_an_over_budget_validation_on_a_refusal_still_degrades_the_whole_audit_row(self, tool_name: str) -> None:
        """``validation`` is optional on these models, but it is framing: over budget, the row degrades; it is never withheld."""
        wire = self._wire(tool_name, self._call(tool_name, {"blob_id": str(uuid4())}, _many_plain_errors(nodes=40)))
        assert wire["success"] is False
        assert len(wire["validation"]["errors"]) > 64

        stored = redact_tool_call_response(tool_name, wire, telemetry=NoopRedactionTelemetry())

        assert stored == {"_redaction_status": "response_projection_limit"}
