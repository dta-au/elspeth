"""Bound repeated full recorder projections without losing graph/source checks."""

from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_application_graph as graph_codec
from elspeth.web.composer import continuation_batch_data as batch_codec
from tests.unit.web.composer.test_continuation_active_evidence_graph import _recorder_source_graph


@pytest.mark.asyncio
@pytest.mark.parametrize("memo_used", (False, True))
@pytest.mark.parametrize(
    "capture",
    (graph_codec.capture_application_recorder_source, graph_codec.capture_fresh_application_recorder_source),
    ids=("ordinary", "fresh-fitting"),
)
async def test_recorder_capture_admits_original_once_and_keeps_detached_roundtrip_and_environment(monkeypatch, memo_used, capture):
    from elspeth.web import credential_guard
    from elspeth.web.composer import continuation_audit_buffer as audit_buffer
    from elspeth.web.continuation_retention_memo import v4_preparation_memo_scope
    from tests.unit.web.test_continuation_retention_memo import _advisor_clock

    graph, _source = _recorder_source_graph()
    snapshot = graph.recorder
    baseline = graph_codec.capture_application_recorder_source(snapshot, env_ref_names=frozenset())
    original_render = audit_buffer.recorder_snapshot_json
    original_decode = audit_buffer._decode_recorder_document
    original_encode = audit_buffer.encode_private_chunk
    original_guard = credential_guard.require_no_credential_material
    rendered, decoded, encoded, guarded = [], [], [], []

    def render(value):
        rendered.append(value)
        return original_render(value)

    def decode(text):
        value = original_decode(text)
        decoded.append(value)
        return value

    def guard(value, *, surface, env_ref_names, max_string_bytes):
        guarded.append((value, surface, env_ref_names))
        return original_guard(value, surface=surface, env_ref_names=env_ref_names, max_string_bytes=max_string_bytes)

    def encode(**values):
        result = original_encode(**values)
        encoded.append(result)
        return result

    monkeypatch.setattr(graph_codec, "recorder_snapshot_json", render)
    monkeypatch.setattr(audit_buffer, "recorder_snapshot_json", render)
    monkeypatch.setattr(audit_buffer, "_decode_recorder_document", decode)
    monkeypatch.setattr(audit_buffer, "encode_private_chunk", encode)
    monkeypatch.setattr(credential_guard, "require_no_credential_material", guard)
    with v4_preparation_memo_scope(_advisor_clock()) if memo_used else nullcontext():
        for names in (frozenset(), frozenset({"CURRENT_NAME"})):
            result = capture(snapshot, env_ref_names=names)
            assert type(result) is graph_codec.ApplicationRecorderSource
            assert result.chunk == baseline.chunk and result.snapshot == baseline.snapshot
            assert result.snapshot is not snapshot and result.env_ref_names is names
            assert result.chunk is encoded[-1]
            assert graph_codec.require_application_recorder_source(result, names) is result
            with pytest.raises(AuditIntegrityError, match="changed its admitted environment"):
                graph_codec.require_application_recorder_source(result, names | {"ANOTHER_NAME"})
    assert sum(value is snapshot for value in rendered) == 2
    assert len(decoded) == (1 if memo_used else 2)
    assert [value for value in rendered if value is not snapshot] == decoded
    assert len(encoded) == (1 if memo_used else 2)
    # These observe admission entries, not classifier work hidden by its memo.
    assert guarded == [(baseline.chunk.canonical, "composer_continuation_private_chunk", frozenset())] * (1 if memo_used else 2)


def test_fresh_fitting_capture_preserves_actual_credential_refusal():
    from elspeth.web.composer.withheld_replies import WithheldReply
    from elspeth.web.credential_guard import CredentialMaterialRefused

    graph, _source = _recorder_source_graph()
    snapshot = replace(
        graph.recorder,
        withheld_replies=(*graph.recorder.withheld_replies, WithheldReply("planner_prose_unadmitted", "https://user:password@example.com")),
    )
    with pytest.raises(CredentialMaterialRefused) as ordinary:
        graph_codec.capture_application_recorder_source(snapshot, env_ref_names=frozenset())
    with pytest.raises(CredentialMaterialRefused) as fresh:
        graph_codec.capture_fresh_application_recorder_source(snapshot, env_ref_names=frozenset())
    assert fresh.value.finding == ordinary.value.finding
    assert str(fresh.value) == str(ordinary.value)


@pytest.mark.parametrize("route", ("encode", "decode"))
def test_graph_reprojects_current_recorder_but_reuses_exact_admitted_bytes(monkeypatch, route):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    original = graph_codec.encode_recorder_snapshot
    original_projection = graph_codec.recorder_snapshot_json
    observed = []
    encoded_snapshots = []

    def counted(snapshot):
        encoded_snapshots.append(snapshot)
        return original(snapshot)

    def projected(snapshot):
        observed.append(snapshot)
        return original_projection(snapshot)

    with monkeypatch.context() as probe:
        probe.setattr(graph_codec, "encode_recorder_snapshot", counted)
        probe.setattr(graph_codec, "recorder_snapshot_json", projected)
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
    assert encoded_snapshots == []


def test_recorder_source_admits_original_bytes_and_refuses_new_changed_raw_chunk():
    _graph, source = _recorder_source_graph()
    admitted = graph_codec.admit_application_recorder_source(source.chunk, env_ref_names=frozenset())
    assert admitted.snapshot == source.snapshot
    assert admitted.chunk == source.chunk
    changed = replace(source.chunk, canonical=source.chunk.canonical + " ")
    assert changed.reference == source.chunk.reference
    with pytest.raises(AuditIntegrityError, match=r"^application recorder exceeds its original complete document bound$"):
        graph_codec.admit_application_recorder_source(changed, env_ref_names=frozenset())


def test_raw_recorder_source_rejects_actual_oversize_before_public_decode(monkeypatch):
    _graph, source = _recorder_source_graph()
    actual = graph_codec.decode_recorder_snapshot
    decoded = []

    def decode(chunk):
        decoded.append(chunk)
        return actual(chunk)

    monkeypatch.setattr(graph_codec, "decode_recorder_snapshot", decode)
    assert graph_codec.admit_application_recorder_source(source.chunk, env_ref_names=frozenset()).chunk == source.chunk
    assert decoded == [source.chunk]
    # Malformed oversized bytes would fail parsing if the early bound moved.
    canonical = "{" + "x" * graph_codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    encoded = canonical.encode("utf-8")
    reference = replace(
        source.chunk,
        reference=source.chunk.reference.model_copy(update={"digest": sha256(encoded).hexdigest(), "byte_length": len(encoded)}),
        canonical=canonical,
    )
    with pytest.raises(AuditIntegrityError, match="application recorder exceeds its original complete document bound"):
        graph_codec.admit_application_recorder_source(reference, env_ref_names=frozenset())
    assert decoded == [source.chunk]


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
