"""Tests for ToolRedactionPolicy construction-time validators (spec §4.2.3 / Task 6).

Six cases covering the four invariants enforced by __post_init__:
  1. Orphan summarizer raises ValueError.
  2. handles_no_sensitive_data=True without reason struct raises ValueError.
  3. handles_no_sensitive_data=False with non-None reason struct raises ValueError.
  4. handles_no_sensitive_data=False without known_response_keys raises ValueError.
  5. Valid construction with sensitive data succeeds (no exception).
  6. Container fields are deeply frozen after construction.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from elspeth.web.composer.redaction import (
    GetBlobContentArgumentsModel,
    GetBlobContentDataModel,
    HandlesNoSensitiveDataReason,
    ToolRedactionPolicy,
)


@pytest.mark.parametrize("offset", [True, False, -1, 1.5, "1", None])
def test_blob_continuation_offset_has_strict_type_and_bounds(offset: object) -> None:
    with pytest.raises(ValidationError):
        GetBlobContentArgumentsModel.model_validate({"blob_id": "blob", "offset_chars": offset, "expected_content_hash": "a" * 64})


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n", True, 12])
def test_blob_continuation_identity_is_canonical_sha256(digest: object) -> None:
    with pytest.raises(ValidationError):
        GetBlobContentArgumentsModel.model_validate({"blob_id": "blob", "expected_content_hash": digest})


def test_blob_continuation_requires_identity_after_zero_and_forbids_extra_fields() -> None:
    assert GetBlobContentArgumentsModel.model_validate({"blob_id": "blob"}).offset_chars == 0
    assert (
        GetBlobContentArgumentsModel.model_validate(
            {"blob_id": "blob", "offset_chars": 0, "expected_content_hash": None}
        ).expected_content_hash
        is None
    )
    assert (
        GetBlobContentArgumentsModel.model_validate({"blob_id": "blob", "offset_chars": 1, "expected_content_hash": "a" * 64}).offset_chars
        == 1
    )
    for arguments in (
        {"blob_id": "blob", "offset_chars": 1},
        {"blob_id": "blob", "offset_chars": 1, "expected_content_hash": None},
        {"blob_id": "blob", "limit": 100_000},
    ):
        with pytest.raises(ValidationError):
            GetBlobContentArgumentsModel.model_validate(arguments)


@pytest.mark.parametrize("dialect_name", ["none", "openai_strict"])
def test_blob_actual_loop_wire_selectors_preserve_runtime_null_contract(dialect_name: str) -> None:
    from jsonschema import Draft202012Validator

    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions

    dialect = ToolContractDialect(dialect_name)
    loop = next(tool["function"] for tool in composer_loop_tool_definitions(dialect) if tool["function"]["name"] == "get_blob_content")
    schema = loop["parameters"]
    initial = {"blob_id": "blob", "offset_chars": 0, "expected_content_hash": None}
    Draft202012Validator(schema).validate(initial)
    assert GetBlobContentArgumentsModel.model_validate(initial).offset_chars == 0
    continuation = {**initial, "offset_chars": 1, "expected_content_hash": "a" * 64}
    Draft202012Validator(schema).validate(continuation)
    assert GetBlobContentArgumentsModel.model_validate(continuation).offset_chars == 1
    if dialect is ToolContractDialect.OPENAI_STRICT:
        assert loop["strict"] is True
        assert set(schema["required"]) == {"blob_id", "offset_chars", "expected_content_hash"}
        # Strict transport lowers optional selectors to required/nullable;
        # runtime semantic guards still refuse null offsets and unbound pages.
        Draft202012Validator(schema).validate({**initial, "offset_chars": None})
    for rejected in ({**initial, "offset_chars": None}, {**initial, "offset_chars": 1}):
        with pytest.raises(ValidationError):
            GetBlobContentArgumentsModel.model_validate(rejected)


def test_blob_expected_hash_is_sensitive_caller_input_not_response_identity() -> None:
    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER, redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

    arguments = {"blob_id": "blob", "offset_chars": 1, "expected_content_hash": "a" * 64}
    projected = redact_tool_call_arguments("get_blob_content", arguments, telemetry=NoopRedactionTelemetry())
    assert projected["expected_content_hash"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert "a" * 64 not in str(projected)
    with pytest.raises(ValidationError):
        GetBlobContentArgumentsModel.model_validate(projected)


def test_blob_strategies_cover_initial_continuation_terminal_and_empty_ranges() -> None:
    from hypothesis import find
    from hypothesis import strategies as st

    arguments = st.from_type(GetBlobContentArgumentsModel)
    initial = find(arguments, lambda value: value.offset_chars == 0 and "expected_content_hash" not in value.model_fields_set)
    continuation = find(arguments, lambda value: value.offset_chars > 0)
    assert initial.expected_content_hash is None
    assert continuation.expected_content_hash is not None
    data = st.from_type(GetBlobContentDataModel)
    middle = find(data, lambda value: value.start_char > 0 and value.truncated)
    empty = find(data, lambda value: value.total_chars == 0)
    terminal = find(data, lambda value: value.start_char > 0 and not value.truncated)
    assert middle.next_offset_chars == middle.end_char > middle.start_char
    assert empty.start_char == empty.end_char == 0
    assert empty.next_offset_chars is None
    assert terminal.end_char == terminal.total_chars
    assert terminal.next_offset_chars is None
    for defect in ({"end_char": middle.end_char + 1}, {"content_hash": "private-invalid-hash"}):
        with pytest.raises(ValidationError):
            GetBlobContentDataModel.model_validate({**middle.model_dump(), **defect})


@pytest.mark.parametrize("choice", ["on_validation_failure", "on_write_failure"])
@pytest.mark.parametrize("supplied_null", [False, True])
def test_full_pipeline_fixture_choices_remain_required_and_nonnull(choice: str, supplied_null: bool) -> None:
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel

    source = {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard"}
    output = {"sink_name": "result", "plugin": "json", "options": {}, "on_write_failure": "discard"}
    component = source if choice == "on_validation_failure" else output
    if supplied_null:
        component[choice] = None
    else:
        del component[choice]
    with pytest.raises(ValidationError):
        SetPipelineArgumentsModel.model_validate({"source": source, "nodes": [], "edges": [], "outputs": [output]})


def _redact(v: object) -> str:
    return "<redacted>"


def _ok_reason() -> HandlesNoSensitiveDataReason:
    return HandlesNoSensitiveDataReason(
        sensitive_data_locations=("nowhere — see test",),
        why_arguments_safe="A" * 32,
        why_responses_safe="B" * 32,
    )


# ---------------------------------------------------------------------------
# Test 1: Orphan summarizer key not in sensitive_argument_keys raises
# ---------------------------------------------------------------------------


def test_orphan_summarizer_raises() -> None:
    """argument_summarizers key not declared in sensitive_argument_keys raises ValueError."""
    with pytest.raises(ValueError, match="orphan"):
        ToolRedactionPolicy(
            sensitive_argument_keys=("path",),
            known_response_keys=("status",),
            argument_summarizers={"path": _redact, "extra_key": _redact},
        )


def test_known_argument_keys_must_cover_sensitive_arguments_when_declared() -> None:
    """An opt-in argument allowlist must include every sensitive argument key."""
    with pytest.raises(ValueError, match="known_argument_keys"):
        ToolRedactionPolicy(
            sensitive_argument_keys=("path",),
            known_argument_keys=("name",),
            known_response_keys=("status",),
            argument_summarizers={"path": _redact},
        )


# ---------------------------------------------------------------------------
# Test 2: handles_no_sensitive_data=True without reason struct raises
# ---------------------------------------------------------------------------


def test_handles_no_sensitive_data_true_without_reason_raises() -> None:
    """handles_no_sensitive_data=True with no reason struct raises ValueError."""
    with pytest.raises(ValueError, match="handles_no_sensitive_data_reason_struct"):
        ToolRedactionPolicy(
            handles_no_sensitive_data=True,
            handles_no_sensitive_data_reason_struct=None,
        )


# ---------------------------------------------------------------------------
# Test 3: handles_no_sensitive_data=False with non-None reason struct raises
# ---------------------------------------------------------------------------


def test_handles_no_sensitive_data_false_with_reason_raises() -> None:
    """handles_no_sensitive_data=False with a non-None reason struct raises ValueError."""
    with pytest.raises(ValueError, match="handles_no_sensitive_data_reason_struct"):
        ToolRedactionPolicy(
            handles_no_sensitive_data=False,
            handles_no_sensitive_data_reason_struct=_ok_reason(),
            known_response_keys=("status",),
        )


# ---------------------------------------------------------------------------
# Test 4: handles_no_sensitive_data=False without known_response_keys raises
# ---------------------------------------------------------------------------


def test_handles_false_without_known_response_keys_raises() -> None:
    """handles_no_sensitive_data=False with empty known_response_keys raises ValueError."""
    with pytest.raises(ValueError, match="known_response_keys"):
        ToolRedactionPolicy(
            handles_no_sensitive_data=False,
            sensitive_argument_keys=("path",),
            argument_summarizers={"path": _redact},
            known_response_keys=(),
        )


# ---------------------------------------------------------------------------
# Test 5: Valid construction with sensitive data succeeds
# ---------------------------------------------------------------------------


def test_valid_policy_with_sensitive_data_is_constructable() -> None:
    """handles_no_sensitive_data=False with full valid fields constructs without error."""
    policy = ToolRedactionPolicy(
        sensitive_argument_keys=("path",),
        argument_summarizers={"path": _redact},
        known_argument_keys=("path",),
        known_response_keys=("status",),
        handles_no_sensitive_data=False,
        handles_no_sensitive_data_reason_struct=None,
    )
    assert policy.sensitive_argument_keys == ("path",)
    assert policy.known_argument_keys == ("path",)
    assert policy.known_response_keys == ("status",)
    assert policy.handles_no_sensitive_data is False
    assert policy.handles_no_sensitive_data_reason_struct is None


# ---------------------------------------------------------------------------
# Test 6: Container fields are deeply frozen after construction
# ---------------------------------------------------------------------------


def test_container_fields_are_deeply_frozen() -> None:
    """All tuple/Mapping container fields are deeply frozen after __post_init__."""
    policy = ToolRedactionPolicy(
        sensitive_argument_keys=("path",),
        sensitive_response_keys=("body",),
        known_argument_keys=("path",),
        known_response_keys=("status",),
        argument_summarizers={"path": _redact},
        handles_no_sensitive_data=False,
    )

    # Tuples are already immutable; verify they remain tuples (not converted).
    assert isinstance(policy.sensitive_argument_keys, tuple)
    assert isinstance(policy.sensitive_response_keys, tuple)
    assert isinstance(policy.known_argument_keys, tuple)
    assert isinstance(policy.known_response_keys, tuple)

    # argument_summarizers must be wrapped as MappingProxyType (deeply frozen).
    assert isinstance(policy.argument_summarizers, MappingProxyType)

    # The dataclass itself must be immutable (frozen=True prevents assignment).
    with pytest.raises(AttributeError):
        policy.handles_no_sensitive_data = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Queue exposure must not narrow the generic persisted transport model
# (elspeth-a5b86149d4). ``_PipelineNodeModel.node_type`` stays a plain ``str``
# so set_pipeline keeps Tier-3 LLM-recoverable feedback for unknown enum
# values; the closed vocabulary lives on the composer state / review DTOs.
# ---------------------------------------------------------------------------


def test_pipeline_node_transport_model_node_type_stays_open_str() -> None:
    from elspeth.web.composer.redaction import _PipelineNodeModel

    assert _PipelineNodeModel.model_fields["node_type"].annotation is str
    model = _PipelineNodeModel.model_validate({"id": "inbound", "node_type": "queue", "input": "inbound", "options": {"description": "x"}})
    assert model.node_type == "queue"
