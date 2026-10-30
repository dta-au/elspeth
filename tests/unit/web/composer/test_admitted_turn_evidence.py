"""Actual source admission, fixed DATA reuse, and fresh live capture boundaries."""

import copy
import json
import pickle
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_batch_data as batch_codec
from elspeth.web.composer.continuation_application_graph import decode_application_graph, encode_application_graph
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_evidence import (
    AdmittedTurnEvidence,
    TurnEvidenceSnapshot,
    admit_turn_evidence,
    decode_turn_evidence,
    encode_turn_evidence,
    require_admitted_turn_evidence,
)
from elspeth.web.composer.continuation_evidence_application_data import (
    build_portable_p4_application,
    require_admitted_p4_application,
    require_selected_applied_batch_chunk,
)
from elspeth.web.composer.continuation_evidence_graph import _restore_current_evidence, capture_turn_evidence
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.manual_advisor_allowance import ManualAdvisorAllowance
from tests.unit.web.composer.test_continuation_batch_data import actual_cut
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence, snapshot
from tests.unit.web.composer.test_portable_evidence_application import _graph_codec_counts, _require_one_source_decode, portable_input
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case

operation_store = test_composer_operation_authority.operation_store


def _require_no_application_graph_work(counts):
    for key in ("decode_application_graph", "encode_application_graph", "decode_application_true", "encode_application_true"):
        assert counts[key] == 0


def test_reuse_and_serialization_do_not_accept_a_raw_snapshot_as_an_admission_certificate():
    owner, messages, recorder, logical, _policy = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    assert admit_turn_evidence(admitted.snapshot, env_ref_names=frozenset()).snapshot == admitted.snapshot
    for consumer in (require_admitted_turn_evidence, encode_turn_evidence):
        with pytest.raises(TypeError, match="actual source-admitted owner"):
            consumer(admitted.snapshot, env_ref_names=frozenset())


@pytest.mark.parametrize("source_kind", ("snapshot", "chunk"))
def test_factory_owns_complete_source_admission_and_immutable_environment_policy(source_kind):
    owner, messages, recorder, logical, _policy = rich_evidence()
    original = snapshot(owner, messages, recorder, logical)
    names = frozenset({"EXPLICIT_REFERENCE_NAME"})
    source = original.snapshot if source_kind == "snapshot" else encode_turn_evidence(original)
    admitted = AdmittedTurnEvidence(source, env_ref_names=names)
    assert admitted.snapshot == original.snapshot and admitted.graph is None
    assert admitted.env_ref_names is names
    equal_names = frozenset(set(names))
    assert equal_names == names and equal_names is not names
    assert require_admitted_turn_evidence(admitted, env_ref_names=equal_names) is admitted
    assert decode_turn_evidence(encode_turn_evidence(admitted, env_ref_names=names), env_ref_names=names).snapshot == original.snapshot


@pytest.mark.parametrize("bad_names", ({"EXPLICIT_REFERENCE_NAME"}, frozenset({1})), ids=("mutable", "non-string"))
def test_factory_and_reuse_refuse_an_unowned_environment_name_domain(bad_names):
    owner, messages, recorder, logical, _policy = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    with pytest.raises(TypeError, match="closed environment-name set"):
        AdmittedTurnEvidence(admitted.snapshot, env_ref_names=bad_names)
    with pytest.raises(TypeError, match="closed environment-name set"):
        require_admitted_turn_evidence(admitted, env_ref_names=bad_names)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("fields", "graph", "callback", "flag", "replace", "copy", "deepcopy", "pickle"))
