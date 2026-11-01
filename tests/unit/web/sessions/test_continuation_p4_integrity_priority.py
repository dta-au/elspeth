"""The failed-terminal selector needs the original physical P4 integrity owner."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
from elspeth.web.coordination.composer_operation_authority import _select_failure_on_connection
from elspeth.web.required_sql_outcomes import RequiredSQLRaised
from elspeth.web.sessions import continuation_p4
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.continuation import ContinuationOwnerCommit
from elspeth.web.sessions.continuation_p4 import P4WriterObservation
from elspeth.web.sessions.continuation_tool_failure import (
    _CurrentFailureSourceSelection,
    _physical_p4_integrity_failure_on_connection,
)


def _case():
    running = object()
    coordinator = SimpleNamespace(validate_compose_checkpoint_sql_outcome=lambda **kwargs: None)
    ticket = object()
    effect_key = "batch:original:p4"
    response = PrivateChunkRef(codec="composer.response.v1", digest="a" * 64, byte_length=1)
    batch = ToolBatchFrame(
        phase="effect_reconciling",
        response=response,
        accumulator=PrivateChunkRef(codec="composer.p4-pending-batch.v1", digest="b" * 64, byte_length=1),
        next_tool_index=2,
        settled_effect_ids=(),
    )
    selection = _CurrentFailureSourceSelection(None, SimpleNamespace(stack=SimpleNamespace(frames=(batch,))), (effect_key,))
    fault = OperationalError("state insert", {}, RuntimeError("physical database fault"))
    wrapper = AuditIntegrityError("audit insert failed after tool success")
    wrapper.__cause__ = fault
    prepared = object()
    observed = P4WriterObservation(
        ContinuationOwnerCommit(running, effect_key, "c" * 64, "original-checkpoint", 3),
        prepared,
        original_error=fault,
        known_not_committed=True,
    )
    owner = object.__new__(ContinuationLiveP4)
    owner.inventory = SimpleNamespace(running=running)
    owner.work = SimpleNamespace(running=running, coordinator=coordinator)
    owner.writer_observation = observed
    owner.commit = observed.commit
    owner.prepared = prepared
    owner._checkpoint_ticket = ticket
    owner.physical = RequiredSQLRaised(wrapper, ())
    owner.committed_outcome = None
    owner.publication = None
    owner.terminal_completion = None
    errors = (BaseExceptionGroup("original and retained physical failures", (wrapper, fault)),)
    return running, coordinator, selection, observed, owner, errors, wrapper


def test_synthetic_known_rollback_owner_rechecks_before_authoritative_selection(monkeypatch) -> None:
    running, coordinator, selection, observed, owner, errors, wrapper = _case()
    checked: list[object] = []

    def validate(*, ticket, actual_outcome):
        assert ticket is owner._checkpoint_ticket and actual_outcome is owner.physical
        checked.append(ticket)

    def readback(conn, witness, *, env_ref_names):
        assert conn is connection and witness is observed and env_ref_names == frozenset()
        checked.append(witness)
        return None

    coordinator.validate_compose_checkpoint_sql_outcome = validate
    connection = object()
    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", readback)
    selected = _physical_p4_integrity_failure_on_connection(
        connection,
        running,
        selection,
        required_work=coordinator,
        env_ref_names=frozenset(),
        errors=errors,
        p4_writer_observation=observed,
        p4_live_owner=owner,
    )
    assert selected is wrapper and checked == [owner._checkpoint_ticket, observed]
    public = project_composer_operation_error(selected, request_id="original-request")
    stopped = SimpleNamespace(cancel_requested_at=datetime.now(UTC), request_id="original-request")
    assert _select_failure_on_connection(stopped, public, authoritative_failure=True) is public
    assert _select_failure_on_connection(stopped, public, authoritative_failure=False).failure_code == "request_cancelled"


@pytest.mark.parametrize(
    "change",
    ("unknown", "committed", "foreign_owner", "foreign_ticket", "extra_effect", "changed_cause", "missing_original"),
)
def test_unproved_physical_p4_never_grants_authoritative_integrity(monkeypatch, change) -> None:
    running, coordinator, selection, observed, owner, errors, wrapper = _case()
    if change == "unknown":
        owner.physical = None
    elif change == "committed":
        observed.committed = True
    elif change == "foreign_owner":
        owner.inventory = SimpleNamespace(running=object())
    elif change == "foreign_ticket":
        owner._checkpoint_ticket = None
    elif change == "extra_effect":
        selection = _CurrentFailureSourceSelection(None, selection.selected, (*selection.unresolved, "tool:unknown"))
    elif change == "changed_cause":
        wrapper.__cause__ = OperationalError("other", {}, RuntimeError("different physical fault"))
    else:
        errors = (observed.original_error,)
    checked: list[object] = []

    def readback(*args, **kwargs):
        checked.append(object())
        return None

    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", readback)
    assert (
        _physical_p4_integrity_failure_on_connection(
            object(),
            running,
            selection,
            required_work=coordinator,
            env_ref_names=frozenset(),
            errors=errors,
            p4_writer_observation=observed,
            p4_live_owner=owner,
        )
        is None
    )
    assert not checked


def test_later_committed_p4_readback_refuses_prior_rollback_witness(monkeypatch) -> None:
    running, coordinator, selection, observed, owner, errors, _wrapper = _case()
    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", lambda *args, **kwargs: object())
    assert (
        _physical_p4_integrity_failure_on_connection(
            object(),
            running,
            selection,
            required_work=coordinator,
            env_ref_names=frozenset(),
            errors=errors,
            p4_writer_observation=observed,
            p4_live_owner=owner,
        )
        is None
    )
