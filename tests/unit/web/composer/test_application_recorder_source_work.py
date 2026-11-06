"""Bound repeated full recorder projections without losing graph/source checks."""

import json
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_application_graph as graph_codec
from elspeth.web.composer.withheld_replies import WithheldReply
from tests.unit.web.composer.test_continuation_active_evidence_graph import _recorder_source_graph
from tests.unit.web.composer.test_portable_evidence_application import portable_input
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.parametrize("relation", ("identical", "equal", "different"))
def test_current_recorder_projection_supplies_the_exact_text_to_bounded_admission(monkeypatch, relation):
    from elspeth.web.composer import continuation_audit_buffer as audit_buffer

    _graph, source = _recorder_source_graph()
    snapshot = source.snapshot if relation == "identical" else replace(source.snapshot)
    if relation == "different":
        snapshot = replace(
            snapshot, withheld_replies=(*snapshot.withheld_replies, WithheldReply("planner_prose_unadmitted", "later reply"))
        )
    expected = audit_buffer.encode_recorder_snapshot(snapshot)
    original_projection = graph_codec.recorder_snapshot_json
    original_encode = graph_codec.encode_private_chunk
    projections, admitted = [], []

    def project(value):
        canonical = original_projection(value)
        projections.append((value, canonical))
        return canonical

    def encode(**values):
        admitted.append(values["canonical"])
        return original_encode(**values)

    monkeypatch.setattr(graph_codec, "recorder_snapshot_json", project)
    monkeypatch.setattr(audit_buffer, "recorder_snapshot_json", project)
    monkeypatch.setattr(graph_codec, "encode_private_chunk", encode)
    monkeypatch.setattr(audit_buffer, "encode_private_chunk", encode)
    assert graph_codec.application_recorder_chunk(snapshot, recorder_source=source) == expected
    assert len(projections) == (0 if relation == "identical" else 1)
    assert len(admitted) == (1 if relation == "different" else 0)
    if admitted:
        assert admitted[0] is projections[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("encode", "decode"))
