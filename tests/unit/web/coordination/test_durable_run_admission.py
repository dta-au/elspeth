"""Admission, start and peer cancellation share the durable session lock."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind, StartPermitState
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.coordination.run_cancellation_authority import RepositoryRunCancellationAuthority
from elspeth.web.coordination.run_recovery_authority import RepositoryGlobalRunRecoveryAuthority
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.execution.envelope import RunExecutionInput
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import (
    composition_states_table,
    identities_table,
    quota_policies_table,
    run_events_table,
    run_execution_inputs_table,
    run_start_permits_table,
    runs_table,
    session_operation_fences_table,
    sessions_table,
)
from elspeth.web.sessions.protocol import RunAlreadyActiveError

NO_QUOTA_POLICY = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def test_pre_restore_assessment_leaves_success_pending_and_cancellable(engine):
    authority, context, run, _ = _admission(engine)
    assessed = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert assessed.state is StartPermitState.PENDING
    assert assessed.admission_decision is None and assessed.permit_id is None
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.saga_state)).scalar_one() == "start_intent"
    RepositoryRunCancellationAuthority(engine).request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    cancelled = authority.mutate(context, lambda tx: tx.runs.observe_start_permit_for_cleanup(run_id=run.id))
    assert cancelled.state is StartPermitState.CANCELLED_BEFORE_PERMIT
    assert cancelled.permit_id is None


@pytest.mark.parametrize("disable_after_assessment", [False, True])
def test_post_restore_mint_rechecks_identity(engine, disable_after_assessment):
    authority, context, run, _ = _admission(engine)
    assert (
        authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY)).state
        is StartPermitState.PENDING
    )
    if disable_after_assessment:
        with engine.begin() as conn:
            conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    issued = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert issued.state is (StartPermitState.REFUSED if disable_after_assessment else StartPermitState.START_PERMITTED)
    assert issued.admission_decision.allowed is not disable_after_assessment


@pytest.mark.parametrize("already_issued", [False, True])
def test_pre_restore_assessment_records_refusal_preserving_issued_history(engine, already_issued):
    authority, context, run, _ = _admission(engine)
    first = (
        authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY)) if already_issued else None
    )
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    refused = authority.mutate(context, lambda tx: tx.runs.assess_start_admission(run_id=run.id, policy=NO_QUOTA_POLICY))
    if first is not None:
        assert refused.subject_hash == first.subject_hash
        assert refused.admission_decision == first.admission_decision
        assert refused.execution_refusal.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
    else:
        assert refused.state is StartPermitState.REFUSED
        assert refused.admission_decision.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
        assert refused.permit_id is None
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.saga_state)).scalar_one() == "admission_refusal_pending"
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 1


def _admission(engine):
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    authority = SQLiteLocalSessionOperationAuthority(engine)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="admission", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    fence = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="owner", lease_seconds=30
    )
    context = fence
    state_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(composition_states_table).values(
                id=str(state_id), session_id=str(session.id), version=1, provenance="session_seed", created_at=datetime.now(UTC)
            )
        )
    envelope = RunExecutionInput(
        schema_version=1,
        envelope_json="{}",
        canonical_input_digest="a" * 64,
        topology_digest="b" * 64,
        source_manifest_digest="c" * 64,
        application_fingerprint="d" * 64,
        plugin_registry_fingerprint="e" * 64,
        configuration_fingerprint="f" * 64,
        graph_fingerprint="a" * 64,
        runtime_fingerprint="b" * 64,
        implementation_fingerprint="c" * 64,
        deployment_generation="test",
        session_epoch=1,
        landscape_epoch=1,
        coordination_protocol=1,
        automatic_recovery_eligible=True,
    )
    run_id = uuid4()

    def admit(tx):
        return tx.runs.create_pending_run(
            run_id=run_id, state_id=state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=envelope
        )

    run = authority.mutate(context, admit)
    return authority, context, run, envelope


def test_atomic_admission_has_one_envelope_permit_and_owner(engine):
    authority, context, run, envelope = _admission(engine)
    with pytest.raises(RunAlreadyActiveError):
        authority.mutate(
            context,
            lambda tx: tx.runs.create_pending_run(
                run_id=uuid4(), state_id=run.state_id, pipeline_yaml=None, started_at=datetime.now(UTC), execution_input=envelope
            ),
        )
    with engine.connect() as conn:
        assert [
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (runs_table, run_execution_inputs_table, run_start_permits_table)
        ] == [1, 1, 1]
        row = conn.execute(select(runs_table)).one()
        assert row.owner_epoch == context.fence.operation_epoch
        assert row.saga_state == "start_intent"


def test_cancel_before_permit_forbids_dispatch_and_is_durable(engine):
    authority, context, run, _ = _admission(engine)
    cancel = RepositoryRunCancellationAuthority(engine)
    cancelled = cancel.request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    assert cancelled.status == "cancelled"
    assert cancelled.cancel_requested_at is not None
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert permit.state is StartPermitState.CANCELLED_BEFORE_PERMIT
    assert permit.permit_id is None
    assert cancel.request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local") == cancelled
    with engine.connect() as conn:
        event = conn.execute(select(run_events_table).where(run_events_table.c.run_id == str(run.id))).one()
        assert event.event_type == "cancelled"
        assert event.sequence == 1
        assert event.timestamp.replace(tzinfo=UTC) == cancelled.finished_at
        assert event.data == {
            "status": "cancelled",
            "source_rows_processed": 0,
            "tokens_succeeded": 0,
            "tokens_failed": 0,
            "tokens_quarantined": 0,
            "tokens_routed_success": 0,
            "tokens_routed_failure": 0,
        }


def test_issued_permit_is_immutable_and_cancel_after_permit_is_cooperative(engine):
    authority, context, run, _ = _admission(engine)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert permit.state is StartPermitState.START_PERMITTED
    cancelled = RepositoryRunCancellationAuthority(engine).request(
        run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local"
    )
    assert cancelled.status == "pending"
    assert cancelled.cancel_requested_at is not None
    assert authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY)) == permit
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.parametrize(
    ("user_id", "provider", "wrong_run"), [("mallory", "local", False), ("alice", "oidc", False), ("alice", "local", True)]
)
def test_peer_cancellation_rejects_confused_deputy_without_writes(engine, user_id, provider, wrong_run):
    _, _, run, _ = _admission(engine)
    with pytest.raises(SessionDerivedCustodyError):
        RepositoryRunCancellationAuthority(engine).request(
            uuid4() if wrong_run else run.id, session_id=run.session_id, user_id=user_id, auth_provider_type=provider
        )
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.cancel_requested_at)).scalar_one() is None
        assert conn.execute(select(run_start_permits_table.c.start_state)).scalar_one() == "pending"


def test_expired_owner_cannot_issue_but_peer_can_cancel(engine):
    authority, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(run.session_id))
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    with pytest.raises(SessionOperationFenceLost):
        authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert RepositoryGlobalRunRecoveryAuthority(engine).list_recoverable_run_records()[0].id == run.id
    cancelled = RepositoryRunCancellationAuthority(engine).request(
        run.id, session_id=UUID(context.fence.session_id), user_id="alice", auth_provider_type="local"
    )
    assert cancelled.status == "cancelled"


def test_fresh_owner_adopts_only_linked_pending_output_custody(engine):
    from elspeth.web.sessions.models import blob_run_links_table, blobs_table

    authority, context, run, _ = _admission(engine)
    blob_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=str(blob_id),
                session_id=str(run.session_id),
                filename="output.csv",
                mime_type="text/csv",
                size_bytes=0,
                storage_path="outputs/output.csv",
                created_at=datetime.now(UTC),
                created_by="pipeline",
                status="pending",
                custody_operation_id=context.fence.operation_id,
                custody_operation_epoch=context.fence.operation_epoch,
                custody_operation_kind="execute",
            )
        )
        conn.execute(insert(blob_run_links_table).values(blob_id=str(blob_id), run_id=str(run.id), direction="output"))
        conn.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(run.session_id))
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )
    fresh = authority.acquire(
        session_id=run.session_id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="replacement", lease_seconds=30
    )
    authority.mutate(fresh, lambda tx: tx.runs.rebind_run_ownership(run_id=run.id))
    outputs = authority.mutate(fresh, lambda tx: tx.blobs.list_pending_run_output_blobs(run_id=run.id))
    assert [blob.id for blob in outputs] == [blob_id]
    with engine.connect() as conn:
        blob = conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).one()
        assert blob.custody_operation_id == fresh.fence.operation_id
        assert blob.custody_operation_epoch == fresh.fence.operation_epoch


def test_terminal_event_reconciliation_is_idempotent(engine):
    from elspeth.web.sessions.models import run_events_table

    authority, context, run, _ = _admission(engine)

    def append(tx):
        return tx.runs.append_terminal_run_event_once(
            run_id=run.id, timestamp=datetime.now(UTC), event_type="cancelled", data={"reason": "requested"}
        )

    first = authority.mutate(context, append)
    second = authority.mutate(context, append)
    assert first.id == second.id
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 1


def test_permit_creation_failure_rolls_back_admission_envelope_and_run(engine, monkeypatch):
    from elspeth.web.coordination.run_start_permit_authority import RepositoryRunStartPermitAuthority

    def fail_creation(connection_token, *, run_id):
        raise RuntimeError("simulated admission crash")

    monkeypatch.setattr(RepositoryRunStartPermitAuthority, "create_pending", fail_creation)
    with pytest.raises(RuntimeError, match="simulated admission crash"):
        _admission(engine)
    with engine.connect() as conn:
        for table in (runs_table, run_execution_inputs_table, run_start_permits_table):
            assert conn.execute(select(func.count()).select_from(table)).scalar_one() == 0


def test_archived_session_cannot_receive_durable_cancellation(engine):
    _, _, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(run.session_id)).values(archived_at=datetime.now(UTC)))
    with pytest.raises(SessionDerivedCustodyError):
        RepositoryRunCancellationAuthority(engine).request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    with engine.connect() as conn:
        row = conn.execute(select(runs_table).where(runs_table.c.id == str(run.id))).one()
        assert row.cancel_requested_at is None
        assert row.status == "pending"
        assert conn.execute(select(func.count()).select_from(run_events_table)).scalar_one() == 0


@pytest.mark.parametrize(
    "access_state,reason", [("disabled", AdmissionRefusalReason.IDENTITY_DISABLED), ("pending", AdmissionRefusalReason.IDENTITY_PENDING)]
)
def test_inactive_owner_refusal_commits_terminal_run_and_event(engine, access_state, reason):
    authority, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state=access_state))
    refused = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert refused.state is StartPermitState.REFUSED
    assert refused.admission_decision.refusal_reason is reason
    assert refused.permit_id is None
    with engine.connect() as conn:
        saved = conn.execute(select(runs_table).where(runs_table.c.id == str(run.id))).one()
        assert saved.status == "failed" and saved.finished_at is not None
        event = conn.execute(select(run_events_table).where(run_events_table.c.run_id == str(run.id))).one()
        assert event.event_type == "failed"
        assert reason.value in event.data["detail"]
    assert authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY)) == refused


@pytest.mark.parametrize("with_policy", [False, True])
def test_enabled_quota_never_treats_empty_ledger_as_zero(engine, with_policy):
    authority, context, run, _ = _admission(engine)
    if with_policy:
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
    policy = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=policy))
    expected = AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE if with_policy else AdmissionRefusalReason.QUOTA_POLICY_MISSING
    assert permit.admission_decision.refusal_reason is expected
    assert permit.admission_decision.evidence.identity_policy_id == ("quota-alice" if with_policy else None)


def test_recovery_refuses_disabled_owner_without_rewriting_original_permit(engine):
    authority, context, run, _ = _admission(engine)
    first = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    second = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    assert second.subject_hash == first.subject_hash
    assert second.admission_decision == first.admission_decision
    assert second.execution_refusal.refusal_reason is AdmissionRefusalReason.IDENTITY_DISABLED
    with engine.connect() as conn:
        assert conn.execute(select(runs_table.c.status).where(runs_table.c.id == str(run.id))).scalar_one() == "failed"


def test_disabled_owner_does_not_block_existing_cancel_cleanup_binding(engine):
    authority, context, run, _ = _admission(engine)
    issued = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    RepositoryRunCancellationAuthority(engine).request(run.id, session_id=run.session_id, user_id="alice", auth_provider_type="local")
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    assert authority.mutate(context, lambda tx: tx.runs.observe_start_permit_for_cleanup(run_id=run.id)) == issued


def test_wiring_generation_change_refuses_recovery_with_original_evidence_intact(engine):
    authority, context, run, _ = _admission(engine)
    issued = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    changed = ChargeableAdmissionPolicy(secret_wiring_hash="f" * 64)
    refused = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=changed))
    assert refused.subject_hash == issued.subject_hash
    assert refused.admission_decision == issued.admission_decision
    assert refused.execution_refusal.refusal_reason is AdmissionRefusalReason.POLICY_GENERATION_CHANGED


def test_permit_evidence_hash_is_checked_on_replay(engine):
    authority, context, run, _ = _admission(engine)
    authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    with engine.begin() as conn:
        conn.execute(
            update(run_start_permits_table).where(run_start_permits_table.c.run_id == str(run.id)).values(admission_decision_hash="f" * 64)
        )
    with pytest.raises(AuditIntegrityError, match="evidence hash"):
        authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))


def test_refusal_remains_recoverable_until_cleanup_acknowledged(engine):
    authority, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(access_state="disabled"))
    authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    authority.release(context)
    candidates = RepositoryGlobalRunRecoveryAuthority(engine).list_recoverable_run_records()
    assert [candidate.id for candidate in candidates] == [run.id]
    assert candidates[0].saga_state.value == "admission_refusal_pending"
    recovered = authority.acquire(
        session_id=run.session_id, operation_kind=SessionOperationKind.EXECUTE, owner_instance_id="recovery", lease_seconds=30
    )
    authority.mutate(recovered, lambda tx: tx.runs.complete_admission_refusal(run_id=run.id))
    authority.release(recovered)
    assert RepositoryGlobalRunRecoveryAuthority(engine).list_recoverable_run_records() == ()


def test_owner_provider_custody_corruption_cannot_issue_permit(engine):
    authority, context, run, _ = _admission(engine)
    with engine.begin() as conn:
        conn.execute(update(identities_table).where(identities_table.c.identity_id == "alice").values(provider="oidc"))
    with pytest.raises(AuditIntegrityError, match="provider custody"):
        authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=NO_QUOTA_POLICY))
    with engine.connect() as conn:
        assert conn.execute(select(run_start_permits_table.c.start_state)).scalar_one() == "pending"


def test_stored_admission_evidence_rejects_boolean_version_and_unassessed_accounting():
    import json

    from pydantic import ValidationError

    from elspeth.contracts.chargeable_admission import AdmissionPolicyEvidence, QuotaDisposition

    valid = AdmissionPolicyEvidence(
        quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE,
        identity_policy_id="quota-alice",
        secret_wiring_hash=NO_QUOTA_POLICY.secret_wiring_hash,
    )
    assert AdmissionPolicyEvidence.model_validate_json(valid.model_dump_json(), strict=True) == valid
    boolean_version = valid.model_dump(mode="json")
    boolean_version["schema_version"] = True
    with pytest.raises(ValidationError, match="exact integer"):
        AdmissionPolicyEvidence.model_validate_json(json.dumps(boolean_version), strict=True)
    missing_policy = valid.model_dump(mode="json")
    missing_policy["identity_policy_id"] = None
    with pytest.raises(ValidationError, match="assessed quota policy"):
        AdmissionPolicyEvidence.model_validate_json(json.dumps(missing_policy), strict=True)
