from __future__ import annotations

import errno
import inspect
import json
from pathlib import Path

import pytest
from fastapi import Request
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException
from structlog.testing import capture_logs

from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.secrets import FingerprintKeyMissingError, SecretDecryptionError
from elspeth.web.app import create_app
from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.composer.protocol import ComposerMutationAuthorityRevoked
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error, request_cancelled_error
from elspeth.web.sessions.protocol import StaleComposeStateError

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)

_EXECUTOR_DETAIL = "Server execution capacity is currently unavailable. Review the session before retrying."


@pytest.fixture
def parity_app(tmp_path: Path):
    return create_app(
        settings=WebSettings(
            data_dir=tmp_path,
            landscape_url=f"sqlite:///{tmp_path}/audit.db",
            payload_store_path=tmp_path / "payloads",
            composer_max_composition_turns=15,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=60,
            composer_boot_probe_enabled=False,
            composer_rate_limit_per_minute=10,
            shareable_link_signing_key=b"\x00" * 32,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(AuditIntegrityError("PRIVATE exception marker"), id="audit-integrity"),
        pytest.param(RequiredGenerationUnavailable("PRIVATE generation detail"), id="generation-unavailable"),
        pytest.param(HTTPException(status_code=400, detail="No user message to recompose"), id="string-no-user"),
        pytest.param(HTTPException(status_code=404, detail="State not found"), id="string-state-missing"),
        pytest.param(HTTPException(status_code=504, detail="Pipeline commit timed out"), id="string-settlement-timeout"),
        pytest.param(
            HTTPException(
                status_code=422,
                detail={"error_type": "composer_convergence_error", "detail": "Convergence failed", "reason": "max_turns_reached"},
            ),
            id="dict-convergence",
        ),
        pytest.param(
            HTTPException(
                status_code=502, detail={"error_type": "llm_unavailable", "detail": "Provider unavailable", "guidance": "Retry later"}
            ),
            id="dict-provider-guidance",
        ),
        pytest.param(
            HTTPException(status_code=409, detail={"error_type": "recompose_already_completed", "detail": "A reply was already saved"}),
            id="dict-saved-reply",
        ),
        pytest.param(StaleComposeStateError("PRIVATE state"), id="stale-flat"),
        pytest.param(FingerprintKeyMissingError("PRIVATE key"), id="key-missing"),
        pytest.param(SecretDecryptionError("PRIVATE ciphertext"), id="decryption"),
        pytest.param(HTTPException(status_code=422, detail={"detail": "A structured request error"}), id="dict-without-error-type"),
        pytest.param(OSError(errno.EIO, "PRIVATE exception marker"), id="storage"),
        pytest.param(OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE driver")), id="database"),
    ],
)
async def test_projector_matches_real_registered_handler(parity_app, exc) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/parity",
            "headers": [],
            "query_string": b"",
            "app": parity_app,
            "state": {"request_id": "parity-request"},
        }
    )
    handler = next(parity_app.exception_handlers[cls] for cls in type(exc).__mro__ if cls in parity_app.exception_handlers)
    response = await handler(request, exc)
    projected = project_composer_operation_error(exc, request_id="parity-request")
    assert projected.http_status == response.status_code
    assert projected.body == json.loads(response.body)
    assert "PRIVATE" not in projected.model_dump_json()


def test_integrity_group_outweighs_unknown_defect_and_never_uses_exception_text() -> None:
    group = ExceptionGroup("PRIVATE group", [RuntimeError("PRIVATE unknown"), AuditIntegrityError("PRIVATE audit")])
    projected = project_composer_operation_error(group, request_id="submitted-id")
    assert projected.error_type == "audit_integrity_error"
    assert "PRIVATE" not in projected.model_dump_json()


def test_nonretryable_permission_failure_is_safe_diagnostic() -> None:
    projected = project_composer_operation_error(PermissionError("PRIVATE marker"), request_id="submitted-id")
    assert projected.failure_code == "operation_failed"
    assert projected.diagnostic_id is not None
    assert "PRIVATE" not in projected.model_dump_json()


def test_failure_projection_does_not_log_before_atomic_terminal_audit() -> None:
    with capture_logs() as logs:
        projected = project_composer_operation_error(RuntimeError("PRIVATE failure"), request_id="submitted-id")
    assert projected.failure_code == "operation_failed"
    assert logs == [], "Projection cannot publish a diagnostic breadcrumb before its audit transaction commits"


