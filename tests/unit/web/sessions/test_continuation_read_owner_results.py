"""Read-result value admission and real row equality, without runtime activation."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

import pytest
from sqlalchemy import delete, insert, select, update

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json, encode_tool_result_value
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationSummary
from elspeth.web.composer.tools import _registry
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadObservations,
    build_read_owner_result,
    decode_read_owner_result,
    re_admit_read_owner_result,
    require_read_versions_on_connection,
)
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def read_result(*, success=True):
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Read café"), version=3)
    return ToolResult(
        success=success,
        updated_state=state,
        validation=ValidationSummary(True, ()),
        affected_nodes=(),
        data={"available": [PluginSummary(name="csv", description="CSV café", plugin_type="source", config_fields=[])], "prohibited": []}
        if success
        else None,
        post_call_hints=("first exact hint", "second exact hint"),
        _validation_snapshot_hash="a" * 64,
        _state_validation_withheld=True,
    )


def retained_read(*, success=True, observations=None):
    result = read_result(success=success)
    return build_read_owner_result(
        tool_name="list_sources",
        arguments={},
        result=result,
        pre_state=result.updated_state,
        observations=ContinuationReadObservations() if observations is None else observations,
    )


def observed_blob():
    return {
        "id": str(UUID(int=20)),
        "content_hash": "a" * 64,
        "size_bytes": 7,
        "filename": "café.csv",
        "mime_type": "text/csv",
        "status": "ready",
        "created_by": "user",
        "creation_modality": "verbatim",
    }


@pytest.mark.parametrize("success", (True, False))
def test_complete_read_value_re_admits_actual_nominal_result(success):
    actual = read_result(success=success)
    observations = ContinuationReadObservations()
    observations.observe_blob(observed_blob())
    arguments = {}
    value = build_read_owner_result(
        tool_name="list_sources",
        arguments=arguments,
        result=actual,
        pre_state=actual.updated_state,
        observations=observations,
    )
    encoded = canonical_json(value.model_dump(mode="json"))
    restored = decode_read_owner_result(encoded)
    assert restored == value
    assert re_admit_read_owner_result(restored) == admit_discovery_result("list_sources", actual).to_dict()
    assert restored.tool_result_json == canonical_json(encode_tool_result_value("list_sources", actual).model_dump(mode="json"))
    decoded = decode_tool_result_json(restored.tool_result_json)
    assert decoded.updated_state == actual.updated_state and decoded.updated_state is not actual.updated_state
    assert decoded.post_call_hints == actual.post_call_hints
    assert decoded._validation_snapshot_hash == actual._validation_snapshot_hash
    assert decoded._state_validation_withheld is True
    assert restored.blob_versions == observations.versions
    arguments["changed_after_capture"] = True
    assert json.loads(restored.arguments_json) == {}
    assert decode_read_owner_result(json.dumps(json.loads(encoded), ensure_ascii=False, indent=2)) == restored


@pytest.mark.parametrize("change", ("state_identity", "affected_nodes", "tool", "observations", "nominal_result"))
def test_build_refuses_mutation_or_unowned_result_after_positive(change):
    actual = read_result()
    kwargs = {
        "tool_name": "list_sources",
        "arguments": {},
        "result": actual,
        "pre_state": actual.updated_state,
        "observations": ContinuationReadObservations(),
    }
    assert build_read_owner_result(**kwargs).tool_name == "list_sources"
    if change == "state_identity":
        kwargs["pre_state"] = replace(actual.updated_state)
        assert kwargs["pre_state"] == actual.updated_state and kwargs["pre_state"] is not actual.updated_state
    elif change == "affected_nodes":
        kwargs["result"] = replace(actual, affected_nodes=("source",))
    elif change == "tool":
        kwargs["tool_name"] = "preview_pipeline"
    elif change == "observations":
        kwargs["observations"] = ()
    else:
        kwargs["result"] = actual.to_dict()
    with pytest.raises(AuditIntegrityError, match="actual nonmutating handler result"):
        build_read_owner_result(**kwargs)


@pytest.mark.parametrize("change", ("duplicate", "missing", "extra", "scalar"))
def test_read_outer_codec_refuses_malformed_owned_value(change):
    value = retained_read()
    encoded = canonical_json(value.model_dump(mode="json"))
    assert decode_read_owner_result(encoded) == value
    payload = json.loads(encoded)
    if change == "duplicate":
        corrupted = '{"kind":"read",' + encoded[1:]
    else:
        if change == "missing":
            del payload["tool_result_json"]
        elif change == "extra":
            payload["unowned"] = True
        else:
            payload["tool_name"] = 17
        corrupted = canonical_json(payload)
    with pytest.raises(AuditIntegrityError):
        decode_read_owner_result(corrupted)
    assert decode_read_owner_result(encoded) == value


@pytest.mark.parametrize("change", ("foreign_tool", "affected_nodes", "envelope", "duplicate_observation", "unknown_owner"))
def test_read_re_admission_binds_complete_nominal_result_and_observations(change):
    observations = ContinuationReadObservations()
    observations.observe_blob(observed_blob())
    # Failed discovery results have the same public shape across different selected contracts.
    value = retained_read(success=False, observations=observations)
    assert re_admit_read_owner_result(value)["success"] is False
    if change in ("foreign_tool", "affected_nodes"):
        nominal = json.loads(value.tool_result_json)
        if change == "foreign_tool":
            nominal["tool_name"] = "list_transforms"
        else:
            nominal["affected_nodes"] = ["source"]
        encoded = canonical_json(nominal)
        # The real B1 codec accepts this valid value; the read boundary must refuse its different binding/effect.
        decoded = decode_tool_result_json(encoded)
        assert decoded.success is False
        value = value.model_copy(update={"tool_result_json": encoded})
    elif change == "envelope":
        envelope = json.loads(value.response_json)
        envelope["version"] += 1
        value = value.model_copy(update={"response_json": canonical_json(envelope)})
    elif change == "duplicate_observation":
        value = value.model_copy(update={"blob_versions": value.blob_versions * 2})
    else:
        value = value.model_copy(update={"tool_name": "no_registered_read"})
    with pytest.raises(AuditIntegrityError):
        re_admit_read_owner_result(value)


@pytest.mark.parametrize("change", ("missing_data", "failure_data", "invalid_data", "invalid_success", "arguments"))
def test_read_response_uses_current_selected_contract_and_complete_envelope(change):
    value = retained_read()
    envelope = re_admit_read_owner_result(value)
    if change == "missing_data":
        del envelope["data"]
    elif change == "failure_data":
        envelope["success"] = False
    elif change == "invalid_data":
        envelope["data"]["unowned"] = True
    elif change == "invalid_success":
        envelope["success"] = 1
    else:
        value = value.model_copy(update={"arguments_json": "[]"})
    changed = value.model_copy(update={"response_json": canonical_json(envelope)})
    with pytest.raises((AuditIntegrityError, FrameworkBugError)):
        re_admit_read_owner_result(changed)


def test_read_refuses_removed_current_response_contract_after_positive(monkeypatch):
    value = retained_read()
    assert re_admit_read_owner_result(value)["success"] is True
    changed = dict(_registry._DECLARATIONS_BY_NAME)
    changed["list_sources"] = replace(
        changed["list_sources"], json_schema=deep_thaw(changed["list_sources"].json_schema), response_contract=None
    )
    with monkeypatch.context() as patch:
        patch.setattr(_registry, "_DECLARATIONS_BY_NAME", MappingProxyType(changed))
        with pytest.raises(AuditIntegrityError, match="supported actual read owner"):
            re_admit_read_owner_result(value)
    assert re_admit_read_owner_result(value)["success"] is True


@pytest.mark.parametrize(
    "change,replacement",
    (
        ("content_hash", "b" * 64),
        ("size_bytes", 8),
        ("filename", "other.csv"),
        ("mime_type", "text/plain"),
        ("status", "pending"),
        ("created_by", "assistant"),
        ("creation_modality", "llm_generated"),
        ("missing", None),
        ("foreign_session", None),
    ),
    ids=("content-hash", "size", "filename", "mime", "status", "creator", "modality", "missing", "foreign-session"),
)
def test_current_blob_version_check_reads_real_rows_and_preserves_transaction(operation_store, change, replacement):
    engine, _repository, _authority, _service, session_id = operation_store
    blob = observed_blob()
    observations = ContinuationReadObservations()
    observations.observe_blob(blob)
    value = retained_read(observations=observations)
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                **blob,
                session_id=str(session_id),
                storage_path="unused-private-read-fixture",
                created_at=datetime(2026, 10, 23, tzinfo=UTC),
            )
        )
    with engine.connect() as conn:
        original = tuple(conn.execute(select(blobs_table)).all())
        require_read_versions_on_connection(conn, session_id=str(session_id), result=value)
        assert tuple(conn.execute(select(blobs_table)).all()) == original
        original_value = value
        savepoint = conn.begin_nested()
        try:
            selected_session = str(session_id)
            if change == "missing":
                conn.execute(delete(blobs_table).where(blobs_table.c.id == blob["id"]))
            elif change == "foreign_session":
                selected_session = str(UUID(int=999))
            elif change == "creation_modality":
                # Change only the retained input: a valid non-verbatim SQL row
                # additionally needs real LLM provenance, which this test does not invent.
                value = value.model_copy(
                    update={"blob_versions": (value.blob_versions[0].model_copy(update={"creation_modality": replacement}),)}
                )
            else:
                conn.execute(update(blobs_table).where(blobs_table.c.id == blob["id"]).values(**{change: replacement}))
            changed = tuple(conn.execute(select(blobs_table)).all())
            with pytest.raises(AuditIntegrityError, match="retained blob observation"):
                require_read_versions_on_connection(conn, session_id=selected_session, result=value)
            assert tuple(conn.execute(select(blobs_table)).all()) == changed
        finally:
            savepoint.rollback()
        value = original_value
        require_read_versions_on_connection(conn, session_id=str(session_id), result=value)
        assert tuple(conn.execute(select(blobs_table)).all()) == original


@pytest.mark.parametrize("modality", tuple(CreationModality))
def test_read_re_admits_actual_recorded_creation_modality_vocabulary(modality):
    observations = ContinuationReadObservations()
    blob = observed_blob()
    blob["creation_modality"] = modality.value
    observations.observe_blob(blob)
    value = retained_read(observations=observations)
    assert re_admit_read_owner_result(value)["success"] is True
    assert decode_read_owner_result(canonical_json(value.model_dump(mode="json"))) == value
    assert value.blob_versions[0].creation_modality == modality.value


@pytest.mark.parametrize("field,unknown", (("creation_modality", "not-a-modality"), ("status", "not-a-status")))
@pytest.mark.parametrize("boundary", ("re_admit", "decode"))
def test_read_refuses_unknown_recorded_blob_vocabulary_after_positive(field, unknown, boundary):
    observations = ContinuationReadObservations()
    observations.observe_blob(observed_blob())
    value = retained_read(observations=observations)
    encoded = canonical_json(value.model_dump(mode="json"))
    assert re_admit_read_owner_result(value)["success"] is True
    assert decode_read_owner_result(encoded) == value
    corrupted = value.model_copy(update={"blob_versions": (value.blob_versions[0].model_copy(update={field: unknown}),)})
    if field == "creation_modality":
        assert corrupted.blob_versions[0].creation_modality == unknown
    else:
        assert corrupted.blob_versions[0].status == unknown
    if boundary == "re_admit":
        with pytest.raises(AuditIntegrityError, match="invalid recorded blob vocabulary"):
            re_admit_read_owner_result(corrupted)
    else:
        corrupted_json = canonical_json(corrupted.model_dump(mode="json"))
        assert json.loads(corrupted_json)["blob_versions"][0][field] == unknown
        message = "owned codec" if field == "status" else "invalid recorded blob vocabulary"
        with pytest.raises(AuditIntegrityError, match=message):
            decode_read_owner_result(corrupted_json)
    assert re_admit_read_owner_result(value)["success"] is True
    assert decode_read_owner_result(encoded) == value
