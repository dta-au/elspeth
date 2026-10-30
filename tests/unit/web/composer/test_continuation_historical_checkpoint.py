"""Lossless historical raw-column DATA; actual writer/receipt owners remain decisive."""

import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_content import ContentPublication
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_p4_outcome import (
    HistoricalCheckpointData,
    capture_p4_outcome,
    decode_audit_outcome_data,
    decode_p4_outcome,
    encode_audit_outcome_data,
    encode_p4_outcome,
    historical_checkpoint_columns_json,
    historical_checkpoint_record,
    require_same_p4_outcome,
)
from elspeth.web.composer.continuation_runtime import _registered_read
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions.content_publication import CONTENT_PUBLICATION_KEY, read_content_publication, storage_content_hash
from elspeth.web.sessions.continuation_p4 import ReadmittedP4Result, verify_p4_immutable_receipt_on_connection
from elspeth.web.sessions.protocol import CompositionStateRecord
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_p4_product import _contains_identity

operation_store = _operation_store


def _record(**columns):
    """Lawful owned historical DATA for codec controls, never a commit witness."""
    record = CompositionStateRecord(
        id=UUID("10000000-0000-4000-8000-000000000001"),
        session_id=UUID("10000000-0000-4000-8000-000000000002"),
        version=2,
        created_at=datetime(2026, 10, 30, tzinfo=UTC),
        derived_from_state_id=None,
        is_valid=False,
        source=None,
        sources=None,
        nodes=None,
        edges=None,
        outputs=None,
        metadata_={"name": "before", "description": 'Unicode café and "quoted"\ntext'},
        validation_errors=None,
        composer_meta=None,
    )
    record = replace(record, **columns)
    publication = ContentPublication(record.session_id, record.id, record.version, storage_content_hash(record))
    metadata = {} if record.composer_meta is None else dict(record.composer_meta)
    metadata[CONTENT_PUBLICATION_KEY] = publication.to_payload()
    return replace(record, composer_meta=metadata)


def _data(record):
    return HistoricalCheckpointData(
        id=record.id,
        session_id=record.session_id,
        version=record.version,
        original_columns_json=historical_checkpoint_columns_json(record),
        is_valid=record.is_valid,
        created_at=record.created_at,
        derived_from_state_id=record.derived_from_state_id,
        review_projection=None,
        creator_operation_id="10000000-0000-4000-8000-000000000003",
        inserted_state_ids=(record.id,),
    )


def _changed(data, columns):
    return data.model_copy(
        update={"original_columns_json": json.dumps(columns, ensure_ascii=False, separators=(",", ":"), allow_nan=False)}
    )


@pytest.mark.parametrize("shape", ("null", "empty", "ordered_numbers", "legacy_source", "modern_sources", "env_reference"))
def test_historical_checkpoint_preserves_original_raw_column_values(shape):
    columns = {}
    if shape == "empty":
        columns = {"sources": {}, "nodes": [], "edges": [], "outputs": []}
    elif shape == "ordered_numbers":
        columns = {"composer_meta": {"z": {"second": 1.0, "first": 1}, "a": [-0.0, 0, 2**53 - 1, 'é\n"']}}
    elif shape in ("legacy_source", "modern_sources"):
        source = {"plugin": "csv", "on_success": "output", "options": {"path": "safe.csv"}, "on_validation_failure": "discard"}
        columns = {"source": source} if shape == "legacy_source" else {"sources": {"source": source}}
    elif shape == "env_reference":
        columns = {"composer_meta": {"reference": "${DATABASE_URL}"}}
    record = _record(**columns)
    original = _data(record)
    names = frozenset({"DATABASE_URL"}) if shape == "env_reference" else frozenset()
    cold = HistoricalCheckpointData.model_validate_json(original.model_dump_json(), strict=True)
    restored = historical_checkpoint_record(cold, env_ref_names=names)
    assert historical_checkpoint_columns_json(restored) == original.original_columns_json
    assert storage_content_hash(restored) == storage_content_hash(record)
    assert read_content_publication(restored) == read_content_publication(record)
    assert list(restored.metadata_) == ["name", "description"]
    if shape == "ordered_numbers":
        retained = json.loads(cold.original_columns_json)["composer_meta"]
        assert list(retained) == ["z", "a", CONTENT_PUBLICATION_KEY]
        assert list(retained["z"]) == ["second", "first"]
        assert type(retained["z"]["second"]) is float and type(retained["z"]["first"]) is int
        assert '"a":[-0.0,0,' in cold.original_columns_json
    if shape == "null":
        assert restored.source is restored.sources is restored.nodes is restored.edges is restored.outputs is None
    if shape == "empty":
        assert restored.source is None and restored.sources == {} and restored.nodes == restored.edges == restored.outputs == ()


