"""Pure owner-result values; no writer, dispatch or current-use authority is claimed."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from elspeth.contracts.composer_interpretation import InterpretationEventRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationBlobVersion,
    ContinuationReadObservations,
    PersistedComposerTurnReceipt,
    decode_interpretation_owner_result,
    decode_proposal_owner_result,
    encode_interpretation_owner_result,
    encode_proposal_owner_result,
)
from elspeth.web.sessions.protocol import CompositionProposalRecord, PipelineProposalPublicMetadata
from tests.unit.contracts.test_composer_interpretation import _opted_out_record_kwargs, _resolved_record_kwargs


@pytest.fixture(params=(False, True), ids=("direct", "pipeline"))
def proposal_record(request):
    now = datetime(2026, 10, 23, tzinfo=UTC)
    metadata = (
        PipelineProposalPublicMetadata(
            draft_hash="a" * 64,
            base={"kind": "absent"},
            repair_count=1,
            skill_hash="b" * 64,
            audit_payload_hash="c" * 64,
            custody_result="ready",
        )
        if request.param
        else None
    )
    return CompositionProposalRecord(
        id=UUID(int=1),
        session_id=UUID(int=2),
        tool_call_id="pure-codec-call",
        user_message_id=UUID(int=3),
        composer_model_identifier="pure-model",
        composer_model_version="pure-version",
        composer_provider="pure-provider",
        composer_skill_hash="d" * 64,
        tool_arguments_hash="e" * 64,
        tool_name="set_pipeline",
        status="pending",
        summary="Preserve café and the exact record",
        rationale="Pure owned result fixture",
        affects=("graph",),
        arguments_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
        arguments_redacted_json={"sources": {}, "nodes": [], "edges": [], "outputs": []},
        base_state_id=None,
        committed_state_id=None,
        audit_event_id=None,
        created_at=now,
        updated_at=now,
        pipeline_metadata=metadata,
    )


@pytest.fixture(params=(_resolved_record_kwargs, _opted_out_record_kwargs), ids=("resolved", "opt_out"))
def interpretation_record(request):
    return InterpretationEventRecord(**request.param())


@pytest.fixture
def committed_receipt():
    return PersistedComposerTurnReceipt(
        effect_key="response:1:p4",
        input_sha256="a" * 64,
        assistant_id=UUID(int=10),
        current_state_id=UUID(int=12),
        checkpoint_disposition="committed",
        creator_operation_id="pure-creator",
        inserted_state_ids=(UUID(int=11), UUID(int=12)),
        authored_state_hash="b" * 64,
        chat_message_ids=(UUID(int=10), UUID(int=13)),
    )


@pytest.fixture
def blob_row():
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


def test_proposal_codec_preserves_complete_nominal_record(proposal_record):
    encoded = encode_proposal_owner_result(proposal_record)
    decoded = decode_proposal_owner_result(encoded)
    assert type(decoded) is CompositionProposalRecord
    assert decoded == proposal_record
    assert decoded.pipeline_metadata == proposal_record.pipeline_metadata
    assert encode_proposal_owner_result(decoded) == encoded
    assert decode_proposal_owner_result(json.dumps(json.loads(encoded), ensure_ascii=False, indent=2)) == decoded
    with pytest.raises(TypeError, match="actual nominal writer result"):
        encode_proposal_owner_result(json.loads(encoded))
    assert encode_proposal_owner_result(proposal_record) == encoded


@pytest.mark.parametrize(
    "change, reason",
    (
        ("missing", "violates its owned codec"),
        ("scalar", "violates its owned codec"),
        ("extra", "lost fields during decode"),
        ("duplicate", "not strict JSON"),
    ),
)
def test_proposal_codec_refuses_malformed_retained_record_after_positive(proposal_record, change, reason):
    encoded = encode_proposal_owner_result(proposal_record)
    assert decode_proposal_owner_result(encoded) == proposal_record
    value = json.loads(encoded)
    if change == "missing":
        del value["tool_name"]
    elif change == "scalar":
        value["tool_call_id"] = 17
    elif change == "extra":
        value["unowned_field"] = True
    else:
        value = None
    malformed = '{"id":' + canonical_json(str(proposal_record.id)) + "," + encoded[1:] if change == "duplicate" else canonical_json(value)
    with pytest.raises(AuditIntegrityError, match=reason):
        decode_proposal_owner_result(malformed)
    assert decode_proposal_owner_result(encoded) == proposal_record


def test_proposal_retention_preserves_all_fields_and_distinct_frozen_json(proposal_record):
    arguments = {"nested": {"private": ["original", 7, None, True]}}
    redacted = {"nested": {"public": ["redacted", 3.5, False]}}
    base = {"kind": "present", "nested": {"versions": ["base-version", 2]}}
    metadata = PipelineProposalPublicMetadata(
        draft_hash="a" * 64,
        base=base,
        repair_count=2,
        skill_hash="b" * 64,
        audit_payload_hash="c" * 64,
        custody_result="ready",
    )
    record = replace(proposal_record, arguments_json=arguments, arguments_redacted_json=redacted, pipeline_metadata=metadata)
    encoded = encode_proposal_owner_result(record)
    wire = json.loads(encoded)
    assert set(wire) == {field.name for field in fields(CompositionProposalRecord)}
    assert set(wire["pipeline_metadata"]) == {field.name for field in fields(PipelineProposalPublicMetadata)}
    assert wire["arguments_json"] == arguments
    assert wire["arguments_redacted_json"] == redacted
    assert wire["pipeline_metadata"]["base"] == base
    arguments["nested"]["private"].append("changed")
    redacted["nested"]["public"].append("changed")
    base["nested"]["versions"].append("changed")
    restored = decode_proposal_owner_result(encoded)
    assert restored == record
    assert encode_proposal_owner_result(record) == encoded
    assert encode_proposal_owner_result(restored) == encoded
    with pytest.raises(TypeError):
        restored.arguments_json["nested"]["private"][0] = "changed"
    with pytest.raises(TypeError):
        restored.arguments_redacted_json["nested"]["public"][0] = "changed"
    with pytest.raises(TypeError):
        restored.pipeline_metadata.base["nested"]["versions"][0] = "changed"


@pytest.mark.parametrize("field_name", ("arguments_json", "arguments_redacted_json"))
@pytest.mark.parametrize("value", ([], "wrong-root"))
def test_proposal_retention_refuses_argument_roots_after_positive(proposal_record, field_name, value):
    encoded = encode_proposal_owner_result(proposal_record)
    assert decode_proposal_owner_result(encoded) == proposal_record
    with pytest.raises(AuditIntegrityError, match="argument objects"):
        encode_proposal_owner_result(replace(proposal_record, **{field_name: value}))


@pytest.mark.parametrize("field_name", ("arguments_json", "arguments_redacted_json", "base"))
@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf"), {1: "not-a-string-key"}, object()))
def test_proposal_retention_refuses_non_json_values_after_positive(proposal_record, field_name, value):
    encoded = encode_proposal_owner_result(proposal_record)
    assert decode_proposal_owner_result(encoded) == proposal_record
    if field_name == "base":
        metadata = PipelineProposalPublicMetadata(
            draft_hash="a" * 64,
            base={"nested": [value]},
            repair_count=1,
            skill_hash="b" * 64,
            audit_payload_hash="c" * 64,
            custody_result="ready",
        )
        changed = replace(proposal_record, pipeline_metadata=metadata)
    else:
        changed = replace(proposal_record, **{field_name: {"nested": [value]}})
    with pytest.raises(AuditIntegrityError, match="continuation"):
        encode_proposal_owner_result(changed)


@pytest.mark.parametrize("value", ({"base": {}}, []))
def test_proposal_retention_refuses_unowned_metadata_after_positive(proposal_record, value):
    assert decode_proposal_owner_result(encode_proposal_owner_result(proposal_record)) == proposal_record
    with pytest.raises(AuditIntegrityError, match="actual pipeline metadata"):
        encode_proposal_owner_result(replace(proposal_record, pipeline_metadata=value))


@pytest.mark.parametrize("base", ([], "wrong-root"))
def test_proposal_retention_refuses_metadata_base_root_after_positive(proposal_record, base):
    assert decode_proposal_owner_result(encode_proposal_owner_result(proposal_record)) == proposal_record
    metadata = PipelineProposalPublicMetadata(
        draft_hash="a" * 64,
        base=base,
        repair_count=1,
        skill_hash="b" * 64,
        audit_payload_hash="c" * 64,
        custody_result="ready",
    )
    with pytest.raises(AuditIntegrityError, match="base object"):
        encode_proposal_owner_result(replace(proposal_record, pipeline_metadata=metadata))


def test_proposal_retention_keeps_strict_ordinary_field_serialization(proposal_record):
    assert decode_proposal_owner_result(encode_proposal_owner_result(proposal_record)) == proposal_record
    with pytest.raises(PydanticSerializationError, match="tool_call_id"):
        encode_proposal_owner_result(replace(proposal_record, tool_call_id=17))
    metadata = PipelineProposalPublicMetadata(
        draft_hash="a" * 64,
        base={},
        repair_count="wrong-scalar",
        skill_hash="b" * 64,
        audit_payload_hash="c" * 64,
        custody_result="ready",
    )
    with pytest.raises(PydanticSerializationError, match="repair_count"):
        encode_proposal_owner_result(replace(proposal_record, pipeline_metadata=metadata))


def test_interpretation_codec_preserves_actual_owned_contract(interpretation_record):
    encoded = encode_interpretation_owner_result(interpretation_record)
    decoded = decode_interpretation_owner_result(encoded)
    assert type(decoded) is InterpretationEventRecord
    assert decoded == interpretation_record
    assert encode_interpretation_owner_result(decoded) == encoded
    with pytest.raises(TypeError, match="actual nominal writer result"):
        encode_interpretation_owner_result(json.loads(encoded))
    assert decode_interpretation_owner_result(encoded) == interpretation_record


@pytest.mark.parametrize(
    "change, reason",
    (
        ("missing", "violates its owned codec"),
        ("scalar", "violates its owned codec"),
        ("shape", "violates its owned codec"),
        ("extra", "lost fields during decode"),
        ("duplicate", "not strict JSON"),
    ),
)
def test_interpretation_codec_refuses_malformed_retained_record_after_positive(interpretation_record, change, reason):
    encoded = encode_interpretation_owner_result(interpretation_record)
    assert decode_interpretation_owner_result(encoded) == interpretation_record
    value = json.loads(encoded)
    if change == "missing":
        del value["session_id"]
    elif change == "scalar":
        value["id"] = 17
    elif change == "shape":
        value["choice"] = "pending"
    elif change == "extra":
        value["unowned_field"] = True
    malformed = (
        '{"id":' + canonical_json(str(interpretation_record.id)) + "," + encoded[1:] if change == "duplicate" else canonical_json(value)
    )
    with pytest.raises(AuditIntegrityError, match=reason):
        decode_interpretation_owner_result(malformed)
    assert decode_interpretation_owner_result(encoded) == interpretation_record


def test_p4_receipt_retains_historical_ids_without_live_witness(committed_receipt):
    encoded = canonical_json(committed_receipt.model_dump(mode="json"))
    restored = PersistedComposerTurnReceipt.model_validate_json(encoded, strict=True)
    assert restored == committed_receipt
    assert restored.current_state_id == restored.inserted_state_ids[-1]
    assert restored.assistant_id in restored.chat_message_ids
    assert "checkpoint" not in json.loads(encoded)
    assert restored.creator_operation_id == "pure-creator"


@pytest.mark.parametrize(
    "replacement, reason",
    (
        ({"inserted_state_ids": ()}, "actual inserted checkpoint bundle"),
        ({"current_state_id": UUID(int=99)}, "actual inserted checkpoint bundle"),
        ({"authored_state_hash": None}, "actual inserted checkpoint bundle"),
        ({"assistant_id": UUID(int=99)}, "actual distinct transcript rows"),
        ({"chat_message_ids": (UUID(int=10), UUID(int=10))}, "actual distinct transcript rows"),
    ),
)
def test_p4_receipt_refuses_changed_checkpoint_or_transcript_bundle(committed_receipt, replacement, reason):
    original = committed_receipt.model_dump(mode="python")
    assert PersistedComposerTurnReceipt.model_validate(original) == committed_receipt
    with pytest.raises(ValidationError, match=reason):
        PersistedComposerTurnReceipt.model_validate(original | replacement)
    assert PersistedComposerTurnReceipt.model_validate(original) == committed_receipt


@pytest.mark.parametrize("disposition, current_id", (("empty", None), ("no_new_checkpoint", UUID(int=12))))
def test_p4_inherited_or_empty_receipt_cannot_invent_insertions(committed_receipt, disposition, current_id):
    value = committed_receipt.model_dump(mode="python") | {
        "checkpoint_disposition": disposition,
        "current_state_id": current_id,
        "inserted_state_ids": (),
        "authored_state_hash": None,
    }
    legitimate = PersistedComposerTurnReceipt.model_validate(value)
    assert legitimate.checkpoint_disposition == disposition and legitimate.current_state_id == current_id
    assert PersistedComposerTurnReceipt.model_validate_json(canonical_json(legitimate.model_dump(mode="json"))) == legitimate
    with pytest.raises(ValidationError, match="invented a newly inserted checkpoint"):
        PersistedComposerTurnReceipt.model_validate(value | {"inserted_state_ids": (UUID(int=11),)})
    with pytest.raises(ValidationError, match="invented a newly inserted checkpoint"):
        PersistedComposerTurnReceipt.model_validate(value | {"authored_state_hash": "b" * 64})


@pytest.mark.parametrize(
    "replacement",
    ({"assistant_id": "not-a-uuid"}, {"inserted_state_ids": [UUID(int=11), UUID(int=12)]}, {"unowned_field": True}),
)
def test_p4_receipt_preserves_strict_scalar_container_and_closed_fields(committed_receipt, replacement):
    original = committed_receipt.model_dump(mode="python")
    assert PersistedComposerTurnReceipt.model_validate(original) == committed_receipt
    with pytest.raises(ValidationError):
        PersistedComposerTurnReceipt.model_validate(original | replacement)


def test_blob_observations_keep_exact_idempotent_versions_in_stable_order(blob_row):
    observations = ContinuationReadObservations()
    observations.observe_blob(blob_row | {"id": str(UUID(int=21))})
    observations.observe_blob(blob_row)
    observations.observe_blob(dict(blob_row))
    assert tuple(version.blob_id for version in observations.versions) == (UUID(int=20), UUID(int=21))
    first = observations.versions[0]
    assert type(first) is ContinuationBlobVersion
    assert first.content_hash == blob_row["content_hash"] and first.filename == blob_row["filename"]
    assert ContinuationBlobVersion.model_validate_json(canonical_json(first.model_dump(mode="json")), strict=True) == first
    original = observations.versions
    blob_row["filename"] = "changed-after-observation.csv"
    assert observations.versions == original


@pytest.mark.parametrize(
    "replacement",
    (
        {"content_hash": "b" * 64},
        {"size_bytes": 8},
        {"filename": "changed.csv"},
        {"mime_type": "text/plain"},
        {"status": "pending"},
        {"created_by": "assistant"},
        {"creation_modality": "llm_generated"},
    ),
)
def test_one_read_cannot_replace_an_observed_immutable_blob_version(blob_row, replacement):
    observations = ContinuationReadObservations()
    observations.observe_blob(blob_row)
    original = observations.versions
    with pytest.raises(AuditIntegrityError, match="two versions of the same blob"):
        observations.observe_blob(blob_row | replacement)
    assert observations.versions == original
    observations.observe_blob(blob_row)
    assert observations.versions == original


@pytest.mark.parametrize("replacement", ({"size_bytes": -1}, {"size_bytes": True}, {"status": "not-recorded"}))
def test_blob_version_refuses_invalid_owned_scalar_and_vocabulary_after_positive(blob_row, replacement):
    observations = ContinuationReadObservations()
    observations.observe_blob(blob_row)
    original = observations.versions
    with pytest.raises(ValidationError):
        observations.observe_blob(blob_row | replacement)
    assert observations.versions == original


def test_blob_observation_requires_recorded_creation_modality_after_positive(blob_row):
    observations = ContinuationReadObservations()
    observations.observe_blob(blob_row)
    original = observations.versions
    with pytest.raises(AuditIntegrityError, match="invalid recorded blob vocabulary"):
        observations.observe_blob(blob_row | {"creation_modality": "unrecorded-modality"})
    assert observations.versions == original
