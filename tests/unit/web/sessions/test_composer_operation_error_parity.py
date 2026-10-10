from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest
from fastapi import Request
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.secrets import FingerprintKeyMissingError, SecretDecryptionError
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error, request_cancelled_error
from elspeth.web.sessions.protocol import StaleComposeStateError


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
        "detail": "Database is currently unavailable. Please retry in a moment.",
        "error_type": "database_unavailable",
        "request_id": "owned-correlation",
    }


@pytest.mark.parametrize("owned_wrapper", ["settlement", "recovery", "cancellation"])
def test_owned_generation_cause_preserves_fixed_projection_and_original_identity(owned_wrapper) -> None:
    import asyncio

    from elspeth.contracts.errors import ComposerOwnedSettlementFailure
    from elspeth.web.sessions.composer_operations import ComposerRequiredRecoveryFailure

    original = RequiredGenerationUnavailable("PRIVATE generation detail")
    if owned_wrapper == "settlement":
        wrapper = ComposerOwnedSettlementFailure()
        wrapper.__cause__ = original
    elif owned_wrapper == "recovery":
        wrapper = ComposerRequiredRecoveryFailure(original)
    else:
        wrapper = asyncio.CancelledError()
        wrapper.__cause__ = original
    projected = project_composer_operation_error(wrapper, request_id="owned")
    assert projected.http_status == 503 and projected.error_type == "database_unavailable"
    assert wrapper.__cause__ is original
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.parametrize("wrapper_type", [RuntimeError, TimeoutError, ValueError])
def test_unowned_generation_cause_is_never_promoted(wrapper_type) -> None:
    original = RequiredGenerationUnavailable("PRIVATE generation detail")
    wrapper = wrapper_type("PRIVATE database shutdown timeout text")
    wrapper.__cause__ = original
    projected = project_composer_operation_error(wrapper, request_id="owned")
    assert projected.http_status == 500 and projected.error_type == "operation_failed"
    assert wrapper.__cause__ is original
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("audit", [False, True])
def test_generation_group_priority_is_order_independent_and_retains_original_group(reverse, audit) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    generation = RequiredGenerationUnavailable("PRIVATE")
    leaves = [generation, OSError(errno.ENOSPC, "PRIVATE"), SQLAlchemyError("PRIVATE"), RuntimeError("PRIVATE")]
    if audit:
        leaves.append(AuditIntegrityError("PRIVATE"))
    if reverse:
        leaves.reverse()
    group = ExceptionGroup("PRIVATE", leaves)
    original_leaves = group.exceptions
    projected = project_composer_operation_error(group, request_id="owned")
    assert projected.http_status == (500 if audit else 503)
    assert projected.error_type == ("audit_integrity_error" if audit else "database_unavailable")
    assert group.exceptions is original_leaves and generation in group.exceptions
    assert "PRIVATE" not in projected.model_dump_json()
