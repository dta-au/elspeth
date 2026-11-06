"""Original paid/P4 refusal sources and independent cold SQL opponents.

The SDK supplies the actual provider words; no completion outcome, physical
receipt, P4 audit outcome or observer capability is fabricated here.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import partial
from hashlib import sha256
from pathlib import Path

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_classifier_handoff import ClassifySourceProofStop
from elspeth.web.composer.continuation_classifier_owned_source import decode_classifier_owned_source
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_return import (
    capture_original_classification_pending,
    complete_continuation_iteration_return,
    observe_continuation_classifier_end,
    observe_continuation_classifier_end_input,
    observe_continuation_iteration_classification,
    refresh_original_classification_source,
)
from elspeth.web.composer.service import _reply_only_protocol_records
from elspeth.web.sessions import continuation_iteration_return as stages
from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection
from elspeth.web.sessions.graph_recovery import checkpoint_data
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table as blobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from tests.helpers.continuation_app import end_admit, sdk_reply
from tests.unit.web.composer.test_service import _create_session_blob_for_test
from tests.unit.web.composer.test_tools import _stage1_valid_preview_state
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization
from tests.unit.web.sessions import test_continuation_live_phases as live_fixture
from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case
from tests.unit.web.sessions.test_continuation_classifier_primary import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_classifier_service import _draft_with_review_term
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


def _select_actual_tools(monkeypatch, *, first, following=None, settings_overrides=None):
    original = live_fixture._phase_case
    monkeypatch.setattr(
        live_fixture,
        "_phase_case",
        partial(original, tool_call=first, following_tool_call=following, settings_overrides=settings_overrides),
    )


async def _current(phases, running, stored):
    selected = await load_selected_continuation(authority=phases.authority, running=running, selected=stored, required_work=phases.work)
    runtime, _inventory = await phases.current(selected)
    return runtime, selected


async def _retain_actual_return(phases, running, runtime, observed):
    runtime, selected = await refresh_original_classification_source(observed, phases, runtime)
    outcome = observed.outcome
    assert outcome.action == "return" and outcome.result is not None
    runtime.live.composition_turns_used += outcome.composition_turns_delta
    runtime.live.discovery_turns_used += outcome.discovery_turns_delta
    runtime.live.advisor_checkpoint_passes_used += outcome.advisor_passes_delta
    runtime.live.repair_turns_used += outcome.repair_turns_delta
    returned, captured = await complete_continuation_iteration_return(observed, phases, runtime)
    completed = stages.retain_iteration_returned(
        phases.authority,
        running,
        selected=selected,
        inventory=runtime.materialization.inventory,
        observed=returned,
        captured=captured,
    )
    with locked_session_transaction(phases.authority._engine, str(running.claim.session_id)) as conn:
        stage = stages.read_iteration_stage_on_connection(conn, running, selected=completed)
    assert stage is not None and stage.stage == "returned"
    return stage.owner_effect_key


def _read_owned(conn, running, effect_key):
    return stages.read_original_owned_iteration_return_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect_key=effect_key
    )


def _cold_source_and_opponents(engine, running, effect_key, *, expected_kind):
    before = stored_rows(engine)
    with locked_session_transaction(engine, str(running.claim.session_id)) as conn:
        first = _read_owned(conn, running, effect_key)
        assert first.kind == expected_kind
        assert _read_owned(conn, running, effect_key) == first
        row = conn.execute(
            select(chunks).where(
                chunks.c.session_id == str(running.claim.session_id),
                chunks.c.operation_id == running.claim.operation_id,
                chunks.c.codec == "composer.iteration-owned-observation.v1",
            )
        ).one()
        original = json.loads(bytes(row.payload).decode("utf-8"))
        observation = json.loads(original["observation_json"])
        # Recompute the complete chunk's bytes/hash/length, so corruption is
        # rejected by original source provenance rather than byte integrity.
        for field, replacement in (
            ("physical_operation_epoch", observation["physical_operation_epoch"] + 1),
            ("state_sha256", "0" * 64),
        ):
            with conn.begin_nested() as savepoint:
                forged = dict(original, observation_json=canonical_json(dict(observation, **{field: replacement})))
                payload = canonical_json(forged).encode("utf-8")
                assert (
                    conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == row.session_id,
                            chunks.c.operation_id == row.operation_id,
                            chunks.c.chunk_key == row.chunk_key,
                        )
                        .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                    ).rowcount
                    == 1
                )
                with pytest.raises(AuditIntegrityError, match=r"original stage or physical observation|original observed state"):
                    _read_owned(conn, running, effect_key)
                savepoint.rollback()
        assert _read_owned(conn, running, effect_key) == first
    assert stored_rows(engine) == before
    return first, decode_classifier_owned_source(original["observation_json"])


@pytest.mark.asyncio
async def test_actual_paid_protocol_echo_retains_original_reply_and_rejects_coherent_source_substitution(
    operation_store, tmp_path, monkeypatch
):
    _select_actual_tools(
        monkeypatch,
        first=("draft", "set_pipeline", _draft_with_review_term()),
        following=(
            "review",
            "request_interpretation_review",
            {
                "affected_node_id": "rate_node",
                "kind": "vague_term",
                "user_term": "cool",
                "llm_draft": "modern, useful, engaging, and clear for the public.",
            },
        ),
    )
    async with _classifier_case(operation_store, tmp_path, monkeypatch) as (phases, running, runtime, _issuer, _input, prior_sdk):
        physical = []
        original_echo = []

        async def provider(**request):
            physical.append(request)
            records = [
                record
                for message in request["messages"]
                if type(message.get("content")) is str
                for record in _reply_only_protocol_records(message["content"])
            ]
            assert records, "The real reply-only request must contain its original quoted tool protocol"
            echo = "Historical tool protocol record (quoted data):\n" + canonical_json(records[0])
            original_echo.append(echo)
            return sdk_reply(echo)

        monkeypatch.setattr(litellm, "acompletion", provider)
        assert phases.finished_batch is not None
        dispatch, persist, _prestate = phases.finished_batch
        observed = await observe_continuation_iteration_classification(
            phases, runtime, selected=runtime.source.selected, dispatch=dispatch, persist=persist
        )
        effect_key = await _retain_actual_return(phases, running, runtime, observed)
        cold, observation = _cold_source_and_opponents(operation_store[0], running, effect_key, expected_kind="reply_protocol_echo")
        assert len(prior_sdk) == len(physical) == len(original_echo) == 1
        assert cold.returned.primary_response is not None and cold.returned.primary_effect_key is not None
        assert cold.returned.end_effect_key is cold.returned.end_pass_message_id is cold.returned.end_pass_index is None
        assert cold.returned.result.raw_assistant_content in (None, "")
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            original_primary = read_original_classifier_primary_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=cold.returned.primary_effect_key,
            )
        assert original_primary.completion.message.content == original_echo[0]
        assert original_primary.response.reference == cold.returned.primary_response
        assert observation.proof_json is observation.deadline is None


@asynccontextmanager
async def _actual_unavailable_preview_case(store, tmp_path, monkeypatch):
    engine, _repository, _authority, sessions, sid = store
    data_dir = tmp_path / "source-proof-blobs"
    blob_id = _create_session_blob_for_test(
        engine=engine, session_id=str(sid), data_dir=data_dir, filename="values.csv", content="amount\n10\n"
    )
    with engine.begin() as conn:
        path = Path(conn.execute(select(blobs.c.storage_path).where(blobs.c.id == blob_id)).scalar_one())
        assert conn.execute(update(blobs).where(blobs.c.id == blob_id).values(status="pending")).rowcount == 1
    state = _stage1_valid_preview_state()
    state = replace(
        state,
        sources={
            "source": replace(state.sources["source"], options={"path": str(path), "blob_ref": blob_id, "schema": {"mode": "observed"}})
        },
    )
    original_bootstrap = initialization.bootstrap_case
    original_save = sessions.save_composition_state

    async def bootstrap_with_actual_source(*args, **kwargs):
        async def save_actual_seed(session_id, data, **options):
            if options.get("provenance") == "session_seed":
                data = checkpoint_data(state)
            return await original_save(session_id, data, **options)

        # Only the fixture's initial DATA changes. The original fenced SQL
        # writer creates the actual state and bootstrap/source receipts.
        with monkeypatch.context() as seed_patch:
            seed_patch.setattr(sessions, "save_composition_state", save_actual_seed)
            return await original_bootstrap(*args, **dict(kwargs, bound_base=True))

    monkeypatch.setattr(initialization, "bootstrap_case", bootstrap_with_actual_source)
    _select_actual_tools(monkeypatch, first=("preview", "preview_pipeline", {}), settings_overrides={"data_dir": str(data_dir)})
    async with _classifier_case(store, tmp_path, monkeypatch) as case:
        # Arrival after the actual P4 preview must not erase that immutable
        # unavailable observation or reopen the classifier PRIMARY dialogue.
        with engine.begin() as conn:
            assert conn.execute(update(blobs).where(blobs.c.id == blob_id).values(status="ready")).rowcount == 1
        yield case


@pytest.mark.asyncio
async def test_actual_unavailable_preview_survives_later_ready_blob_and_blocked_end_cold_replay(operation_store, tmp_path, monkeypatch):
    async with _actual_unavailable_preview_case(operation_store, tmp_path, monkeypatch) as case:
        phases, running, runtime, _issuer, _input, prior_sdk = case
        assert phases.finished_batch is not None
        dispatch, persist, _prestate = phases.finished_batch
        observed = await observe_continuation_iteration_classification(
            phases, runtime, selected=runtime.source.selected, dispatch=dispatch, persist=persist
        )
        assert type(observed.outcome) is ClassifySourceProofStop and len(prior_sdk) == 1
        captured, current = await capture_original_classification_pending(observed, phases, runtime)
        runtime = phases.runtime
        assert runtime is not None and runtime.source.selected is current
        ready = stages.retain_iteration_pending_end(
            phases.authority,
            running,
            selected=current,
            inventory=runtime.materialization.inventory,
            observed=observed,
            captured=captured,
        )
        runtime, current = await _current(phases, running, ready)
        pending = stages.read_iteration_pending_end(
            phases.authority, running, selected=current, inventory=runtime.materialization.inventory
        )
        assert pending.primary_response is None and pending.owned_source_json is not None
        # A fresh action starts only after the actual durable unpaid END-ready
        # boundary. The original admitted advisor policy/hard fence remain.
        timing = phases.work.coordinator.sql_deadline_owner
        assert timing is not None
        clock = ContinuationActionClock(timing.policy.actions.advisor)
        timing.begin_action(timing.policy.actions.advisor, started_at=clock.started_monotonic)
        phases.bind_action_clock(clock)
        end_input = observe_continuation_classifier_end_input(
            phases,
            runtime,
            selected=current,
            pending=pending.pending,
            dispatch=dispatch,
            persist=persist,
            primary_response=None,
        )
        begun = stages.begin_iteration_paid_stage(
            phases.authority,
            running,
            selected=current,
            inventory=runtime.materialization.inventory,
            stage="end",
            end_input=end_input,
        )
        runtime, current = await _current(phases, running, begun)
        physical = []

        async def advisor(**request):
            physical.append(request)
            return sdk_reply(canonical_json(end_admit()), model=request["model"])

        monkeypatch.setattr(litellm, "acompletion", advisor)
        ended = await observe_continuation_classifier_end(
            phases,
            runtime,
            selected=current,
            pending=pending.pending,
            dispatch=dispatch,
            persist=persist,
            primary_response=None,
            owned_source_json=pending.owned_source_json,
        )
        effect_key = await _retain_actual_return(phases, running, runtime, ended)
        cold, observation = _cold_source_and_opponents(operation_store[0], running, effect_key, expected_kind="source_proof_refusal")
        assert len(prior_sdk) == len(physical) == 1
        assert observation.proof_origin == "preview" and observation.terminal_tool_call_id == "preview"
        assert cold.returned.primary_effect_key is cold.returned.primary_response is None
        assert cold.returned.end_disposition == "blocked" and cold.returned.end_effect_key == pending.end_effect_key
        assert cold.returned.end_pass_message_id is cold.returned.end_pass_index is None
        assert cold.returned.result.runtime_preflight is not None
        assert not cold.returned.result.runtime_preflight.readiness.completion_ready
