"""Current worker admission refusals keep their real quota owner and safe terminal.

The prior V1 route handlers are not called by Continuation. These tests use the
production app, admission route, worker, quota authority and terminal writer.
Only the external provider/guard boundaries use the canonical test harness.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import OperationalError

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionRefused,
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.models import (
    composer_async_operations_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.schemas import ComposerOperationFailurePageResponse
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import ACTOR, continuation_app, install_sdk, submit_and_settle


@pytest.mark.parametrize(
    ("reason", "disposition", "expected_http", "expected_code"),
    (
        (AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE, QuotaDisposition.ACCOUNTING_UNAVAILABLE, 503, "token_accounting_unavailable"),
        (AdmissionRefusalReason.QUOTA_EXCEEDED, QuotaDisposition.EXCEEDED, 403, "admission_refused"),
    ),
)
def test_chargeable_refusal_projector_uses_owned_reason_and_keeps_integrity_priority(
    reason: AdmissionRefusalReason, disposition: QuotaDisposition, expected_http: int, expected_code: str
) -> None:
    private_id = "private-quota-policy-123"
    private_hash = "a" * 64
    measured = disposition is QuotaDisposition.EXCEEDED
    refusal = ChargeableAdmissionRefused(
        ChargeableAdmissionDecision(
            refusal_reason=reason,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id=private_id,
                container_policy_id=None,
                quota_disposition=disposition,
                secret_wiring_hash=private_hash,
                dimension="tokens" if measured else None,
                cap=1_000 if measured else None,
                usage=1_000 if measured else None,
            ),
        )
    )
    selected = project_composer_operation_error(refusal, request_id="safe-request")
    assert selected.http_status == expected_http and selected.failure_code == "http_error"
    assert selected.error_type == "composer_admission_refused" and selected.diagnostic_id is None
    assert selected.body["error_type"] == "composer_admission_refused"
    assert selected.body["failure_code"] == expected_code
    assert selected.body["request_id"] == "safe-request"
    assert private_id not in str(selected.body) and private_hash not in str(selected.body)

    # Text alone cannot impersonate the owned refusal, and an SQL fault keeps
    # its higher-priority public classification even when the refusal survives.
    impostor = project_composer_operation_error(RuntimeError(str(refusal)), request_id="safe-request")
    assert impostor.error_type == "operation_failed"
    fault = OperationalError("SELECT private", {}, RuntimeError("private database detail"))
    grouped = project_composer_operation_error(ExceptionGroup("admission recording fault", [refusal, fault]), request_id="safe-request")
    assert grouped.http_status == 503 and grouped.error_type == "database_unavailable"
    assert "private" not in str(grouped.body)


async def _leave_actual_unknown_attempt(harness) -> str:
    """A prior provider admission with no terminal usage, as after process loss."""
    service = harness.app.state.session_service
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=harness.session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    async with lease:
        attempt = await service.begin_provider_attempt(session_operation_context=lease.context, source="composer")
    return attempt.attempt_id


def _spend_existing_allowance(harness) -> str:
    """An already durable, accountable daily spend at the identity cap."""
    entry_id = str(uuid4())
    with harness.app.state.session_engine.begin() as conn:
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id=entry_id,
                identity_id=ACTOR,
                source="composer",
                session_id=str(harness.session_id),
                run_id=None,
                model="test/model",
                prompt_tokens=1_000,
                completion_tokens=0,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                recorded_at=datetime.now(UTC),
            )
        )
    return entry_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_http", "expected_reason"),
    (
        ("unknown_usage", 503, "token_accounting_unavailable"),
        ("quota_exceeded", 403, "admission_refused"),
    ),
)
async def test_actual_worker_chargeable_refusal_keeps_original_durable_owner_and_no_sdk(
    tmp_path, monkeypatch, case: str, expected_http: int, expected_reason: str
) -> None:
    sdk_calls: list[object] = []

    async def unexpected_sdk(**kwargs: object) -> None:
        sdk_calls.append(kwargs)
        raise AssertionError("a refused chargeable attempt reached the physical SDK")

    install_sdk(monkeypatch, unexpected_sdk)
    # The shared harness seeds the active container ceiling; configuring one at
    # startup would provision a second row in the same unique container slot.
    async with continuation_app(
        tmp_path,
        monkeypatch,
        quota_default_tokens_per_day=1_000,
        quota_default_storage_bytes=1_000_000,
    ) as harness:
        previous_attempt_id = await _leave_actual_unknown_attempt(harness) if case == "unknown_usage" else None
        existing_ledger_id = _spend_existing_allowance(harness) if case == "quota_exceeded" else None
        operation_id = str(uuid4())
        snapshot = await submit_and_settle(
            harness,
            message_body("Explain a CSV to JSONL pipeline.", operation_id=operation_id),
        )
        assert snapshot.status == "failed" and snapshot.result is None
        error = snapshot.error
        assert error is not None
        assert error.http_status == expected_http and error.failure_code == "http_error"
        assert error.error_type == "composer_admission_refused"
        assert error.body["error_type"] == "composer_admission_refused"
        assert error.body["failure_code"] == expected_reason
        assert error.body["request_id"] is not None
        assert sdk_calls == []

        with harness.app.state.session_engine.connect() as conn:
            terminal = conn.execute(
                select(composer_async_operations_table).where(
                    composer_async_operations_table.c.session_id == str(harness.session_id),
                    composer_async_operations_table.c.operation_id == operation_id,
                )
            ).one()
            attempts = conn.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == str(harness.session_id))
            ).all()
            ledger = conn.execute(
                select(token_usage_ledger_table).where(token_usage_ledger_table.c.session_id == str(harness.session_id))
            ).all()
        assert terminal.status == "failed" and terminal.failure_code == "http_error"
        assert ComposerOperationError.model_validate_json(terminal.result_json, strict=True) == error
        assert all(row.operation_id != operation_id for row in attempts)
        if previous_attempt_id is not None:
            assert len(attempts) == 1 and attempts[0].attempt_id == previous_attempt_id
            assert attempts[0].settled_at is None and attempts[0].ledger_entry_id is None
            assert ledger == []
        else:
            assert attempts == []
            assert len(ledger) == 1 and ledger[0].entry_id == existing_ledger_id
            assert ledger[0].prompt_tokens == 1_000 and ledger[0].completion_tokens == 0
        public = str(error.body)
        for private in (ACTOR, "quota-identity", "quota-container", previous_attempt_id, existing_ledger_id):
            if private is not None:
                assert private not in public

        # This is the actual authenticated history route after a worker-origin
        # refusal, not a projector-shaped substitute for the browser's input.
        async with harness.client() as client:
            response = await client.get(f"/api/sessions/{harness.session_id}/operations", params={"operation_id": operation_id})
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        history = ComposerOperationFailurePageResponse.model_validate_json(response.content, strict=True)
        assert history.session_id == harness.session_id and history.next_cursor is None
        assert len(history.items) == 1
        outcome = history.items[0]
        assert str(outcome.operation_id) == operation_id
        assert outcome.error_type == error.error_type == "composer_admission_refused"
        assert outcome.failure_code == error.failure_code == "http_error"
        assert outcome.diagnostic_id is None and outcome.request_id == error.body["request_id"]
        assert outcome.saved_revisions.attribution == "fenced" and outcome.saved_revisions.count == 0
        assert sdk_calls == []

        # Freeze the full producer payload for the real TypeScript decoder and
        # history client. Normalize only the generated identities and wall
        # clocks; schema, status/revision, public body and saved facts stay exact.
        captured = {"terminal": json.loads(snapshot.model_dump_json()), "history": json.loads(history.model_dump_json())}
        fixed_session = "11111111-1111-4111-8111-111111111111"
        fixed_operation = "22222222-2222-4222-8222-222222222222"
        fixed_user = "33333333-3333-4333-8333-333333333333"
        fixed_time = "2026-11-02T00:00:00Z"
        captured["terminal"]["operation_id"] = fixed_operation
        captured["terminal"]["created_at"] = captured["terminal"]["observed_at"] = fixed_time
        captured["terminal"]["error"]["body"]["request_id"] = "safe-request"
        captured["history"]["session_id"] = fixed_session
        item = captured["history"]["items"][0]
        item["session_id"], item["operation_id"] = fixed_session, fixed_operation
        if item["user_message_id"] is not None:
            item["user_message_id"] = fixed_user
        item["settled_at"], item["request_id"] = fixed_time, "safe-request"
        fixture_path = Path(__file__).resolve().parents[3] / "fixtures/web/composer/composer_admission_refusal_producer.json"
        if os.environ.get("ELSPETH_UPDATE_ADMISSION_REFUSAL_FIXTURE") == "1":
            fixtures = json.loads(fixture_path.read_text()) if fixture_path.exists() else {}
            fixtures[case] = captured
            fixture_path.write_text(json.dumps(fixtures, indent=2) + "\n")
        assert json.loads(fixture_path.read_text())[case] == captured
