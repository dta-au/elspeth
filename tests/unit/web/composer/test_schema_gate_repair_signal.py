"""S-gate rejections carry a closed repair signal on the compose loop (S1 T9).

The closed-root S gate (``tools/_dispatch.py``) builds one
:class:`SchemaViolation` per jsonschema error and carries them on the raised
:class:`ToolArgumentError` on every path. Only the compose loop renders them:
``arg_error_payload`` emits them as ``validation_errors`` (loc + closed code +
fixed message), the same shape the pydantic canonicaliser emits. A loc names
only schema-declared properties (or the generic ``field``/``item``/``index``
tokens), so the model-authored key of an ``additionalProperties`` failure is
never echoed (plan C10). MCP text and the planner's discovery
``argument_error`` body stay byte-equal to the base (D19, ruling 9).

Plan: ``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`` T9.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator

from elspeth.composer_mcp.server import create_server
from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer.audit import VALIDATION_ERROR_MESSAGES
from elspeth.web.composer.protocol import SchemaViolation, SchemaViolationCode, ToolArgumentError
from elspeth.web.composer.provider_discovery_response import argument_error_response
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._dispatch import require_arguments_conform_to_schema, require_schema_valid_arguments
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.protocol import CompositionStateData

from .conftest import (
    _fake_llm_response,
    _FakeChoice,
    _FakeComposeLLM,
    _FakeFunction,
    _FakeLLMResponse,
    _FakeMessage,
    _FakeToolCall,
)

_KEY_CANARY = "stray_key_canary_sk_live_9Zx4"

_MSG_BOUNDS = "Value is outside allowed bounds"
_MSG_UNEXPECTED = "Unexpected value"
_MSG_MISSING = "Required value is missing"
_MSG_TYPE = "Value has invalid type"
_MSG_CHOICE = "Value is not an allowed choice"
_MSG_INVALID = "Validation failed"


class _RecordingComposeLLM(_FakeComposeLLM):
    """Fake compose LLM that keeps every tool-role message it is sent."""

    def __init__(self, responses: tuple[_FakeLLMResponse, ...]) -> None:
        super().__init__(responses)
        self.tool_messages: list[dict[str, Any]] = []

    async def __call__(self, _messages: Any, _tools: Any) -> _FakeLLMResponse:
        for message in _messages:
            if type(message) is dict and message["role"] == "tool":
                self.tool_messages.append(dict(message))
        return await super().__call__(_messages, _tools)


def _raw_call_llm(*, name: str, raw_arguments: str) -> _RecordingComposeLLM:
    first = _FakeLLMResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall(id="call_repair", function=_FakeFunction(name=name, arguments=raw_arguments))],
                )
            )
        ]
    )
    return _RecordingComposeLLM((first, _fake_llm_response(content="Done.")))


async def _compose_arg_error(
    service: ComposerServiceImpl, session_id: str, *, name: str, arguments: object, current_state_id: str | None = None
) -> tuple[dict[str, Any], str]:
    """Run one compose-loop turn with one bad call; return (audited payload, LLM tool-message text)."""
    llm = _raw_call_llm(name=name, raw_arguments=json.dumps(arguments))
    result = await service._run_one_turn_for_test(llm=llm, session_id=session_id, current_state_id=current_state_id)
    assert len(result.tool_invocations) == 1
    invocation: ComposerToolInvocation = result.tool_invocations[0]
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert invocation.error_class == "ToolArgumentError"
    assert invocation.result_canonical is not None
    audited = cast(dict[str, Any], json.loads(invocation.result_canonical))
    sent = [message for message in llm.tool_messages if message["tool_call_id"] == "call_repair"]
    assert len(sent) == 1
    content = sent[0]["content"]
    assert type(content) is str
    # The model reads exactly what the audit row records.
    assert json.loads(content) == audited
    return audited, content


# ---------------------------------------------------------------------------
# Compose loop: an ordinary tool through tool_batch's arg_error_payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_bounds_limit_names_the_declared_field(fake_composer_service: ComposerServiceImpl, result_session_id: str) -> None:
    """RED premise (measured, ``t9-baseline-texts.log``): today the payload is the error text alone."""
    audited, _ = await _compose_arg_error(fake_composer_service, result_session_id, name="list_models", arguments={"limit": 0})

    assert audited["validation_errors"] == [{"loc": ["limit"], "msg": _MSG_BOUNDS, "type": "out_of_bounds"}]
    assert "repair_instruction" not in audited
    # The existing error text is unchanged; the signal is additive.
    assert audited["error"] == (
        "Tool 'list_models' failed: 'tool arguments' must be an object conforming to the declared argument schema, got invalid_schema"
    )


@pytest.mark.asyncio
async def test_extra_key_is_unexpected_at_its_parent_and_never_echoed(
    fake_composer_service: ComposerServiceImpl, result_session_id: str
) -> None:
    audited, content = await _compose_arg_error(
        fake_composer_service, result_session_id, name="list_models", arguments={"limit": 5, _KEY_CANARY: 1}
    )

    assert audited["validation_errors"] == [{"loc": [], "msg": _MSG_UNEXPECTED, "type": "unexpected"}]
    assert _KEY_CANARY not in content
    assert "stray_key_canary" not in json.dumps(audited)


@pytest.mark.asyncio
@pytest.mark.parametrize("unexpected_key", ["x", _KEY_CANARY])
async def test_zero_argument_tool_rejection_tells_planner_to_send_empty_object(
    fake_composer_service: ComposerServiceImpl, result_session_id: str, unexpected_key: str
) -> None:
    audited, content = await _compose_arg_error(
        fake_composer_service, result_session_id, name="preview_pipeline", arguments={unexpected_key: 1}
    )

    assert audited["validation_errors"] == [{"loc": [], "msg": _MSG_UNEXPECTED, "type": "unexpected"}]
    assert audited["repair_instruction"] == "This tool takes no arguments. Call it with {}."
    assert json.dumps(unexpected_key) not in content


@pytest.mark.asyncio
async def test_root_extra_property_repair_names_only_declared_properties(
    fake_composer_service: ComposerServiceImpl, result_session_id: str
) -> None:
    audited, content = await _compose_arg_error(
        fake_composer_service, result_session_id, name="list_models", arguments={"limit": 5, _KEY_CANARY: 1}
    )

    assert audited["repair_instruction"] == "Remove unsupported root properties. Allowed properties: limit, provider."
    assert _KEY_CANARY not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"_elspeth_no_arguments": False},
        {"_elspeth_no_arguments": "true"},
        {"_elspeth_no_arguments": True, _KEY_CANARY: 1},
        {_KEY_CANARY: 1},
    ],
    ids=["missing", "false", "string", "extra", "unknown"],
)
async def test_strict_zero_argument_rejection_names_wire_marker(
    fake_composer_service: ComposerServiceImpl, result_session_id: str, arguments: dict[str, Any]
) -> None:
    fake_composer_service._planner_dialect = ToolContractDialect.OPENAI_STRICT
    audited, content = await _compose_arg_error(fake_composer_service, result_session_id, name="preview_pipeline", arguments=arguments)

    assert audited["error"] == (
        "Tool 'preview_pipeline' takes no semantic arguments. Call it with {\"_elspeth_no_arguments\": true} exactly."
    )
    assert "{}" not in content
    assert "set_pipeline" not in content
    assert _KEY_CANARY not in content


@pytest.mark.asyncio
async def test_strict_marker_rejection_allows_repaired_tool_call_with_composition_budget_one(
    fake_composer_service: ComposerServiceImpl, result_session_id: str
) -> None:
    fake_composer_service._planner_dialect = ToolContractDialect.OPENAI_STRICT
    fake_composer_service._max_composition_turns = 1
    fake_composer_service._max_discovery_turns = 3
    llm = _RecordingComposeLLM(
        (
            _fake_llm_response(tool_calls=({"id": "bad", "name": "preview_pipeline", "arguments": {}},)),
            _fake_llm_response(tool_calls=({"id": "repaired", "name": "preview_pipeline", "arguments": {"_elspeth_no_arguments": True}},)),
            _fake_llm_response(content="Done."),
        )
    )

    result = await fake_composer_service._run_one_turn_for_test(llm=llm, session_id=result_session_id)

    assert [(call.tool_call_id, call.status) for call in result.tool_invocations] == [
        ("bad", ComposerToolStatus.ARG_ERROR),
        ("repaired", ComposerToolStatus.SUCCESS),
    ]


# A nested ``required`` failure cannot reach the S gate through the compose
# loop (measured, t9-red2.log): ``splice_transform.node.*`` is stopped first
# by the required-paths pre-check (MISSING_REQUIRED_PATH), and set_pipeline's
# ``sources.<name>.*`` by the set_pipeline candidate's pydantic model, whose
# own canonicaliser renders it. So the next two tests drive the gate directly
# and render through the same ``arg_error_payload`` the loop calls.


def _rendered(tool_name: str, arguments: dict[str, Any]) -> str:
    with pytest.raises(ToolArgumentError) as caught:
        require_schema_valid_arguments(tool_name, arguments)
    assert caught.value.__cause__ is None  # the S gate itself, not a pydantic wrap
    return json.dumps(arg_error_payload(caught.value, tool_name))


def test_missing_nested_required_names_the_declared_property() -> None:
    arguments = {"predecessor_id": "source", "successor_id": "sink", "node": {"plugin": "passthrough", "options": {}}}

    assert json.loads(_rendered("splice_transform", arguments))["validation_errors"] == [
        {"loc": ["node", "id"], "msg": _MSG_MISSING, "type": "missing"}
    ]


def test_nested_extra_property_does_not_receive_root_repair_guidance() -> None:
    arguments = {
        "predecessor_id": "source",
        "successor_id": "sink",
        "node": {"id": "map", "plugin": "passthrough", "options": {}, _KEY_CANARY: 1},
    }
    rendered = _rendered("splice_transform", arguments)
    payload = json.loads(rendered)

    assert payload["validation_errors"] == [{"loc": ["node"], "msg": _MSG_UNEXPECTED, "type": "unexpected"}]
    assert "repair_instruction" not in payload
    assert _KEY_CANARY not in rendered


@pytest.mark.parametrize(
    "source_key",
    [
        pytest.param("my key!", id="non-identifier"),
        # ``plugin`` is a declared name one level down, not at ``sources``,
        # whose members are model-named: it is still generic there.
        pytest.param("plugin", id="undeclared-identifier"),
    ],
)
def test_undeclared_path_segment_becomes_generic(source_key: str) -> None:
    rendered = _rendered("set_pipeline", {"sources": {source_key: {}}, "nodes": [], "edges": [], "outputs": []})

    assert json.loads(rendered)["validation_errors"] == [
        {"loc": ["sources", "item", "plugin"], "msg": _MSG_MISSING, "type": "missing"},
        {"loc": ["sources", "item", "on_success"], "msg": _MSG_MISSING, "type": "missing"},
    ]
    if source_key == "my key!":
        assert source_key not in rendered


@pytest.mark.asyncio
async def test_more_than_eight_violations_collapse_to_one_truncated_entry(
    fake_composer_service: ComposerServiceImpl, result_session_id: str
) -> None:
    # Nine wrong-typed items: nine jsonschema errors, one past the cap.
    arguments = {"blob_ids": list(range(9)), "on_success": "rows"}

    audited, _ = await _compose_arg_error(fake_composer_service, result_session_id, name="set_source_from_blobs", arguments=arguments)

    assert audited["validation_errors"] == [{"loc": [], "msg": "Validation produced more than 8 errors", "type": "truncated"}]


# ---------------------------------------------------------------------------
# Compose loop: the session-aware carve-out through service.py's
# arg_error_payload site. (The advisor carve-out renders its own fixed
# AdvisorArgumentRejection text, not arg_error_payload, so it is unchanged.)
# ---------------------------------------------------------------------------


async def _seed_state(service: ComposerServiceImpl, session_id: str) -> str:
    sessions = service._sessions_service
    assert sessions is not None
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).to_dict()
    record = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(nodes=state["nodes"], sources=state["sources"], metadata_=state["metadata"], is_valid=True),
        provenance="tool_call",
    )
    return str(record.id)


@pytest.mark.asyncio
async def test_interpretation_review_extra_key_is_unexpected_and_not_echoed(
    fake_composer_service: ComposerServiceImpl, result_session_id: str
) -> None:
    state_id = await _seed_state(fake_composer_service, result_session_id)
    arguments = {"affected_node_id": "n1", "kind": "vague_term", "user_term": "term", _KEY_CANARY: True}

    audited, content = await _compose_arg_error(
        fake_composer_service, result_session_id, name="request_interpretation_review", arguments=arguments, current_state_id=state_id
    )

    assert audited["validation_errors"] == [{"loc": [], "msg": _MSG_UNEXPECTED, "type": "unexpected"}]
    assert _KEY_CANARY not in content


# ---------------------------------------------------------------------------
# The gate itself: code mapping, loc walk and truncation input.
# ---------------------------------------------------------------------------


def _gate_violations(tool_name: str, arguments: dict[str, Any]) -> tuple[SchemaViolation, ...]:
    with pytest.raises(ToolArgumentError) as caught:
        require_schema_valid_arguments(tool_name, arguments)
    return caught.value.schema_violations


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        pytest.param("list_models", {"limit": "5"}, (("limit",), SchemaViolationCode.INVALID_TYPE), id="type"),
        pytest.param(
            "request_advisor_hint",
            {"trigger": "nope", "problem_summary": "s", "recent_errors": [], "attempted_actions": []},
            (("trigger",), SchemaViolationCode.INVALID_CHOICE),
            id="enum",
        ),
        pytest.param("clear_source", {"source_name": ""}, (("source_name",), SchemaViolationCode.OUT_OF_BOUNDS), id="minLength"),
        pytest.param(
            "splice_transform",
            {"predecessor_id": "source", "successor_id": "sink", "node": {"id": "fork", "plugin": "passthrough", "options": {}}},
            (("node", "id"), SchemaViolationCode.INVALID),
            id="not",
        ),
        pytest.param(
            "request_advisor_hint",
            {"trigger": "proactive_security_safety", "problem_summary": "s", "recent_errors": [7], "attempted_actions": []},
            (("recent_errors", "index"), SchemaViolationCode.INVALID_TYPE),
            id="array-index",
        ),
    ],
)
def test_validator_keyword_maps_to_a_closed_code(
    tool_name: str, arguments: dict[str, Any], expected: tuple[tuple[str, ...], SchemaViolationCode]
) -> None:
    loc, code = expected
    assert _gate_violations(tool_name, arguments) == (SchemaViolation(loc=loc, code=code),)


def test_gate_keeps_every_error_for_the_renderer_to_truncate() -> None:
    violations = _gate_violations(
        "request_advisor_hint",
        {"trigger": "proactive_security_safety", "problem_summary": "s", "recent_errors": list(range(9)), "attempted_actions": []},
    )
    # One maxItems bound plus one type fault per item: ten, one per jsonschema error.
    assert violations == (
        SchemaViolation(loc=("recent_errors",), code=SchemaViolationCode.OUT_OF_BOUNDS),
        *(SchemaViolation(loc=("recent_errors", "index"), code=SchemaViolationCode.INVALID_TYPE) for _ in range(9)),
    )


def test_caller_owned_schema_is_walked_the_same_way() -> None:
    """MCP session tools admit through a caller-owned schema; the exception carries violations there too.

    A root-level model-named key is ``field``; the loc is capped at four segments.
    """
    schema = {
        "type": "object",
        "properties": {
            "a": {
                "type": "object",
                "properties": {
                    "b": {
                        "type": "object",
                        "properties": {
                            "c": {"type": "object", "properties": {"d": {"type": "object", "properties": {"e": {"type": "integer"}}}}}
                        },
                    }
                },
            }
        },
        "additionalProperties": {"type": "integer"},
    }
    validator = Draft202012Validator(schema)
    with pytest.raises(ToolArgumentError) as caught:
        require_arguments_conform_to_schema("synthetic_tool", validator, {"a": {"b": {"c": {"d": {"e": "x"}}}}, "model named": "x"})

    assert caught.value.schema_violations == (
        SchemaViolation(loc=("a", "b", "c", "d"), code=SchemaViolationCode.INVALID_TYPE),
        SchemaViolation(loc=("field",), code=SchemaViolationCode.INVALID_TYPE),
    )


# ---------------------------------------------------------------------------
# The carrier: ToolArgumentError.schema_violations is closed and frozen.
# ---------------------------------------------------------------------------


def _carrier(violations: tuple[SchemaViolation, ...] = ()) -> ToolArgumentError:
    return ToolArgumentError(argument="limit", expected="a valid value", actual_type="int", schema_violations=violations)


def test_violations_default_to_none_and_render_nothing() -> None:
    exc = ToolArgumentError(argument="limit", expected="a valid value", actual_type="int")
    assert exc.schema_violations == ()
    assert "validation_errors" not in arg_error_payload(exc, "list_models")


def test_violations_are_frozen_after_construction() -> None:
    exc = _carrier((SchemaViolation(loc=("limit",), code=SchemaViolationCode.OUT_OF_BOUNDS),))
    with pytest.raises(AttributeError, match="frozen after construction"):
        exc.schema_violations = ()
    with pytest.raises(AttributeError, match="frozen after construction"):
        exc._safe_schema_violations = ()


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([SchemaViolation(loc=("limit",), code=SchemaViolationCode.OUT_OF_BOUNDS)], id="list-not-tuple"),
        pytest.param(({"loc": ["limit"], "type": "out_of_bounds"},), id="dict-item"),
    ],
)
def test_carrier_rejects_anything_but_a_tuple_of_owned_violations(bad: object) -> None:
    with pytest.raises(ValueError, match="schema_violations"):
        ToolArgumentError(argument="limit", expected="a valid value", actual_type="int", schema_violations=cast(Any, bad))


@pytest.mark.parametrize(
    ("loc", "code"),
    [
        pytest.param(["limit"], SchemaViolationCode.OUT_OF_BOUNDS, id="list-loc"),
        pytest.param(("a", "b", "c", "d", "e"), SchemaViolationCode.OUT_OF_BOUNDS, id="too-deep"),
        pytest.param(("",), SchemaViolationCode.OUT_OF_BOUNDS, id="empty-segment"),
        pytest.param((7,), SchemaViolationCode.OUT_OF_BOUNDS, id="int-segment"),
        pytest.param(("limit",), "out_of_bounds", id="str-code"),
        pytest.param(("limit",), "truncated", id="truncated-is-not-a-violation"),
    ],
)
def test_violation_is_closed_at_construction(loc: object, code: object) -> None:
    with pytest.raises(ValueError, match="SchemaViolation"):
        SchemaViolation(loc=cast(Any, loc), code=cast(Any, code))


@pytest.mark.parametrize("replacement", ("VIOLATION_CANARY_sk_live_3Rt8", ("VIOLATION_CANARY_sk_live_3Rt8",), 7))
def test_corrupt_private_violations_fall_back_to_nothing(replacement: object) -> None:
    exc = _carrier((SchemaViolation(loc=("limit",), code=SchemaViolationCode.OUT_OF_BOUNDS),))
    BaseException.__setattr__(exc, "_safe_schema_violations", replacement)
    BaseException.__setattr__(exc, "schema_violations", ("VIOLATION_CANARY_sk_live_3Rt8",))

    assert exc.schema_violations == ()
    payload = arg_error_payload(exc, "list_models")
    assert "validation_errors" not in payload
    assert "VIOLATION_CANARY" not in json.dumps(payload)


def test_a_missing_private_violations_slot_is_a_framework_bug() -> None:
    """The constructor always binds the slot; only reflection can remove it."""
    exc = _carrier((SchemaViolation(loc=("limit",), code=SchemaViolationCode.OUT_OF_BOUNDS),))
    BaseException.__delattr__(exc, "_safe_schema_violations")
    with pytest.raises(FrameworkBugError, match="schema violations are missing"):
        _ = exc.schema_violations


def test_a_pydantic_cause_still_takes_precedence() -> None:
    """The existing pydantic canonicaliser path is unchanged: a cause wins over carried violations."""
    from pydantic import BaseModel, ConfigDict
    from pydantic import ValidationError as PydanticValidationError

    class _Probe(BaseModel):
        model_config = ConfigDict(strict=True)
        count: int

    try:
        _Probe.model_validate({"count": "x"})
    except PydanticValidationError as cause:
        exc = _carrier((SchemaViolation(loc=("limit",), code=SchemaViolationCode.OUT_OF_BOUNDS),))
        exc.__cause__ = cause
    payload = arg_error_payload(exc, "list_models")
    assert payload["validation_errors"] == [{"loc": ["count"], "msg": _MSG_TYPE, "type": "invalid_type"}]


def test_every_violation_code_has_a_fixed_message_shared_with_the_canonicaliser() -> None:
    assert {code.value for code in SchemaViolationCode} == {
        "missing",
        "unexpected",
        "invalid_choice",
        "invalid_type",
        "out_of_bounds",
        "invalid",
    }
    assert {code.value for code in SchemaViolationCode} <= set(VALIDATION_ERROR_MESSAGES)
    assert VALIDATION_ERROR_MESSAGES["out_of_bounds"] == _MSG_BOUNDS
    assert VALIDATION_ERROR_MESSAGES["unexpected"] == _MSG_UNEXPECTED
    assert VALIDATION_ERROR_MESSAGES["missing"] == _MSG_MISSING
    assert VALIDATION_ERROR_MESSAGES["invalid_type"] == _MSG_TYPE
    assert VALIDATION_ERROR_MESSAGES["invalid_choice"] == _MSG_CHOICE
    assert VALIDATION_ERROR_MESSAGES["invalid"] == _MSG_INVALID


# ---------------------------------------------------------------------------
# Unchanged surfaces (characterization; D19, ruling 9). Literals measured on
# the base in ``t9-baseline-texts.log``.
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.invocations: list[ComposerToolInvocation] = []

    def record(self, invocation: ComposerToolInvocation) -> None:
        self.invocations.append(invocation)

    def resolve_session(self, session_id: str) -> None:
        return


async def _mcp_error_text(name: str, arguments: dict[str, Any]) -> str:
    from mcp.types import CallToolRequest, CallToolRequestParams

    with tempfile.TemporaryDirectory() as td:
        server = create_server(
            create_catalog_service(), Path(td), recorder=_Recorder(), runtime_preflight=None, runtime_preflight_settings_hash=None
        )
        request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
        response = await server.request_handlers[CallToolRequest](request)
    assert response.root.isError is True
    text = response.root.content[0].text
    assert type(text) is str
    return text


_MCP_TYPE_TEXT = (
    "Tool error: 'tool arguments' must be an object conforming to the declared argument schema. Match the tool's declared "
    "JSON types. Supply object and array fields as actual JSON objects and arrays, not strings containing JSON., got invalid_schema"
)
_MCP_PLAIN_TEXT = "Tool error: 'tool arguments' must be an object conforming to the declared argument schema, got invalid_schema"


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        pytest.param("new_session", {"name": 123}, _MCP_TYPE_TEXT, id="session-tool-type"),
        pytest.param("new_session", {"name": "x", _KEY_CANARY: 1}, _MCP_PLAIN_TEXT, id="session-tool-extra-key"),
        pytest.param("list_models", {"limit": 0}, _MCP_PLAIN_TEXT, id="composer-tool-bound"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_error_text_is_unchanged(name: str, arguments: dict[str, Any], expected: str) -> None:
    """Characterization: MCP builds its own text and does not render violations."""
    assert await _mcp_error_text(name, arguments) == expected


def test_planner_discovery_argument_error_body_is_unchanged() -> None:
    """Characterization: planner discovery projects the error without its message or violations."""
    with pytest.raises(ToolArgumentError) as caught:
        require_schema_valid_arguments("list_models", {"limit": 0})
    assert caught.value.schema_violations  # the exception does carry them
    assert json.dumps(argument_error_response(caught.value).to_wire()) == (
        '{"argument_error": {"component": "tool arguments", "severity": "high", '
        '"error_code": "SCHEMA_VALIDATION", "error_class": "ToolArgumentError"}}'
    )
