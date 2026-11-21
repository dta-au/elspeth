"""An event-loop stall longer than the session lease leaves the composer owner unsettled.

Gold-3 battery, edits 44 and 51-57: synchronous work on the event loop before
the END Advisor call (about 14-20 s at that session's size) kept the 30 s
COMPOSE lease's 10 s renewal from running. The lease lapsed, the turn could
write nothing more, the owner Task ended with an exception group and no
terminal, and the reaper settled the row. These tests drive the same sequence
with a 3 s lease and a 4.5 s blocking stall inside the composer call.
"""

from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

import pytest

from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions import composer_async_worker as worker_module
from elspeth.web.sessions.composer_operation_errors import operation_failure_diagnostic_id
from tests.unit.web.sessions.test_composer_async_worker import _admit, _BudgetedComposer, _file_app, _worker
from tests.unit.web.sessions.test_composer_settled_log_pointer import _RecordingLog

_LEASE_SECONDS = 3
_STALL_SECONDS = 4.5


class _LoopStallingComposer(_BudgetedComposer):
    """Holds the event loop the way a long synchronous prompt render does."""

    async def compose(self, *args, **kwargs):
        time.sleep(_STALL_SECONDS)
        return await super().compose(*args, **kwargs)


class _AwaitingComposer(_BudgetedComposer):
    """The same delay, awaited: the loop stays free to renew the lease."""

    async def compose(self, *args, **kwargs):
        await asyncio.sleep(_STALL_SECONDS)
        return await super().compose(*args, **kwargs)


async def _stall_one_operation(tmp_path, monkeypatch, composer_type=_LoopStallingComposer):
    app, service, engine, _composer = _file_app(tmp_path)
    app.state.composer_service = composer_type()
    # The production default is 30 s renewed every 10 s; scaled down, same shape.
    service._session_operation_lease_seconds = _LEASE_SECONDS
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id, claim_lease_seconds=30
    )
    worker = _worker(app, authority)
    log = _RecordingLog()
    monkeypatch.setattr(worker_module, "slog", log)
    try:
        record = await _admit(app, service, authority, operation_id=str(uuid4()))
        await asyncio.wait_for(worker.run_until_idle(), timeout=60)
        terminal = authority.get(session_id=record.session_id, operation_id=record.operation_id)
    finally:
        engine.dispose()
    return record, terminal, log


@pytest.mark.asyncio
async def test_a_stall_past_the_lease_ends_the_owner_unsettled_and_the_reaper_settles(tmp_path, monkeypatch) -> None:
    _record, terminal, log = await _stall_one_operation(tmp_path, monkeypatch)
    unsettled = log.named("composer_operation.owner_unsettled")
    assert len(unsettled) == 1, log.calls
    level, fields = unsettled[0]
    assert level == "error"
    assert fields["exc_class"] in ("BaseExceptionGroup", "ExceptionGroup")
    assert log.named("composer_operation.settled") == [], "the owner never wrote its terminal"
    assert terminal is not None and terminal.status == "failed"
    assert terminal.settled_by == "settle_lost", "the reaper, not the owner, settled the operation"
    assert terminal.session_operation_id is not None


@pytest.mark.asyncio
async def test_a_live_owner_that_lost_its_lease_is_not_reported_as_a_stopped_server(tmp_path, monkeypatch) -> None:
    """The reaper settles it, in the process that saw the owner end: the server is running."""
    record, terminal, _log = await _stall_one_operation(tmp_path, monkeypatch)
    assert terminal is not None and terminal.status == "failed" and terminal.settled_by == "settle_lost"
    assert terminal.failure_code == "operation_failed"
    assert terminal.result_json is not None
    result = json.loads(terminal.result_json)
    support_id = operation_failure_diagnostic_id(session_id=str(record.session_id), operation_id=record.operation_id)
    assert result["diagnostic_id"] == support_id, "the support id names the owner_unsettled log line"
    detail = result["body"]["detail"]
    assert detail["error_type"] == "operation_failed" and detail["diagnostic_id"] == support_id
    assert "too busy to keep its hold on the session" in detail["detail"]
    assert "server stopped" not in terminal.result_json


@pytest.mark.asyncio
async def test_the_same_delay_awaited_keeps_the_lease_and_completes(tmp_path, monkeypatch) -> None:
    """Control: the turn outlasting its lease is not the cause; the blocked loop is."""
    _record, terminal, log = await _stall_one_operation(tmp_path, monkeypatch, _AwaitingComposer)
    assert log.named("composer_operation.owner_unsettled") == []
    assert terminal is not None and terminal.status == "completed" and terminal.settled_by == "owner_terminal"


@pytest.mark.asyncio
async def test_the_unsettled_owner_line_names_the_group_leaves(tmp_path, monkeypatch) -> None:
    record, _terminal, log = await _stall_one_operation(tmp_path, monkeypatch)
    ((_level, fields),) = log.named("composer_operation.owner_unsettled")
    assert fields["session_id"] == str(record.session_id)
    assert fields["operation_id"] == record.operation_id
    assert fields["diagnostic_id"] == operation_failure_diagnostic_id(session_id=str(record.session_id), operation_id=record.operation_id)
    leaves = fields["leaves"]
    assert type(leaves) is list and 0 < len(leaves) <= fields["leaf_count"]
    assert len(leaves) <= worker_module._OWNER_UNSETTLED_LOGGED_LEAVES
    classes = {leaf["class"] for leaf in leaves}
    assert "SessionOperationFenceLost" in classes, leaves
    fence = next(leaf for leaf in leaves if leaf["class"] == "SessionOperationFenceLost")
    assert fence["message"] is not None and fence["message"].startswith("session operation authority lost: ")
    for leaf in leaves:
        assert set(leaf) == {"class", "message", "cause_class"}
        assert leaf["message"] is None or len(leaf["message"].encode("utf-8")) <= 512


def test_owner_leaf_fields_bound_and_filter_text() -> None:
    from elspeth.contracts.credential_material import REDACTED_CREDENTIAL_TEXT

    inner = ValueError("x" * 600)
    secret = RuntimeError("Provider rejected key sk-" + "a" * 24)
    caused = KeyError("missing")
    caused.__cause__ = OSError("disk")
    group = BaseExceptionGroup("outer", [inner, BaseExceptionGroup("nested", [secret, caused]), inner])
    leaves = worker_module._owner_failure_leaves(group)
    assert leaves == (inner, secret, caused), "depth first, in order, each leaf once"
    fields = [worker_module._owner_failure_leaf_fields(leaf) for leaf in leaves]
    assert fields[0] == {"class": "ValueError", "message": None, "cause_class": None}
    assert fields[1]["message"] == REDACTED_CREDENTIAL_TEXT
    assert fields[2] == {"class": "KeyError", "message": "missing", "cause_class": "OSError"}
