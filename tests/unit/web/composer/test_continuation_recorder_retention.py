"""Fresh recorder capacity keeps complete source validation and cold DATA.

These tests use the existing evidence owner and real recorder append methods.
They do not establish SQL authority or retention of an owed paid effect.
"""

import json
from dataclasses import replace
from hashlib import sha256

import pytest
from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_application_graph import (
    ProspectiveApplicationRecorderSource,
    admit_application_recorder_source,
    capture_fresh_application_recorder_source,
    require_application_recorder_capacity,
    require_application_recorder_source,
)
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_batch_data import RestoredPreparedBatchData
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_active_evidence_graph import active_graph
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence
from tests.unit.web.composer.test_turn_effects import CONTEXT

_DRAINED = RecorderDrainCursors(llm_calls=1)


def _capture(owner, messages, recorder, logical, *, data=None):
    return capture_turn_evidence(
        owner,
        messages,
        recorder=recorder,
        logical=logical,
        drained=_DRAINED,
        env_ref_names=frozenset(),
        mandatory_retention=True,
        data=data,
    )


def _require_fitting_cold_control(owner, messages, recorder, logical, *, data=None):
    admitted = _capture(owner, messages, recorder, logical, data=data)
    evidence = encode_turn_evidence(admitted, mandatory_retention=True)
    decoded = decode_turn_evidence(evidence, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks)
    cold = _restore_current_evidence(decoded, context=CONTEXT)
    assert cold.evidence is not owner and cold.recorder is not recorder
    assert cold.recorder.visible_tool_events[0][0] is cold.recorder.invocations[0]
    assert cold.messages[-3] is cold.messages[-2] and cold.messages[-3] is not cold.messages[-1]
    assert encode_recorder_snapshot(cold.recorder.snapshot_for_continuation(drained=cold.drained)) == admitted.recorder_chunk
    cold_data = None
    if cold.batch is not None:
        assert cold.selected_audit is not None and cold.selected_dialect is not None
        cold_data = RestoredPreparedBatchData(
            cold.batch.batch, cold.batch.accumulator, cold.batch.calls, cold.selected_audit, cold.selected_dialect
        )
    rebound = _capture(cold.evidence, cold.messages, cold.recorder, logical, data=cold_data)
    assert encode_turn_evidence(rebound, mandatory_retention=True) == evidence


def _project_actual_reply_growth(base_chunk, base_snapshot, current_snapshot):
    """Vary only real reply DATA in a complete actual codec document.

    The caller controls this oracle against both the actual baseline encoder
    and its actual fitting append before using it for over-limit measurement.
    No oversized PrivateChunk is represented as an admitted source.
    """
    assert replace(current_snapshot, withheld_replies=base_snapshot.withheld_replies) == base_snapshot
    assert current_snapshot.withheld_replies[: len(base_snapshot.withheld_replies)] == base_snapshot.withheld_replies
    document = json.loads(base_chunk.canonical)
    document["snapshot"]["withheld_replies"] = [
        {"origin": reply.origin, "content": reply.content} for reply in current_snapshot.withheld_replies
    ]
    return canonical_json(document)