def test_explicit_canonical_diagnostic_identity_survives_projection_comparison() -> None:
    assert "diagnostic_id" in inspect.signature(project_composer_operation_error).parameters
    canonical_id = "11111111-1111-4111-8111-111111111111"
    first = project_composer_operation_error(RuntimeError("PRIVATE first"), request_id="submitted-id", diagnostic_id=canonical_id)
    second = project_composer_operation_error(PermissionError("PRIVATE second"), request_id="submitted-id", diagnostic_id=canonical_id)
    assert first == second
    assert first.diagnostic_id == first.body["detail"]["diagnostic_id"] == canonical_id


def test_stop_has_reviewed_flat_body() -> None:
    projected = request_cancelled_error(request_id="submitted-id")
    assert projected.http_status == 499
    assert projected.body == {
        "error_type": "request_cancelled",
        "detail": "The composer request was stopped.",
        "request_id": "submitted-id",
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_group_integrity_priority_is_independent_of_leaf_order(reverse: bool) -> None:
    import errno

    from sqlalchemy.exc import OperationalError

    leaves = [
        OperationalError("statement", {}, RuntimeError("private")),
        OSError(errno.ENOSPC, "private"),
        AuditIntegrityError("private"),
        RuntimeError("private"),
    ]
    if reverse:
        leaves.reverse()
    projected = project_composer_operation_error(ExceptionGroup("private", leaves), request_id="owned-request")
    assert projected.error_type == "audit_integrity_error"
    assert projected.http_status == 500
    assert "private" not in projected.model_dump_json()


def test_owned_cancellation_preserves_nominal_integrity_cause() -> None:
    import asyncio

    cancellation = asyncio.CancelledError()
    cancellation.__cause__ = AuditIntegrityError("private")
    projected = project_composer_operation_error(cancellation, request_id="owned-request")
    assert projected.error_type == "audit_integrity_error"
    assert "private" not in projected.model_dump_json()


def test_cancellation_does_not_promote_unknown_cause() -> None:
    import asyncio

    cancellation = asyncio.CancelledError()
    cancellation.__cause__ = RuntimeError("private")
    projected = project_composer_operation_error(cancellation, request_id="owned-request")
    assert projected.failure_code == "operation_failed"
    assert "private" not in projected.model_dump_json()


def test_owned_settlement_cause_preserves_integrity_priority_without_message_matching() -> None:
    from elspeth.contracts.errors import ComposerOwnedSettlementFailure

    marker = ComposerOwnedSettlementFailure()
    marker.__cause__ = AuditIntegrityError("PRIVATE owned audit failure")
    projected = project_composer_operation_error(marker, request_id="submitted-id")
    assert projected.error_type == "audit_integrity_error"
    assert "PRIVATE" not in str(projected.body)


def test_owned_settlement_unknown_cause_remains_safe_generic_failure() -> None:
    from elspeth.contracts.errors import ComposerOwnedSettlementFailure

    marker = ComposerOwnedSettlementFailure()
    marker.__cause__ = RuntimeError("PRIVATE unrelated text")
    projected = project_composer_operation_error(marker, request_id="submitted-id")
    assert projected.failure_code == "operation_failed"
    assert "PRIVATE" not in str(projected.body)


def test_required_recovery_receipt_preserves_operational_storage_projection() -> None:
    from sqlalchemy.exc import OperationalError

    from elspeth.web.sessions.composer_operations import ComposerRequiredRecoveryFailure

    original = OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE driver"))
    projected = project_composer_operation_error(ComposerRequiredRecoveryFailure(original), request_id="submitted-id")
    assert projected.http_status == 503
    assert projected.error_type == "database_unavailable"
    assert projected.body == {
        "detail": "Database is currently unavailable. Please retry in a moment.",
        "error_type": "database_unavailable",
        "request_id": "submitted-id",
    }
    assert "PRIVATE" not in projected.model_dump_json()


def test_unowned_sql_wrapper_does_not_promote_operational_cause() -> None:
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    wrapper = SQLAlchemyError("PRIVATE wrapper")
    wrapper.__cause__ = OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE driver"))
    projected = project_composer_operation_error(wrapper, request_id="submitted-id")
    assert projected.http_status == 500
    assert projected.failure_code == "operation_failed"
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.asyncio
async def test_generation_handler_uses_no_sql_diagnostics(parity_app, monkeypatch) -> None:
    import elspeth.web.app as web_app

    def forbidden(*args, **kwargs):
        pytest.fail("operational generation handler performed SQL diagnostics")

    monkeypatch.setattr(web_app, "admit_pool_diagnostics", forbidden)
    monkeypatch.setattr(web_app, "database_sqlstate", forbidden)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/parity",
            "headers": [],
            "query_string": b"",
            "app": parity_app,
            "state": {"request_id": "owned-correlation"},
        }
    )
    response = await parity_app.exception_handlers[RequiredGenerationUnavailable](request, RequiredGenerationUnavailable("PRIVATE"))
    assert response.status_code == 503
    assert json.loads(response.body) == {
        "detail": _EXECUTOR_DETAIL,
        "error_type": "executor_unavailable",
        "request_id": "owned-correlation",
    }


