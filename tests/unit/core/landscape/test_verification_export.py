"""F10: signed exports retain the comparison evidence of verify runs."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.audit_export import AUDIT_EXPORT_SERIALIZATION_VERSION
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.exporter import LandscapeExporter
from elspeth.core.landscape.schema import call_verifications_table, runs_table
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run, register_test_node


@pytest.fixture
def verified_exporter():
    setup = make_recorder_with_run(run_id="source", source_node_id="source-node")
    factory = setup.factory
    factory.run_lifecycle.begin_run(
        config={}, canonical_version="v1", run_id="verify", run_mode=RunMode.VERIFY, replay_from_run_id="source"
    )
    register_test_node(factory.data_flow, "verify", "source-node", node_type=NodeType.SOURCE, plugin_name="source")
    operations = {
        run_id: factory.execution.begin_operation(
            "source-node", "source_load", coordination_token=leader_coordination_token(factory, run_id)
        )
        for run_id in ("source", "verify")
    }
    decisions = []
    for is_match, differences in (
        (True, "{}"),
        (False, '{"response_hash":{"expected":"a","actual":"b"}}'),
        (None, '{"reason":"missing_source_call"}'),
    ):
        source_call = factory.execution.record_operation_call(
            operations["source"].operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            request_data=RawCallPayload({"url": "https://example.test"}),
            coordination_token=leader_coordination_token(factory, "source"),
        )
        current_call = factory.execution.record_operation_call(
            operations["verify"].operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            request_data=RawCallPayload({"url": "https://example.test"}),
            source_call_id=source_call.call_id if is_match is not None else None,
            coordination_token=leader_coordination_token(factory, "verify"),
        )
        decisions.append(
            factory.execution.record_verification_decision(
                current_run_id="verify",
                current_call_id=current_call.call_id,
                source_run_id="source",
                source_call_id=source_call.call_id if is_match is not None else None,
                is_match=is_match,
                differences_json=differences,
                coordination_token=leader_coordination_token(factory, "verify"),
            )
        )
    with setup.db.engine.begin() as conn:
        conn.execute(runs_table.update().values(status="completed", completed_at=datetime.now(UTC)))
    yield (
        LandscapeExporter(
            setup.db, compartment_id="test-compartment", signing_key=b"verification-export-test", signer_key_id="verification-test-key"
        ),
        decisions,
    )
    setup.db.close()


@pytest.mark.parametrize("export_format", ["json", "csv"])
@pytest.mark.parametrize("signed", [False, True])
def test_export_contains_exact_verification_evidence(verified_exporter, export_format, signed):
    exporter, decisions = verified_exporter
    bundle = exporter.derive_run_bundle(
        "verify",
        sign=signed,
        derivation_config=replace(exporter.derive_run_bundle("verify", sign=signed).config, export_format=export_format),
    )
    records = [json.loads(line) for chunk in bundle.chunk_bytes for line in chunk.splitlines()]
    verdicts = [record for record in records if record["record_type"] == "call_verification"]
    if signed:
        for verdict in verdicts:
            signature = verdict.pop("signature")
            assert signature == exporter._sign_record(verdict)
    assert verdicts == [
        {
            "record_type": "call_verification",
            "run_id": "verify",
            "current_call_id": decision.current_call_id,
            "source_run_id": "source",
            "source_call_id": decision.source_call_id,
            "is_match": decision.is_match,
            "differences_json": decision.differences_json,
            "recorded_at": decision.recorded_at.replace(tzinfo=None).isoformat(),
        }
        for decision in decisions
    ]
    exported_calls = {record["call_id"] for record in records if record["record_type"] == "call"}
    assert {record["current_call_id"] for record in verdicts} <= exported_calls
    assert not [record for record in exporter.export_run("source") if record["record_type"] == "call_verification"]
    assert bundle.config.serialization_version == "audit-export-v3"
    assert bundle.final_manifest["record_count"] == len(records)


def test_serialization_contract_rejects_previous_version(verified_exporter):
    exporter, _decisions = verified_exporter
    assert AUDIT_EXPORT_SERIALIZATION_VERSION == "audit-export-v3"
    config = exporter.derive_run_bundle("verify").config
    with pytest.raises(ValueError, match="serialization_version"):
        replace(config, serialization_version="audit-export-v2")


def test_export_rejects_corrupt_verification_evidence(verified_exporter):
    exporter, decisions = verified_exporter
    with exporter._db.engine.begin() as conn:
        conn.execute(
            call_verifications_table.update()
            .where(call_verifications_table.c.current_call_id == decisions[0].current_call_id)
            .values(differences_json="[]")
        )
    with pytest.raises(AuditIntegrityError, match="differences_json"):
        list(exporter.export_run("verify"))
