"""R2 reads live approvals before first dispatch and every recovered dispatch."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.coordination.test_durable_run_admission import _admission

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ApprovalDisposition,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.web.coordination.approval_authority import ApprovalGateInputs
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import _register_mutation_connection, _unregister_mutation_connection
from elspeth.web.coordination.run_start_permit_authority import RepositoryRunStartPermitAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import (
    approvals_table,
    composition_states_table,
    quota_policies_table,
    run_events_table,
    run_start_permits_table,
    runs_table,
)

GOVERNED = ChargeableAdmissionPolicy(workflow_governance_on=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def _gate() -> ApprovalGateInputs:
    evidence = WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="a" * 64,
        snapshot_hash="b" * 64,
        authorized_plugin_ids=("sink:csv", "source:csv"),
        available_plugin_ids=("sink:csv", "source:csv"),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(),
        binding_generation_fingerprint="c" * 64,
        decision_codes=("policy_allowed",),
    )
    return ApprovalGateInputs(
        evidence=evidence,
        config_hash="1" * 64,
        canonical_version="sha256-rfc8785-v1",
        openrouter_catalog_sha256="2" * 64,
        runtime_val_manifest_sha256="3" * 64,
    )


def _approve(engine, run, binding, *, state_id=None) -> str:
    approval_id = str(uuid4())
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="approver")
        conn.execute(
            insert(approvals_table).values(
                approval_id=approval_id,
                session_id=str(run.session_id),
                state_id=str(state_id or run.state_id),
                binding_json=binding.as_json(),
                requested_by_identity_id="alice",
                approver_identity_id="approver",
                requested_at=datetime.now(UTC),
                decided_at=datetime.now(UTC),
                decision="approved",
            )
        )
    return approval_id


def _direct_permit(engine, context, run, *, policy=GOVERNED, approval=None, issue=False):
    with locked_session_transaction(engine, str(run.session_id)) as conn:
        token = _register_mutation_connection(conn)
        try:
            method = RepositoryRunStartPermitAuthority.issue if issue else RepositoryRunStartPermitAuthority.assess
            return method(token, run_id=str(run.id), context=context, now=database_now(conn), policy=policy, approval=approval)
        finally:
            _unregister_mutation_connection(token)


def test_governance_on_missing_gate_cannot_mint_or_assess_permit(engine) -> None:
    _, context, run, _ = _admission(engine)
    with pytest.raises(AuditIntegrityError, match="approval input"):
        _direct_permit(engine, context, run)
    with pytest.raises(AuditIntegrityError, match="approval input"):
        _direct_permit(engine, context, run, issue=True)
    with engine.connect() as conn:
        assert conn.execute(select(run_start_permits_table.c.start_state)).scalar_one() == "pending"
        assert conn.execute(select(runs_table.c.status)).scalar_one() == "pending"


def test_no_approval_is_a_persisted_refusal(engine) -> None:
    authority, context, run, _ = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=GOVERNED, approval=_gate()))
    assert permit.state is StartPermitState.REFUSED
    assert permit.admission_decision is not None
    assert permit.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED
    # Admission assesses quota first. Even without a configured quota, the
    # persisted approval refusal retains that truthful assessment.
    assert permit.admission_decision.evidence.quota_disposition is QuotaDisposition.NOT_CONFIGURED
    with engine.connect() as conn:
        row = conn.execute(select(run_start_permits_table)).one()
        assert row.admission_decision_hash == permit.admission_decision.canonical_hash
        assert conn.execute(select(runs_table.c.saga_state)).scalar_one() == "admission_refusal_pending"
        assert conn.execute(select(run_events_table.c.event_type)).scalar_one() == "failed"


def test_exact_binding_admits_but_a_different_field_refuses(engine) -> None:
    _, context, run, _ = _admission(engine)
    gate = _gate()
    _approve(engine, run, gate.binding)
    permit = _direct_permit(engine, context, run, approval=gate, issue=True)
    assert permit.state is StartPermitState.START_PERMITTED

    _, context2, run2, _ = _admission(engine)
    _approve(engine, run2, replace(gate.binding, binding_generation_fingerprint="f" * 64))
    refused = _direct_permit(engine, context2, run2, approval=gate, issue=True)
    assert refused.state is StartPermitState.REFUSED
    assert refused.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_BINDING_MISMATCH


def test_later_rejection_retirement_refuses_recovery_of_issued_permit(engine) -> None:
    _, context, run, _ = _admission(engine)
    gate = _gate()
    old_id = _approve(engine, run, gate.binding)
    issued = _direct_permit(engine, context, run, approval=gate, issue=True)
    assert issued.state is StartPermitState.START_PERMITTED
    with engine.begin() as conn:
        conn.execute(update(approvals_table).where(approvals_table.c.approval_id == old_id).values(decision="superseded"))
        conn.execute(
            insert(approvals_table).values(
                approval_id=str(uuid4()),
                session_id=str(run.session_id),
                state_id=str(run.state_id),
                binding_json=gate.binding.as_json(),
                requested_by_identity_id="alice",
                approver_identity_id="approver",
                requested_at=datetime.now(UTC),
                decided_at=datetime.now(UTC),
                decision="rejected",
            )
        )
    reassessed = _direct_permit(engine, context, run, approval=gate)
    assert reassessed.state is StartPermitState.START_PERMITTED
    assert reassessed.admission_decision == issued.admission_decision
    assert reassessed.execution_refusal is not None
    assert reassessed.execution_refusal.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED


def test_approval_for_a_different_state_cannot_admit_this_run(engine) -> None:
    _, context, run, _ = _admission(engine)
    gate = _gate()
    other_state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(other_state_id),
                session_id=str(run.session_id),
                version=2,
                provenance="session_seed",
                created_at=datetime.now(UTC),
            )
        )
    _approve(engine, run, gate.binding, state_id=other_state_id)
    refused = _direct_permit(engine, context, run, approval=gate)
    assert refused.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED


def test_recovery_cannot_retarget_an_issued_permit_to_a_new_binding(engine) -> None:
    _, context, run, _ = _admission(engine)
    first_gate = _gate()
    _approve(engine, run, first_gate.binding)
    issued = _direct_permit(engine, context, run, approval=first_gate, issue=True)
    second_gate = replace(first_gate, config_hash="9" * 64)
    _approve(engine, run, second_gate.binding)
    reassessed = _direct_permit(engine, context, run, approval=second_gate)
    assert reassessed.state is StartPermitState.START_PERMITTED
    assert reassessed.subject_hash == issued.subject_hash
    assert reassessed.execution_refusal is not None
    assert reassessed.execution_refusal.refusal_reason is AdmissionRefusalReason.POLICY_GENERATION_CHANGED
    assert reassessed.execution_refusal.evidence.quota_disposition is QuotaDisposition.NOT_CONFIGURED
    assert reassessed.execution_refusal.evidence.approval_disposition is ApprovalDisposition.MATCHED


def test_governance_off_accepts_without_approval_input(engine) -> None:
    _, context, run, _ = _admission(engine)
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=GOVERNED.secret_wiring_hash)
    permit = _direct_permit(engine, context, run, policy=policy, issue=True)
    assert permit.state is StartPermitState.START_PERMITTED


def test_governance_off_ignores_optional_gate_without_changing_decision(engine) -> None:
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=GOVERNED.secret_wiring_hash)
    _, context, run, _ = _admission(engine)
    first = _direct_permit(engine, context, run, policy=policy, approval=_gate(), issue=True)
    _, context2, run2, _ = _admission(engine)
    second = _direct_permit(engine, context2, run2, policy=policy, issue=True)
    assert first.admission_decision == second.admission_decision
    assert first.admission_decision.canonical_hash == second.admission_decision.canonical_hash


def test_approval_refusal_preserves_measured_quota_evidence(engine) -> None:
    _, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(quota_policies_table).values(
                policy_id="quota-alice",
                identity_id="alice",
                tokens_per_day=1000,
                storage_bytes=1000,
                set_by_actor="identity",
                set_by_identity_id="alice",
                set_at=datetime.now(UTC),
            )
        )
    policy = GOVERNED.model_copy(update={"identity_token_quota_configured": True})
    refusal = _direct_permit(engine, context, run, policy=policy, approval=_gate())
    assert refusal.admission_decision.refusal_reason is AdmissionRefusalReason.APPROVAL_REQUIRED
    assert refusal.admission_decision.evidence.schema_version == 3
    assert refusal.admission_decision.evidence.approval_disposition is ApprovalDisposition.REQUIRED
    assert refusal.admission_decision.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
    assert refusal.admission_decision.evidence.identity_policy_id == "quota-alice"
    assert refusal.admission_decision.evidence.cap == 1000
    assert refusal.admission_decision.evidence.usage == 0


def test_version_two_evidence_replays_with_its_original_hash() -> None:
    evidence = AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash="a" * 64)
    decision = ChargeableAdmissionDecision(refusal_reason=None, evidence=evidence)
    original_payload = decision.model_dump(mode="json")
    original_payload["evidence"].pop("approval_disposition")
    original_payload["evidence"].pop("approval_binding_hash")
    assert decision.canonical_hash == stable_hash(original_payload)
    assert (
        ChargeableAdmissionDecision.model_validate_json(decision.model_dump_json(), strict=True).canonical_hash == decision.canonical_hash
    )