@pytest.mark.parametrize(
    "defect",
    (
        "missing",
        "extra",
        "duplicate",
        "nested_duplicate",
        "wrong_metadata",
        "wrong_nodes",
        "wrong_source",
        "nan",
        "infinity",
        "negative_infinity",
        "overflow",
        "invalid_utf8",
        "whitespace",
        "malformed",
        "dual_sources",
        "old_shape",
    ),
)
def test_historical_checkpoint_rejects_unclosed_or_unsafe_original_columns(defect):
    data = _data(_record())
    assert historical_checkpoint_record(data).metadata_["name"] == "before"
    values = json.loads(data.original_columns_json)
    if defect == "old_shape":
        old = data.model_dump()
        old.pop("original_columns_json")
        old.update(values)
        with pytest.raises(ValidationError):
            HistoricalCheckpointData.model_validate(old, strict=True)
        return
    if defect == "missing":
        del values["source"]
    elif defect == "extra":
        values["unexpected"] = None
    elif defect == "wrong_metadata":
        values["metadata_"] = []
    elif defect == "wrong_nodes":
        values["nodes"] = {}
    elif defect == "wrong_source":
        values["source"] = []
    elif defect == "dual_sources":
        values["source"] = {"plugin": "csv", "options": {}, "on_success": "output", "on_validation_failure": "discard"}
        values["sources"] = {}
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if defect == "duplicate":
        encoded = encoded.replace('"source":null', '"source":null,"source":null', 1)
    elif defect == "nested_duplicate":
        encoded = encoded.replace('"name":"before"', '"name":"before","name":"before"', 1)
    elif defect == "nan":
        encoded = encoded.replace('"validation_errors":null', '"validation_errors":NaN', 1)
    elif defect in ("infinity", "negative_infinity"):
        literal = "Infinity" if defect == "infinity" else "-Infinity"
        encoded = encoded.replace('"validation_errors":null', '"validation_errors":' + literal, 1)
    elif defect == "overflow":
        encoded = encoded.replace('"validation_errors":null', '"validation_errors":1e400', 1)
    elif defect == "invalid_utf8":
        encoded = encoded.replace("before", "\ud800", 1)
    elif defect == "whitespace":
        encoded += " "
    elif defect == "malformed":
        encoded = encoded[:-1]
    with pytest.raises(AuditIntegrityError):
        historical_checkpoint_record(data.model_copy(update={"original_columns_json": encoded}))


@pytest.mark.parametrize("column", ("source", "sources", "nodes", "edges", "outputs", "metadata_"))
def test_original_publication_rejects_each_changed_authored_column(column):
    data = _data(_record())
    positive = historical_checkpoint_record(data)
    assert read_content_publication(positive) is not None
    values = json.loads(data.original_columns_json)
    if column == "source":
        values[column] = {"plugin": "csv", "on_success": "output", "options": {"path": "safe.csv"}, "on_validation_failure": "discard"}
    elif column == "sources":
        values[column] = {}
    elif column == "metadata_":
        values[column]["name"] = "changed"
    else:
        values[column] = []
    with pytest.raises(AuditIntegrityError, match="Content publication does not match"):
        historical_checkpoint_record(_changed(data, values))


@pytest.mark.parametrize("field", ("digest", "session", "state", "version", "utc", "empty_cohort", "duplicate_cohort", "last_cohort"))
def test_historical_checkpoint_keeps_original_publication_and_cohort_guards(field):
    data = _data(_record())
    assert read_content_publication(historical_checkpoint_record(data)) is not None
    values = json.loads(data.original_columns_json)
    if field == "digest":
        values["composer_meta"][CONTENT_PUBLICATION_KEY]["storage_content_hash"] = "0" * 64
        changed = _changed(data, values)
    else:
        updates = {
            "session": {"session_id": UUID("20000000-0000-4000-8000-000000000001")},
            "state": {
                "id": UUID("20000000-0000-4000-8000-000000000001"),
                "inserted_state_ids": (UUID("20000000-0000-4000-8000-000000000001"),),
            },
            "version": {"version": 3},
            "utc": {"created_at": data.created_at.astimezone(timezone(timedelta(hours=1)))},
            "empty_cohort": {"inserted_state_ids": ()},
            "duplicate_cohort": {"inserted_state_ids": (data.id, data.id)},
            "last_cohort": {"inserted_state_ids": (UUID("20000000-0000-4000-8000-000000000001"),)},
        }
        changed = data.model_copy(update=updates[field])
    with pytest.raises(AuditIntegrityError):
        historical_checkpoint_record(changed)