@pytest.mark.parametrize("shared", (False, True))
async def test_historical_recorder_admission_follows_distinct_snapshot_identities(operation_store, tmp_path, monkeypatch, route, shared):
    from elspeth.web.composer.continuation_evidence_application_data import build_portable_p4_application

    async with portable_input(operation_store, tmp_path, monkeypatch) as (_running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        original = bundle.evidence_after.graph
        assert original is not None and len(original.cuts) == 1
        source = graph_codec.admit_application_recorder_source(bundle.audit_chunk, env_ref_names=values["env_ref_names"])
        cut = original.cuts[0]
        before = replace(
            cut.recorder_before,
            withheld_replies=(*cut.recorder_before.withheld_replies, WithheldReply("planner_prose_unadmitted", "historical sentinel")),
        )
        after = before if shared else replace(before)
        historical_cut = replace(cut, recorder_before=before, recorder_after=after)
        graph = replace(original, cuts=(historical_cut, historical_cut))
        encoded = graph_codec.encode_application_graph(graph, env_ref_names=values["env_ref_names"], recorder_source=source)
        encode = graph_codec.encode_private_chunk
        admitted = []

        def observe(**kwargs):
            chunk = encode(**kwargs)
            admitted.append(chunk)
            return chunk

        with monkeypatch.context() as probe:
            probe.setattr(graph_codec, "encode_private_chunk", observe)
            if route == "encode":
                assert graph_codec.encode_application_graph(graph, env_ref_names=values["env_ref_names"], recorder_source=source) == encoded
            else:
                restored = graph_codec.decode_application_graph(encoded, env_ref_names=values["env_ref_names"], recorder_source=source)
                assert restored.cuts[0] is restored.cuts[1]
                assert (restored.cuts[0].recorder_before is restored.cuts[0].recorder_after) is shared
                assert restored.cuts[0].recorder_before == before
        assert len(admitted) == (1 if shared else 2) * (2 if route == "encode" else 1)
        assert all(chunk.canonical == admitted[0].canonical for chunk in admitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("encode", "decode"))
@pytest.mark.parametrize("corruption", ("cursor", "credential", "capacity"))
async def test_historical_recorder_refusal_survives_shared_graph_projection(operation_store, tmp_path, monkeypatch, route, corruption):
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.composer.continuation_evidence_application_data import build_portable_p4_application
    from elspeth.web.credential_guard import CredentialMaterialRefused

    async with portable_input(operation_store, tmp_path, monkeypatch) as (_running, _args, values, _audit, _messages):
        bundle = build_portable_p4_application(**values)
        original = bundle.evidence_after.graph
        assert original is not None and len(original.cuts) == 1
        source = graph_codec.admit_application_recorder_source(bundle.audit_chunk, env_ref_names=values["env_ref_names"])
        cut = original.cuts[0]
        marker = "historical recorder sentinel"
        reply = WithheldReply("planner_prose_unadmitted", marker)
        prior_replies = cut.recorder_before.withheld_replies
        historical = replace(
            cut.recorder_before, withheld_replies=prior_replies + (reply,) * 60, drained=replace(cut.recorder_before.drained)
        )
        graph = replace(original, cuts=(replace(cut, recorder_before=historical, recorder_after=historical),))
        encoded = graph_codec.encode_application_graph(graph, env_ref_names=values["env_ref_names"], recorder_source=source)
        restored = graph_codec.decode_application_graph(encoded, env_ref_names=values["env_ref_names"], recorder_source=source)
        assert restored.cuts[0].recorder_before is restored.cuts[0].recorder_after
        assert restored.cuts[0].recorder_before.withheld_replies[-60] is restored.cuts[0].recorder_before.withheld_replies[-1]
        if corruption == "cursor":
            bad_cursor = len(historical.llm_calls) + 1
            changed = replace(historical, drained=replace(historical.drained, llm_calls=bad_cursor))
            payload = json.loads(encoded)
            recorders = [node for node in payload["nodes"] if node["kind"] == "application_recorder"]
            assert len(recorders) == 2
            cursor_ref = dict(recorders[-1]["value"])["drained"]["ref"]
            cursor_fields = dict(payload["nodes"][cursor_ref]["value"])
            cursor = json.loads(cursor_fields["canonical"]["scalar"])
            cursor["llm_calls"] = bad_cursor
            cursor_fields["canonical"]["scalar"] = canonical_json(cursor)
            changed_wire = canonical_json(payload)
            error, message = AuditIntegrityError, "buffer drain cursor exceeds its retained channel"
        else:
            content = "https://user:password@example.com" if corruption == "credential" else "x" * 20_000
            changed_reply = replace(reply, content=content)
            changed = replace(historical, withheld_replies=prior_replies + (changed_reply,) * 60)
            assert encoded.count(marker) == 1
            changed_wire = encoded.replace(marker, content)
            assert len(changed_wire.encode("utf-8")) < JSON_MAX_TOTAL_UTF8_BYTES
            if corruption == "credential":
                error, message = CredentialMaterialRefused, ""
            else:
                error, message = AuditIntegrityError, "private chunk exceeds the complete JSON admission bound"
        with pytest.raises(error, match=message):
            if route == "encode":
                changed_graph = replace(graph, cuts=(replace(graph.cuts[0], recorder_before=changed, recorder_after=changed),))
                graph_codec.encode_application_graph(changed_graph, env_ref_names=values["env_ref_names"], recorder_source=source)
            else:
                graph_codec.decode_application_graph(changed_wire, env_ref_names=values["env_ref_names"], recorder_source=source)


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


@pytest.mark.parametrize("route", ("encode", "decode", "projection"))
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
        elif route == "decode":
            restored = graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
            assert restored.recorder == graph.recorder
            assert restored.recorder.llm_calls[0] is not restored.recorder.llm_calls[1]
            selected_recorder = restored.recorder
        else:
            assert graph_codec._application_graph_projection(graph, env_ref_names=frozenset(), recorder_source=source) == baseline
            selected_recorder = graph.recorder

    source_projections = sum(snapshot is source.snapshot for snapshot in observed)
    selected_projections = sum(snapshot is selected_recorder for snapshot in observed)
    assert source_projections == 0, source_projections
    assert selected_projections == 1, selected_projections
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


@pytest.mark.parametrize("route", ("encode", "projection"))
def test_graph_refuses_equal_python_recorder_with_different_nested_json_types(route):
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
        if route == "encode":
            graph_codec.encode_application_graph(mismatched, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec._application_graph_projection(mismatched, env_ref_names=frozenset(), recorder_source=source)


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


@pytest.mark.parametrize("route", ("encode", "decode"))
@pytest.mark.parametrize("constructor", ("recorder", "llm"))
def test_graph_post_constructor_admission_refuses_changed_recorder_values(monkeypatch, route, constructor):
    graph, source = _recorder_source_graph()
    baseline = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    restored = graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert restored.recorder == graph.recorder
    assert restored.recorder.llm_calls[0] is not restored.recorder.llm_calls[1]
    original_member = graph_codec.construct_application_member
    original_llm = graph_codec.construct_application_llm
    changed = []

    def changed_member(kind, members):
        value = original_member(kind, members)
        if kind == "application_recorder":
            replacement = replace(
                value, withheld_replies=(*value.withheld_replies, WithheldReply("planner_prose_unadmitted", "unexpected constructor reply"))
            )
            assert replacement != value
            changed.append(replacement)
            return replacement
        return value

    def changed_llm(actual_source, slot):
        value = original_llm(actual_source, slot)
        replacement = replace(value, reasoning_content="unexpected constructor reasoning")
        assert replacement != value
        changed.append(replacement)
        return replacement

    if constructor == "recorder":
        monkeypatch.setattr(graph_codec, "construct_application_member", changed_member)
    else:
        monkeypatch.setattr(graph_codec, "construct_application_llm", changed_llm)
    # Model an ordinary constructor returning wrong DATA, without mutating
    # frozen storage. Admission after construction must bind the actual value.
    with pytest.raises(AuditIntegrityError, match=r"^application graph lost its call-local selected recorder check$"):
        if route == "encode":
            graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
        else:
            graph_codec.decode_application_graph(baseline, env_ref_names=frozenset(), recorder_source=source)
    assert changed
    assert graph.recorder == source.snapshot


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
