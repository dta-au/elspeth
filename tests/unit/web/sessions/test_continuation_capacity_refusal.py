"""Capacity terminal selection preserves the original P4 SQL outcome."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.sessions import continuation_p4
from elspeth.web.sessions.continuation import ContinuationOwnerCommit
from elspeth.web.sessions.continuation_p4 import P4WriterObservation
from elspeth.web.sessions.continuation_tool_failure import (
    _CurrentFailureSourceSelection,
    _rolled_back_p4_capacity_refusal_on_connection,
)


def _case():
    running = object()
    effect_key = "batch:original:p4"
    source = PrivateChunkRef(codec="composer.response.v1", digest="a" * 64, byte_length=1)
    batch = ToolBatchFrame(
        phase="effect_reconciling",
        response=source,
        accumulator=PrivateChunkRef(codec="composer.p4-pending-batch.v1", digest="b" * 64, byte_length=1),
        next_tool_index=2,
        settled_effect_ids=(),
    )
    selected = SimpleNamespace(stack=SimpleNamespace(frames=(batch,)))
    selection = _CurrentFailureSourceSelection(None, selected, (effect_key,))
    original = ComposerContextUnsupported("retained_chunk_bytes")
    observed = P4WriterObservation(
        ContinuationOwnerCommit(running, effect_key, "c" * 64, "original-checkpoint", 3),
        object(),
        candidate=object(),
        original_error=original,
        known_not_committed=True,
    )
    return running, selection, observed, original


def test_known_rollback_witness_rechecks_p4_before_refusal(monkeypatch) -> None:
    running, selection, observed, original = _case()
    readbacks: list[P4WriterObservation] = []

    def readback(conn, witness, *, env_ref_names):
        assert conn is connection and env_ref_names == frozenset()
        readbacks.append(witness)
        return None

    connection = object()
    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", readback)
    assert (
        _rolled_back_p4_capacity_refusal_on_connection(
            connection,
            running,
            selection,
            env_ref_names=frozenset(),
            errors=(original,),
            p4_writer_observation=observed,
            stop_requested=False,
            deferred_cancellation=False,
        )
        is original
    )
    assert readbacks == [observed]


@pytest.mark.parametrize(
    "failure",
    ("unknown", "committed", "foreign", "extra_effect", "secondary", "corrupt_original", "stop", "deferred_cancel"),
)
def test_unproved_p4_or_secondary_error_never_becomes_capacity_refusal(monkeypatch, failure) -> None:
    running, selection, observed, original = _case()
    errors: tuple[BaseException, ...] = (original,)
    stop_requested = False
    deferred_cancellation = False
    if failure == "unknown":
        observed.known_not_committed = False
    elif failure == "committed":
        observed.committed = True
    elif failure == "foreign":
        running = object()
    elif failure == "extra_effect":
        selection = _CurrentFailureSourceSelection(None, selection.selected, (*selection.unresolved, "provider:unknown"))
    elif failure == "secondary":
        errors = (original, AuditIntegrityError("secondary integrity failure"))
    elif failure == "stop":
        stop_requested = True
    elif failure == "deferred_cancel":
        deferred_cancellation = True
    else:
        observed.original_error = AuditIntegrityError("persisted evidence changed")
    readbacks: list[object] = []

    def readback(*args, **kwargs):
        readbacks.append(object())
        return None

    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", readback)
    assert (
        _rolled_back_p4_capacity_refusal_on_connection(
            object(),
            running,
            selection,
            env_ref_names=frozenset(),
            errors=errors,
            p4_writer_observation=observed,
            stop_requested=stop_requested,
            deferred_cancellation=deferred_cancellation,
        )
        is None
    )
    assert not readbacks


def test_later_committed_p4_readback_cannot_use_prior_rollback_witness(monkeypatch) -> None:
    running, selection, observed, original = _case()

    def committed(*args, **kwargs):
        return object()

    monkeypatch.setattr(continuation_p4, "read_p4_transaction_outcome_on_connection", committed)
    assert (
        _rolled_back_p4_capacity_refusal_on_connection(
            object(),
            running,
            selection,
            env_ref_names=frozenset(),
            errors=(original,),
            p4_writer_observation=observed,
            stop_requested=False,
            deferred_cancellation=False,
        )
        is None
    )