@pytest.mark.asyncio
async def test_actual_sql_handler_retains_database_diagnosis_and_owned_diagnostics(parity_app, monkeypatch) -> None:
    import elspeth.web.app as web_app

    with parity_app.state.session_engine.connect() as conn, pytest.raises(OperationalError) as caught:
        conn.exec_driver_sql("SELECT * FROM f1_private_missing_table")
    original = caught.value
    diagnostics = []
    actual_pool = web_app.admit_pool_diagnostics
    actual_sqlstate = web_app.database_sqlstate

    def observe_pool(pool):
        diagnostics.append(("pool", pool))
        return actual_pool(pool)

    def observe_sqlstate(error):
        diagnostics.append(("sqlstate", error))
        return actual_sqlstate(error)

    monkeypatch.setattr(web_app, "admit_pool_diagnostics", observe_pool)
    monkeypatch.setattr(web_app, "database_sqlstate", observe_sqlstate)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/parity",
            "headers": [],
            "query_string": b"",
            "app": parity_app,
            "state": {"request_id": "sql-correlation"},
        }
    )
    with capture_logs() as logs:
        response = await parity_app.exception_handlers[OperationalError](request, original)
        projected = project_composer_operation_error(original, request_id="sql-correlation")
    assert response.status_code == projected.http_status == 503
    assert (
        json.loads(response.body)
        == projected.body
        == {
            "detail": "Database is currently unavailable. Please retry in a moment.",
            "error_type": "database_unavailable",
            "request_id": "sql-correlation",
        }
    )
    assert diagnostics == [("pool", parity_app.state.session_engine.pool), ("sqlstate", original)]
    assert [row["event"] for row in logs] == ["http_database_unavailable"]
    assert original.__cause__ is original.orig
    assert "f1_private_missing_table" not in response.body.decode() + repr(logs)


@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
def test_executor_projector_retains_fixed_diagnosis_without_logging(executor_type) -> None:
    original = executor_type("PRIVATE executor input")
    cause = RuntimeError("PRIVATE prior cause")
    original.__cause__ = cause
    with capture_logs() as logs:
        projected = project_composer_operation_error(original, request_id="executor-correlation")
    assert projected.http_status == 503
    assert projected.failure_code == "http_error"
    assert projected.error_type == "executor_unavailable"
    assert projected.diagnostic_id is None
    assert projected.body == {
        "detail": _EXECUTOR_DETAIL,
        "error_type": "executor_unavailable",
        "request_id": "executor-correlation",
    }
    assert original.__cause__ is cause
    assert "PRIVATE" not in projected.model_dump_json()
    assert logs == []
    if executor_type is AsyncWorkerAdmissionTimeoutError:
        assert isinstance(original, TimeoutError)


