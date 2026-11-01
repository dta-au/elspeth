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
    assert 1 <= source_projections <= 2, source_projections
    assert 1 <= selected_projections <= 2, selected_projections


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_refuses_nested_recorder_change_after_its_first_full_source_projection(monkeypatch, route):
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
    with pytest.raises(
        AuditIntegrityError,
        match=r"^application graph (?:lost its call-local selected recorder check|differs from its complete original recorder source)$",
    ):
        if route == "encode":
            graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert changed == [True]


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_final_source_reproof_refuses_nested_change_after_graph_projection(monkeypatch, route):
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
    with pytest.raises(AuditIntegrityError, match=r"^application recorder changed its complete original fields$"):
        if route == "encode":
            graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert changed == [True]


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_refuses_recorder_chunk_change_after_its_first_full_source_projection(monkeypatch, route):
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
    with pytest.raises(AuditIntegrityError):
        if route == "encode":
            graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert changed == [True]
