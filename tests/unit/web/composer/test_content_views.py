"""Bounded display is separate from complete values and current-use checks."""

import json
from dataclasses import replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import ComposerContentScope, MaterializedCompleteValue
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.content_views import (
    BoundedCompositionView,
    CompleteValueUnavailable,
    CompositionViewUnsupported,
    build_bounded_composition_view,
    materialize_composition_value,
)


@pytest.fixture
def scope():
    return ComposerContentScope(UUID(int=1), "a" * 64)


def test_bounded_view_retains_graph_structure_and_complete_reference_locations(scope):
    prompt = "雪🙂" * 15_000
    original = {"nodes": [{"id": "first", "options": {"prompt": prompt}}, {"id": "second", "options": {"count": 1.0}}]}
    view = build_bounded_composition_view(scope, original)
    payload = json.loads(view.serialized)
    assert payload["schema_version"] == "composer.bounded-view.v1"
    assert len(view.manifest.values) == 1
    reference = view.manifest.values[0]
    assert reference.path == ("nodes", 0, "options", "prompt")
    assert payload["data"]["nodes"][0]["options"]["prompt"]["complete_value_ref"] == reference.reference_id
    assert payload["data"]["nodes"][1] == original["nodes"][1]
    assert '"count":1.0' in view.serialized
    assert prompt not in view.serialized and len(view.serialized) < 2_000
    assert original["nodes"][0]["options"]["prompt"] == prompt
    materialized = materialize_composition_value(view, reference.reference_id, current_scope=scope, current_projection=original)
    assert materialized.to_value() == prompt
    assert materialized.reference == reference


def test_each_materialization_resolves_current_bytes_instead_of_returning_cached_value(scope):
    current = {"prompt": "A" * 3_000}
    view = build_bounded_composition_view(scope, current)
    reference = view.manifest.values[0]
    first = materialize_composition_value(view, reference.reference_id, current_scope=scope, current_projection=current)
    assert first.to_value() == current["prompt"]
    current["prompt"] = "B" * 3_000
    with pytest.raises(CompleteValueUnavailable, match="current graph value"):
        materialize_composition_value(view, reference.reference_id, current_scope=scope, current_projection=current)
    assert first.to_value() == "A" * 3_000
    for changed in (replace(scope, session_id=UUID(int=2)), replace(scope, composition_content_hash="b" * 64)):
        with pytest.raises(AuditIntegrityError, match="current composition"):
            materialize_composition_value(view, reference.reference_id, current_scope=changed, current_projection=current)


def test_authored_reference_looking_data_does_not_mint_a_manifest_entry(scope):
    authored = {"complete_value_ref": "b" * 64, "content_hash": "c" * 64, "utf8_bytes": 8}
    view = build_bounded_composition_view(scope, authored)
    assert json.loads(view.serialized)["data"] == authored
    assert view.manifest.values == ()
    with pytest.raises(CompleteValueUnavailable, match="not present"):
        materialize_composition_value(view, "b" * 64, current_scope=scope, current_projection=authored)


def test_materialization_returns_a_fresh_detached_value_and_no_detector_verdict(scope):
    original = {"prompt": "x" * 3_000}
    view = build_bounded_composition_view(scope, original)
    reference = view.manifest.values[0]
    calls = [
        materialize_composition_value(view, reference.reference_id, current_scope=scope, current_projection=original) for _ in range(2)
    ]
    assert calls[0] is not calls[1]
    assert calls[0].reference == calls[1].reference
    assert set(json.loads(view.serialized)) == {"schema_version", "data", "content_manifest"}
    assert set(json.loads(view.serialized)["content_manifest"]) == {"schema_version", "session_id", "composition_content_hash", "values"}


def test_authored_source_and_branch_order_survives_display_and_materialization(scope):
    original = {"sources": {"zeta": {"plugin": "csv"}, "alpha": {"plugin": "csv"}}, "branches": {"right": "r", "left": "l"}}
    view = build_bounded_composition_view(scope, original)
    displayed = json.loads(view.serialized)["data"]
    assert list(displayed["sources"]) == ["zeta", "alpha"]
    assert list(displayed["branches"]) == ["right", "left"]
    materialized = MaterializedCompleteValue.from_value(scope, (), original).to_value()
    assert list(materialized["sources"]) == ["zeta", "alpha"]
    assert list(materialized["branches"]) == ["right", "left"]


@pytest.mark.parametrize("limit", [True, 0, -1, 10_001])
def test_inline_policy_refuses_coercion_or_expansion_past_guard_limit(scope, limit):
    with pytest.raises(ValueError, match="inline threshold"):
        build_bounded_composition_view(scope, {}, inline_characters=limit)


def test_unsupported_complete_key_and_oversized_view_do_not_trigger_slicing(scope):
    with pytest.raises(CompositionViewUnsupported, match="object key"):
        build_bounded_composition_view(scope, {"k" * 3_000: "value"})
    large = ["x" * 1_900 for _ in range(JSON_MAX_TOTAL_UTF8_BYTES // 1_900 + 1)]
    with pytest.raises(CompositionViewUnsupported, match="byte envelope"):
        build_bounded_composition_view(scope, large)


def test_display_refuses_under_byte_cap_structural_exhaustion(scope):
    empty = build_bounded_composition_view(scope, {})
    manifest = json.dumps(empty.manifest.to_payload(), separators=(",", ":"))
    raw = '{"schema_version":"composer.bounded-view.v1","data":' + "[" * 2_000 + "null" + "]" * 2_000
    raw += ',"content_manifest":' + manifest + "}"
    assert len(raw.encode()) < JSON_MAX_TOTAL_UTF8_BYTES
    with pytest.raises(CompositionViewUnsupported):
        BoundedCompositionView(raw, empty.manifest)
    with pytest.raises(CompositionViewUnsupported):
        build_bounded_composition_view(scope, [None] * 10_001)
    nested = None
    for _ in range(70):
        nested = [nested]
    with pytest.raises(CompositionViewUnsupported):
        build_bounded_composition_view(scope, nested)


def test_view_constructor_rejects_changed_or_missing_marker_positions(scope):
    view = build_bounded_composition_view(scope, {"prompt": "x" * 3_000})
    payload = json.loads(view.serialized)
    payload["data"]["prompt"]["utf8_bytes"] += 1
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(AuditIntegrityError, match="reference positions"):
        BoundedCompositionView(encoded, view.manifest)
    payload["data"] = {}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(AuditIntegrityError, match="reference position"):
        BoundedCompositionView(encoded, view.manifest)


@pytest.mark.parametrize("reference_id", [None, True, "not-a-reference", "f" * 64])
def test_materialization_refuses_unknown_or_foreign_handles(scope, reference_id):
    original = {"prompt": "x" * 3_000}
    view = build_bounded_composition_view(scope, original)
    with pytest.raises(CompleteValueUnavailable):
        materialize_composition_value(view, reference_id, current_scope=scope, current_projection=original)