@pytest.mark.parametrize("surface", ("graph", "metadata", "validation", "composer_meta", "late_composer_meta", "creator"))
def test_historical_checkpoint_keeps_credential_admission(surface):
    data = _data(_record())
    assert historical_checkpoint_record(data).version == 2
    secret = "sk-" + "syntheticcredential" * 3
    values = json.loads(data.original_columns_json)
    changed = data
    if surface == "creator":
        changed = data.model_copy(update={"creator_operation_id": secret})
    else:
        if surface == "graph":
            values["sources"] = {
                "source": {
                    "plugin": "csv",
                    "on_success": "output",
                    "on_validation_failure": "discard",
                    "options": {"path": "safe.csv", "api_key": secret},
                }
            }
        elif surface == "metadata":
            values["metadata_"]["description"] = secret
        elif surface == "validation":
            values["validation_errors"] = [{"message": secret, "error_code": "checkpoint_control", "component": "metadata"}]
        else:
            if surface == "late_composer_meta":
                values["composer_meta"]["lawful_prefix"] = "x" * 16384
            values["composer_meta"]["diagnostic"] = secret
        changed = _changed(data, values)
    with pytest.raises(CredentialMaterialRefused):
        historical_checkpoint_record(changed)


@asynccontextmanager
async def _actual_p4(store, tmp_path, monkeypatch, mode):
    from elspeth.web.composer import tool_batch, turn_audit
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    descriptor = ("historical-metadata", "set_metadata", {"patch": {"name": "after"}}) if mode == "checkpoint" else None
    if mode == "checkpoint":
        actual_payload = turn_audit._state_payload_for_compose_turn

        def payload(*args, **kwargs):
            # Lawful extra operational metadata enters before original P4
            # capture and the actual writer. No receipt/outcome is fabricated.
            original = actual_payload(*args, **kwargs)
            metadata = {} if original.data.composer_meta is None else dict(original.data.composer_meta)
            metadata["lossless_control"] = {"z": 1.0, "a": 1}
            return replace(original, data=replace(original.data, composer_meta=metadata))

        monkeypatch.setattr(turn_audit, "_state_payload_for_compose_turn", payload)
    async with _phase_case(store, tmp_path, monkeypatch, tool_call=descriptor) as (owner, running, _primary, sdk):
        errors = ()
        if mode == "unavailable":
            original = RuntimeError("actual historical checkpoint tool failure")
            storage = OperationalError("actual historical checkpoint audit failure", None, RuntimeError("original storage cause"))

            def execute(*args, **kwargs):
                raise original

            def insert(*args, **kwargs):
                raise storage

            monkeypatch.setattr(tool_batch, "execute_tool", execute)
            await _advance(owner)
            from elspeth.contracts.composer_audit import ComposerToolStatus

            custody, data = owner.failure_custody, owner.runtime.graph.batch
            assert custody is not None and custody.completed and custody.original is original
            assert data is not None and data.batch.stop_issued_batch and data.batch.next_tool_index == 1
            assert data.batch.retained_failure is not None and data.batch.retained_failure.kind == "plugin_crash"
            invocation = custody.invocation
            assert invocation is owner.runtime.live.recorder.invocations[-1]
            assert invocation.status is ComposerToolStatus.PLUGIN_CRASH and invocation.private_failure is not None
            assert invocation.result_canonical is invocation.result_hash is invocation.version_after is None
            assert invocation.error_class == invocation.error_message == type(original).__name__
            assert invocation.private_authoring is not None
            assert invocation.private_authoring.tool_call_id == invocation.tool_call_id == data.calls[0].id
            assert invocation.private_authoring.tool_name == invocation.tool_name == data.calls[0].function.name == "get_pipeline_state"
            assert data.batch.tool_outcomes[0].call is data.calls[0] and data.batch.tool_outcomes[0].response is None
            assert custody.call.index == 0 and custody.call.tool_call is data.calls[0] and owner.p4 is None
            monkeypatch.setattr(owner.service._require_sessions_service(), "_insert_chat_message", insert)
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            owner.observe_stop()
            with pytest.raises(BaseException) as caught:
                await owner.finish_owed_prefix(owner.runtime.source.selected)
            assert _contains_identity(caught.value, original) and _contains_identity(caught.value, storage)
            errors = (original, storage, caught.value)
        else:
            await _advance(owner)
            if mode == "empty":
                await _advance(owner)
            await _advance(owner)
        phase = owner.p4
        assert phase is not None and phase.prepared is not None and phase.publication is not None
        assert phase.committed_outcome is not None and phase.application is phase.committed_outcome.application
        assert phase.publication.audit_outcome is phase.committed_outcome.audit_outcome
        assert len(sdk) == 1
        yield owner, running, phase, sdk, errors
        assert owner.p4 is phase and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("checkpoint", "empty", "unavailable"))
