"""Actual source admission, fixed DATA reuse, and fresh live capture boundaries."""

import copy
import json
import pickle
import sys
from contextlib import contextmanager
from dataclasses import replace
from types import FrameType

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_audit_buffer as recorder_codec
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
from tests.unit.web.composer.test_portable_evidence_application import (
    _graph_codec_counts,
    _recorder_source,
    _require_one_source_decode,
    portable_input,
)
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_owed_evidence import committed_evidence_case

operation_store = test_composer_operation_authority.operation_store


def _require_no_application_graph_work(counts):
    for key in ("decode_application_graph", "encode_application_graph", "decode_application_true", "encode_application_true"):
        assert counts[key] == 0


def _require_owed_joint_final_transfer_work(counts):
    # Received from the actual owed-reader joint/no-history transfer. Other
    # historical shapes need their own measured lineage, not this constant.
    expected = {
        "decode_application_graph": 0,
        "encode_application_graph": 1,
        "decode_application_true": 1,
        "decode_application_false": 0,
        "encode_application_true": 2,
        "encode_application_false": 0,
    }
    actual = dict(counts)
    assert actual.keys() == expected.keys(), f"final_transfer_keys: {actual}"
    assert all(type(value) is int and value >= 0 for value in actual.values()), f"final_transfer_types: {actual}"
    for key, required in expected.items():
        assert actual[key] == required, f"{key}: {actual}"


def _require_owed_joint_final_transfer_recorder_work(counts):
    # Received only for this owed-reader joint/no-history transfer, including
    # its source-context preparation. Historical shapes need their own measure.
    expected = {"decode_recorder_snapshot": 1, "encode_recorder_snapshot": 11}
    actual = dict(counts)
    assert actual.keys() == expected.keys(), f"final_transfer_recorder_keys: {actual}"
    assert all(type(value) is int and value >= 0 for value in actual.values()), f"final_transfer_recorder_types: {actual}"
    for key, required in expected.items():
        assert actual[key] == required, f"{key}: {actual}"


@contextmanager
def _recorder_codec_counts():
    """Actual whole-R entries, separately from the unchanged graph metrics."""
    counts = {"decode_recorder_snapshot": 0, "encode_recorder_snapshot": 0}
    decoder_code = recorder_codec.decode_recorder_snapshot.__code__
    encoder_code = recorder_codec.encode_recorder_snapshot.__code__
    module_globals = recorder_codec.__dict__
    source_path = recorder_codec.__file__
    previous = sys.getprofile()

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == "call" and frame.f_globals is module_globals and frame.f_code.co_filename == source_path:
            if frame.f_code is decoder_code:
                counts["decode_recorder_snapshot"] += 1
            elif frame.f_code is encoder_code:
                counts["encode_recorder_snapshot"] += 1
        if previous is not None:
            previous(frame, event, argument)

    sys.setprofile(profile)
    try:
        yield counts
    finally:
        sys.setprofile(previous)


def test_recorder_counter_observes_real_original_codecs_and_zero_immutable_reuse(record_property):
    owner, messages, recorder, logical, _policy = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    source = _recorder_source(admitted)
    previous = sys.getprofile()
    with _recorder_codec_counts() as decoded_counts:
        decoded = recorder_codec.decode_recorder_snapshot(admitted.recorder_chunk)
    assert sys.getprofile() is previous
    assert decoded_counts["decode_recorder_snapshot"] == 1
    assert decoded == source.snapshot
    with _recorder_codec_counts() as encoded_counts:
        encoded = recorder_codec.encode_recorder_snapshot(decoded)
    assert sys.getprofile() is previous
    assert encoded_counts == {"decode_recorder_snapshot": 0, "encode_recorder_snapshot": 1}
    assert encoded == admitted.recorder_chunk
    with _recorder_codec_counts() as reuse_recorder, _graph_codec_counts() as reuse_graph:
        assert require_admitted_turn_evidence(admitted, env_ref_names=admitted.env_ref_names) is admitted
        assert encode_turn_evidence(admitted, env_ref_names=admitted.env_ref_names) == admitted.source_chunk
    assert sys.getprofile() is previous
    _require_no_application_graph_work(reuse_graph)
    assert reuse_recorder == {"decode_recorder_snapshot": 0, "encode_recorder_snapshot": 0}
    record_property(
        "recorder_counter_controls",
        json.dumps(
            {"decode": decoded_counts, "encode": encoded_counts, "reuse_recorder": reuse_recorder, "reuse_graph": dict(reuse_graph)}
        ),
    )


