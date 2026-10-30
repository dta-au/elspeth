"""Actual closed evidence/recorder source controls; no SQL authority is inferred."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot, admit_turn_evidence, decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence
from elspeth.web.composer.continuation_message_shelf import MessageShelfSnapshot, RetainedObservedMessage
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.continuation_selection import nested_private_chunks
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence, snapshot
from tests.unit.web.composer.test_turn_effects import CONTEXT
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


def _pair_candidate(*, over=0, credential=False):
    """Real captured fields and audit; tune only the new actual recorder bytes.

    The negative transcript is valid closed DATA, not an admitted producer or
    SQL receipt. Its late current-message body is the intended credential fault.
    """
    owner, messages, recorder, logical, _ = rich_evidence()
    # Four real plain user messages keep each string within its original
    # structural inspection domain while making a substantial lawful prefix.
    messages.extend({"role": "user", "content": "x" * 60_000} for _ in range(4))
    messages.append({"role": "user", "content": "late safe body"})
    owner.transcript.observe_messages(messages)
    base = snapshot(owner, messages, recorder, logical)
    shelf = base.snapshot.transcript
    if credential:
        # Preserve all aliases/positions and strict JSON. No live registration
        # is performed for this deliberately corrupted retained source.
        wire = shelf.messages_json[-1]
        secret = "postgresql://user:secret@host/db"  # secret-scan: allow-this-line
        wires = (*shelf.messages_json[:-1], wire.replace("late safe body", secret))
        observed = tuple(
            RetainedObservedMessage(**{**item.__dict__, "event_json": item.event_json.replace("late safe body", secret)})
            for item in shelf.observed
        )
        shelf = MessageShelfSnapshot(**{**shelf.__dict__, "messages_json": wires, "observed": observed})

    def candidate(chars):
        actual, drained = BufferingRecorder.for_continuation(decode_recorder_snapshot(base.recorder_chunk))
        actual.record_withheld_reply("planner_prose_unadmitted", "r" * chars)
        audit = encode_recorder_snapshot(actual.snapshot_for_continuation(drained=drained))
        value = TurnEvidenceSnapshot(**{**base.snapshot.__dict__, "transcript": shelf, "recorder": audit.reference})
        canonical = canonical_json(value.model_dump(mode="json"))
        encoded = canonical.encode("utf-8")
        source = PrivateChunk(
            PrivateChunkRef(codec="composer.evidence.v2", digest=sha256(encoded).hexdigest(), byte_length=len(encoded)), canonical
        )
        return value, audit, source

    value, audit, source = candidate(200_000)
    remaining = JSON_MAX_TOTAL_UTF8_BYTES - source.reference.byte_length - audit.reference.byte_length
    assert remaining > 0
    value, audit, source = candidate(200_000 + remaining + over)
    assert source.reference.byte_length <= JSON_MAX_TOTAL_UTF8_BYTES
    assert audit.reference.byte_length <= JSON_MAX_TOTAL_UTF8_BYTES
    assert source.reference.byte_length + audit.reference.byte_length == JSON_MAX_TOTAL_UTF8_BYTES + over
    return value, audit, source


@pytest.mark.parametrize("mode", ("default", "mandatory", "decode"))
def test_actual_two_document_boundary_and_one_byte_excess(mode):
    value, audit, source = _pair_candidate()
    positive = (
        decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=(audit,))
        if mode == "decode"
        else admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(audit,), mandatory_retention=mode == "mandatory")
    )
    assert positive.recorder_chunk is audit and encode_turn_evidence(positive) == source
    value, audit, source = _pair_candidate(over=1)
    if mode == "mandatory":
        with pytest.raises(ComposerContextUnsupported) as caught:
            admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(audit,), mandatory_retention=True)
        assert caught.value.reason == "retained_chunk_bytes"
    else:
        with pytest.raises(AuditIntegrityError, match="pair bound"):
            if mode == "decode":
                decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=(audit,))
            else:
                admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(audit,))


@pytest.mark.parametrize("mode", ("default", "mandatory", "decode"))
def test_late_transcript_credentials_win_over_actual_pair_excess(mode):
    value, audit, source = _pair_candidate(over=1, credential=True)
    assert value.joint_graph_json is None and value.application_entry is None
    assert "postgresql://" in value.transcript.messages_json[-1]
    with pytest.raises(CredentialMaterialRefused):
        if mode == "decode":
            decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=(audit,))
        else:
            admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(audit,), mandatory_retention=mode == "mandatory")


def test_actual_recorder_reference_preserves_graphless_source_and_cold_channels():
    owner, messages, recorder, logical, _ = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    evidence = encode_turn_evidence(admitted)
    audit = admitted.recorder_chunk
    assert evidence.reference.codec == "composer.evidence.v2"
    assert admitted.snapshot.recorder == audit.reference
    assert "recorder_json" not in admitted.snapshot.model_dump(mode="json")
    assert admitted.sources.chunks == ()
    assert admitted.dependency_chunks == (audit,)
    assert admitted.dependency_chunks[0] is audit
    assert nested_private_chunks(evidence) == (audit.reference,)
    current = decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks)
    assert current.recorder_chunk is audit
    cold = _restore_current_evidence(current, context=CONTEXT)
    assert encode_recorder_snapshot(cold.recorder.snapshot_for_continuation(drained=cold.drained)) == audit
    assert cold.recorder.visible_tool_events[0][0] is cold.recorder.invocations[0]
    assert cold.messages[-3] is cold.messages[-2] and cold.messages[-3] is not cold.messages[-1]
    assert encode_turn_evidence(snapshot(cold.evidence, cold.messages, cold.recorder, logical)) == evidence


@pytest.mark.parametrize("fault", ("missing", "wrong_codec", "conflicting_length", "corrupt_bytes"))
def test_recorder_dependency_refuses_after_actual_positive(fault):
    owner, messages, recorder, logical, _ = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    evidence = encode_turn_evidence(admitted)
    audit = admitted.recorder_chunk
    positive = decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=(audit,))
    assert positive.recorder_chunk is audit
    if fault == "missing":
        sources = ()
    elif fault == "wrong_codec":
        sources = (PrivateChunk(audit.reference.model_copy(update={"codec": "composer.state.v1"}), audit.canonical),)
    elif fault == "conflicting_length":
        conflicting = PrivateChunk(audit.reference.model_copy(update={"byte_length": audit.reference.byte_length + 1}), audit.canonical)
        sources = (audit, conflicting)
    else:
        assert audit.canonical.endswith("}")
        sources = (replace(audit, canonical=audit.canonical[:-1] + " "),)
    with pytest.raises(AuditIntegrityError):
        decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=sources)


@pytest.mark.asyncio
async def test_missing_original_recorder_is_not_reconstructed_from_selected_graph(operation_store, tmp_path, monkeypatch):
    from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
    from tests.unit.web.sessions.test_continuation_p4_product import _current_admitted

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        admitted, evidence, _selected, _runtime = await _current_admitted(owner, running)
        dispatched = len(sdk)
        evidence = encode_turn_evidence(admitted)
        assert admitted.graph is not None
        with pytest.raises(AuditIntegrityError):
            decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=admitted.sources.chunks)
        assert len(sdk) == dispatched


@pytest.mark.parametrize("mode", ("default", "mandatory", "decode"))
@pytest.mark.parametrize("fault", ("credential", "cursor"))
def test_late_original_recorder_integrity_wins_over_actual_pair_excess(mode, fault):
    positive, original_audit, original = _pair_candidate()
    admitted = admit_turn_evidence(positive, env_ref_names=frozenset(), source_chunks=(original_audit,))
    assert encode_turn_evidence(admitted) == original and admitted.recorder_chunk is original_audit
    value, audit, _source = _pair_candidate(over=1)
    payload = json.loads(audit.canonical)
    if fault == "credential":
        secret = "postgresql://user:secret@host/db"  # secret-scan: allow-this-line
        body = payload["withheld_replies"][-1]["content"]
        assert len(body) > len(secret)
        payload["withheld_replies"][-1]["content"] = body[: -len(secret)] + secret
    else:
        payload["drained"]["withheld_replies"] = 9_999_999
    canonical = canonical_json(payload)
    encoded = canonical.encode()
    reference = PrivateChunkRef(codec=audit.reference.codec, digest=sha256(encoded).hexdigest(), byte_length=len(encoded))
    corrupt = PrivateChunk(reference, canonical)
    value = TurnEvidenceSnapshot(**{**value.__dict__, "recorder": reference})
    evidence_text = canonical_json(value.model_dump(mode="json"))
    evidence = PrivateChunk(
        PrivateChunkRef(
            codec="composer.evidence.v2", digest=sha256(evidence_text.encode()).hexdigest(), byte_length=len(evidence_text.encode())
        ),
        evidence_text,
    )
    assert reference.byte_length <= JSON_MAX_TOTAL_UTF8_BYTES and evidence.reference.byte_length <= JSON_MAX_TOTAL_UTF8_BYTES
    assert reference.byte_length + evidence.reference.byte_length > JSON_MAX_TOTAL_UTF8_BYTES
    assert value.joint_graph_json is None and value.application_entry is None
    expected = CredentialMaterialRefused if fault == "credential" else AuditIntegrityError
    with pytest.raises(expected) as caught:
        if mode == "decode":
            decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=(corrupt,))
        else:
            admit_turn_evidence(value, env_ref_names=frozenset(), source_chunks=(corrupt,), mandatory_retention=mode == "mandatory")
    if fault == "cursor":
        assert str(caught.value) == "buffer drain cursor exceeds its retained channel"
