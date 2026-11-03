"""SDK DATA capacity controls; actual entry/source authority is tested separately."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.sessions.continuation import StoredContinuationSnapshot, prepare_snapshot_append
from elspeth.web.sessions.continuation_checkpoint_sdk_reservation import prepare_checkpoint_sdk_terminal_hold_plan
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import prepare_checkpoint_terminal_hold_plan
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowCost, PaidOverflowUsage
from elspeth.web.sessions.continuation_primary_paid_overflow_capacity import (
    V4PrimaryResultBatch,
    admit_v4_primary_result_batch_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import ProspectiveContinuationChunk, ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_tail import (
    SdkTerminalFamily,
    build_sdk_tail_batch,
    prepare_sdk_tail_plan,
    require_sdk_tail_batch,
)
from elspeth.web.sessions.continuation_sdk_terminal import SdkTerminalReceipt
from elspeth.web.sessions.engine import create_session_engine
from tests.unit.web.sessions.test_continuation_checkpoint_paid_tail import _source


def _case():
    admitted, identity = _source(cursor=9)
    root = decode_continuation_snapshot(admitted.payload_json)
    outer = EffectFrontierEntry(effect_id="advance-end:" + "d" * 64, disposition="dispatched", receipt_id=None)
    root = root.model_copy(update={"effect_frontier": (outer, *root.effect_frontier)})
    admitted = StoredContinuationSnapshot(encode_continuation_snapshot(root), admitted.cursor, admitted.checkpoint_id)
    plan = prepare_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=1, family=SdkTerminalFamily.CHECKPOINT_END)
    receipt = SdkTerminalReceipt(
        schema_version="composer.sdk-terminal.v1",
        identity=identity,
        producer="checkpoint_advisor",
        outcome="terminal_sdk_failure",
        observation="provider_failure",
        sdk_entered=True,
        sdk_result_observed=False,
        retryable=False,
        cost=PaidOverflowCost(status="unknown", amount=None, source="not_available"),
        usage=PaidOverflowUsage(
            status="unavailable",
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            finished_at=None,
        ),
    )
    return plan, receipt, build_sdk_tail_batch(plan, receipt=receipt, published_at=datetime.now(UTC))


def _with_append(batch, append):
    return replace(
        batch,
        append=append,
        prospective=(
            batch.prospective[0],
            *(
                ProspectiveContinuationChunk(f"event:{append.expected_cursor + index}", event.payload_json.encode(), "event.v2")
                for index, event in enumerate(append.events, 1)
            ),
            ProspectiveContinuationChunk(f"snapshot:{append.checkpoint_id}", append.snapshot_json.encode(), "snapshot.v2"),
        ),
    )


def test_sdk_exact_multi_outer_vector_accepts_distinct_original_append_ids():
    plan, receipt, batch = _case()
    other = build_sdk_tail_batch(plan, receipt=receipt, published_at=batch.append.published_at)
    assert other.append.checkpoint_id != batch.append.checkpoint_id
    assert other.append.events == batch.append.events
    assert len(batch.append.events) == 4
    assert [event.tag for event in batch.append.events] == ["checkpoint", "effect_unknown", "effect_unknown", "settled"]
    assert decode_continuation_snapshot(batch.append.snapshot_json).cursor == 13
    for value in (batch, other):
        require_sdk_tail_batch(plan, value)
    assert batch.prospective[-1].key == f"snapshot:{batch.append.checkpoint_id}"


@pytest.mark.parametrize("mutation", ["remove", "add", "reorder", "snapshot_key", "one_byte", "predecessor", "handoff"])
def test_sdk_capacity_rejects_even_resealed_changed_append_and_exact_bytes(mutation):
    plan, _receipt, batch = _case()
    if mutation == "snapshot_key":
        changed = replace(batch, prospective=(*batch.prospective[:-1], replace(batch.prospective[-1], key="snapshot:other")))
    elif mutation == "one_byte":
        changed = replace(
            batch, prospective=(replace(batch.prospective[0], payload=batch.prospective[0].payload + b" "), *batch.prospective[1:])
        )
    else:
        events = batch.append.events
        if mutation == "remove":
            events = (events[0], *events[2:])
        elif mutation == "add":
            events = (*events[:-1], events[1], events[-1])
        elif mutation == "reorder":
            events = (events[0], events[2], events[1], events[3])
        predecessor = plan.admitted
        if mutation == "predecessor":
            root = decode_continuation_snapshot(predecessor.payload_json)
            root = root.model_copy(update={"pending_settlement_ids": ()})
            predecessor = replace(predecessor, payload_json=encode_continuation_snapshot(root))
        snapshot = decode_continuation_snapshot(batch.append.snapshot_json).model_copy(update={"cursor": predecessor.cursor})
        append = prepare_snapshot_append(
            predecessor,
            snapshot_json=encode_continuation_snapshot(snapshot),
            events=events,
            published_at=batch.append.published_at,
            handoff=mutation == "handoff",
        )
        changed = _with_append(batch, append)
    with pytest.raises(AuditIntegrityError, match="exact sealed append or prospective bytes"):
        require_sdk_tail_batch(plan, changed)


def test_end_sdk_hold_keeps_exact_v2_and_refuses_unknown_recipe_or_changed_bound():
    plan, _receipt, batch = _case()
    kwargs = {"identity": plan.identity, "admitted": plan.admitted, "phase": "end", "creator_sequence": 1, "requested_model": "test/model"}
    old = prepare_checkpoint_terminal_hold_plan(**kwargs)
    current = prepare_checkpoint_sdk_terminal_hold_plan(**kwargs)
    assert current.contract_sha256 != old.contract_sha256
    assert current.reserved_bytes == max(old.reserved_bytes, plan.reserved_bytes)
    assert prepare_checkpoint_sdk_terminal_hold_plan(**kwargs, historical_contract_sha256=old.contract_sha256) == old
    assert prepare_checkpoint_sdk_terminal_hold_plan(**kwargs, historical_contract_sha256=current.contract_sha256) == current
    with pytest.raises(AuditIntegrityError, match="unknown historical"):
        prepare_checkpoint_sdk_terminal_hold_plan(**kwargs, historical_contract_sha256="0" * 64)
    with pytest.raises(AuditIntegrityError, match="exact original recipe"):
        require_sdk_tail_batch(replace(plan, reserved_bytes=plan.reserved_bytes - 1), batch)
    early = {**kwargs, "phase": "early"}
    assert prepare_checkpoint_sdk_terminal_hold_plan(**early) == prepare_checkpoint_terminal_hold_plan(**early)
    with pytest.raises(AuditIntegrityError, match="uninstalled SDK"):
        prepare_checkpoint_sdk_terminal_hold_plan(**early, historical_contract_sha256=current.contract_sha256)


def test_primary_capacity_still_rejects_checkpoint_event_count():
    plan, _receipt, tail = _case()
    identity = plan.identity
    source = ProviderTerminalHoldSource(
        session_id=identity.session_id,
        operation_id=identity.operation_id,
        effect_key=identity.effect_key,
        policy_sha256=identity.policy_sha256,
        allowance_bytes=100_000,
        source_checkpoint_id=identity.source_checkpoint_id,
        source_cursor=identity.source_cursor,
        source_snapshot_sha256=identity.source_snapshot_sha256,
        source_input_sha256="e" * 64,
        creator_sequence=1,
        creator_generation=identity.ownership_generation,
        claim_attempt=1,
        session_operation_id=identity.session_operation_id,
        session_operation_epoch=identity.session_operation_epoch,
        lease_sha256=identity.lease_sha256,
        reserved_bytes=plan.reserved_bytes,
        fallback_contract_sha256=plan.contract_sha256,
    )
    batch = V4PrimaryResultBatch(source, plan.admitted, (tail.prospective[0],), tail.append, tail.prospective)
    engine = create_session_engine("sqlite://")
    try:
        with (
            engine.begin() as conn,
            pytest.raises(AuditIntegrityError, match="closed complete or terminal shape"),
            admit_v4_primary_result_batch_on_connection(conn, batch=batch),
        ):
            pytest.fail("PRIMARY accepted checkpoint's longer SDK tail")
    finally:
        engine.dispose()