def test_reuse_and_serialization_do_not_accept_a_raw_snapshot_as_an_admission_certificate():
    owner, messages, recorder, logical, _policy = rich_evidence()
    admitted = snapshot(owner, messages, recorder, logical)
    assert (
        admit_turn_evidence(admitted.snapshot, env_ref_names=frozenset(), source_chunks=admitted.dependency_chunks).snapshot
        == admitted.snapshot
    )
    for consumer in (require_admitted_turn_evidence, encode_turn_evidence):
        with pytest.raises(TypeError, match="actual source-admitted owner"):
            consumer(admitted.snapshot, env_ref_names=frozenset())


@pytest.mark.parametrize("source_kind", ("snapshot", "chunk"))
def test_factory_owns_complete_source_admission_and_immutable_environment_policy(source_kind):
    owner, messages, recorder, logical, _policy = rich_evidence()
    original = snapshot(owner, messages, recorder, logical)
    names = frozenset({"EXPLICIT_REFERENCE_NAME"})
    source = original.snapshot if source_kind == "snapshot" else encode_turn_evidence(original)
    admitted = AdmittedTurnEvidence(source, env_ref_names=names, source_chunks=original.dependency_chunks)
    assert admitted.snapshot == original.snapshot and admitted.graph is None
    assert admitted.env_ref_names is names
    equal_names = frozenset(set(names))
    assert equal_names == names and equal_names is not names
    assert require_admitted_turn_evidence(admitted, env_ref_names=equal_names) is admitted
    assert (
        decode_turn_evidence(
            encode_turn_evidence(admitted, env_ref_names=names), env_ref_names=names, source_chunks=admitted.dependency_chunks
        ).snapshot
        == original.snapshot
    )


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
async def test_actual_carrier_cannot_be_constructed_or_copied_from_an_arbitrary_source_graph_pair(
    operation_store, route, tmp_path, monkeypatch
):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (_running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        admitted, names = bundle.evidence_after, values["env_ref_names"]
        foreign = decode_application_graph(bundle.entry.graph_json, env_ref_names=names, recorder_source=_recorder_source(admitted))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ("reuse", "serialize", "restore", "build", "result", "selected"))
async def test_fixed_admission_cannot_be_restamped_with_a_changed_credential_policy(operation_store, consumer, tmp_path, monkeypatch):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (running, _args, values, _audit, _messages):
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
        fresh = admit_turn_evidence(admitted.snapshot, env_ref_names=changed_names, source_chunks=admitted.dependency_chunks)
        assert fresh is not admitted and fresh.graph is not admitted.graph
        assert fresh.snapshot == admitted.snapshot and fresh.env_ref_names == changed_names


@pytest.mark.asyncio
@pytest.mark.parametrize("paired", (False, True))
async def test_factory_retains_outer_evidence_before_inner_journal_refusal(operation_store, paired, tmp_path, monkeypatch):
    async with portable_input(operation_store, tmp_path, monkeypatch, kind="nongraph") as (_running, _args, values, _audit, _messages):
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
            AdmittedTurnEvidence(changed, env_ref_names=values["env_ref_names"], source_chunks=bundle.evidence_after.dependency_chunks)
        assert refused["decode_application_graph"] == 1 and refused["encode_application_graph"] == 0


