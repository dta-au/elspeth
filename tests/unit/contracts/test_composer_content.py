"""Complete-value identity is immutable and never detector permission."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import (
    CompleteValueManifest,
    CompleteValueReference,
    ComposerContentScope,
    MaterializedCompleteValue,
    load_content_manifest,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_freeze


@pytest.fixture
def scope():
    return ComposerContentScope(UUID("00000000-0000-0000-0000-000000000001"), "a" * 64)


def test_complete_oversized_scalar_is_lossless_and_detached(scope):
    value = {"prompt": "雪🙂" * 15_000, "count": 1.0}
    materialized = MaterializedCompleteValue.from_value(scope, ("nodes", 0, "options"), value)
    original = materialized.json_text
    value["prompt"] = "changed"
    assert materialized.json_text == original
    assert "雪🙂" * 15_000 in original
    assert '"count":1.0' in original
    assert materialized.reference.utf8_bytes == len(original.encode("utf-8")) > 10_000
    assert "雪" not in repr(materialized)
    with pytest.raises(FrozenInstanceError):
        materialized.json_text = "changed"


def test_identity_binds_scope_path_value_type_and_content(scope):
    def identity(*, context=scope, path=("options", "prompt"), value=1):
        return MaterializedCompleteValue.from_value(context, path, value).reference.reference_id

    baseline = identity()
    assert identity() == baseline
    assert identity(context=replace(scope, session_id=UUID(int=2))) != baseline
    assert identity(context=replace(scope, composition_content_hash="b" * 64)) != baseline
    assert identity(path=("other",)) != baseline
    assert identity(value=True) != baseline
    assert identity(value=1.0) != baseline
    assert identity(value=2) != baseline


def test_manifest_roundtrips_owned_frozen_metadata_and_preserves_unrelated_fields(scope):
    value = MaterializedCompleteValue.from_value(scope, ("nodes", 0, "options", "prompt"), "complete")
    manifest = CompleteValueManifest(scope, (value.reference,))
    payload = manifest.to_payload()
    assert CompleteValueManifest.from_payload(payload) == manifest
    assert load_content_manifest(deep_freeze({"other": "unchanged", "content_manifest": payload})) == manifest
    assert set(payload) == {"schema_version", "session_id", "composition_content_hash", "values"}
    assert "complete" not in str(payload)
    manifest.require_scope(scope)
    for changed in (replace(scope, session_id=UUID(int=2)), replace(scope, composition_content_hash="b" * 64)):
        with pytest.raises(AuditIntegrityError, match="current composition"):
            manifest.require_scope(changed)


@pytest.mark.parametrize("legacy", [None, {}, {"completion_gates": {}}])
def test_historical_absence_is_not_fabricated_evidence(legacy):
    assert load_content_manifest(legacy) is None


@pytest.mark.parametrize("present", [None, False, {}, {"schema_version": 999}])
def test_malformed_present_manifest_refuses(present):
    with pytest.raises(AuditIntegrityError):
        load_content_manifest({"content_manifest": present})


def test_metadata_loading_refuses_unordered_arrays_without_thawing_them(scope):
    empty = CompleteValueManifest(scope, ()).to_payload()
    with pytest.raises(AuditIntegrityError):
        load_content_manifest({"content_manifest": {**empty, "values": frozenset()}})
    reference = MaterializedCompleteValue.from_value(scope, (), "complete").reference
    payload = CompleteValueManifest(scope, (reference,)).to_payload()
    payload["values"][0]["path"] = frozenset()
    with pytest.raises(AuditIntegrityError):
        load_content_manifest({"content_manifest": payload})


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2), ("allow", True), ("clean", True)])
def test_manifest_rejects_unknown_versions_and_permission_claims(scope, field, value):
    payload = CompleteValueManifest(scope, ()).to_payload()
    with pytest.raises(AuditIntegrityError):
        CompleteValueManifest.from_payload({**payload, field: value})


def test_reference_payload_tampering_cannot_transplant_identity(scope):
    reference = MaterializedCompleteValue.from_value(scope, ("options", "prompt"), "complete").reference
    assert CompleteValueReference.from_payload(scope, reference.to_payload()) == reference
    for changes in ({"path": ["other"]}, {"content_hash": "b" * 64}, {"utf8_bytes": 1}, {"reference_id": "0" * 64}):
        with pytest.raises(AuditIntegrityError):
            CompleteValueReference.from_payload(scope, {**reference.to_payload(), **changes})
    with pytest.raises(AuditIntegrityError):
        CompleteValueReference.from_payload(replace(scope, session_id=UUID(int=2)), reference.to_payload())


@pytest.mark.parametrize("path", [(True,), (-1,), (1.0,), ("\ud800",)])
def test_path_refuses_coercion_and_invalid_unicode(scope, path):
    with pytest.raises(AuditIntegrityError):
        CompleteValueReference(scope, path, "a" * 64, 1)


def test_manifest_refuses_duplicate_overlapping_and_foreign_members(scope):
    parent = MaterializedCompleteValue.from_value(scope, ("options",), {"prompt": "complete"}).reference
    child = MaterializedCompleteValue.from_value(scope, ("options", "prompt"), "complete").reference
    foreign = replace(parent, scope=replace(scope, session_id=UUID(int=2)))
    for values in ((parent, parent), (parent, child), (foreign,)):
        with pytest.raises(AuditIntegrityError):
            CompleteValueManifest(scope, values)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"bad": float("-inf")}, {1: "coerced-key"}, object()])
def test_value_admission_refuses_non_json_without_exposing_content(scope, value):
    with pytest.raises(AuditIntegrityError, match="finite UTF-8 JSON") as caught:
        MaterializedCompleteValue.from_value(scope, (), value)
    assert "coerced-key" not in str(caught.value)


def test_materialization_refuses_changed_bytes_and_noncanonical_duplicate_keys(scope):
    value = MaterializedCompleteValue.from_value(scope, (), {"prompt": "complete"})
    with pytest.raises(AuditIntegrityError, match="reference"):
        MaterializedCompleteValue(value.reference, '"changed"')
    with pytest.raises(AuditIntegrityError, match="canonical"):
        MaterializedCompleteValue(value.reference, '{"prompt":"old","prompt":"complete"}')
    payload = CompleteValueManifest(scope, (value.reference,)).to_payload()
    changed = deepcopy(payload)
    changed["values"][0]["path"].append("injected")
    assert payload["values"][0]["path"] == []
    assert value.reference.path == ()