@pytest.mark.parametrize("owned_wrapper", ["settlement", "recovery", "cancellation"])
@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
def test_owned_generation_cause_preserves_fixed_projection_and_original_identity(owned_wrapper, executor_type) -> None:
    import asyncio

    from elspeth.contracts.errors import ComposerOwnedSettlementFailure
    from elspeth.web.sessions.composer_operations import ComposerRequiredRecoveryFailure

    original = executor_type("PRIVATE executor detail")
    if owned_wrapper == "settlement":
        wrapper = ComposerOwnedSettlementFailure()
        wrapper.__cause__ = original
    elif owned_wrapper == "recovery":
        wrapper = ComposerRequiredRecoveryFailure(original)
    else:
        wrapper = asyncio.CancelledError()
        wrapper.__cause__ = original
    projected = project_composer_operation_error(wrapper, request_id="owned")
    assert projected.http_status == 503 and projected.error_type == "executor_unavailable"
    assert wrapper.__cause__ is original
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.parametrize("wrapper_type", [RuntimeError, TimeoutError, ValueError])
@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
def test_unowned_generation_cause_is_never_promoted(wrapper_type, executor_type) -> None:
    original = executor_type("PRIVATE executor detail")
    wrapper = wrapper_type("PRIVATE database shutdown timeout text")
    wrapper.__cause__ = original
    projected = project_composer_operation_error(wrapper, request_id="owned")
    assert projected.http_status == 500 and projected.error_type == "operation_failed"
    assert wrapper.__cause__ is original
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("audit", [False, True])
@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
def test_generation_group_priority_is_order_independent_and_retains_original_group(reverse, audit, executor_type) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    generation = executor_type("PRIVATE")
    leaves = [generation, OSError(errno.ENOSPC, "PRIVATE"), SQLAlchemyError("PRIVATE"), RuntimeError("PRIVATE")]
    if audit:
        leaves.append(AuditIntegrityError("PRIVATE"))
    if reverse:
        leaves.reverse()
    group = ExceptionGroup("PRIVATE", leaves)
    original_leaves = group.exceptions
    projected = project_composer_operation_error(group, request_id="owned")
    assert projected.http_status == (500 if audit else 503)
    assert projected.error_type == ("audit_integrity_error" if audit else "executor_unavailable")
    assert group.exceptions is original_leaves and generation in group.exceptions
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.parametrize("executor_first", [False, True], ids=["database-first", "executor-first"])
@pytest.mark.parametrize("audit", [False, True], ids=["availability", "audit"])
@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
def test_mixed_availability_group_projects_stable_first_witness(executor_first, audit, executor_type) -> None:
    database = OperationalError("PRIVATE SQL", {}, RuntimeError("PRIVATE driver"))
    executor = executor_type("PRIVATE executor")
    leaves = [executor, database] if executor_first else [database, executor]
    if audit:
        leaves.append(AuditIntegrityError("PRIVATE audit"))
    group = ExceptionGroup("PRIVATE mixed availability", leaves)
    original_leaves = group.exceptions
    projected = project_composer_operation_error(group, request_id="mixed-correlation")
    assert projected.http_status == (500 if audit else 503)
    assert projected.error_type == (
        "audit_integrity_error" if audit else "executor_unavailable" if executor_first else "database_unavailable"
    )
    if not audit:
        assert projected.body == {
            "detail": _EXECUTOR_DETAIL if executor_first else "Database is currently unavailable. Please retry in a moment.",
            "error_type": "executor_unavailable" if executor_first else "database_unavailable",
            "request_id": "mixed-correlation",
        }
        assert projected.failure_code == "http_error" and projected.diagnostic_id is None
    assert group.exceptions is original_leaves
    assert group.exceptions[:2] == tuple(leaves[:2])
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "executor_type", [RequiredGenerationUnavailable, AsyncWorkerAdmissionTimeoutError], ids=["generation", "admission"]
)
async def test_actual_manual_executor_failure_keeps_original_custody_and_safe_presentation(
    operation_store, tmp_path, monkeypatch, executor_type
) -> None:
    import httpx

    from elspeth.web import app as app_module
    from elspeth.web import async_workers
    from elspeth.web.composer.pipeline_commit import PipelineCommitError
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions.routes.composer import pipeline_settlement
    from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app, originals
    from tests.unit.web.sessions.test_pipeline_rejection_required import prepared_rejection

    _engine, repository, _authority, _service, sid = operation_store
    expected, running = await prepared_rejection(operation_store)
    repository.release(running.session_operation_context)
    app = mounted_app(operation_store, tmp_path)
    body = PipelineCommitError("controlled candidate validation failure", code="VALIDATION_FAILED")
    original = executor_type("PRIVATE executor input")
    cause = RuntimeError("PRIVATE prior executor cause")
    original.__cause__ = cause
    reached, captured = [], []
    pending_admission = False
    actual_generation = async_workers._generation_for
    actual_admission = async_workers._acquire_admission
    actual_consume = app_module.consume_manual_proposal_failure

    async def fail_candidate(**kwargs):
        raise body

    def observe_generation(executor, ticket, finalizer):
        nonlocal pending_admission
        if ticket is not None and ticket.key.source is RequiredWorkSource.PROPOSAL_REJECTION_SQL:
            assert not reached
            reached.append(ticket)
            if executor_type is RequiredGenerationUnavailable:
                raise original
            pending_admission = True
        return actual_generation(executor, ticket, finalizer)

    async def observe_admission():
        nonlocal pending_admission
        if pending_admission:
            pending_admission = False
            raise original
        return await actual_admission()

    def consume(carrier):
        observation = actual_consume(carrier)
        captured.append(observation)
        return observation

    monkeypatch.setattr(pipeline_settlement, "prepare_pipeline_proposal_commit", fail_candidate)
    monkeypatch.setattr(async_workers, "_generation_for", observe_generation)
    monkeypatch.setattr(async_workers, "_acquire_admission", observe_admission)
    monkeypatch.setattr(app_module, "consume_manual_proposal_failure", consume)
    with capture_logs() as logs:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                f"/api/sessions/{sid}/proposals/{expected.authority.row.id}/accept",
                json={"draft_hash": expected.authority.proposal.draft_hash},
                headers={"X-Request-ID": "f1-manual-correlation"},
            )
    assert len(reached) == len(captured) == 1 and not pending_admission
    observation = captured[0]
    assert observation.selected_original is original
    assert observation.lease.closed
    observation.coordinator.assert_completed()
    assert any(leaf is body for leaf in originals(observation.original_root))
    assert any(leaf is original for leaf in originals(observation.original_root))
    assert original.__cause__ is cause
    assert response.status_code == 503
    assert response.json() == {
        "detail": _EXECUTOR_DETAIL,
        "error_type": "executor_unavailable",
        "request_id": "f1-manual-correlation",
    }
    assert "PRIVATE" not in response.text + repr(logs)
    assert not any(row["event"] == "http_database_unavailable" for row in logs)


