"""Actual V4 original private metadata and diagnostic audit representation controls."""

from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_primary as primary_composer
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.bounded_json import PROVIDER_ARTIFACT_UNAVAILABLE
from elspeth.web.composer.continuation_codec import decode_continuation_response
from elspeth.web.composer.continuation_primary_observation import observe_primary_provider_result
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.sessions.continuation import private_chunk_key
from elspeth.web.sessions.continuation_primary import (
    PrimaryProviderResult,
    read_committed_primary_response_data_on_connection,
    read_primary_provider_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.sessions import test_continuation_primary_v4_live
from tests.unit.web.sessions.test_continuation_primary_v4_incomplete import _invoke
from tests.unit.web.sessions.test_continuation_primary_v4_live import _v4_primary_input

operation_store = test_continuation_primary_v4_live.operation_store
_REASONING = "r" * 100_000


def _response():
    return ModelResponse(
        model="test/model",
        choices=[
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "Actual original reply",
                    "reasoning_content": _REASONING,
                    "tool_calls": None,
                },
            }
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
    )


@pytest.mark.asyncio
async def test_actual_v4_original_reasoning_survives_observer_sql_and_both_cold_readers(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)
    sdk = []
    observed = []
    observer = primary_composer.observe_v4_primary_provider_result

    async def completion(**_kwargs):
        sdk.append(1)
        return _response()

    def observe(**values):
        observed.append(values["outcome"])
        # The exact same actual source fails the unchanged legacy comparator.
        with pytest.raises(AuditIntegrityError, match="native audit"):
            observe_primary_provider_result(**values)
        return observer(**values)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(primary_composer, "observe_v4_primary_provider_result", observe)
    result = await _invoke(kwargs, produced, recorder, store)
    assert type(result) is PrimaryProviderResult and sdk == [1] and len(observed) == 1
    assert result.outcome.completion.provider_metadata.reasoning_content == _REASONING
    assert result.outcome.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
    assert decode_continuation_response(result.response).provider_metadata.reasoning_content == _REASONING
    running = kwargs["inventory"].running
    work = inventory_work(running)
    selected = await load_selected_continuation(
        authority=kwargs["authority"],
        running=running,
        selected=result.snapshot,
        required_work=work,
    )
    work.coordinator.assert_completed()
    source = {
        "selected": selected,
        "checkpoint_id": result.snapshot.checkpoint_id,
        "cursor": result.snapshot.cursor,
        "effect_key": result.effect_key,
        "input_sha256": selected.stack.frames[-1].provider_input_sha256,
    }
    with store[0].begin() as conn:
        current = read_primary_provider_on_connection(conn, running, **source)
        historical = read_committed_primary_response_data_on_connection(
            conn,
            session_id=store[4],
            operation_id=running.claim.operation_id,
            selected=selected,
            reference=result.response.reference,
        )
        assert current.response == historical == result.response
        assert current.outcome.completion.provider_metadata.v4_completeness is None
        assert current.outcome.completion.provider_metadata.reasoning_content == _REASONING
        assert current.outcome.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
        # Both originals would project to the same diagnostic marker. The
        # immutable selected response binding must still reject substitution.
        key = private_chunk_key(result.response)
        row = conn.execute(select(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == key)).one()
        original = row.payload
        changed = original.replace(_REASONING.encode(), ("s" * 100_000).encode())
        assert changed != original and len(changed) == len(original)
        conn.execute(update(chunks).where(chunks.c.session_id == str(store[4]), chunks.c.chunk_key == key).values(payload=changed))
        with pytest.raises(AuditIntegrityError):
            read_primary_provider_on_connection(conn, running, **source)
        with pytest.raises(AuditIntegrityError):
            read_committed_primary_response_data_on_connection(
                conn,
                session_id=store[4],
                operation_id=running.claim.operation_id,
                selected=selected,
                reference=result.response.reference,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("original", "bounded_audit"))
async def test_actual_v4_primary_rejects_changed_original_or_diagnostic_audit_before_sql(operation_store, tmp_path, monkeypatch, changed):
    kwargs, produced, recorder, store = await _v4_primary_input(operation_store, tmp_path, monkeypatch)
    observer = primary_composer.observe_v4_primary_provider_result
    sdk = []

    async def completion(**_kwargs):
        sdk.append(1)
        return _response()

    def observe(**values):
        outcome = values["outcome"]
        assert outcome.completion.provider_metadata.reasoning_content == _REASONING
        assert outcome.audit_call.reasoning_content == PROVIDER_ARTIFACT_UNAVAILABLE
        if changed == "original":
            metadata = replace(outcome.completion.provider_metadata, reasoning_content="s" * 100_000)
            values["outcome"] = _CallModelOutcome(replace(outcome.completion, provider_metadata=metadata), outcome.audit_call)
        else:
            values["outcome"] = _CallModelOutcome(outcome.completion, replace(outcome.audit_call, reasoning_content="changed audit"))
        return observer(**values)

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(primary_composer, "observe_v4_primary_provider_result", observe)
    with pytest.raises(AuditIntegrityError):
        await _invoke(kwargs, produced, recorder, store)
    assert sdk == [1]