async def test_actual_carrier_cannot_be_constructed_or_copied_from_an_arbitrary_source_graph_pair(operation_store, route):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        admitted, names = bundle.evidence_after, values["env_ref_names"]
        foreign = decode_application_graph(bundle.entry.graph_json, env_ref_names=names)
        assert admitted.graph is not None and foreign is not admitted.graph
        with pytest.raises(TypeError):
            if route == "fields":
                AdmittedTurnEvidence(snapshot=admitted.snapshot, graph=foreign, env_ref_names=names)
            elif route == "graph":
                AdmittedTurnEvidence(admitted.snapshot, graph=foreign, env_ref_names=names)
            elif route == "callback":
                AdmittedTurnEvidence(admitted.snapshot, callback=lambda: foreign, env_ref_names=names)
            elif route == "flag":
                AdmittedTurnEvidence(admitted.snapshot, admitted=True, env_ref_names=names)
            elif route == "replace":
                replace(admitted, graph=foreign)
            elif route == "copy":
                copy.copy(admitted)
            elif route == "deepcopy":
                copy.deepcopy(admitted)
            else:
                pickle.dumps(admitted)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ("reuse", "serialize", "restore", "build", "result", "selected"))
async def test_fixed_admission_cannot_be_restamped_with_a_changed_credential_policy(operation_store, consumer):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        admitted = bundle.evidence_after
        changed_names = admitted.env_ref_names | {"OTHER_REFERENCE_NAME"}
        with pytest.raises(AuditIntegrityError, match="changed its admitted credential environment"):
            if consumer == "reuse":
                require_admitted_turn_evidence(admitted, env_ref_names=changed_names)
            elif consumer == "serialize":
                encode_turn_evidence(admitted, env_ref_names=changed_names)
            elif consumer == "restore":
                _restore_current_evidence(admitted, context=running.session_operation_context, env_ref_names=changed_names)
            elif consumer == "build":
                build_portable_p4_application(**{**values, "env_ref_names": changed_names})
            elif consumer == "result":
                require_admitted_p4_application(
                    admitted,
                    result_chunk=values["result_chunk"],
                    result=values["result"],
                    producer=values["producer"],
                    env_ref_names=changed_names,
                )
            else:
                require_selected_applied_batch_chunk(bundle.batch_chunk, evidence=admitted, env_ref_names=changed_names)
        # A different policy requires a new complete source admission.
        fresh = admit_turn_evidence(admitted.snapshot, env_ref_names=changed_names)
        assert fresh is not admitted and fresh.graph is not admitted.graph
        assert fresh.snapshot == admitted.snapshot and fresh.env_ref_names == changed_names
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("paired", (False, True))
async def test_factory_retains_outer_evidence_before_inner_journal_refusal(operation_store, paired):
    running, _args, values, _audit, _messages = await portable_input(operation_store, kind="nongraph")
    try:
        bundle = build_portable_p4_application(**values)
        source = bundle.evidence_after.snapshot
        assert source.initial_is_latest is True
        assert source.effects.initial_state_json == source.effects.latest_verified_state_json
        record = bundle.entry.applications[0].model_copy(update={"receipt_id": "different-well-formed-receipt"})
        entry = bundle.entry.model_copy(update={"applications": (record, *bundle.entry.applications[1:])})
        changed = source.model_copy(update={"application_entry": entry, **({"initial_is_latest": False} if paired else {})})
        assert TurnEvidenceSnapshot.model_validate_json(changed.model_dump_json()) == changed
        expected = (
            "evidence differs from its actual shared effects/recorder graph"
            if paired
            else "portable journal changed its immutable receipt locator"
        )
        with _graph_codec_counts() as refused, pytest.raises(AuditIntegrityError, match=expected):
            AdmittedTurnEvidence(changed, env_ref_names=values["env_ref_names"])
        assert refused["decode_application_graph"] == 1 and refused["encode_application_graph"] == 0
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_repeated_equal_source_admission_never_interns_mutable_graph_owners(operation_store, record_property):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        with _graph_codec_counts() as primitive:
            decode_application_graph(bundle.entry.graph_json, env_ref_names=names)
        with _graph_codec_counts() as first:
            left = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names)
        with _graph_codec_counts() as second:
            right = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names)
        assert left is not right and left.snapshot == right.snapshot
        assert left.graph is not right.graph
        assert left.graph.selected.accumulator is left.graph.cuts[-1].data.accumulator
        assert right.graph.selected.accumulator is right.graph.cuts[-1].data.accumulator
        assert left.graph.selected.accumulator is not right.graph.selected.accumulator
        retained = len(right.graph.selected.batch.tool_outcomes)
        assert retained > 0
        # These owners have escaped for the isolation control; neither carrier
        # is then reused as a fresh source admission or live capture.
        left.graph.selected.batch.tool_outcomes.clear()
        assert len(right.graph.selected.batch.tool_outcomes) == retained
        with _graph_codec_counts() as third:
            fresh = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names)
        assert fresh.graph is not left.graph and fresh.graph is not right.graph
        assert len(fresh.graph.selected.batch.tool_outcomes) == retained
        for counts in (first, second, third):
            _require_one_source_decode(counts, primitive)
        record_property(
            "independent_equal_source_counts",
            json.dumps({"primitive": dict(primitive), "first": dict(first), "second": dict(second), "fresh": dict(third)}),
        )
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_carrier_admits_once_then_reuses_without_application_codec_roundtrips(operation_store, record_property):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        with _graph_codec_counts() as primitive:
            decode_application_graph(bundle.entry.graph_json, env_ref_names=names)
        with _graph_codec_counts() as admission:
            admitted = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names)
        _require_one_source_decode(admission, primitive)
        with _graph_codec_counts() as reuse:
            assert require_admitted_turn_evidence(admitted, env_ref_names=names) is admitted
            assert encode_turn_evidence(admitted, env_ref_names=names) == bundle.evidence_chunk
            require_admitted_p4_application(
                admitted,
                result_chunk=values["result_chunk"],
                result=values["result"],
                producer=values["producer"],
                env_ref_names=names,
            )
            require_selected_applied_batch_chunk(bundle.batch_chunk, evidence=admitted, env_ref_names=names)
            # Last graph reuse: restoration now transfers these decoded owners.
            restored = _restore_current_evidence(admitted, context=running.session_operation_context, env_ref_names=names)
        _require_no_application_graph_work(reuse)
        record_property(
            "admitted_graph_codec_counts", json.dumps({"primitive": dict(primitive), "admission": dict(admission), "reuse": dict(reuse)})
        )
        assert restored.evidence._portable_p4_graph is not None
        assert restored.evidence.effects is restored.evidence._portable_p4_graph.effects
        assert restored.batch.accumulator is restored.evidence._portable_p4_graph.cuts[-1].data.accumulator
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ("decoder", "encoder", "low_decoder", "low_encoder"))
async def test_reuse_count_oracle_rejects_real_extra_application_codec_work(operation_store, extra, record_property):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        admitted, names = bundle.evidence_after, values["env_ref_names"]
        with _graph_codec_counts() as positive:
            encode_turn_evidence(admitted, env_ref_names=names)
        _require_no_application_graph_work(positive)
        with _graph_codec_counts() as observed:
            encode_turn_evidence(admitted, env_ref_names=names)
            if extra == "decoder":
                decode_application_graph(bundle.entry.graph_json, env_ref_names=names)
            elif extra == "encoder":
                encode_application_graph(admitted.graph, env_ref_names=names)
            elif extra == "low_decoder":
                batch_codec._decode_graph(bundle.entry.graph_json, names, application=True)
            else:
                batch_codec._encode(admitted.graph.selected, names, application=admitted.graph)
        record_property(
            "admitted_redundant_codec_counts", json.dumps({"fault": extra, "positive": dict(positive), "actual": dict(observed)})
        )
        with pytest.raises(AssertionError):
            _require_no_application_graph_work(observed)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_fresh_capture_preserves_warm_owners_and_admits_changed_source_after_graph_escape(operation_store, record_property):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        source_graph = values["evidence_snapshot"].graph
        assert source_graph is not None
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        assert bundle.batch_after.accumulator is source_graph.selected.accumulator
        assert bundle.batch_after.calls is source_graph.selected.calls
        assert bundle.evidence_after.graph is not None
        assert bundle.batch_after is not bundle.evidence_after.graph.selected
        assert bundle.recorder_after is not bundle.evidence_after.graph.recorder
        old_chunk = encode_turn_evidence(bundle.evidence_after, env_ref_names=names)
        restored = _restore_current_evidence(bundle.evidence_after, context=running.session_operation_context, env_ref_names=names)
        message = {"role": "system", "content": "Fresh materialization after the fixed graph owners escaped"}
        restored.messages.append(message)
        restored.evidence.transcript.observe_messages(restored.messages)
        with _graph_codec_counts() as capture:
            fresh = capture_turn_evidence(
                restored.evidence,
                restored.messages,
                logical=values["logical"],
                recorder=restored.recorder,
                drained=restored.drained,
                env_ref_names=names,
                data=restored.batch,
            )
        record_property("journal_bearing_fresh_capture_counts", json.dumps(dict(capture)))
        # Retain both mandatory-order admissions: the journal check before
        # warm-history checks, then the final complete snapshot admission.
        assert capture["decode_application_graph"] == 2
        assert capture["encode_application_graph"] == 1
        assert capture["decode_application_true"] >= capture["decode_application_graph"]
        assert capture["encode_application_true"] >= capture["encode_application_graph"]
        assert encode_turn_evidence(fresh, env_ref_names=names) != old_chunk
        shelf = fresh.snapshot.transcript
        retained_message = shelf.messages_json[shelf.current_indexes[-1]]
        assert retained_message == encode_transcript_object_json(message)
        assert json.loads(retained_message) == message
        assert tuple(json.loads(retained_message)) == tuple(message)
        assert fresh.graph is not restored.evidence._portable_p4_graph
        assert fresh.graph.selected.batch is not restored.batch.batch
        assert fresh.snapshot.application_entry.applications == bundle.entry.applications
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.parametrize("dispatched", (False, True), ids=("reserved", "dispatched-unclosed"))
def test_fresh_evidence_capture_keeps_the_real_u22_reservation_boundary(dispatched):
    owner, messages, recorder, logical, _policy = rich_evidence()
    batch, accumulator, calls = actual_cut()
    batch.advisor_calls_used = 0
    allowance = ManualAdvisorAllowance(limit=2, used=0)
    batch.manual_advisor_allowance = allowance
    data = RestoredBatchData(batch, accumulator, calls)

    def capture():
        return capture_turn_evidence(
            owner, messages, logical=logical, recorder=recorder, drained=RecorderDrainCursors(), env_ref_names=frozenset(), data=data
        )

    positive = capture()
    reservation = allowance.reserve()
    assert reservation is not None
    if dispatched:
        reservation.dispatch()
        batch.advisor_calls_used = allowance.used
    with pytest.raises(AuditIntegrityError, match="reservation"):
        capture()
    reservation.close()
    fresh = capture()
    assert fresh is not positive and fresh.graph.selected.batch.manual_advisor_allowance is None
    assert fresh.graph.selected.batch.advisor_calls_used == allowance.used == (1 if dispatched else 0)
    assert fresh.graph.selected.accumulator.advisor_calls_used == accumulator.advisor_calls_used == 2


