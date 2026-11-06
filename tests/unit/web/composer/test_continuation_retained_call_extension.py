"""Cold recorder-extension comparisons against the existing publication owner."""

import json
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_evidence_application_data as application_data
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, RecorderSnapshot, decode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence, decode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
)
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_batch_data import actual_cut
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence
from tests.unit.web.composer.test_continuation_evidence_graph import snapshot as capture_snapshot
from tests.unit.web.composer.test_continuation_physical_outcomes import actual_record
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual

operation_store = test_composer_operation_authority.operation_store


def _coherent_chunk(original: PrivateChunk, document: object) -> PrivateChunk:
    """Retained-store corruption with a valid new checksum and byte length."""
    canonical = canonical_json(document)
    encoded = canonical.encode("utf-8")
    return PrivateChunk(
        PrivateChunkRef(codec=original.reference.codec, digest=sha256(encoded).hexdigest(), byte_length=len(encoded)),
        canonical,
    )


def _compare(original, call, recorder, retained, names=frozenset()):
    return application_data.require_retained_call_extension(
        original,
        call=call,
        recorder=recorder,
        retained_evidence=retained,
        env_ref_names=names,
    )


def _pure_extension(*, joint: bool = False) -> tuple[AdmittedTurnEvidence, ComposerLLMCall, RecorderSnapshot, AdmittedTurnEvidence]:
    owner, messages, recorder, logical, _policy = rich_evidence()
    if joint:
        batch, accumulator, calls = actual_cut()
        original = capture_turn_evidence(
            owner,
            messages,
            logical=logical,
            recorder=recorder,
            drained=RecorderDrainCursors(),
            env_ref_names=frozenset(),
            data=RestoredBatchData(batch, accumulator, calls),
        )
    else:
        original = capture_snapshot(owner, messages, recorder, logical)
    call, _metadata = actual_record()
    call = replace(call, call_id=str(UUID(int=919)))
    before = decode_recorder_snapshot(original.recorder_chunk)
    extended = replace(before, llm_calls=(*before.llm_calls, call))
    published = application_data.append_call_to_evidence(original, call=call, recorder=extended, env_ref_names=frozenset())
    return original, call, extended, published


@pytest.mark.asyncio
async def test_retained_extension_matches_actual_publication_for_each_graph_role(operation_store, tmp_path, monkeypatch):
    """One paid/P4 setup supplies both real journal and prior joint owners."""
    kwargs, _producers, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch, post_p4=True)
    running = kwargs["inventory"].running
    try:
        assert result is not None and len(sdk) == 1
        call = result.outcome.audit_call
        assert call is not None and result.buffer.llm_calls[-1] is call
        selected, names = kwargs["selected"], kwargs["inventory"].names
        frame = selected.stack.frames[0]
        journal = decode_turn_evidence(
            next(chunk for chunk in selected.chunks if chunk.reference == frame.evidence),
            env_ref_names=names,
            source_chunks=selected.chunks,
        )
        joint = kwargs["prior_joint_snapshot"]
        owner, messages, recorder, logical, _policy = rich_evidence()
        absent = capture_turn_evidence(
            owner,
            messages,
            logical=logical,
            recorder=recorder,
            drained=RecorderDrainCursors(),
            env_ref_names=names,
        )
        assert absent.graph is None
        assert joint.snapshot.joint_graph_json is not None and not joint.graph.cuts
        assert journal.snapshot.application_entry is not None and journal.graph.cuts

        for original in (absent, joint, journal):
            before = decode_recorder_snapshot(original.recorder_chunk)
            extended = replace(before, llm_calls=(*before.llm_calls, call))
            published = application_data.append_call_to_evidence(original, call=call, recorder=extended, env_ref_names=names)
            compared = _compare(original, call, extended, published.source_chunk, names)
            assert compared.evidence is published.source_chunk
            assert compared.recorder == published.recorder_chunk
            expected_graph = (
                published.snapshot.application_entry.graph_json
                if published.snapshot.application_entry is not None
                else published.snapshot.joint_graph_json
            )
            assert compared.graph_json == expected_graph
            assert published.snapshot.transcript == original.snapshot.transcript
            assert published.sources.chunks == original.sources.chunks

            # These remain validly hashed documents. A shallow reference match
            # must not substitute for the complete unchanged evidence envelope.
            changed = json.loads(published.source_chunk.canonical)
            changed["registration_revision"] += 1
            with pytest.raises(AuditIntegrityError):
                _compare(original, call, extended, _coherent_chunk(published.source_chunk, changed), names)
            changed = json.loads(published.source_chunk.canonical)
            changed["transcript"]["request"] += " changed retained request"
            with pytest.raises(AuditIntegrityError):
                _compare(original, call, extended, _coherent_chunk(published.source_chunk, changed), names)

            if original.snapshot.application_entry is not None:
                changed = json.loads(published.source_chunk.canonical)
                changed["application_entry"]["applications"][0]["cursor"] += 1
                with pytest.raises(AuditIntegrityError):
                    _compare(original, call, extended, _coherent_chunk(published.source_chunk, changed), names)
                changed = json.loads(published.source_chunk.canonical)
                changed["application_entry"]["graph_json"] = original.snapshot.application_entry.graph_json
                with pytest.raises(AuditIntegrityError):
                    _compare(original, call, extended, _coherent_chunk(published.source_chunk, changed), names)
            elif original.snapshot.joint_graph_json is not None:
                changed = json.loads(published.source_chunk.canonical)
                changed["joint_graph_json"] = original.snapshot.joint_graph_json
                with pytest.raises(AuditIntegrityError):
                    _compare(original, call, extended, _coherent_chunk(published.source_chunk, changed), names)

            assert _compare(original, call, extended, published.source_chunk, names).evidence is published.source_chunk
    finally:
        operation_store[1].release(running.session_operation_context)