def mutation_error():
    return ComposerMutationAuthorityRevoked(
        revocation=ComposerMutationRevocation(
            ComposerMutationCeiling("11111111-1111-4111-8111-111111111111", "request", "auto_commit"),
            "explicit_approve",
        ),
        llm_calls=(),
        failed_turn=None,
        state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        initial_version=1,
    )


def test_mutation_revocation_projects_same_safe_http_outcome():
    error = mutation_error()
    nominal = project_composer_operation_error(error, request_id="request")
    route = project_composer_operation_error(HTTPException(status_code=409, detail=error.to_payload()), request_id="request")
    assert nominal.http_status == route.http_status == 409
    assert nominal.failure_code == route.failure_code == "http_error"
    assert nominal.error_type == route.error_type == "composer_mutation_authority_revoked"
    assert nominal.body == route.body == {"detail": error.to_payload()}
    assert error.partial_state is None
    assert "proposal" not in json.dumps(nominal.body)


def test_audit_failure_still_outranks_mutation_revocation():
    error = mutation_error()
    original = AuditIntegrityError("PRIVATE original audit failure")
    projected = project_composer_operation_error(ExceptionGroup("original pair", [error, original]), request_id="request")
    assert projected.error_type == "audit_integrity_error"
    assert projected.http_status == 500
    assert "PRIVATE" not in projected.model_dump_json()


def test_mutation_refusal_roundtrips_actual_durable_error_codec():
    from elspeth.web.sessions.composer_operations import ComposerOperationError, composer_operation_result_hash

    projected = project_composer_operation_error(mutation_error(), request_id="request")
    encoded = projected.model_dump_json()
    recovered = ComposerOperationError.model_validate_json(encoded, strict=True)
    assert recovered == projected
    assert composer_operation_result_hash(encoded) == composer_operation_result_hash(recovered.model_dump_json())
    assert recovered.http_status == 409 and recovered.error_type == "composer_mutation_authority_revoked"
    assert set(recovered.body["detail"]) == {"error_type", "detail", "request_id", "initial_trust_mode", "current_trust_mode"}
    assert recovered.body["detail"]["request_id"] == "request"