def _grow_actual_recorder(recorder):
    baseline = recorder.snapshot_for_continuation(drained=_DRAINED)
    base_chunk = encode_recorder_snapshot(baseline)
    assert baseline.withheld_replies, "an existing reply gives each append the same array separator cost"
    assert _project_actual_reply_growth(base_chunk, baseline, baseline) == base_chunk.canonical

    content = 'é\\"\n' * 24_000
    assert len(content.encode("utf-8")) > len(content)
    recorder.record_withheld_reply("planner_prose_unadmitted", content)
    probe = recorder.snapshot_for_continuation(drained=_DRAINED)
    probe_chunk = encode_recorder_snapshot(probe)
    probe_text = _project_actual_reply_growth(base_chunk, baseline, probe)
    assert probe_text == probe_chunk.canonical, "positive control binds the oracle to the complete actual v2 encoder"
    assert len(probe_text.encode("utf-8")) == probe_chunk.reference.byte_length
    assert probe_chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    opposing = replace(probe, withheld_replies=baseline.withheld_replies)
    assert _project_actual_reply_growth(base_chunk, baseline, opposing) == base_chunk.canonical

    append_bytes = probe_chunk.reference.byte_length - base_chunk.reference.byte_length
    assert append_bytes > len(content.encode("utf-8")), "JSON escaping also consumes actual recorder bytes"
    append_count = (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES - base_chunk.reference.byte_length) // append_bytes + 1
    assert append_count > 1
    for _ in range(append_count - 1):
        recorder.record_withheld_reply("planner_prose_unadmitted", content)
    current = recorder.snapshot_for_continuation(drained=_DRAINED)
    text = _project_actual_reply_growth(base_chunk, baseline, current)
    assert len(text.encode("utf-8")) == base_chunk.reference.byte_length + append_count * append_bytes
    assert len(text.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES

    last_fitting = replace(current, withheld_replies=current.withheld_replies[:-1])
    last_chunk = encode_recorder_snapshot(last_fitting)
    assert last_chunk.canonical == _project_actual_reply_growth(base_chunk, baseline, last_fitting)
    assert last_chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    return base_chunk, baseline, current


def test_natural_fresh_recorder_overflow_uses_capacity_refusal_after_cold_positive():
    owner, messages, recorder, logical, _ = rich_evidence()
    _require_fitting_cold_control(owner, messages, recorder, logical)
    _base_chunk, _baseline, original = _grow_actual_recorder(recorder)
    with pytest.raises(ComposerContextUnsupported) as caught:
        _capture(owner, messages, recorder, logical)
    assert caught.value.reason == "retained_chunk_bytes"
    assert recorder.snapshot_for_continuation(drained=_DRAINED) == original
    assert all(actual is expected for actual, expected in zip(recorder.withheld_replies, original.withheld_replies, strict=True))


@pytest.mark.parametrize("natural_excess", (False, True), ids=("fitting", "natural-excess"))
@pytest.mark.parametrize("fault", ("registration_revision", "metadata_credential", "recorder_credential"))
def test_late_source_validation_precedes_natural_recorder_refusal(natural_excess, fault):
    owner, messages, recorder, logical, _ = rich_evidence()
    _require_fitting_cold_control(owner, messages, recorder, logical)
    if natural_excess:
        base_chunk, baseline, _current = _grow_actual_recorder(recorder)
    if fault == "registration_revision":
        owner._registration_revision = -1
    elif fault == "metadata_credential":
        owner._abandonment_transcript_binding = "sk-" + "syntheticcredential" * 3
    else:
        secret = "postgresql://user:secret@host/db"  # secret-scan: allow-this-line
        recorder.record_withheld_reply("planner_prose_unadmitted", secret)
    original = recorder.snapshot_for_continuation(drained=_DRAINED)
    if natural_excess:
        original_text = _project_actual_reply_growth(base_chunk, baseline, original)
        assert len(original_text.encode("utf-8")) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES

    if fault == "registration_revision":
        with pytest.raises(ValidationError, match="registration_revision"):
            _capture(owner, messages, recorder, logical)
    else:
        with pytest.raises(CredentialMaterialRefused) as caught:
            _capture(owner, messages, recorder, logical)
        assert caught.value.finding.category == ("token_shape" if fault == "metadata_credential" else "credential_url")
    assert recorder.snapshot_for_continuation(drained=_DRAINED) == original
    assert all(actual is expected for actual, expected in zip(recorder.withheld_replies, original.withheld_replies, strict=True))


@pytest.mark.parametrize("mode", ("default_encoder", "stored_source"))
def test_default_and_stored_recorder_excess_keep_integrity_refusal(mode):
    owner, messages, recorder, logical, _ = rich_evidence()
    _require_fitting_cold_control(owner, messages, recorder, logical)
    base_chunk, baseline, current = _grow_actual_recorder(recorder)
    if mode == "default_encoder":
        with pytest.raises(AuditIntegrityError, match="private chunk exceeds the complete JSON admission bound"):
            encode_recorder_snapshot(current)
    else:
        canonical = _project_actual_reply_growth(base_chunk, baseline, current)
        encoded = canonical.encode("utf-8")
        unadmitted = PrivateChunk(
            PrivateChunkRef(codec=base_chunk.reference.codec, digest=sha256(encoded).hexdigest(), byte_length=len(encoded)), canonical
        )
        with pytest.raises(AuditIntegrityError):
            admit_application_recorder_source(unadmitted, env_ref_names=frozenset())
    assert recorder.snapshot_for_continuation(drained=_DRAINED) == current


@pytest.mark.parametrize("late_fault", (False, True), ids=("valid-natural-excess", "late-invalid-natural-excess"))
def test_natural_recorder_excess_validates_complete_active_graph_before_late_fields(monkeypatch, late_fault):
    owner, messages, recorder, logical, prepared = active_graph(monkeypatch)
    _require_fitting_cold_control(owner, messages, recorder, logical, data=prepared)
    _base_chunk, _baseline, original = _grow_actual_recorder(recorder)
    if late_fault:
        owner._registration_revision = -1
        with pytest.raises(ValidationError, match="registration_revision"):
            _capture(owner, messages, recorder, logical, data=prepared)
    else:
        with pytest.raises(ComposerContextUnsupported) as caught:
            _capture(owner, messages, recorder, logical, data=prepared)
        assert caught.value.reason == "retained_chunk_bytes"
    assert recorder.snapshot_for_continuation(drained=_DRAINED) == original


def test_prospective_recorder_is_refusal_only_and_cannot_reclassify_fitting_source():
    _owner, _messages, recorder, _logical, _ = rich_evidence()
    base_chunk, baseline, current = _grow_actual_recorder(recorder)
    prospective = capture_fresh_application_recorder_source(current, env_ref_names=frozenset())
    assert type(prospective) is ProspectiveApplicationRecorderSource
    assert require_application_recorder_source(prospective, frozenset()) is prospective
    with pytest.raises(ComposerContextUnsupported) as caught:
        require_application_recorder_capacity(prospective)
    assert caught.value.reason == "retained_chunk_bytes"
    with pytest.raises(AuditIntegrityError):
        admit_application_recorder_source(prospective.chunk, env_ref_names=frozenset())

    fitting = ProspectiveApplicationRecorderSource(base_chunk, baseline, frozenset())
    with pytest.raises(AuditIntegrityError, match="lost its actual complete document excess"):
        require_application_recorder_source(fitting, frozenset())
