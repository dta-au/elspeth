"""Bound repeated full recorder projections without losing graph/source checks."""

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_application_graph as graph_codec
from elspeth.web.composer import continuation_batch_data as batch_codec
from tests.unit.web.composer.test_continuation_active_evidence_graph import _recorder_source_graph


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_reuses_each_unchanged_recorder_source_with_at_most_two_full_projections(monkeypatch, route):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    original = graph_codec.encode_recorder_snapshot
    observed = []

    def counted(snapshot):
        observed.append(snapshot)
        return original(snapshot)

    with monkeypatch.context() as probe:
        probe.setattr(graph_codec, "encode_recorder_snapshot", counted)
        if route == "encode":
            encoded = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
            assert encoded == baseline
            selected_recorder = graph.recorder
        else:
            restored = graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
            assert restored.recorder == graph.recorder
            selected_recorder = restored.recorder

    source_projections = sum(snapshot is source.snapshot for snapshot in observed)
    selected_projections = sum(snapshot is selected_recorder for snapshot in observed)
    assert source_projections == 0, source_projections
    assert 1 <= selected_projections <= 2, selected_projections


def test_recorder_source_admits_original_bytes_and_refuses_new_changed_raw_chunk():
    _graph, source = _recorder_source_graph()
    admitted = graph_codec.admit_application_recorder_source(source.chunk, env_ref_names=frozenset())
    assert admitted.snapshot == source.snapshot
    assert admitted.chunk == source.chunk
    changed = replace(source.chunk, canonical=source.chunk.canonical + " ")
    assert changed.reference == source.chunk.reference
    with pytest.raises(AuditIntegrityError, match=r"^application recorder exceeds its original complete document bound$"):
        graph_codec.admit_application_recorder_source(changed, env_ref_names=frozenset())


def test_graph_refuses_equal_python_recorder_with_different_nested_json_types():
    graph, _source = _recorder_source_graph()
    integer_recorder = replace(
        graph.recorder,
        llm_calls=tuple(replace(call, reasoning_details={"x": 1}) for call in graph.recorder.llm_calls),
    )
    source = graph_codec.capture_application_recorder_source(integer_recorder, env_ref_names=frozenset())
    matching = replace(graph, recorder=integer_recorder)
    encoded = graph_codec.encode_application_graph(matching, env_ref_names=frozenset(), recorder_source=source)
    restored = graph_codec.decode_application_graph(encoded, env_ref_names=frozenset(), recorder_source=source)
    assert restored.recorder == integer_recorder
    assert restored.recorder.llm_calls[0] is not restored.recorder.llm_calls[1]
    boolean_recorder = replace(
        integer_recorder,
        llm_calls=tuple(replace(call, reasoning_details={"x": True}) for call in integer_recorder.llm_calls),
    )
    assert boolean_recorder == source.snapshot
    assert boolean_recorder is not source.snapshot
    assert graph_codec.encode_recorder_snapshot(boolean_recorder) != source.chunk
    mismatched = replace(graph, recorder=boolean_recorder)
    # The actual encoder captures this graph's recorder, then checks its ref
    # against the selected source before projecting any slot references.
    with pytest.raises(AuditIntegrityError, match=r"^application graph lost its call-local selected recorder check$"):
        graph_codec.encode_application_graph(mismatched, env_ref_names=frozenset(), recorder_source=source)


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_does_not_reproject_source_or_invoke_nested_change_hook(monkeypatch, route):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    context = source.snapshot.llm_calls[0].private_request
    assert context is not None and context.canonical is not None
    original = graph_codec.encode_recorder_snapshot
    changed = []

    def change_after_first_source_projection(snapshot):
        chunk = original(snapshot)
        if snapshot is source.snapshot and not changed:
            object.__setattr__(context, "canonical", canonical_json({"messages": [{"role": "user", "content": "changed private tail"}]}))
            changed.append(True)
        return chunk

    monkeypatch.setattr(graph_codec, "encode_recorder_snapshot", change_after_first_source_projection)
    if route == "encode":
        assert graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source) == baseline
        selected = graph
    else:
        selected = graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
        assert graph_codec.encode_application_graph(selected, env_ref_names=frozenset(), recorder_source=source) == baseline
    assert selected.recorder == graph.recorder
    assert selected.recorder.llm_calls[0] is not selected.recorder.llm_calls[1]
    assert changed == []


@pytest.mark.parametrize(
    ("route", "expected_message"),
    (
        ("encode", "application constructors changed their complete selected recorder"),
        ("decode", "application graph changed its complete selected recorder"),
    ),
)
def test_graph_final_source_reproof_refuses_nested_change_after_graph_projection(monkeypatch, route, expected_message):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    context = source.snapshot.llm_calls[0].private_request
    assert context is not None and context.canonical is not None
    original = batch_codec._encode_values
    changed = []

    def change_after_graph_projection(*args, **kwargs):
        encoded = original(*args, **kwargs)
        if kwargs.get("application") is not None and kwargs["application"] is not graph and not changed:
            object.__setattr__(context, "canonical", canonical_json({"messages": [{"role": "user", "content": "changed final tail"}]}))
            changed.append(True)
        return encoded

    monkeypatch.setattr(batch_codec, "_encode_values", change_after_graph_projection)
    # Admitted LLM nodes share immutable private context values. The forced
    # mutation therefore reaches the reconstructed recorder's earlier guard.
    with pytest.raises(AuditIntegrityError, match=f"^{expected_message}$"):
        if route == "encode":
            graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert changed == [True]


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_does_not_reproject_source_or_invoke_chunk_change_hook(monkeypatch, route):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    original = graph_codec.encode_recorder_snapshot
    changed = []

    def change_chunk_after_first_source_projection(snapshot):
        chunk = original(snapshot)
        if snapshot is source.snapshot and not changed:
            object.__setattr__(source, "chunk", replace(source.chunk, canonical=f"{source.chunk.canonical} "))
            changed.append(True)
        return chunk

    monkeypatch.setattr(graph_codec, "encode_recorder_snapshot", change_chunk_after_first_source_projection)
    if route == "encode":
        assert graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source) == baseline
        selected = graph
    else:
        selected = graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
        assert graph_codec.encode_application_graph(selected, env_ref_names=frozenset(), recorder_source=source) == baseline
    assert selected.recorder == graph.recorder
    assert selected.recorder.llm_calls[0] is not selected.recorder.llm_calls[1]
    assert changed == []