@pytest.mark.asyncio
async def test_repeated_equal_source_admission_never_interns_mutable_graph_owners(operation_store, record_property, tmp_path, monkeypatch):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (_running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        with _graph_codec_counts() as primitive:
            decode_application_graph(bundle.entry.graph_json, env_ref_names=names, recorder_source=_recorder_source(bundle.evidence_after))
        with _graph_codec_counts() as first:
            left = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names, source_chunks=bundle.evidence_after.dependency_chunks)
        with _graph_codec_counts() as second:
            right = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names, source_chunks=bundle.evidence_after.dependency_chunks)
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
            fresh = decode_turn_evidence(bundle.evidence_chunk, env_ref_names=names, source_chunks=bundle.evidence_after.dependency_chunks)
        assert fresh.graph is not left.graph and fresh.graph is not right.graph
        assert len(fresh.graph.selected.batch.tool_outcomes) == retained
        for counts in (first, second, third):
            _require_one_source_decode(counts, primitive)
        record_property(
            "independent_equal_source_counts",
            json.dumps({"primitive": dict(primitive), "first": dict(first), "second": dict(second), "fresh": dict(third)}),
        )


@pytest.mark.asyncio
async def test_actual_carrier_admits_once_then_reuses_without_application_codec_roundtrips(
    operation_store, record_property, tmp_path, monkeypatch
):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        names = values["env_ref_names"]
        with _graph_codec_counts() as primitive:
            decode_application_graph(bundle.entry.graph_json, env_ref_names=names, recorder_source=_recorder_source(bundle.evidence_after))
        with _graph_codec_counts() as admission:
            admitted = decode_turn_evidence(
                bundle.evidence_chunk, env_ref_names=names, source_chunks=bundle.evidence_after.dependency_chunks
            )
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
        _require_no_application_graph_work(reuse)
        # Mutable-root transfer has its own charged window. This historical
        # shape is not assigned the separate no-history fixture's exact vector.
        with _recorder_codec_counts() as transfer_recorder, _graph_codec_counts() as transfer_graph:
            restored = _restore_current_evidence(admitted, context=running.session_operation_context, env_ref_names=names)
        record_property(
            "admitted_graph_codec_counts",
            json.dumps(
                {
                    "primitive": dict(primitive),
                    "admission": dict(admission),
                    "reuse": dict(reuse),
                    "final_transfer_graph": dict(transfer_graph),
                    "final_transfer_recorder": transfer_recorder,
                }
            ),
        )
        assert restored.evidence._portable_p4_graph is not None
        assert restored.evidence.effects is restored.evidence._portable_p4_graph.effects
        assert restored.batch.accumulator is restored.evidence._portable_p4_graph.cuts[-1].data.accumulator


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ("decoder", "encoder", "low_decoder", "low_encoder"))
async def test_reuse_count_oracle_rejects_real_extra_application_codec_work(operation_store, extra, record_property, tmp_path, monkeypatch):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (_running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        admitted, names = bundle.evidence_after, values["env_ref_names"]
        with _graph_codec_counts() as positive:
            encode_turn_evidence(admitted, env_ref_names=names)
        _require_no_application_graph_work(positive)
        with _graph_codec_counts() as observed:
            encode_turn_evidence(admitted, env_ref_names=names)
            if extra == "decoder":
                decode_application_graph(bundle.entry.graph_json, env_ref_names=names, recorder_source=_recorder_source(admitted))
            elif extra == "encoder":
                encode_application_graph(admitted.graph, env_ref_names=names, recorder_source=_recorder_source(admitted))
            elif extra == "low_decoder":
                batch_codec._decode_graph(bundle.entry.graph_json, names, application=True, recorder_source=_recorder_source(admitted))
            else:
                batch_codec._encode(admitted.graph.selected, names, application=admitted.graph, recorder_source=_recorder_source(admitted))
        record_property(
            "admitted_redundant_codec_counts", json.dumps({"fault": extra, "positive": dict(positive), "actual": dict(observed)})
        )
        with pytest.raises(AssertionError):
            _require_no_application_graph_work(observed)


@pytest.mark.asyncio
async def test_fresh_capture_preserves_warm_owners_and_admits_changed_source_after_graph_escape(
    operation_store, record_property, tmp_path, monkeypatch
):
    async with portable_input(operation_store, tmp_path, monkeypatch) as (running, _args, values, _audit, _messages):
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
            decode_application_graph(
                expected.snapshot.joint_graph_json, env_ref_names=inventory.names, recorder_source=_recorder_source(expected)
            )
        # Observe the actual locked SQL owner synchronously, so its real codec
        # calls execute in this profiler's thread; no reader/decoder is replaced.
        with _graph_codec_counts() as read:
            checked = operation_store[2].read_continuation_owed_p4_evidence(
                running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
        _require_one_source_decode(read, primitive)
        with _recorder_codec_counts() as recorder_projection, _graph_codec_counts() as projection:
            restored = _restore_current_evidence(checked.snapshot, context=running.session_operation_context, env_ref_names=inventory.names)
        record_property(
            "owed_current_read_graph_counts",
            json.dumps(
                {
                    "primitive": dict(primitive),
                    "read": dict(read),
                    "projection": dict(projection),
                    "recorder_projection": recorder_projection,
                }
            ),
        )
        print("owed_final_transfer " + json.dumps({"graph": dict(projection), "recorder": recorder_projection}, sort_keys=True))
        _require_owed_joint_final_transfer_work(projection)
        _require_owed_joint_final_transfer_recorder_work(recorder_projection)
        assert checked.snapshot.snapshot == expected.snapshot
        assert restored.batch is not None and restored.batch.batch.phase == "p4_pending"
        assert checked.result.receipt.effect_key == commit.effect_key
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_final_transfer_contract_rejects_real_extra_public_decoder(operation_store, record_property):
    running, selected, inventory, expected, commit = await committed_evidence_case(operation_store, joint=True)
    try:
        assert expected.snapshot.application_entry is None and expected.snapshot.joint_graph_json is not None
        original_wire = expected.snapshot.joint_graph_json
        recorder_source = _recorder_source(expected)
        with _graph_codec_counts() as primitive:
            decode_application_graph(original_wire, env_ref_names=inventory.names, recorder_source=recorder_source)
        with _graph_codec_counts() as read:
            checked = operation_store[2].read_continuation_owed_p4_evidence(
                running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
        _require_one_source_decode(read, primitive)
        assert checked.snapshot.snapshot == expected.snapshot
        assert checked.snapshot.recorder_chunk == recorder_source.chunk
        assert checked.result.receipt.effect_key == commit.effect_key
        with _recorder_codec_counts() as recorder_counts, _graph_codec_counts() as observed:
            restored = _restore_current_evidence(checked.snapshot, context=running.session_operation_context, env_ref_names=inventory.names)
            required = dict(observed)
            required_recorder = dict(recorder_counts)
            # The actual required transfer and original joins must be healthy
            # before adding any extra codec work to this same observation.
            _require_owed_joint_final_transfer_work(required)
            _require_owed_joint_final_transfer_recorder_work(required_recorder)
            assert restored.batch is not None and restored.batch.batch.phase == "p4_pending"
            extra = decode_application_graph(original_wire, env_ref_names=inventory.names, recorder_source=recorder_source)
            assert extra.selected is not restored.batch and extra.selected.batch.phase == "p4_pending"
            assert extra.recorder == recorder_source.snapshot
        actual = dict(observed)
        record_property(
            "owed_final_transfer_public_decoder_surplus",
            json.dumps(
                {
                    "required": required,
                    "surplus": actual,
                    "primitive": dict(primitive),
                    "required_recorder": required_recorder,
                    "surplus_recorder": recorder_counts,
                }
            ),
        )
        print("owed_public_decoder_surplus " + json.dumps({"required": required, "surplus": actual}, sort_keys=True))
        _require_owed_joint_final_transfer_work(required)
        assert actual["decode_application_graph"] == 1
        with pytest.raises(AssertionError, match=r"^decode_application_graph:"):
            _require_owed_joint_final_transfer_work(actual)
        with pytest.raises(AssertionError, match=r"^encode_recorder_snapshot:"):
            _require_owed_joint_final_transfer_recorder_work(recorder_counts)
        # Explain the observed surplus using an independently executed real
        # primitive. The gate above receives the full unmodified observation.
        assert actual == {key: required[key] + primitive[key] for key in required}
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_recorder_surplus_is_visible_outside_exact_graph_transfer_counts(operation_store, record_property):
    running, selected, inventory, expected, commit = await committed_evidence_case(operation_store, joint=True)
    try:
        assert expected.snapshot.application_entry is None and expected.snapshot.joint_graph_json is not None
        recorder_source = _recorder_source(expected)
        with _graph_codec_counts() as primitive_graph:
            decode_application_graph(expected.snapshot.joint_graph_json, env_ref_names=inventory.names, recorder_source=recorder_source)
        with _graph_codec_counts() as read:
            checked = operation_store[2].read_continuation_owed_p4_evidence(
                running, selected=selected, inventory=inventory, effect_key=commit.effect_key
            )
        _require_one_source_decode(read, primitive_graph)
        assert checked.snapshot.snapshot == expected.snapshot
        assert checked.snapshot.recorder_chunk == recorder_source.chunk
        assert checked.result.receipt.effect_key == commit.effect_key
        with _recorder_codec_counts() as primitive_recorder:
            decoded = recorder_codec.decode_recorder_snapshot(checked.snapshot.recorder_chunk)
        assert primitive_recorder["decode_recorder_snapshot"] == 1
        assert decoded == recorder_source.snapshot
        with _recorder_codec_counts() as recorder_counts, _graph_codec_counts() as graph_counts:
            restored = _restore_current_evidence(checked.snapshot, context=running.session_operation_context, env_ref_names=inventory.names)
            required_graph = dict(graph_counts)
            required_recorder = dict(recorder_counts)
            _require_owed_joint_final_transfer_work(required_graph)
            _require_owed_joint_final_transfer_recorder_work(required_recorder)
            assert restored.batch is not None and restored.batch.batch.phase == "p4_pending"
            extra = recorder_codec.decode_recorder_snapshot(checked.snapshot.recorder_chunk)
            assert extra == recorder_source.snapshot
        actual_graph, actual_recorder = dict(graph_counts), dict(recorder_counts)
        record_property(
            "owed_final_transfer_recorder_surplus",
            json.dumps(
                {
                    "required_graph": required_graph,
                    "surplus_graph": actual_graph,
                    "required_recorder": required_recorder,
                    "surplus_recorder": actual_recorder,
                    "primitive_recorder": primitive_recorder,
                }
            ),
        )
        print(
            "owed_recorder_surplus "
            + json.dumps(
                {"graph": actual_graph, "required_recorder": required_recorder, "surplus_recorder": actual_recorder}, sort_keys=True
            )
        )
        # The unchanged six graph metrics alone miss this actual whole-R decode.
        _require_owed_joint_final_transfer_work(actual_graph)
        assert actual_graph == required_graph
        with pytest.raises(AssertionError, match=r"^decode_recorder_snapshot:"):
            _require_owed_joint_final_transfer_recorder_work(actual_recorder)
        assert actual_recorder["decode_recorder_snapshot"] == required_recorder["decode_recorder_snapshot"] + 1
        assert actual_recorder == {key: required_recorder[key] + primitive_recorder[key] for key in required_recorder}
    finally:
        operation_store[1].release(running.session_operation_context)
