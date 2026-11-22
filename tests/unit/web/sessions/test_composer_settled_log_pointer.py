"""The settled log line points at the private failure facts of a failed operation.

A failed composer operation records its exception graph in a private
``role='audit'`` row; the 500 body says only that the audit trail could not be
verified. These tests pin the pointer the log carries to that row: the ids that
find it, and for an audit-integrity failure the leaf's class and text, never
more than the row may retain and never twice.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from elspeth.contracts.credential_material import REDACTED_CREDENTIAL_TEXT
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import composer_async_worker as worker_module
from elspeth.web.sessions.composer_operation_errors import operation_failure_diagnostic_id
from tests.unit.web.sessions.test_composer_async_worker import _admit, _file_app, _worker


class _RecordingLog:
    """Stands in for the worker's structlog logger and keeps every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.calls.append(("info", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.calls.append(("error", event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.calls.append(("warning", event, fields))

    def named(self, event: str) -> list[tuple[str, dict[str, object]]]:
        return [(level, fields) for level, name, fields in self.calls if name == event]


async def _fail_one_operation(tmp_path, monkeypatch, failure: BaseException):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    log = _RecordingLog()

    async def compose(*args, **kwargs):
        raise failure

    monkeypatch.setattr(composer, "compose", compose)
    monkeypatch.setattr(worker_module, "slog", log)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        await asyncio.wait_for(worker.run_until_idle(), timeout=30)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
    finally:
        engine.dispose()
    return record, terminal, log


@pytest.mark.asyncio
async def test_integrity_failure_logs_ids_and_leaf_once(tmp_path, monkeypatch) -> None:
    record, terminal, log = await _fail_one_operation(
        tmp_path, monkeypatch, AuditIntegrityError("Graph review receipt does not match its base")
    )
    assert terminal is not None and terminal.status == "failed"
    support_id = operation_failure_diagnostic_id(session_id=str(record.session_id), operation_id=record.operation_id)

    settled = log.named("composer_operation.settled")
    assert len(settled) == 1, "the settled line is written exactly once"
    level, fields = settled[0]
    assert level == "info"
    assert fields["session_id"] == str(record.session_id)
    assert fields["operation_id"] == record.operation_id
    assert fields["diagnostic_id"] == support_id
    assert fields["status"] == "failed"

    integrity = log.named("composer_operation.audit_integrity_failure")
    assert len(integrity) == 1, "one error line per failed operation, not one per leaf or per retry"
    level, fields = integrity[0]
    assert level == "error"
    assert fields["session_id"] == str(record.session_id)
    assert fields["operation_id"] == record.operation_id
    assert fields["diagnostic_id"] == support_id
    assert fields["leaf_class"] == "AuditIntegrityError"
    assert fields["leaf_message"] == "Graph review receipt does not match its base"
    assert fields["integrity_leaf_count"] == 1


@pytest.mark.asyncio
async def test_integrity_failure_text_is_credential_filtered(tmp_path, monkeypatch) -> None:
    candidate = "sk-" + "a" * 24
    _record, _terminal, log = await _fail_one_operation(
        tmp_path, monkeypatch, AuditIntegrityError(f"Provider rejected key {candidate} during audit")
    )
    ((_level, fields),) = log.named("composer_operation.audit_integrity_failure")
    assert fields["leaf_message"] == REDACTED_CREDENTIAL_TEXT
    assert all(candidate not in repr(call) for call in log.calls)


@pytest.mark.asyncio
async def test_integrity_failure_text_over_the_bound_is_omitted(tmp_path, monkeypatch) -> None:
    long_text = "receipt drift " + "x" * 600
    _record, _terminal, log = await _fail_one_operation(tmp_path, monkeypatch, AuditIntegrityError(long_text))
    ((_level, fields),) = log.named("composer_operation.audit_integrity_failure")
    assert fields["leaf_class"] == "AuditIntegrityError"
    assert fields["leaf_message"] is None
    assert all(long_text not in repr(call) for call in log.calls)


@pytest.mark.asyncio
async def test_ordinary_failure_logs_the_pointer_but_no_integrity_line(tmp_path, monkeypatch) -> None:
    record, terminal, log = await _fail_one_operation(tmp_path, monkeypatch, RuntimeError("ordinary fault"))
    assert terminal is not None and terminal.status == "failed"
    ((_level, fields),) = log.named("composer_operation.settled")
    assert fields["operation_id"] == record.operation_id
    assert log.named("composer_operation.audit_integrity_failure") == []
    assert all("ordinary fault" not in repr(call) for call in log.calls)


@pytest.mark.asyncio
async def test_integrity_failure_held_only_by_required_work_is_the_one_logged(tmp_path, monkeypatch) -> None:
    """The public failure can come from a required-work receipt, not the turn's exception.

    The turn ends with an ordinary error while a ticket holds the integrity
    error; ``_failure_for`` projects the receipt, so the log names that leaf.
    """
    app, service, engine, _composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    log = _RecordingLog()
    body_error = RuntimeError("ordinary body fault")
    ticket_error = AuditIntegrityError("Required work witness disagrees with its ticket")
    owners = []

    async def fail_with_ticket_integrity(services, running, lease, settlement, setup_ticket):
        owners.append(asyncio.current_task())
        assert setup_ticket is not None
        setup_ticket.complete_owned(ticket_error)
        settlement.failure_attempted = True
        settlement.terminal_receipt = await worker._settle_failure(services, running, body_error)
        raise body_error

    monkeypatch.setattr(worker_module, "slog", log)
    monkeypatch.setattr(worker, "_run_started_under_lease", fail_with_ticket_integrity)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        assert await worker._claim_once() == 1
        async with asyncio.timeout(10):
            while not owners:
                await asyncio.sleep(0.001)
            await asyncio.gather(owners[0], return_exceptions=True)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
    finally:
        engine.dispose()
    assert terminal is not None and terminal.status == "failed"
    assert terminal.result_json is not None and "audit_integrity" in terminal.result_json
    ((_level, fields),) = log.named("composer_operation.audit_integrity_failure")
    assert fields["leaf_message"] == "Required work witness disagrees with its ticket"
    assert fields["integrity_leaf_count"] == 1
    assert len(log.named("composer_operation.settled")) == 1
