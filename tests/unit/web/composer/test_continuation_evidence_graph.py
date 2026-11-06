"""Actual evidence/recorder DATA graph oracles; no SQL authorization claimed here."""

from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence
from elspeth.web.composer import continuation_evidence as evidence_codec
from elspeth.web.composer import turn_effects as effects_module
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, finish_success
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_evidence import admit_turn_evidence, decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_continuation_physical_outcomes import private_record
from tests.unit.web.composer.test_turn_effects import (
    CONTEXT,
    SID,
    _abandonment_snapshot,
    _delivery_call,
    _page,
    _source_inspection_receipt,
    _state,
)


def rich_evidence():
    request = "Inspect café and retain every original record"
    logical = LogicalRequestBinding(
        session_id=SID,
        operation_id=str(UUID(int=71)),
        original_user_message_id="original",
        request_hash="f" * 64,
        kind="compose_message",
        base_state_id=None,
    )
    evidence = TurnEvidence(
        session_id=SID,
        session_operation_context=CONTEXT,
        request_id="original",
        request=request,
        initial_state_id=None,
        initial_state=_state(),
        actor_user_id="alice",
        retained_operation_context=RetainedOperationContext(UUID(SID), "alice", "unavailable"),
    )
    messages = [{"role": "user", "content": request}]
    evidence.transcript.begin_messages(messages)
    for call_id, produced in (("page", _page(0, 3)), ("source", _source_inspection_receipt()), ("conflict", _page(0, 2))):
        admitted, content = produced
        assert evidence.register_blob_page(
            tool_call_id=call_id, admitted=admitted, selected_content=content, producer_call_id="producer-" + call_id
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        assert evidence.register_quoted_inspection(
            tool_call_id=call_id, original_content=content, quoted_role="user", quoted_content="Exact quote for " + call_id
        )
    evidence.transcript.observe_messages(messages)
    delivered = evidence.prepare_delivery(messages, previous_calls=(), tools=None, scope="primary")
    assert evidence.confirm_delivery(delivered, (_delivery_call(messages, call_id="first"),))
    changed, changed_content = _page(2, 4)
    assert not evidence.register_blob_page(
        tool_call_id="conflict", admitted=changed, selected_content=changed_content, producer_call_id="changed-producer"
    )
    current = evidence.prepare_delivery(messages, previous_calls=(_delivery_call(messages, call_id="first"),), tools=None, scope="primary")
    assert evidence.confirm_delivery(current, (_delivery_call(messages, call_id="primary-current"),))
    repeated = {"role": "user", "content": "Same words, separate identity"}
    messages.extend((repeated, repeated, dict(repeated)))
    evidence.transcript.observe_messages(messages)
    policy = _abandonment_snapshot()
    review = evidence.abandonment_context(
        request=request, current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy, messages=messages
    )
    decision = AdvisorAbandonmentResponse(
        decision="CONTINUE_WITH_LIMITATION", reason="Only selected ranges are read", next_action="Read the remainder", question=None
    )
    evidence.accept_abandonment(review, decision, current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy)
    pending = evidence.prepare_delivery(messages, previous_calls=(_delivery_call(messages, call_id="prior"),), tools=None, scope="planner")
    assert evidence._pending_deliveries["planner"] is pending
    recorder = BufferingRecorder()
    recorder.restore_received_llm_call(private_record())
    authoring = PrivateToolAuthoring("metadata", "set_metadata", '{"name":"café"}', '{"name":"café"}', '{"name":"café"}')
    recorder.record_private_tool_authoring(authoring)
    invocation = finish_success(
        begin_dispatch("metadata", "set_metadata", {"name": "café"}, version_before=1, actor="composer-web:alice"),
        result_payload={"success": True, "detail": "retained"},
        version_after=1,
    )
    recorder.record(invocation)
    invocation = recorder.invocations[0]
    recorder.record_visible_tool_event(
        invocation,
        VisibleToolEvent.capture(
            context=CONTEXT,
            request_id="original",
            tool_call_id="metadata",
            tool_name="set_metadata",
            received_arguments=authoring.raw_arguments,
            effective_arguments=authoring.arguments_canonical,
            selected_result=invocation.result_canonical,
            display_arguments=authoring.arguments_canonical,
            display_result=invocation.result_canonical,
        ),
    )
    recorder.record_withheld_reply("repair_gate_superseded", "Keep the withheld café tail")
    return evidence, messages, recorder, logical, policy


def snapshot(evidence, messages, recorder, logical):
    return capture_turn_evidence(
        evidence, messages, recorder=recorder, logical=logical, drained=RecorderDrainCursors(llm_calls=1), env_ref_names=frozenset()
    )


def test_complete_graph_values_aliases_pending_and_private_recorder_roundtrip():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    original = snapshot(evidence, messages, recorder, logical)
    chunk = encode_turn_evidence(original)
    public_bytes = canonical_json(original.snapshot.model_dump(mode="json")).encode("utf-8")
    assert chunk.canonical.encode("utf-8") == public_bytes
    assert chunk.reference.byte_length == len(public_bytes)
    assert chunk.reference.digest == sha256(public_bytes).hexdigest()
    decoded = decode_turn_evidence(chunk, env_ref_names=frozenset(), source_chunks=original.dependency_chunks)
    restored = _restore_current_evidence(decoded, context=CONTEXT)
    owner = restored.evidence
    assert owner is not evidence and restored.recorder is not recorder
    assert owner.effects.initial_state is owner.effects.latest_verified_state
    assert owner._pages["page"] is owner._historical_pages["page"]
    assert "conflict" not in owner._pages and "conflict" in owner._conflicting_page_ids
    assert owner._historical_pages["conflict"].tool_call_id == "conflict"
    assert owner._current_pages["primary"][0] is owner._pages["page"]
    quoted = next(item for item in owner._quoted_inspections.values() if item.page.tool_call_id == "page")
    assert quoted.owner is owner and quoted.page is owner._pages["page"]
    pending = owner._pending_deliveries["planner"]
    assert pending.owner is owner and pending.pages[0] is owner._pages["page"]
    assert pending.previous_call_ids == frozenset({"prior"})
    assert owner._abandonment_review is not None
    review, decision = owner._abandonment_review
    limitation = owner._abandonment_limitations[review.binding]
    assert limitation[0] is review and limitation[1] is decision
    assert restored.messages[-3] is restored.messages[-2]
    assert restored.messages[-3] == restored.messages[-1] and restored.messages[-3] is not restored.messages[-1]
    assert owner.transcript._excluded[0] is restored.messages[0]
    assert restored.recorder.visible_tool_events[0][0] is restored.recorder.invocations[0]
    assert restored.recorder._lock is not recorder._lock
    assert (
        encode_recorder_snapshot(restored.recorder.snapshot_for_continuation(drained=restored.drained)).canonical
        == original.recorder_chunk.canonical
    )
    assert snapshot(owner, restored.messages, restored.recorder, logical).snapshot == original.snapshot
    assert encode_turn_evidence(snapshot(owner, restored.messages, restored.recorder, logical)) == chunk


@pytest.mark.parametrize("mandatory", (False, True), ids=("ordinary-no-prior", "mandatory"))
def test_fresh_capture_keeps_first_admitted_graph_and_original_capture_order(monkeypatch, mandatory):
    from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
    from elspeth.web.composer.continuation_application_graph import capture_application_recorder_source, encode_application_graph
    from elspeth.web.composer.continuation_batch_data import RestoredBatchData
    from tests.unit.web.composer.test_continuation_batch_data import actual_cut

    owner, messages, recorder, logical, _policy = rich_evidence()
    batch, accumulator, calls = actual_cut()
    data = RestoredBatchData(batch, accumulator, calls)
    original_fields = evidence_codec.require_evidence_fields_admission
    original_semantics = evidence_codec._require_evidence_field_semantics
    original_transcript = AdvisorTranscriptOwner.snapshot_for_continuation
    field_inputs, graphs, order = [], [], []

    def admit_fields(value, **kwargs):
        field_inputs.append(value)
        return original_fields(value, **kwargs)

    def admit_semantics(value, **kwargs):
        order.append("fields")
        graph = original_semantics(value, **kwargs)
        graphs.append(graph)
        return graph

    def transcript(self, *args, **kwargs):
        if self is owner.transcript:
            order.append("transcript")
        return original_transcript(self, *args, **kwargs)

    with monkeypatch.context() as observed:
        observed.setattr(evidence_codec, "require_evidence_fields_admission", admit_fields)
        observed.setattr(evidence_codec, "_require_evidence_field_semantics", admit_semantics)
        observed.setattr(AdvisorTranscriptOwner, "snapshot_for_continuation", transcript)
        fresh = capture_turn_evidence(
            owner,
            messages,
            logical=logical,
            recorder=recorder,
            drained=RecorderDrainCursors(),
            env_ref_names=frozenset(),
            data=data,
            mandatory_retention=mandatory,
        )
        assert order == (["fields", "transcript"] if mandatory else ["transcript", "fields"])
        assert len(field_inputs) == len(graphs) == 1
        first_graph = graphs[0]
        assert first_graph is not None and fresh.graph is first_graph
        assert first_graph.selected.batch is not data.batch
        # Calibrate the observers with the public complete-source admission;
        # this separate boundary must still perform its own full field pass.
        public = admit_turn_evidence(
            fresh.snapshot,
            env_ref_names=frozenset(),
            source_chunks=fresh.dependency_chunks,
            mandatory_retention=mandatory,
        )
        assert len(field_inputs) == len(graphs) == 2 and field_inputs[-1] is fresh.snapshot
        assert public.graph is graphs[-1] and public.graph is not first_graph

    assert fresh.snapshot == public.snapshot and fresh.source_chunk == public.source_chunk
    assert fresh.dependency_chunks == public.dependency_chunks
    assert fresh.snapshot.application_entry is None
    assert fresh._graph_lock is not public._graph_lock
    source = capture_application_recorder_source(first_graph.recorder, env_ref_names=frozenset())
    original_graph_json = encode_application_graph(first_graph, env_ref_names=frozenset(), recorder_source=source)
    assert encode_application_graph(public.graph, env_ref_names=frozenset(), recorder_source=source) == original_graph_json
    copied = fresh.copy_graph()
    assert copied is not None and copied is not first_graph and copied.selected.batch is not first_graph.selected.batch
    assert copied.selected.batch.decoded_args_by_call_id["call-1"] is copied.selected.batch.decoded_args_by_call_id["call-2"]
    assert copied.selected.batch.decoded_args_by_call_id is not first_graph.selected.batch.decoded_args_by_call_id
    assert encode_application_graph(copied, env_ref_names=frozenset(), recorder_source=source) == original_graph_json
    assert fresh.take_graph() is first_graph and public.graph is graphs[-1]
    with pytest.raises(AuditIntegrityError, match="admitted graph cannot be transferred twice"):
        fresh.take_graph()


def test_native_evidence_projections_keep_public_bytes_without_reencoding_immutable_transfer(monkeypatch):
    owner, messages, recorder, logical, _ = rich_evidence()
    messages.append({"role": "user", "content": 'Retain "quotes", \\ slash,\n\t café 😀', "retained_numbers": {"tiny": 1e-7}})
    owner.transcript.observe_messages(messages)
    original_writer = evidence_codec.canonical_json_native_strings
    calls = []

    def observe(value):
        calls.append(True)
        result = original_writer(value)
        assert result.encode("utf-8") == canonical_json(value).encode("utf-8")
        return result

    monkeypatch.setattr(evidence_codec, "canonical_json_native_strings", observe)
    admitted = snapshot(owner, messages, recorder, logical)
    assert calls == [True, True]
    chunk = encode_turn_evidence(admitted)
    public_bytes = canonical_json(admitted.snapshot.model_dump(mode="json")).encode("utf-8")
    assert chunk.canonical.encode("utf-8") == public_bytes
    assert chunk.reference.byte_length == len(public_bytes)
    assert chunk.reference.digest == sha256(public_bytes).hexdigest()
    decoded = decode_turn_evidence(chunk, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks)
    assert decoded.snapshot == admitted.snapshot and calls == [True] * 3
    assert decoded.take_graph() is None
    assert calls == [True] * 3


@pytest.mark.parametrize("stage", ("emit", "self_roundtrip", "decode"))
def test_changed_evidence_writer_is_refused_at_each_original_document_check(monkeypatch, stage):
    owner, messages, recorder, logical, _ = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    chunk = encode_turn_evidence(admitted)
    assert decode_turn_evidence(chunk, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks).snapshot == admitted.snapshot
    original_writer = evidence_codec.canonical_json_native_strings
    calls = []
    changed_call = 2 if stage == "self_roundtrip" else 1

    def changed(value):
        calls.append(True)
        result = original_writer(value)
        return result + " " if len(calls) == changed_call else result

    monkeypatch.setattr(evidence_codec, "canonical_json_native_strings", changed)
    refusal = {
        "emit": "private chunk encoding differs from its complete retention preflight",
        "self_roundtrip": "evidence capture changed fields during complete admission",
        "decode": "evidence chunk changed fields during its owned decode",
    }
    with pytest.raises(AuditIntegrityError, match=refusal[stage]):
        if stage == "decode":
            decode_turn_evidence(chunk, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks)
        else:
            admit_turn_evidence(admitted.snapshot, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks)
    assert calls == [True] * changed_call


def test_distinct_equal_initial_latest_are_not_merged_by_json_equality():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    equal = CompositionState.from_dict(evidence.effects.initial_state.to_dict())
    assert equal == evidence.effects.initial_state and equal is not evidence.effects.initial_state
    evidence.effects = replace(evidence.effects, latest_verified_state=equal)
    retained = snapshot(evidence, messages, recorder, logical)
    assert not retained.snapshot.initial_is_latest
    restored = _restore_current_evidence(
        decode_turn_evidence(encode_turn_evidence(retained), env_ref_names=frozenset(), source_chunks=retained.dependency_chunks),
        context=CONTEXT,
    )
    assert restored.evidence.effects.initial_state == restored.evidence.effects.latest_verified_state
    assert restored.evidence.effects.initial_state is not restored.evidence.effects.latest_verified_state


def test_new_current_context_does_not_promote_historical_abandonment_scope():
    evidence, messages, recorder, logical, policy = rich_evidence()
    review = evidence._abandonment_review[0]
    evidence.accept_abandonment(
        review,
        AdvisorAbandonmentResponse(decision="STOP_CONFIRMED", reason="No supported action", next_action=None, question=None),
        current_state=evidence.effects.latest_verified_state,
        plugin_snapshot=policy,
    )
    assert evidence.confirmed_abandonment(current_state=evidence.effects.latest_verified_state, plugin_snapshot=policy)
    retained = snapshot(evidence, messages, recorder, logical)
    current = SessionOperationContext(SessionOperationFence(SID, "new-current-operation", "new-current-lease", 2), CONTEXT.operation_kind)
    restored = _restore_current_evidence(retained, context=current)
    assert restored.evidence.operation_id == current.fence.operation_id
    assert restored.evidence._continuation_current_epoch == 2
    assert restored.evidence._abandonment_review[0].request_scope == evidence._abandonment_review[0].request_scope
    assert (
        restored.evidence.confirmed_abandonment(current_state=restored.evidence.effects.latest_verified_state, plugin_snapshot=policy)
        is False
    )


def test_pending_delivery_resumes_real_confirmation_and_does_not_repeat():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    restored = _restore_current_evidence(snapshot(evidence, messages, recorder, logical), context=CONTEXT)
    owner = restored.evidence
    attempt = owner._pending_deliveries["planner"]
    call = _delivery_call(restored.messages, call_id="cold-planner")
    assert owner.confirm_delivery(attempt, (call,))
    assert "planner" not in owner._pending_deliveries
    assert not owner.confirm_delivery(attempt, (call,))


@pytest.mark.parametrize("field", ("recorder_json", "initial_is_latest", "decision_shelf", "historical_application_count"))
def test_missing_complete_field_cannot_default_on_decode(field):
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import encode_private_chunk

    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    assert (
        decode_turn_evidence(encode_turn_evidence(retained), env_ref_names=frozenset(), source_chunks=retained.dependency_chunks).snapshot
        == retained.snapshot
    )
    payload = retained.snapshot.model_dump(mode="json")
    del payload["recorder" if field == "recorder_json" else field]
    chunk = encode_private_chunk(codec="composer.evidence.v2", canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode_turn_evidence(chunk, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)


def test_new_metadata_credentials_refuse_after_real_positive():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    encode_turn_evidence(retained)
    bad = retained.snapshot.model_copy(update={"actor_user_id": "postgresql://user:secret@host/db"})  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        admit_turn_evidence(bad, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)


def test_equal_evidence_states_reuse_parse_and_hash_but_check_both_retained_hashes(monkeypatch):
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    original_read = evidence_codec.state_from_evidence_json
    original_hash = effects_module.authored_state_hash
    states, hashes = [], []

    def read(text):
        state = original_read(text)
        states.append((text, state))
        return state

    def hash_state(state):
        result = original_hash(state)
        hashes.append(result)
        return result

    monkeypatch.setattr(evidence_codec, "state_from_evidence_json", read)
    monkeypatch.setattr(effects_module, "authored_state_hash", hash_state)
    checked = evidence_codec.TurnEvidenceSnapshot.model_validate_json(retained.source_chunk.canonical)
    assert checked == retained.snapshot
    assert checked.effects.initial_state_json == checked.effects.latest_verified_state_json
    assert len(states) == 1 and states[0][0] == checked.effects.initial_state_json
    assert canonical_json(states[0][1].to_dict()) == checked.effects.initial_state_json
    assert hashes == [checked.effects.initial_authored_state_hash]
    assert checked.effects.latest_authored_state_hash == hashes[0]

    # Each hash is independently authoritative even when the state bytes agree.
    for field in ("initial_authored_state_hash", "latest_authored_state_hash"):
        changed = checked.model_dump(mode="json")
        changed["effects"][field] = "0" * 64
        assert changed["effects"][field] != hashes[0]
        source = encode_private_chunk(codec="composer.evidence.v2", canonical=canonical_json(changed))
        with pytest.raises(AuditIntegrityError, match="closed owned codec") as refused:
            decode_turn_evidence(source, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)
        assert "authored graph hashes" in str(refused.value.__cause__)


def test_evidence_state_credentials_reuse_equal_bytes_preserving_distinct_versions_and_roles(monkeypatch):
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    effects = retained.snapshot.effects
    original_read = evidence_codec.state_from_evidence_json
    original_guard = evidence_codec.require_no_credential_material_in_state
    parsed, scanned = [], []

    def read(text):
        parsed.append(text)
        return original_read(text)

    def guard(state, *, surface, env_ref_names):
        scanned.append((canonical_json(state.to_dict()), surface))
        original_guard(state, surface=surface, env_ref_names=env_ref_names)

    monkeypatch.setattr(evidence_codec, "state_from_evidence_json", read)
    monkeypatch.setattr(evidence_codec, "require_no_credential_material_in_state", guard)
    initial_surface = "composer_continuation_evidence_initial_state"
    latest_surface = "composer_continuation_evidence_latest_state"
    evidence_codec._require_evidence_state_credentials(effects, env_ref_names=frozenset())
    assert parsed == [effects.initial_state_json]
    assert scanned == [(effects.initial_state_json, initial_surface)]

    initial = original_read(effects.initial_state_json)
    latest = replace(initial, version=initial.version + 1)
    latest_text = canonical_json(latest.to_dict())
    assert latest_text != effects.initial_state_json
    assert effects_module.authored_state_hash(initial) == effects_module.authored_state_hash(latest)
    distinct = effects.model_copy(update={"latest_verified_state_json": latest_text})
    parsed.clear()
    scanned.clear()
    evidence_codec._require_evidence_state_credentials(distinct, env_ref_names=frozenset())
    assert parsed == [effects.initial_state_json, latest_text]
    assert scanned == [(effects.initial_state_json, initial_surface), (latest_text, latest_surface)]

    # Hash equality cannot relax the independent initial/latest relationship.
    candidate = retained.snapshot.model_copy(update={"effects": distinct})
    with pytest.raises(AuditIntegrityError, match="retention preflight") as refused:
        admit_turn_evidence(candidate, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)
    assert "shared initial/latest state" in str(refused.value.__cause__)

    secret_state = replace(initial, metadata=replace(initial.metadata, description="sk-" + "syntheticcredential" * 3))
    secret_text = canonical_json(secret_state.to_dict())
    for initial_text, expected_surface in ((effects.initial_state_json, latest_surface), (secret_text, initial_surface)):
        secret_pair = effects.model_copy(update={"initial_state_json": initial_text, "latest_verified_state_json": secret_text})
        with pytest.raises(CredentialMaterialRefused) as credential_refused:
            evidence_codec._require_evidence_state_credentials(secret_pair, env_ref_names=frozenset())
        assert credential_refused.value.surface == expected_surface

    # Latest bytes must still receive losslessness validation after a clean initial state.
    dropped_field = canonical_json(latest.to_dict() | {"undeclared_state_field": "cannot disappear"})
    invalid = effects.model_copy(update={"latest_verified_state_json": dropped_field})
    with pytest.raises(ValueError, match="lossless canonical owned state"):
        evidence_codec._require_evidence_state_credentials(invalid, env_ref_names=frozenset())


def test_equal_state_credentials_refuse_at_initial_role_on_actual_evidence_admission():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    assert decode_turn_evidence(retained.source_chunk, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks).snapshot == (
        retained.snapshot
    )
    state = evidence_codec.state_from_evidence_json(retained.snapshot.effects.initial_state_json)
    state = replace(state, metadata=replace(state.metadata, description="sk-" + "syntheticcredential" * 3))
    encoded = canonical_json(state.to_dict())
    state_hash = effects_module.authored_state_hash(state)
    changed_effects = retained.snapshot.effects.model_copy(
        update={
            "initial_state_json": encoded,
            "latest_verified_state_json": encoded,
            "initial_authored_state_hash": state_hash,
            "latest_authored_state_hash": state_hash,
        }
    )
    candidate = retained.snapshot.model_copy(update={"effects": changed_effects})
    with pytest.raises(CredentialMaterialRefused) as refused:
        admit_turn_evidence(candidate, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)
    assert refused.value.surface == "composer_continuation_evidence_initial_state"


def test_committed_history_refuses_without_erasing_its_actual_ids():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    snapshot(evidence, messages, recorder, logical)
    evidence.effects = replace(evidence.effects, committed_revision_ids=(str(UUID(int=88)),))
    with pytest.raises(AuditIntegrityError, match="actual retained P4 provenance"):
        snapshot(evidence, messages, recorder, logical)
    assert evidence.effects.committed_revision_ids == (str(UUID(int=88)),)


def test_live_blob_reader_is_refused_without_calling_or_persisting_callback():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    snapshot(evidence, messages, recorder, logical)

    def reader(*args, **kwargs):
        raise AssertionError("capture must never call the live blob reader")

    evidence.transcript.use_blob_reader(reader)
    with pytest.raises(AuditIntegrityError, match="current blob-reader materialization"):
        snapshot(evidence, messages, recorder, logical)
    assert evidence.transcript._blob_reader is reader


def test_model_copy_cannot_skip_complete_owned_frontier_admission():
    evidence, messages, recorder, logical, _policy = rich_evidence()
    retained = snapshot(evidence, messages, recorder, logical)
    encode_turn_evidence(retained)
    corrupt = retained.snapshot.model_copy(
        update={"effects": retained.snapshot.effects.model_copy(update={"net_changed_fields": ("invented",)})}
    )
    # The retention preflight revalidates every nested owned model before the
    # graph/encode path, so the copied frontier is refused at that first owner.
    with pytest.raises(AuditIntegrityError, match="retention preflight found invalid complete owned model data") as refused:
        admit_turn_evidence(corrupt, env_ref_names=frozenset(), source_chunks=retained.dependency_chunks)
    assert "changed its complete admitted base frontier" in str(refused.value.__cause__)
