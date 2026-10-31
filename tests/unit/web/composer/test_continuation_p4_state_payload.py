"""Actual P4 payload retention; no partial F7 or database owner."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_p4_state_payload import decode_p4_state_payload, encode_p4_state_payload
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.protocol import CompositionStateData, CompositionValidationError
from tests.helpers.composition_state import composition_state_data


def actual_payload():
    return StatePayload(
        CompositionStateData(
            sources={},
            nodes=(
                {
                    "id": "rename",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rows",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
                },
            ),
            edges=({"id": "edge", "from_node": "source", "to_node": "rename", "edge_type": "on_success", "label": None},),
            outputs=({"name": "output", "plugin": "database", "options": {"url": "${DB_URL}"}, "on_write_failure": "discard"},),
            metadata_={"name": "exact name", "description": "café"},
            is_valid=False,
            validation_errors=(CompositionValidationError(message="exact message", error_code="schema_contract", component="rename"),),
            composer_meta={"validation_lane": "authoring", "nested": {"ordered": ["b", "a"]}},
        ),
        derived_from_state_id="actual-parent-state",
    )


def test_complete_actual_payload_preserves_frozen_values_and_sql_lineage():
    original = actual_payload()
    inventory = frozenset({"DB_URL"})
    encoded = encode_p4_state_payload(original, env_ref_names=inventory)
    restored = decode_p4_state_payload(encoded, env_ref_names=inventory)
    assert type(restored) is StatePayload
    assert type(restored.data) is CompositionStateData
    assert restored == original
    assert encode_p4_state_payload(restored, env_ref_names=inventory) == encoded
    assert restored.derived_from_state_id == "actual-parent-state"
    assert restored.data.nodes[0]["options"]["mapping"]["customer_key"] == "customer_id"
    assert restored.data.composer_meta["nested"]["ordered"] == ("b", "a")
    assert restored.data.validation_errors == (
        CompositionValidationError(message="exact message", error_code="schema_contract", component="rename"),
    )
    assert "version" not in json.loads(encoded)
    assert "version" not in json.loads(encoded)["data"]
    with pytest.raises(TypeError):
        restored.data.nodes[0]["options"]["mapping"]["customer_key"] = "changed"
    with pytest.raises(TypeError):
        restored.data.composer_meta["nested"]["ordered"] = ()


def test_nullable_sql_columns_remain_distinct_from_empty_containers():
    absent = StatePayload(composition_state_data())
    empty = StatePayload(
        CompositionStateData(sources={}, nodes=(), edges=(), outputs=(), metadata_={}, validation_errors=(), composer_meta={})
    )
    assert decode_p4_state_payload(encode_p4_state_payload(absent)) == absent
    # Empty metadata is retained by the SQL input DTO but is not a complete
    # CompositionState metadata object. This codec must preserve that valid SQL
    # value while using the current credential owner only for field inspection.
    assert decode_p4_state_payload(encode_p4_state_payload(empty)) == empty
    assert encode_p4_state_payload(absent) != encode_p4_state_payload(empty)


def test_legacy_source_constructor_alias_is_retained_as_actual_owned_sources():
    payload = StatePayload(
        composition_state_data(
            source={"plugin": "csv", "options": {"path": "rows.csv"}, "on_success": "rows", "on_validation_failure": "discard"}
        )
    )
    encoded = encode_p4_state_payload(payload)
    assert decode_p4_state_payload(encoded) == payload
    assert "source" not in json.loads(encoded)["data"]
    assert json.loads(encoded)["data"]["sources"] == {"source": dict(payload.data.sources["source"])}


@pytest.mark.parametrize("member", ["sources", "nodes", "edges", "outputs", "validation_errors", "composer_meta", "is_valid"])
def test_missing_defaulted_owned_field_cannot_silently_normalize(member):
    encoded = encode_p4_state_payload(actual_payload(), env_ref_names=frozenset({"DB_URL"}))
    value = json.loads(encoded)
    del value["data"][member]
    with pytest.raises(AuditIntegrityError, match="lost or normalized"):
        decode_p4_state_payload(canonical_json(value), env_ref_names=frozenset({"DB_URL"}))


def test_missing_required_metadata_is_refused_by_the_owned_contract():
    # Pipeline metadata is required on every stored state, so a payload that
    # lost it fails the owned model itself rather than normalizing to a default.
    encoded = encode_p4_state_payload(actual_payload(), env_ref_names=frozenset({"DB_URL"}))
    value = json.loads(encoded)
    del value["data"]["metadata_"]
    with pytest.raises(AuditIntegrityError, match="violates its owned contract"):
        decode_p4_state_payload(canonical_json(value), env_ref_names=frozenset({"DB_URL"}))


def test_unknown_fields_duplicate_keys_noncanonical_bytes_and_scalar_coercion_refuse():
    encoded = encode_p4_state_payload(actual_payload(), env_ref_names=frozenset({"DB_URL"}))
    unknown = json.loads(encoded)
    unknown["data"]["unknown"] = True
    coerced = json.loads(encoded)
    coerced["data"]["is_valid"] = 1
    for candidate in (canonical_json(unknown), canonical_json(coerced), encoded + " ", '{"data":{},' + encoded[1:]):
        with pytest.raises(AuditIntegrityError):
            decode_p4_state_payload(candidate, env_ref_names=frozenset({"DB_URL"}))


def test_exact_inventory_reference_is_admitted_and_raw_database_credentials_refuse():
    payload = actual_payload()
    encoded = encode_p4_state_payload(payload, env_ref_names=frozenset({"DB_URL"}))
    with pytest.raises(CredentialMaterialRefused):
        decode_p4_state_payload(encoded)
    with pytest.raises(CredentialMaterialRefused):
        decode_p4_state_payload(encoded, env_ref_names=frozenset({"OTHER_URL"}))
    unsafe = replace(
        payload,
        data=replace(
            payload.data,
            outputs=(
                {
                    "name": "output",
                    "plugin": "database",
                    "options": {"url": "postgresql://user:password@example.test/db"},  # secret-scan: allow-this-line
                    "on_write_failure": "discard",
                },
            ),
        ),
    )
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(unsafe, env_ref_names=frozenset({"DB_URL"}))


@pytest.mark.parametrize("where", ["composer_meta", "metadata_", "node_options"])
def test_field_label_projection_does_not_waive_other_credential_slots(where):
    payload = actual_payload()
    if where == "composer_meta":
        data = replace(payload.data, composer_meta={"password": "synthetic-material"})
    elif where == "metadata_":
        data = replace(payload.data, metadata_={"name": "retained", "description": "", "password": "synthetic-material"})
    else:
        node = dict(payload.data.nodes[0])
        node["options"] = dict(node["options"]) | {"password": "synthetic-material"}
        data = replace(payload.data, nodes=(node,))
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(replace(payload, data=data), env_ref_names=frozenset({"DB_URL"}))


def test_actual_database_declared_slot_rejects_opaque_nonreference_material():
    payload = actual_payload()
    output = dict(payload.data.outputs[0])
    output["options"] = {"url": "synthetic-opaque-material"}
    unsafe = replace(payload, data=replace(payload.data, outputs=(output,)))
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(unsafe)