@pytest.mark.asyncio
async def test_actual_owed_sql_reader_hands_off_its_one_admitted_graph_without_projection_readmission(operation_store, record_property):
    running, selected, inventory, expected, commit = await committed_evidence_case(operation_store, joint=True)
    try:
        assert expected.snapshot.application_entry is None and expected.snapshot.joint_graph_json is not None
        with _graph_codec_counts() as primitive:
            decode_application_graph(expected.snapshot.joint_graph_json, env_ref_names=inventory.names)
        # Observe the actual locked SQL owner synchronously, so its real codec
        # calls execute in this profiler's thread; no reader/decoder is replaced.
        with _graph_codec_counts() as read:
            checked = operation_store[2].read_continuation_owed_p4_evidence(
                running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
        _require_one_source_decode(read, primitive)
        with _graph_codec_counts() as projection:
            restored = _restore_current_evidence(checked.snapshot, context=running.session_operation_context, env_ref_names=inventory.names)
        _require_no_application_graph_work(projection)
        record_property(
            "owed_current_read_graph_counts", json.dumps({"primitive": dict(primitive), "read": dict(read), "projection": dict(projection)})
        )
        assert checked.snapshot.snapshot == expected.snapshot
        assert restored.batch is not None and restored.batch.batch.phase == "p4_pending"
        assert checked.result.receipt.effect_key == commit.effect_key
    finally:
        operation_store[1].release(running.session_operation_context)
