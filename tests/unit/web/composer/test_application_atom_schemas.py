"""Warm nominal schemas must not retain payloads, sources or graph aliases."""

import json

import pytest
from pydantic import TypeAdapter, ValidationError
from pydantic_core import PydanticSerializationError

from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_application_graph as graph_codec
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from tests.unit.web.composer.test_continuation_active_evidence_graph import _recorder_source_graph


def test_warm_atom_schema_reads_the_current_owned_fields():
    cursors = RecorderDrainCursors()
    baseline = graph_codec._atom_json(cursors, RecorderDrainCursors)
    original = graph_codec.construct_application_member("application_cursors", {"canonical": baseline})
    assert original == cursors and original is not cursors

    object.__setattr__(cursors, "llm_calls", 1)
    changed = graph_codec._atom_json(cursors, RecorderDrainCursors)
    assert changed != baseline
    following = graph_codec.construct_application_member("application_cursors", {"canonical": changed})
    assert following.llm_calls == 1 and original.llm_calls == 0
    assert graph_codec._atom_json(original, RecorderDrainCursors) == baseline


@pytest.mark.parametrize("change", ("bool", "float", "string", "extra"))
def test_warm_atom_schema_keeps_strict_types_and_closed_fields(change):
    baseline = graph_codec._atom_json(RecorderDrainCursors(), RecorderDrainCursors)
    assert graph_codec.construct_application_member("application_cursors", {"canonical": baseline}) == RecorderDrainCursors()
    changed = json.loads(baseline)
    if change == "extra":
        changed["foreign_field"] = 0
    else:
        changed["llm_calls"] = {"bool": True, "float": 1.5, "string": "1"}[change]
    with pytest.raises(ValidationError):
        graph_codec.construct_application_member("application_cursors", {"canonical": canonical_json(changed)})


def test_integer_valued_float_retains_the_original_uncached_canonicalization():
    payload = json.loads(graph_codec._atom_json(RecorderDrainCursors(), RecorderDrainCursors))
    payload["llm_calls"] = 1.0
    encoded = canonical_json(payload)
    assert type(json.loads(encoded)["llm_calls"]) is int
    original = TypeAdapter(RecorderDrainCursors).validate_json(encoded, strict=True, extra="forbid")
    restored = graph_codec.construct_application_member("application_cursors", {"canonical": encoded})
    assert original == restored and restored.llm_calls == 1


def test_warm_atom_schema_refuses_a_structural_dictionary_for_an_owned_atom():
    baseline = graph_codec._atom_json(RecorderDrainCursors(), RecorderDrainCursors)
    with pytest.raises(PydanticSerializationError):
        graph_codec._atom_json(json.loads(baseline), RecorderDrainCursors)


@pytest.mark.parametrize("repeated", (False, True))
def test_warm_atom_schemas_restore_fresh_graphs_with_the_original_alias_partition(repeated):
    graph, source = _recorder_source_graph(repeated=repeated)
    encoded = graph_codec.encode_application_graph(graph, env_ref_names=frozenset(), recorder_source=source)
    first = graph_codec.decode_application_graph(encoded, env_ref_names=frozenset(), recorder_source=source)
    assert first.recorder == graph.recorder
    assert (first.recorder.llm_calls[0] is first.recorder.llm_calls[1]) is repeated
    original_reasoning = source.snapshot.llm_calls[0].reasoning_content
    object.__setattr__(first.recorder.llm_calls[0], "reasoning_content", "changed only the preceding decoded graph")

    following = graph_codec.decode_application_graph(encoded, env_ref_names=frozenset(), recorder_source=source)
    assert following.recorder == graph.recorder
    assert following.recorder.llm_calls[0].reasoning_content == original_reasoning
    assert following.recorder.llm_calls[0] is not first.recorder.llm_calls[0]
    assert (following.recorder.llm_calls[0] is following.recorder.llm_calls[1]) is repeated
    assert graph_codec.encode_application_graph(following, env_ref_names=frozenset(), recorder_source=source) == encoded
