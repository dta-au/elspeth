"""Adjacent admitted DATA keeps its source checks and independent mutable owners."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_evidence as evidence
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from tests.unit.web.composer.test_continuation_active_evidence_graph import active_graph
from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence, snapshot


def _original(monkeypatch, *, graph=True, changed=False):
    if graph:
        owner, messages, recorder, logical, data = active_graph(monkeypatch)
        captured = capture_turn_evidence(
            owner,
            messages,
            logical=logical,
            recorder=recorder,
            drained=RecorderDrainCursors(llm_calls=1),
            env_ref_names=frozenset(),
            data=data,
        )
    else:
        owner, messages, recorder, logical, _policy = rich_evidence()
        if changed:
            messages.append({"role": "user", "content": "A distinct actual retained message"})
            owner.transcript.observe_messages(messages)
            recorder.record_withheld_reply("planner_prose_unadmitted", "A distinct actual retained recorder entry")
        captured = snapshot(owner, messages, recorder, logical)
    return evidence.decode_turn_evidence(captured.source_chunk, env_ref_names=frozenset(), source_chunks=captured.dependency_chunks)


def _repeat(prior, *, source=None, names=frozenset(), chunks=None):
    return evidence.AdmittedTurnEvidence(
        prior.source_chunk if source is None else source,
        env_ref_names=names,
        source_chunks=prior.dependency_chunks if chunks is None else chunks,
        _prior_evidence=prior,
    )


@pytest.mark.parametrize("graph", (False, True), ids=("graphless", "active-graph"))
def test_matching_prior_copies_actual_data_without_reconstructing_fields(monkeypatch, graph):
    prior = _original(monkeypatch, graph=graph)
    fields = evidence._require_evidence_field_semantics
    observed = []

    def read_fields(*args, **kwargs):
        observed.append(args[0])
        return fields(*args, **kwargs)

    monkeypatch.setattr(evidence, "_require_evidence_field_semantics", read_fields)
    cold = evidence.decode_turn_evidence(prior.source_chunk, env_ref_names=frozenset(), source_chunks=prior.dependency_chunks)
    assert len(observed) == 1
    copied = _repeat(prior)
    assert len(observed) == 1
    assert copied is not prior and copied.snapshot is prior.snapshot
    assert copied.snapshot == cold.snapshot and copied.source_chunk == cold.source_chunk
    assert copied.recorder_chunk == cold.recorder_chunk and copied.sources == cold.sources
    assert copied._graph_lock is not prior._graph_lock
    copied._require_original_graph()
    if graph:
        left, right = copied.graph, prior.graph
        assert left is not None and right is not None and left is not right
        assert left.selected.batch is not right.selected.batch
        arguments = left.selected.batch.decoded_args_by_call_id
        assert arguments["call-1"] is arguments["call-2"]
        assert arguments["call-1"] is not right.selected.batch.decoded_args_by_call_id["call-1"]
        assert left.selected.batch.last_runtime_preflight is left.selected.accumulator.last_runtime_preflight
        arguments["call-1"]["nested"].append("Changed only in copied DATA")
        prior._require_original_graph()
        with pytest.raises(AuditIntegrityError):
            copied._require_original_graph()
    else:
        assert copied.graph is prior.graph is cold.graph is None
    cold.take_graph()
    prior.take_graph()
    with pytest.raises(AuditIntegrityError, match="transferred"):
        prior.take_graph()


@pytest.mark.parametrize("fault", ("value", "alias"))
def test_matching_prior_refuses_accidental_graph_changes_instead_of_cold_replacement(monkeypatch, fault):
    prior = _original(monkeypatch)
    assert _repeat(prior).source_chunk == prior.source_chunk
    graph = prior.graph
    assert graph is not None
    arguments = graph.selected.batch.decoded_args_by_call_id
    assert arguments["call-1"] is arguments["call-2"]
    if fault == "value":
        arguments["call-1"]["nested"].append("accidental mutation")
    else:
        arguments["call-2"] = dict(arguments["call-1"])
        assert arguments["call-1"] == arguments["call-2"] and arguments["call-1"] is not arguments["call-2"]

    def forbidden_fields(*args, **kwargs):
        pytest.fail("matching mutated graph disappeared behind cold reconstruction")

    monkeypatch.setattr(evidence, "_require_evidence_field_semantics", forbidden_fields)
    with pytest.raises(AuditIntegrityError):
        _repeat(prior)


@pytest.mark.parametrize("mismatch", ("environment", "taken", "source-and-recorder"))
def test_inapplicable_prior_uses_actual_cold_admission(monkeypatch, mismatch):
    prior = _original(monkeypatch, graph=mismatch != "source-and-recorder")
    source, chunks, names = prior.source_chunk, prior.dependency_chunks, frozenset()
    if mismatch == "environment":
        names = frozenset({"ANOTHER_CURRENT_ENVIRONMENT"})
    elif mismatch == "taken":
        taken = prior.take_graph()
        assert taken is not None
        taken.selected.batch.decoded_args_by_call_id["call-1"]["nested"].append("legitimate live change")
    else:
        different = _original(monkeypatch, graph=False, changed=True)
        source, chunks = different.source_chunk, different.dependency_chunks
        assert source != prior.source_chunk and different.recorder_chunk != prior.recorder_chunk
    fields = evidence._require_evidence_field_semantics
    calls = []

    def read_fields(*args, **kwargs):
        calls.append(args[0])
        return fields(*args, **kwargs)

    monkeypatch.setattr(evidence, "_require_evidence_field_semantics", read_fields)
    fresh = _repeat(prior, source=source, names=names, chunks=chunks)
    assert len(calls) == 1 and fresh.source_chunk == source and fresh.env_ref_names == names
    fresh._require_original_graph()


@pytest.mark.parametrize("fault", ("missing-recorder", "changed-recorder-bytes", "source-length"))
def test_prior_data_cannot_replace_fresh_source_and_dependency_integrity(monkeypatch, fault):
    prior = _original(monkeypatch, graph=False)
    assert _repeat(prior).source_chunk == prior.source_chunk
    source, chunks = prior.source_chunk, prior.dependency_chunks
    if fault == "missing-recorder":
        chunks = ()
    elif fault == "changed-recorder-bytes":
        chunks = (replace(prior.recorder_chunk, canonical=prior.recorder_chunk.canonical + " "),)
    else:
        source = replace(source, reference=source.reference.model_copy(update={"byte_length": source.reference.byte_length + 1}))
    with pytest.raises(AuditIntegrityError):
        _repeat(prior, source=source, chunks=chunks)


@pytest.mark.parametrize("unit", ("document", "pair"))
def test_matching_prior_still_applies_current_document_and_pair_limits(monkeypatch, unit):
    prior = _original(monkeypatch, graph=False)
    assert _repeat(prior).source_chunk == prior.source_chunk
    if unit == "document":
        monkeypatch.setattr(evidence, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", prior.source_chunk.reference.byte_length - 1)
    else:
        monkeypatch.setattr(
            evidence,
            "CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES",
            prior.source_chunk.reference.byte_length + prior.recorder_chunk.reference.byte_length - 1,
        )
    with pytest.raises(AuditIntegrityError, match="bound"):
        _repeat(prior)
