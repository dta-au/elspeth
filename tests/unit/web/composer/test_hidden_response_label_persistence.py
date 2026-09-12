"""Hidden response labels describe local positions, never source identities."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine

from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, dispatch_with_audit
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes._helpers import _persist_tool_invocations
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.session_fences import acquire_compose_context
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool
from tests.unit.web.sessions import test_service as session_service_tests

engine = session_service_tests.engine
service = session_service_tests.service

_FIRST_KEY = "z_PRIVATE_SCHEMA_KEY_CANARY"
_SECOND_KEY = "a_PRIVATE_SCHEMA_KEY_CANARY"
_NESTED_KEY = "PRIVATE_NESTED_KEY_CANARY"
_VALUE = "PRIVATE_SCHEMA_VALUE_CANARY"
_TEXT = "<redacted-response-text>"
_LABEL_1 = "_redacted_response_field_1"
_LABEL_2 = "_redacted_response_field_2"


@pytest.mark.parametrize("swap", [False, True], ids=["authored-order", "swapped-order"])
@pytest.mark.parametrize("insert_safe", [False, True], ids=["hidden-only", "safe-between"])
@pytest.mark.asyncio
async def test_real_rejection_schema_labels_and_persisted_associations(
    swap: bool, insert_safe: bool, engine: Engine, service: SessionServiceImpl
) -> None:
    # Different child shapes make a lost, duplicated, or reassigned field visible.
    nested = {"properties": {_NESTED_KEY: {"description": _VALUE}}}
    pairs = [(_FIRST_KEY, nested), (_SECOND_KEY, {"type": "string"})]
    if swap:
        pairs.reverse()
    if insert_safe:
        pairs.insert(1, ("description", {"description": _VALUE}))
    properties = dict(pairs)
    catalog = _mock_catalog()
    catalog.get_schema.return_value = PluginSchemaInfo(
        name="passthrough",
        plugin_type="transform",
        description="Test transform schema",
        json_schema={"properties": properties},
        knob_schema={"fields": []},
    )
    state = _empty_state()
    arguments = {
        "id": "node",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "rows",
        "on_success": "sink",
        "on_error": "discard",
        "options": {},  # Real plugin rejection: the required schema is missing.
    }
    recorder = BufferingRecorder()

    async def dispatch() -> ToolResult:
        return execute_tool("upsert_node", arguments, state, catalog)

    outcome = await dispatch_with_audit(
        recorder=recorder,
        audit=begin_dispatch("hidden-label-test", "upsert_node", arguments, version_before=state.version, actor="test"),
        do_dispatch=dispatch,
        version_after_provider=lambda result: result.updated_state.version,
        arg_error_payload_factory=lambda error: {"error": str(error)},
    )
    assert outcome.result.success is False
    assert outcome.result.validation.errors[0].error_code == "plugin_options_invalid"
    raw = outcome.result.to_dict()
    assert list(raw["plugin_schemas"]["transform/passthrough"]["json_schema"]["properties"]) == list(properties)
    # Positive disclosure control: these really reached the producer response.
    for canary in (_FIRST_KEY, _SECOND_KEY, _NESTED_KEY, _VALUE):
        assert canary in json.dumps(raw)

    direct = redact_tool_call_response("upsert_node", raw, telemetry=NoopRedactionTelemetry())
    direct_properties = direct["plugin_schemas"][_LABEL_1]["_redacted_response_field_3"]["properties"]
    redacted_nested = {"properties": {_LABEL_1: {"description": _TEXT}}}
    redacted_scalar_schema = {"type": _TEXT}
    expected = {_LABEL_1: redacted_scalar_schema if swap else redacted_nested}
    if insert_safe:
        expected["description"] = {"description": _TEXT}
    expected[_LABEL_2] = redacted_nested if swap else redacted_scalar_schema
    assert direct_properties == expected
    assert list(direct_properties) == list(expected)

    assert len(recorder.invocations) == 1
    invocation = recorder.invocations[0]
    content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    stored = json.loads(content)
    # Audit canonicalization sorts original keys before its redactor runs.
    # The a-key's scalar schema is therefore first even when authored second.
    stored_properties = stored["plugin_schemas"][_LABEL_1][_LABEL_2]["properties"]
    expected_stored = {_LABEL_1: redacted_scalar_schema, _LABEL_2: redacted_nested}
    if insert_safe:
        expected_stored["description"] = {"description": _TEXT}
    assert stored_properties == expected_stored
    assert envelope["invocation"]["result_canonical"] == content
    assert envelope["invocation"]["result_hash"] == hashlib.sha256(content.encode()).hexdigest()

    session = await service.create_session("alice", "Hidden label SQL witness", "local")
    async with acquire_compose_context(service, session.id) as context:
        await _persist_tool_invocations(
            service,
            session.id,
            (invocation,),
            composition_state_id=None,
            plugin_crash_pending=False,
            session_operation_context=context,
        )
    with engine.connect() as connection:
        row = connection.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(session.id))).one()
    assert row.role == "audit"
    assert row.content == content
    assert row.tool_calls == [envelope]
    loaded = await service.get_messages(session.id)
    assert len(loaded) == 1
    assert loaded[0].content == row.content
    assert loaded[0].tool_calls == tuple(row.tool_calls)
    reloaded_properties = json.loads(loaded[0].content)["plugin_schemas"][_LABEL_1][_LABEL_2]["properties"]
    assert reloaded_properties == expected_stored
    assert list(reloaded_properties) == list(stored_properties)
    persisted = json.dumps({"content": loaded[0].content, "envelopes": row.tool_calls})
    for canary in (_FIRST_KEY, _SECOND_KEY, _NESTED_KEY, _VALUE):
        assert canary not in json.dumps(direct)
        assert canary not in persisted
