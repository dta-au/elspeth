"""Immutable final policy decisions survive storage, export and recovery."""

from dataclasses import replace

import pytest
from sqlalchemy import update

from elspeth.contracts.chargeable_admission import AdmissionPolicyEvidence, ChargeableAdmissionDecision, QuotaDisposition
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.plugin_policy_audit import decode_admission_decision
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.landscape.export_read_model import open_export_read_transaction
from elspeth.core.landscape.exporter import LandscapeExporter
from elspeth.core.landscape.schema import run_web_plugin_policy_table
from elspeth.web.execution.envelope import (
    build_run_execution_input,
    capture_execution_envelope,
    read_cancelled_execution_envelope,
    restore_execution_envelope,
)
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy, SecretWiringRule
from tests.unit.core.landscape.test_run_lifecycle_repository import _make_repo, _web_policy_evidence
from tests.unit.web.execution.test_execution_envelope import SecretStore, snapshot


def _decision() -> ChargeableAdmissionDecision:
    return ChargeableAdmissionDecision(
        evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="d" * 64),
        refusal_reason=None,
    )


@pytest.mark.parametrize("assessed", [False, True])
def test_final_decision_round_trip_and_export(assessed: bool) -> None:
    db, repo = _make_repo()
    decision = _decision() if assessed else None
    evidence = replace(_web_policy_evidence(), admission_decision=decision)
    repo.begin_run(config={}, canonical_version="v1", run_id="policy", web_plugin_policy_evidence=evidence)
    assert repo.get_web_plugin_policy_evidence("policy") == evidence
    with open_export_read_transaction(db.engine) as reader:
        assert reader.get_web_plugin_policy_evidence("policy") == evidence
        records = list(LandscapeExporter(db, read_model=reader).iter_unsigned_run_records("policy"))
    policies = [record for record in records if record["record_type"] == "web_plugin_policy"]
    assert len(policies) == 1
    assert policies[0]["admission_decision_json"] == (decision.model_dump_json() if decision is not None else None)
    assert policies[0]["admission_decision_hash"] == (decision.canonical_hash if decision is not None else None)


def test_corrupt_hash_rejected_by_repository_and_export_snapshot() -> None:
    db, repo = _make_repo()
    repo.begin_run(
        config={},
        canonical_version="v1",
        run_id="policy",
        web_plugin_policy_evidence=replace(_web_policy_evidence(), admission_decision=_decision()),
    )
    with db.write_connection() as conn:
        conn.execute(
            update(run_web_plugin_policy_table)
            .where(run_web_plugin_policy_table.c.run_id == "policy")
            .values(
                admission_decision_hash="0" * 64,
            )
        )
    with pytest.raises(AuditIntegrityError, match="corrupt"):
        repo.get_web_plugin_policy_evidence("policy")
    with open_export_read_transaction(db.engine) as reader, pytest.raises(AuditIntegrityError, match="corrupt"):
        reader.get_web_plugin_policy_evidence("policy")


@pytest.mark.parametrize("value,digest", [(None, "0" * 64), ("{}", None), ("{}", "0" * 64)])
def test_incomplete_or_invalid_assessment_fails_closed(value: str | None, digest: str | None) -> None:
    with pytest.raises(ValueError):
        decode_admission_decision(value, digest)


def test_wiring_hash_describes_effective_authorization() -> None:
    first = SecretWiringRule("ACCESS", "transform", "llm", "api_key")
    second = SecretWiringRule("OTHER", "sink", "json", "credential")
    policy = SecretWiringPolicy((first, second))
    assert policy.canonical_hash == SecretWiringPolicy((second, first, first)).canonical_hash
    assert policy.canonical_hash != SecretWiringPolicy((replace(first, option_key="other"), second)).canonical_hash
    assert policy.canonical_hash != SecretWiringPolicy(()).canonical_hash


def test_extended_evidence_survives_envelope_projection() -> None:
    frozen = FrozenRunSettings(snapshot(), {}, {})
    evidence = replace(_web_policy_evidence(), admission_decision=_decision())
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=SecretStore(),
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image",
        openrouter_catalog_sha256="e" * 64,
        openrouter_catalog_source="bundled",
        web_plugin_policy_evidence=evidence,
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "image", ())
    assert read_cancelled_execution_envelope(execution_input).web_plugin_policy_evidence == evidence
    restored = restore_execution_envelope(
        envelope.to_json(),
        current_snapshot=frozen.plugin_snapshot,
        user_id="user",
        auth_provider_type="oidc",
        resolver=SecretStore(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image",
    )
    assert restored.web_plugin_policy_evidence == evidence


def test_permit_retry_cannot_replace_frozen_wiring_decision() -> None:
    _db, repo = _make_repo()
    binding = RunStartPermitBinding("policy", "permit", 1, "a" * 64)
    decision = _decision()
    evidence = replace(_web_policy_evidence(), admission_decision=decision)
    first = repo.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding, web_plugin_policy_evidence=evidence)
    assert (
        repo.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding, web_plugin_policy_evidence=evidence).run_id
        == first.run_id
    )
    changed = ChargeableAdmissionDecision(
        evidence=AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="e" * 64),
        refusal_reason=None,
    )
    with pytest.raises(AuditIntegrityError, match="policy evidence"):
        repo.begin_run(
            {},
            "v1",
            run_id=binding.run_id,
            run_start_permit=binding,
            web_plugin_policy_evidence=replace(evidence, admission_decision=changed),
        )
    assert repo.get_web_plugin_policy_evidence(binding.run_id) == evidence
