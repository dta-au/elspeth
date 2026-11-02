"""Closed END gate-input DATA and nested bytes, without dispatch authority."""

from dataclasses import replace

import pytest
from pydantic import TypeAdapter

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdvisorReviewState
from elspeth.web.composer.continuation_checkpoint_end_input import (
    CapturedCheckpointEndInput,
    CheckpointEndInputSnapshot,
    CheckpointOrdinaryEndCaller,
    decode_checkpoint_end_input,
    encode_checkpoint_end_input,
    require_checkpoint_end_input_closure,
    require_original_checkpoint_end_input,
)
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_tool_batch import _dump_owned
from elspeth.web.composer.turn_effects import AdvisorPublicationReview
from elspeth.web.sessions.continuation_selection import nested_private_chunks


def input_data():
    runtime = encode_private_chunk(codec="composer.tools.v1", canonical="[]")
    state_hash = "a" * 64
    review = _dump_owned(TypeAdapter(_AdvisorReviewState), _AdvisorReviewState())
    snapshot = CheckpointEndInputSnapshot(
        session_id="session",
        operation_id="operation",
        outer_effect_key="end:original",
        original_checkpoint_id="original",
        original_cursor=2,
        captured_runtime_sha256="b" * 64,
        runtime_chunks=(runtime.reference,),
        state_sha256=state_hash,
        graph_fingerprint="c" * 64,
        assistant_message_json=_dump_owned(TypeAdapter(_AdmittedAssistantMessage), _AdmittedAssistantMessage(content="Actual reply")),
        runtime_preflight_json=None,
        review_state_json=review,
        initial_review_state_json=review,
        publication_review_json=_dump_owned(
            TypeAdapter(AdvisorPublicationReview), AdvisorPublicationReview("d" * 64, "e" * 64, "{}", "{}")
        ),
        completion_gates_json=None,
        advisor_checkpoint_passes_used=0,
        passes_delta=0,
        pass_index=1,
        max_passes=2,
        retry_ordinal=0,
        allow_repair_continue=True,
        orphaned_precheck=(),
        orphan_events_json="[]",
        orphan_source_sha256=stable_hash({"session_id": "session", "state_sha256": state_hash, "events": []}),
        caller=CheckpointOrdinaryEndCaller(
            proof_runtime_preflight_json=None,
            proof_repair_action=None,
            proof_repair_diagnostics_json=None,
            confirmed_stop=False,
            confirmed_stop_evidence_sha256=None,
        ),
        superseded_reply_index=None,
        superseded_reply_sha256=None,
        elide_advisor_exchange=False,
    )
    return snapshot, runtime


def test_end_input_codec_and_original_nested_closure_are_complete():
    snapshot, runtime = input_data()
    chunk = encode_checkpoint_end_input(snapshot, env_ref_names=frozenset())
    assert decode_checkpoint_end_input(chunk, env_ref_names=frozenset()) == snapshot
    assert nested_private_chunks(chunk) == (runtime.reference,)
    require_checkpoint_end_input_closure(chunk, (runtime,))
    with pytest.raises(AuditIntegrityError, match="complete runtime chunk cohort"):
        require_checkpoint_end_input_closure(chunk, ())
    altered = encode_private_chunk(codec="composer.tools.v1", canonical='["substituted"]')
    with pytest.raises(AuditIntegrityError, match="complete runtime chunk cohort"):
        require_checkpoint_end_input_closure(chunk, (altered,))
    with pytest.raises(AuditIntegrityError):
        require_checkpoint_end_input_closure(chunk, (replace(runtime, canonical="{}"),))
    with pytest.raises(AuditIntegrityError, match="original pre-call capture producer"):
        require_original_checkpoint_end_input(CapturedCheckpointEndInput(chunk), phase_source=None, captured=None)


@pytest.mark.parametrize("change", ("missing", "extra", "reset", "orphan_digest"))
def test_end_input_refuses_normalized_or_substituted_cold_data(change):
    snapshot, _runtime = input_data()
    payload = snapshot.model_dump(mode="json")
    if change == "missing":
        del payload["initial_review_state_json"]
    elif change == "extra":
        payload["new_authority"] = True
    elif change == "reset":
        payload["passes_delta"] = 1
    else:
        payload["orphan_source_sha256"] = "f" * 64
    chunk = encode_private_chunk(codec="composer.checkpoint-end-input.v1", canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode_checkpoint_end_input(chunk, env_ref_names=frozenset())