def test_retained_extension_refuses_schema_credential_and_complete_document_excess():
    original, call, extended, published = _pure_extension()
    assert _compare(original, call, extended, published.source_chunk).evidence == published.source_chunk

    missing = json.loads(published.source_chunk.canonical)
    del missing["transcript"]
    with pytest.raises(AuditIntegrityError):
        _compare(original, call, extended, _coherent_chunk(published.source_chunk, missing))

    credential = json.loads(published.source_chunk.canonical)
    credential["transcript"]["request"] = "Retain the complete final text sk-" + "syntheticcredential" * 3
    with pytest.raises(CredentialMaterialRefused):
        _compare(original, call, extended, _coherent_chunk(published.source_chunk, credential))

    oversized = json.loads(published.source_chunk.canonical)
    oversized["transcript"]["request"] = "x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1)
    oversized_chunk = _coherent_chunk(published.source_chunk, oversized)
    assert oversized_chunk.reference.byte_length > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    with pytest.raises(AuditIntegrityError, match="bound"):
        _compare(original, call, extended, oversized_chunk)

    # Each document fits separately; their real combined bytes exceed the
    # pair boundary, before the changed request can reach equality refusal.
    pair_excess = json.loads(published.source_chunk.canonical)
    extra = (
        CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
        - published.source_chunk.reference.byte_length
        - published.recorder_chunk.reference.byte_length
        + 1
    )
    assert extra > 0
    pair_excess["transcript"]["request"] += "x" * extra
    pair_chunk = _coherent_chunk(published.source_chunk, pair_excess)
    assert pair_chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    assert pair_chunk.reference.byte_length + published.recorder_chunk.reference.byte_length > CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
    with pytest.raises(AuditIntegrityError, match="pair bound"):
        _compare(original, call, extended, pair_chunk)

    with pytest.raises(AuditIntegrityError):
        _compare(original, call, extended, published.source_chunk, frozenset({"DIFFERENT_ENV"}))
    assert _compare(original, call, extended, published.source_chunk).recorder == published.recorder_chunk


def test_retained_extension_requires_actual_call_and_unchanged_other_recorder_channels():
    original, call, extended, published = _pure_extension()
    assert _compare(original, call, extended, published.source_chunk).recorder == published.recorder_chunk
    before = decode_recorder_snapshot(original.recorder_chunk)

    copied_call = replace(call)
    assert copied_call == call and copied_call is not call
    with pytest.raises(AuditIntegrityError, match="recorder prefix"):
        _compare(original, copied_call, extended, published.source_chunk)

    duplicate = replace(call, call_id=before.llm_calls[0].call_id)
    duplicate_recorder = replace(before, llm_calls=(*before.llm_calls, duplicate))
    with pytest.raises(AuditIntegrityError, match="recorder prefix"):
        _compare(original, duplicate, duplicate_recorder, published.source_chunk)

    changed_prior = replace(before.llm_calls[0], reasoning_content="Changed old provider reasoning")
    wrong_prefix = replace(extended, llm_calls=(changed_prior, *extended.llm_calls[1:]))
    wrong_channel = replace(
        extended,
        withheld_replies=(*extended.withheld_replies, WithheldReply("planner_prose_unadmitted", "An unrelated extra channel entry")),
    )
    wrong_drain = replace(extended, drained=replace(extended.drained, llm_calls=0))
    assert wrong_drain.drained != extended.drained
    for changed in (wrong_prefix, wrong_channel, wrong_drain):
        with pytest.raises(AuditIntegrityError, match="recorder prefix"):
            _compare(original, call, changed, published.source_chunk)

    late_credential = replace(call, reasoning_content="sk-" + "syntheticcredential" * 3)
    credential_recorder = replace(before, llm_calls=(*before.llm_calls, late_credential))
    with pytest.raises(CredentialMaterialRefused):
        _compare(original, late_credential, credential_recorder, published.source_chunk)
    assert _compare(original, call, extended, published.source_chunk).evidence == published.source_chunk


def test_retained_extension_refuses_ordinary_mutable_graph_value_and_alias_corruption():
    original, call, extended, published = _pure_extension(joint=True)
    assert _compare(original, call, extended, published.source_chunk).graph_json == published.snapshot.joint_graph_json
    graph = original.graph
    assert graph is not None
    arguments = graph.selected.batch.decoded_args_by_call_id
    shared = arguments["call-1"]
    assert shared is arguments["call-2"]
    previous_label = shared["nested"][2]["label"]
    try:
        shared["nested"][2]["label"] = "Changed ordinary mutable descendant"
        with pytest.raises(AuditIntegrityError):
            _compare(original, call, extended, published.source_chunk)
    finally:
        shared["nested"][2]["label"] = previous_label

    try:
        arguments["call-1"] = dict(shared)
        assert arguments["call-1"] == arguments["call-2"]
        assert arguments["call-1"] is not arguments["call-2"]
        with pytest.raises(AuditIntegrityError):
            _compare(original, call, extended, published.source_chunk)
    finally:
        arguments["call-1"] = shared
    assert _compare(original, call, extended, published.source_chunk).graph_json == published.snapshot.joint_graph_json
