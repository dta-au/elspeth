"""Natural cap refusal at manual/checkpoint owners preserves prior attempts.

The full manual loop uses the established independent END stub. The EARLY/END
controls invoke the captured real checkpoint method, real preparation and real
SQL accounting, replacing only SDK transport and observing current inputs.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from uuid import UUID

import litellm
import pytest
import rfc8785
from litellm.exceptions import ServiceUnavailableError
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import advisor_checkpoint, advisor_context, provider_gateway, tool_batch
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.manual_advisor_allowance import ManualAdvisorAllowance
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.protocol import ComposerAdmissionRefused, ComposerContextUnsupported
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.state import PipelineMetadata
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.helpers.composer_sdk import ComposerSDKScript
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures
from tests.unit.web.composer import test_advisor_refusal_accounting as refusal_fixtures
from tests.unit.web.composer import test_advisor_tool as advisor_fixtures
from tests.unit.web.composer import test_compose_loop_interpretation_review_dispatch as review_fixtures

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean
_composer_available_by_default = advisor_fixtures._composer_available_by_default
make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state
physical_advisor = checkpoint_fixtures.physical_advisor
_REAL_CHECKPOINT = AdvisorCheckpointOwner._run_advisor_checkpoint
_USER = "Review the ordinary declared inputs."


@pytest.mark.asyncio
async def test_natural_manual_cap_refusal_releases_reservation_and_keeps_prior_primary_audit(tmp_path, monkeypatch):
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    observed = refusal_fixtures._observe_dispatches(monkeypatch)
    execute = tool_batch._execute_continuation_call_live
    settled = []

    async def require_actual_closed_reservation(**arguments):
        try:
            return await execute(**arguments)
        finally:
            allowance = arguments["batch"].manual_advisor_allowance
            assert type(allowance) is ManualAdvisorAllowance
            allowance.require_settled(used=0)
            assert allowance.available == 1
            settled.append(allowance)

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", require_actual_closed_reservation)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[refusal_fixtures._manual_call(service._provider_gateway._model, "manual-natural-cap")],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[],
    )
    preparations = []
    prepare = advisor_checkpoint.prepare_provider_input

    def prepare_actual(**arguments):
        preparations.append(arguments["max_bytes"])
        return prepare(**arguments)

    monkeypatch.setattr(advisor_checkpoint, "prepare_provider_input", prepare_actual)

    async def primary_then_reduce_current_limit(**kwargs):
        result = await script(**kwargs)
        assert kwargs["model"] == service._provider_gateway._model
        current = service._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
        service._settings = current
        service._provider_gateway._settings = current
        service._advisor_checkpoint._settings = current
        return result

    monkeypatch.setattr(litellm, "acompletion", primary_then_reduce_current_limit)
    with pytest.raises(ComposerContextUnsupported) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)
    refused = caught.value
    assert refused.reason == "prepared_request_bytes"
    assert script.primary.await_count == 1 and script.advisor.await_count == 0
    assert preparations == [1] and len(observed) == 1 and observed[0].used == 0
    assert len(settled) == 1
    assert len(refused.llm_calls) == 1 and refused.llm_calls[0].model_requested == service._provider_gateway._model
    rows = composer_helpers._persisted_tool_responses(service, session_id)
    assert len(rows) == 1 and rows[0]["tool_call_id"] == "manual-natural-cap"
    payload = json.loads(rows[0]["result_canonical"])
    assert payload["success"] is False and payload["outbound_call_made"] is False
    assert payload["budget_used"] == 0 and payload["budget_remaining"] == 1
    assert all(payload[key] == value for key, value in ComposerContextUnsupported("prepared_request_bytes").to_payload().items())
    with service._require_sessions_service()._engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        audit = connection.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).all()
    assert len(attempts) == 1 and attempts[0].attempt_id == refused.llm_calls[0].call_id
    assert attempts[0].settled_at is not None and attempts[0].ledger_entry_id is not None
    guard_roles = [
        envelope["check"]["role"] for row in audit for envelope in row.tool_calls or () if envelope["_kind"] == "prompt_guard_audit"
    ]
    assert guard_roles == ["composer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("site,max_turns", [("next_primary", 15), ("final_chance", 1)])
@pytest.mark.parametrize("admission", ["context", "credential", "native", "integrity", "serialization"])
async def test_primary_preparation_refusals_keep_committed_effect_calls_without_replay(tmp_path, monkeypatch, site, max_turns, admission):
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(),
        settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path).model_copy(
            update={"composer_max_composition_turns": max_turns}
        ),
    )
    primary = refusal_fixtures._manual_call(service._provider_gateway._model, "committed-advisor-before-primary-cap")
    first_call = primary.choices[0].message.tool_calls[0].model_dump(mode="json")
    primary = litellm.ModelResponse(
        model=service._provider_gateway._model,
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "committed-metadata-before-primary-cap",
                            "type": "function",
                            "function": {
                                "name": "set_metadata",
                                "arguments": json.dumps({"patch": {"name": "Preserved before size refusal"}}),
                            },
                        },
                        first_call,
                    ],
                }
            }
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[primary],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[refusal_fixtures._reply(service._settings.composer_advisor_model)],
    )
    classified = []
    classify = service._classify_and_budget_turn

    async def observe_committed_baseline(**arguments):
        classified.append(arguments["persist"].failed_turn)
        return await classify(**arguments)

    monkeypatch.setattr(service, "_classify_and_budget_turn", observe_committed_baseline)
    prepare = provider_gateway.prepare_provider_input
    preparation_count = 0
    originals, callers = [], []
    delivery = service._prepare_primary_delivery

    def observe_delivery(**arguments):
        callers.append(sys._getframe(1).f_code.co_name)
        return delivery(**arguments)

    def inject_actual_producer_fault(**arguments):
        nonlocal preparation_count
        preparation_count += 1
        if preparation_count == 2 and admission != "context":
            # Controlled current-producer faults exercise the checks moved out
            # of the old audit try. The real preparer authors the refusal.
            if admission == "credential":
                tail = {"role": "user", "content": "x" * 50_000 + " https://fixture-user:fixture-pass@example.test"}
            elif admission == "native":
                tail = {"role": "assistant", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": ""}]}
            elif admission == "integrity":
                tail = {"role": "user", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}]}
            else:
                tail = {"role": "user", "content": "", "controlled_number": 2**53}
            arguments["messages"] = [*arguments["messages"], tail]
        try:
            return prepare(**arguments)
        except (ComposerAdmissionRefused, AuditIntegrityError, ValueError) as original:
            originals.append(original)
            raise

    monkeypatch.setattr(service, "_prepare_primary_delivery", observe_delivery)
    monkeypatch.setattr(provider_gateway, "prepare_provider_input", inject_actual_producer_fault)

    async def lower_only_after_successful_advisor(**arguments):
        result = await script(**arguments)
        if arguments["model"] == service._settings.composer_advisor_model:
            current = service._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
            service._settings = current
            service._provider_gateway._settings = current
            service._advisor_checkpoint._settings = current
        return result

    monkeypatch.setattr(litellm, "acompletion", lower_only_after_successful_advisor)
    expected = {
        "context": ComposerContextUnsupported,
        "credential": CredentialMaterialRefused,
        "native": ComposerAdmissionRefused,
        "integrity": AuditIntegrityError,
        "serialization": rfc8785.IntegerDomainError,
    }[admission]
    with pytest.raises(expected) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)
    refused = caught.value
    if admission == "serialization":
        assert isinstance(refused, expected)
    else:
        assert type(refused) is expected
    assert preparation_count == 2 and len(originals) == 1
    if admission == "context":
        assert refused.reason == "prepared_request_bytes"
        assert refused.tool_invocations == () and refused.partial_state is None
        assert refused.failed_turn is classified[0]
        assert refused.__cause__ is originals[0]
    else:
        assert refused is originals[0], "moved credential/native errors keep their exact identity"
    assert callers == ["_call_model_turn", "_call_model_turn" if site == "next_primary" else "_classify_and_budget_turn"]
    assert script.primary.await_count == script.advisor.await_count == 1
    assert len(refused.llm_calls) == 2 and len({call.call_id for call in refused.llm_calls}) == 2
    assert len(classified) == 1 and classified[0] is not None
    rows = composer_helpers._persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["committed-metadata-before-primary-cap", "committed-advisor-before-primary-cap"]
    current = await service._require_sessions_service().get_current_state(UUID(session_id))
    assert current is not None and current.metadata_["name"] == "Preserved before size refusal"
    with service._require_sessions_service()._engine.connect() as connection:
        attempts = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == session_id)
        ).all()
        audit = connection.execute(
            select(chat_messages_table.c.tool_calls).where(
                chat_messages_table.c.session_id == session_id, chat_messages_table.c.role == "audit"
            )
        ).all()
    assert {row.attempt_id for row in attempts} == {call.call_id for call in refused.llm_calls} and len(attempts) == 2
    assert all(row.settled_at is not None and row.ledger_entry_id is not None for row in attempts)
    guard_roles = [
        envelope["check"]["role"] for row in audit for envelope in row.tool_calls or () if envelope["_kind"] == "prompt_guard_audit"
    ]
    assert guard_roles == ["composer", "advisor"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["early", "end"])
@pytest.mark.parametrize("admission", ["context", "credential", "native", "integrity", "serialization"])
async def test_actual_checkpoint_transport_retry_reprepares_and_preparation_exit_keeps_first_call(
    make_service, simple_state, physical_advisor, monkeypatch, phase, admission
):
    service = make_service()
    state = replace(simple_state, metadata=PipelineMetadata(description="Complete ordinary graph description. " * 100))
    fenced = physical_advisor.bind(service, review_state=state, request=_USER)
    if phase == "early":
        fenced.pop("publication_review")
    materialize = advisor_context.materialize_advisor_complete_values
    source_reads, limits, packets, physical = [], [], [], []
    prepare = advisor_checkpoint.prepare_provider_input
    originals = []

    def observe_source(*arguments, **keywords):
        values = materialize(*arguments, **keywords)
        source_reads.append(values)
        return values

    def observe_prepare(**arguments):
        limits.append(arguments["max_bytes"])
        if len(limits) == 2 and admission != "context":
            if admission == "serialization":
                source = arguments["messages"]
                arguments["messages"] = [{**source[0], "controlled_number": 2**53}, *source[1:]]
            else:
                if admission == "credential":
                    tail = {
                        "role": "assistant",
                        "content": "",
                        "thinking_blocks": [{"type": "redacted_thinking", "data": "https://fixture-user:fixture-pass@example.test"}],
                    }
                elif admission == "native":
                    tail = {"role": "assistant", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": ""}]}
                else:
                    tail = {"role": "user", "content": "", "thinking_blocks": [{"type": "redacted_thinking", "data": "opaque"}]}
                arguments["messages"] = [*arguments["messages"], tail]
        try:
            packet = prepare(**arguments)
        except (ComposerAdmissionRefused, AuditIntegrityError, ValueError) as original:
            originals.append(original)
            raise
        packets.append(packet)
        return packet

    monkeypatch.setattr(advisor_context, "materialize_advisor_complete_values", observe_source)
    monkeypatch.setattr(advisor_checkpoint, "prepare_provider_input", observe_prepare)

    async def fail_first_physical(**kwargs):
        physical.append(kwargs)
        assert len(physical) == 1, "second preparation must refuse before physical SDK entry"
        assert "prepared_input" not in kwargs and "prepared_advisor_content" not in kwargs
        current = service._advisor_checkpoint._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
        service._advisor_checkpoint._settings = current
        raise ServiceUnavailableError("PRIVATE controlled retryable outage", model=kwargs["model"], llm_provider="openai")

    monkeypatch.setattr(litellm, "acompletion", fail_first_physical)
    recorder = BufferingRecorder()
    expected = {
        "context": ComposerContextUnsupported,
        "credential": CredentialMaterialRefused,
        "native": ComposerAdmissionRefused,
        "integrity": AuditIntegrityError,
        "serialization": rfc8785.IntegerDomainError,
    }[admission]
    with prompt_guard_user_scope(_USER), pytest.raises(expected) as caught:
        await _REAL_CHECKPOINT(service._advisor_checkpoint, phase=phase, state=state, recorder=recorder, **fenced)
    assert len(originals) == 1
    if admission == "context":
        assert caught.value.reason == "prepared_request_bytes"
    else:
        assert caught.value is originals[0]
    assert len(source_reads) == len(limits) == 2 and all(source_reads)
    assert limits[0] == 2 * 1024 * 1024 and limits[1] == 1
    assert len(packets) == len(physical) == len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert caught.value.llm_calls == (call,) and call.status.name == "API_ERROR"
    assert call.private_request == capture_provider_context(messages=physical[0]["messages"], tools=None)
    assert packets[0].evidence.messages == physical[0]["messages"]
    with physical_advisor.engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
        audit = connection.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).all()
    assert len(attempts) == 1 and attempts[0].attempt_id == call.call_id and attempts[0].settled_at is not None
    kinds = [envelope["_kind"] for row in audit for envelope in row.tool_calls or ()]
    assert kinds.count("prompt_guard_audit") == 1
    assert "advisor_checkpoint_pass_audit" not in kinds and "advisor_terminal_publication_audit" not in kinds


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["integrity", "credential"])
async def test_actual_primary_transport_retry_keeps_prior_call_on_reprepare_exit(tmp_path, monkeypatch, failure):
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(data_dir=tmp_path)
    )
    reprepare = service._provider_gateway.reprepare_input
    physical, originals, fresh = [], [], []

    async def outage(**arguments):
        physical.append(arguments)
        assert len(physical) == 1, "re-preparation exit must precede a second SDK dispatch"
        raise ServiceUnavailableError("PRIVATE controlled retryable outage", model=arguments["model"], llm_provider="openai")

    def real_reprepare_exit(previous):
        try:
            if failure == "integrity":
                # Change the actual stale carrier only after the first call
                # has been recorded/settled; the real verifier refuses it.
                previous.evidence.messages[-1]["content"] += " changed"
                return reprepare(previous)
            fresh.append(reprepare(previous))
            require_no_credential_material("https://fixture-user:fixture-pass@example.test", surface="composer_provider_request")
            raise AssertionError("the real credential producer must refuse")
        except (AuditIntegrityError, CredentialMaterialRefused) as original:
            originals.append(original)
            raise

    monkeypatch.setattr(litellm, "acompletion", outage)
    monkeypatch.setattr(service._provider_gateway, "reprepare_input", real_reprepare_exit)
    expected = AuditIntegrityError if failure == "integrity" else CredentialMaterialRefused
    with pytest.raises(expected) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)
    error = caught.value
    assert originals == [error] and len(physical) == len(error.llm_calls) == 1
    assert len(fresh) == int(failure == "credential")
    call = error.llm_calls[0]
    assert call.status.name == "API_ERROR"
    assert call.private_request == capture_provider_context(messages=physical[0]["messages"], tools=physical[0].get("tools"))
    with service._require_sessions_service()._engine.connect() as connection:
        attempts = connection.execute(select(quota_provider_attempts_table)).all()
    assert len(attempts) == 1 and attempts[0].attempt_id == call.call_id
    assert attempts[0].settled_at is not None and attempts[0].ledger_entry_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", [False, True], ids=["actual-reply-positive", "actual-reply-cap-refusal"])
async def test_actual_staged_review_reply_preparation_keeps_prior_calls_and_committed_effects(tmp_path, monkeypatch, refuse):
    seed, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(data_dir=tmp_path)
    )
    service = review_fixtures._build_composer(tmp_path, seed._require_sessions_service())
    primary = []
    for call_id, name, arguments in (
        ("reply-seed-pipeline", "set_pipeline", {"pipeline": review_fixtures._set_pipeline_with_pending_interpretation_args()}),
        (
            "reply-stage-review",
            "request_interpretation_review",
            {
                "affected_node_id": "rate_node",
                "kind": "vague_term",
                "user_term": "cool",
                "llm_draft": "modern, useful, engaging, and clear for the public.",
            },
        ),
    ):
        primary.append(
            litellm.ModelResponse(
                model=service._provider_gateway._model,
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
                            ],
                        }
                    }
                ],
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
        )
    if not refuse:
        primary.append(
            litellm.ModelResponse(
                model=service._provider_gateway._model,
                choices=[{"message": {"role": "assistant", "content": "Please confirm how you intend cool to be interpreted."}}],
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
        )
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model, primary=primary, advisor_model=service._settings.composer_advisor_model, advisor=[]
    )
    monkeypatch.setattr(litellm, "acompletion", script)
    prepare = service._prepare_primary_delivery
    before = {}

    def observe_actual_reply_owner(**arguments):
        if arguments.get("reply_only", False):
            assert not before, "the actual reply owner must prepare only once"
            assert any(message["role"] == "tool" for message in arguments["messages"])
            assert {tool["function"]["name"] for tool in arguments["tools"]} == {"handoff_to_user", "restore_parked_graph"}
            assert sys._getframe(1).f_code.co_name == "_classify_and_budget_turn"
            before["failed_turn"] = arguments["failed_turn"]
            before["calls"] = arguments["recorder"].llm_calls
            before["tools"] = composer_helpers._persisted_tool_responses(service, session_id)
            assert [row["tool_call_id"] for row in before["tools"]] == ["reply-seed-pipeline", "reply-stage-review"]
            assert all(json.loads(row["result_canonical"])["success"] is True for row in before["tools"])
            # Only the version-advancing mutation publishes a new state row;
            # staging its review succeeds without another graph revision.
            assert before["tools"][0]["composition_state_id"] is not None
            assert before["tools"][1]["composition_state_id"] is None
            assert [node.id for node in arguments["state"].nodes] == ["rate_node"]
            before["nodes"] = arguments["state"].nodes
            with service._require_sessions_service()._engine.connect() as connection:
                before["rows"] = connection.execute(
                    select(chat_messages_table).order_by(chat_messages_table.c.created_at, chat_messages_table.c.id)
                ).all()
                before["attempts"] = connection.execute(
                    select(quota_provider_attempts_table).order_by(quota_provider_attempts_table.c.attempt_id)
                ).all()
            if refuse:
                current = service._settings.model_copy(update={"composer_max_prepared_request_bytes": 1})
                service._settings = current
                service._provider_gateway._settings = current
        return prepare(**arguments)

    monkeypatch.setattr(service, "_prepare_primary_delivery", observe_actual_reply_owner)
    if refuse:
        with pytest.raises(ComposerContextUnsupported) as caught:
            await service.compose(
                "create a workflow that rates how cool pages are", [], advisor_fixtures._empty_state(), session_id=session_id
            )
        error = caught.value
        assert error.reason == "prepared_request_bytes" and error.llm_calls == before["calls"]
        assert error.failed_turn is before["failed_turn"] and error.failed_turn is not None
        assert error.tool_invocations == () and error.partial_state is None
        with service._require_sessions_service()._engine.connect() as connection:
            assert (
                connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.created_at, chat_messages_table.c.id)).all()
                == before["rows"]
            )
            assert (
                connection.execute(select(quota_provider_attempts_table).order_by(quota_provider_attempts_table.c.attempt_id)).all()
                == before["attempts"]
            )
    else:
        result = await service.compose(
            "create a workflow that rates how cool pages are", [], advisor_fixtures._empty_state(), session_id=session_id
        )
        assert "Please confirm how you intend cool" in result.message
        assert len(result.llm_calls) == 3
    assert before and len(before["calls"]) == len(before["attempts"]) == 2
    assert len({call.call_id for call in before["calls"]}) == 2
    assert {row.attempt_id for row in before["attempts"]} == {call.call_id for call in before["calls"]}
    assert all(row.settled_at is not None and row.ledger_entry_id is not None for row in before["attempts"])
    assert script.primary.await_count == (2 if refuse else 3) and script.advisor.await_count == 0
    assert [row["tool_call_id"] for row in before["tools"]] == ["reply-seed-pipeline", "reply-stage-review"]
    assert composer_helpers._persisted_tool_responses(service, session_id) == before["tools"]
    current = await service._require_sessions_service().get_current_state(UUID(session_id))
    assert current is not None and str(current.id) == before["tools"][0]["composition_state_id"]
    assert state_from_record(current).nodes == before["nodes"]