async def test_actual_writer_audit_and_p4_outcomes_preserve_original_columns(operation_store, tmp_path, monkeypatch, mode):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, mode) as (owner, _running, phase, sdk, errors):
        actual = phase.committed_outcome.audit_outcome
        prepared = phase.prepared
        names = phase.inventory.names
        encoded = encode_audit_outcome_data(actual, prepared=prepared, env_ref_names=names)
        audit = decode_audit_outcome_data(encoded, prepared=prepared, env_ref_names=names)
        retained = capture_p4_outcome(prepared, phase.publication, env_ref_names=names)
        cold = decode_p4_outcome(encode_p4_outcome(retained, env_ref_names=names), env_ref_names=names)
        assert require_same_p4_outcome(cold, actual=phase.publication, env_ref_names=names) is phase.publication
        assert phase.publication.audit_outcome is actual and len(sdk) == 1
        if mode == "checkpoint":
            assert actual.checkpoint is not None and audit.checkpoint is not None and cold.checkpoint is not None
            original = historical_checkpoint_columns_json(actual.checkpoint.state)
            assert audit.checkpoint.original_columns_json == cold.checkpoint.original_columns_json == original
            assert historical_checkpoint_columns_json(historical_checkpoint_record(cold.checkpoint, env_ref_names=names)) == original
            assert actual.checkpoint_disposition == "committed" and phase.application.bundle.result.historical_effect is not None
        else:
            assert actual.checkpoint is audit.checkpoint is cold.checkpoint is None
            expected = "unavailable" if mode == "unavailable" else "no_new_checkpoint" if actual.current_state_id is not None else "empty"
            assert actual.checkpoint_disposition == expected
            if mode == "unavailable":
                assert actual.unwind_audit_failed and actual.assistant_id is None
                assert owner.failure_custody.original is errors[0]
                assert _contains_identity(errors[2], errors[0]) and _contains_identity(errors[2], errors[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "composer_meta_order", "composer_meta_number", "created_at", "derived_from", "creator"))
async def test_actual_immutable_sql_row_requires_exact_lossless_historical_columns(operation_store, tmp_path, monkeypatch, change):
    async with _actual_p4(operation_store, tmp_path, monkeypatch, "checkpoint") as (owner, running, phase, sdk, _errors):
        original = phase.application.bundle.result
        data = original.outcome
        checkpoint = data.checkpoint
        assert checkpoint is not None
        record = phase.application.bundle.entry.applications[-1]
        receipt = original.receipt

        async def verify(value):
            def read():
                with operation_store[0].connect() as conn:
                    return verify_p4_immutable_receipt_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=record.effect_key,
                        input_sha256=record.input_sha256,
                        receipt_key=record.receipt_id,
                        creator_sequence=record.creator_sequence,
                        creator_generation=record.creator_generation,
                        receipt=receipt,
                        data=value,
                        selected=phase.prepared,
                        env_ref_names=phase.inventory.names,
                    )

            return await _registered_read(owner.work, ReadmittedP4Result, read)

        positive = await verify(data)
        assert positive == original and positive.receipt is receipt
        if change == "none":
            assert len(sdk) == 1
            return
        values = json.loads(checkpoint.original_columns_json)
        if change == "composer_meta_order":
            values["composer_meta"] = dict(reversed(list(values["composer_meta"].items())))
            opposed = _changed(checkpoint, values)
        elif change == "composer_meta_number":
            assert type(values["composer_meta"]["lossless_control"]["z"]) is float
            values["composer_meta"]["lossless_control"]["z"] = 1
            opposed = _changed(checkpoint, values)
        else:
            updates = {
                "created_at": {"created_at": checkpoint.created_at + timedelta(seconds=1)},
                "derived_from": {"derived_from_state_id": UUID("20000000-0000-4000-8000-000000000001")},
                "creator": {"creator_operation_id": "20000000-0000-4000-8000-000000000001"},
            }
            opposed = checkpoint.model_copy(update=updates[change])
        changed = data.model_copy(update={"checkpoint": opposed})
        if change == "creator":
            with pytest.raises(AuditIntegrityError, match="writer checkpoint differs from preparing custody"):
                decode_audit_outcome_data(
                    canonical_json(changed.model_dump(mode="json")), prepared=phase.prepared, env_ref_names=phase.inventory.names
                )
        else:
            if change.startswith("composer_meta"):
                # Existing whole-record equality and RFC8785 hashes cannot
                # discriminate these two unchanged-publication substitutions.
                assert (
                    historical_checkpoint_record(opposed, env_ref_names=phase.inventory.names)
                    == phase.committed_outcome.audit_outcome.checkpoint.state
                )
                assert opposed.original_columns_json != checkpoint.original_columns_json
            with pytest.raises(AuditIntegrityError, match="historical checkpoint differs from its actual full immutable row"):
                await verify(changed)
        assert len(sdk) == 1
