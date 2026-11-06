"""Real V4 classifier no-SDK terminal keeps its two-frame source and zero-use cohort."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from pydantic import ValidationError
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimaryProvider, V4ClassifierProviderAdmission
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import (
    ClassifierNoSdkReceiptData,
    ClassifierNoSdkRefusalReceipt,
    read_classifier_no_sdk_refusal_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.sessions.test_continuation_classifier_primary import _invoke
from tests.unit.web.sessions.test_continuation_classifier_primary import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_classifier_primary_v4_paid import _v4_classifier_case

operation_store = _operation_store


@pytest.mark.asyncio
async def test_v4_classifier_pre_sdk_refusal_retains_original_two_frames_and_zero_use(operation_store, tmp_path, monkeypatch):
    async with _v4_classifier_case(operation_store, tmp_path, monkeypatch) as (store, case):
        _phases, running, runtime, issuer, prepared, prior_sdk = case
        assert type(runtime.source.policy) is ContinuationAdmissionPolicyV4 and len(prior_sdk) == 1
        further_sdk, actual_custodies, verified_handoffs = [], [], []

        async def unexpected_sdk(**_kwargs):
            further_sdk.append(1)
            raise AssertionError("classifier SDK must remain unentered")

        original = ProviderCallCustody.require_cost_dispatch
        original_validation = RequiredWorkCoordinator.validate_undispatched_cancellation_sql_outcome

        def expire_after_admission(custody):
            if custody.continuation_classifier_v4_admission is not None:
                actual_custodies.append(custody)
                raise TimeoutError("injected after original classifier H, before SDK")
            return original(custody)

        def verify_original_handoff(coordinator, *, ticket, actual_outcome):
            if coordinator is issuer.work.coordinator:
                assert ticket.key.source is RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL
                with pytest.raises(AuditIntegrityError, match="Undispatched cancellation replaced actual finish-once carrier"):
                    original_validation(coordinator, ticket=ticket, actual_outcome=replace(actual_outcome))
                verified_handoffs.append((ticket, actual_outcome))
            return original_validation(coordinator, ticket=ticket, actual_outcome=actual_outcome)

        monkeypatch.setattr(litellm, "acompletion", unexpected_sdk)
        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", expire_after_admission)
        monkeypatch.setattr(RequiredWorkCoordinator, "validate_undispatched_cancellation_sql_outcome", verify_original_handoff)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is ClassifierNoSdkRefusalReceipt and not further_sdk and len(actual_custodies) == len(verified_handoffs) == 1
        original_provider = actual_custodies[0].continuation_classifier_v4
        admission = actual_custodies[0].continuation_classifier_v4_admission
        assert type(original_provider) is PreparedClassifierPrimaryProvider and type(admission) is V4ClassifierProviderAdmission
        with store[0].connect() as conn:
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(running.claim.session_id),
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.effect_key == result.effect_key,
                )
            ).one()
            hold = conn.execute(
                select(holds).where(
                    holds.c.session_id == str(running.claim.session_id),
                    holds.c.operation_id == running.claim.operation_id,
                    holds.c.effect_key == result.effect_key,
                )
            ).one()
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
            ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
            receipt_row = conn.execute(
                select(chunks).where(
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == result.result_chunk_key,
                )
            ).one()
            prior_row = conn.execute(
                select(checkpoints).where(
                    checkpoints.c.session_id == str(running.claim.session_id),
                    checkpoints.c.operation_id == running.claim.operation_id,
                    checkpoints.c.cursor == hold.source_cursor - 1,
                )
            ).one()
            prior_payload = conn.execute(
                select(chunks.c.payload).where(
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == prior_row.snapshot_chunk_key,
                )
            ).scalar_one()
            admitted_row = conn.execute(
                select(checkpoints).where(
                    checkpoints.c.session_id == str(running.claim.session_id),
                    checkpoints.c.operation_id == running.claim.operation_id,
                    checkpoints.c.checkpoint_id == admission.dispatched.checkpoint_id,
                )
            ).one()
            terminal_row = conn.execute(
                select(checkpoints).where(
                    checkpoints.c.session_id == str(running.claim.session_id),
                    checkpoints.c.operation_id == running.claim.operation_id,
                    checkpoints.c.checkpoint_id == result.refused.checkpoint_id,
                )
            ).one()
            appended_rows = conn.execute(
                select(checkpoints.c.cursor, checkpoints.c.checkpoint_id).where(
                    checkpoints.c.session_id == str(running.claim.session_id),
                    checkpoints.c.operation_id == running.claim.operation_id,
                    checkpoints.c.cursor > admission.dispatched.cursor,
                    checkpoints.c.cursor <= result.refused.cursor,
                )
            ).all()
            tail_events = conn.execute(
                select(events)
                .where(
                    events.c.session_id == str(running.claim.session_id),
                    events.c.operation_id == running.claim.operation_id,
                    events.c.sequence > admission.dispatched.cursor,
                    events.c.sequence <= result.refused.cursor,
                )
                .order_by(events.c.sequence)
            ).all()
            refused_event_chunk = conn.execute(
                select(chunks).where(
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == tail_events[0].payload_chunk_key,
                )
            ).one()
            cold = read_classifier_no_sdk_refusal_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=result.effect_key,
            )
        assert cold == result
        receipt_data = ClassifierNoSdkReceiptData.model_validate_json(receipt_row.payload, strict=True)
        assert (hold.source_checkpoint_id, hold.source_cursor, hold.source_snapshot_sha256) == (
            original_provider.prepared.snapshot.checkpoint_id,
            original_provider.prepared.snapshot.cursor,
            sha256(original_provider.prepared.snapshot.payload_json.encode("utf-8")).hexdigest(),
        )
        assert admitted_row.kind == terminal_row.kind == "transition"
        assert admitted_row.cursor == hold.source_cursor + 1
        assert (receipt_data.source_checkpoint_id, receipt_data.source_cursor) == (admitted_row.checkpoint_id, admitted_row.cursor)
        assert terminal_row.cursor == admitted_row.cursor + 2
        assert appended_rows == [(terminal_row.cursor, terminal_row.checkpoint_id)]
        assert [(row.sequence, row.tag) for row in tail_events] == [
            (admitted_row.cursor + 1, "effect_refused"),
            (admitted_row.cursor + 2, "settled"),
        ]
        assert receipt_row.codec == "composer.classifier-primary-no-sdk-refusal.v1"
        original_chunk = encode_private_chunk(codec=receipt_row.codec, canonical=receipt_row.payload.decode("utf-8"))
        verify_private_chunk(original_chunk.reference, original_chunk.canonical)
        assert original_chunk.reference == PrivateChunkRef.model_validate_json(original_chunk.reference.model_dump_json())
        with pytest.raises(ValidationError):
            PrivateChunkRef.model_validate(
                {**original_chunk.reference.model_dump(), "codec": "composer.classifier-primary-no-sdk-refusal.v2"}
            )
        assert CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES == 1_048_576
        with pytest.raises(AuditIntegrityError, match="complete JSON admission bound"):
            encode_private_chunk(
                codec=receipt_row.codec,
                canonical=canonical_json({"padding": "x" * CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES}),
            )
        assert effect.disposition == "refused" and effect.provider_attempt_id == attempt.attempt_id
        assert hold.state == "consumed" and attempt.settled_at is not None
        assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) == (0, 0, 0, 0)
        root = decode_continuation_snapshot(result.refused.payload_json)
        assert len(root.frames) == 2 and root.frames[0].frame_kind == "compose" and root.frames[1].frame_kind == "tool_batch"
        with store[0].connect() as conn:
            transaction = conn.begin()
            try:

                def read_original():
                    return read_classifier_no_sdk_refusal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=result.effect_key,
                    )

                for head in (admitted_row, terminal_row):
                    savepoint = conn.begin_nested()
                    try:
                        changed = conn.execute(
                            update(checkpoints)
                            .where(
                                checkpoints.c.session_id == str(running.claim.session_id),
                                checkpoints.c.operation_id == running.claim.operation_id,
                                checkpoints.c.checkpoint_id == head.checkpoint_id,
                            )
                            .values(kind="handoff")
                        )
                        assert changed.rowcount == 1
                        with pytest.raises(AuditIntegrityError, match="original dispatch and terminal heads"):
                            read_original()
                    finally:
                        savepoint.rollback()
                    assert read_original() == result

                foreign_receipt = canonical_json(
                    receipt_data.model_copy(
                        update={"source_checkpoint_id": hold.source_checkpoint_id, "source_cursor": hold.source_cursor}
                    ).model_dump(mode="json")
                ).encode("utf-8")
                foreign_event = canonical_json(
                    {**json.loads(refused_event_chunk.payload), "effect_key": root.frames[0].provider_effect_key}
                ).encode("utf-8")
                assert root.frames[0].provider_effect_key != result.effect_key
                mutations = (
                    (
                        update(holds)
                        .where(
                            holds.c.session_id == str(running.claim.session_id),
                            holds.c.operation_id == running.claim.operation_id,
                            holds.c.effect_key == result.effect_key,
                        )
                        .values(
                            source_checkpoint_id=prior_row.checkpoint_id,
                            source_cursor=prior_row.cursor,
                            source_snapshot_sha256=sha256(prior_payload).hexdigest(),
                        ),
                        "original prepared H source",
                    ),
                    (
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == result.result_chunk_key,
                        )
                        .values(payload=foreign_receipt, byte_length=len(foreign_receipt), sha256=sha256(foreign_receipt).hexdigest()),
                        "cold receipt contradicts",
                    ),
                    (
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == refused_event_chunk.chunk_key,
                        )
                        .values(payload=foreign_event, byte_length=len(foreign_event), sha256=sha256(foreign_event).hexdigest()),
                        "exact two-event journal",
                    ),
                    (
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == result.result_chunk_key,
                        )
                        .values(sha256="0" * 64),
                        "chunk exceeded its original bound or lost its SQL binding",
                    ),
                )
                assert receipt_row.sha256 != "0" * 64
                for mutation, expected_error in mutations:
                    savepoint = conn.begin_nested()
                    try:
                        changed = conn.execute(mutation)
                        assert changed.rowcount == 1
                        with pytest.raises(AuditIntegrityError, match=expected_error):
                            read_original()
                    finally:
                        savepoint.rollback()
                    assert read_original() == result

                # A well-formed intermediate root is still foreign to the one
                # reserved append; do not fabricate one to satisfy the reader.
                phantom_id = str(uuid4())
                phantom_key = f"snapshot:{phantom_id}"
                phantom_payload = encode_continuation_snapshot(root.model_copy(update={"cursor": admitted_row.cursor + 1})).encode("utf-8")
                savepoint = conn.begin_nested()
                try:
                    conn.execute(
                        insert(chunks).values(
                            session_id=str(running.claim.session_id),
                            operation_id=running.claim.operation_id,
                            chunk_key=phantom_key,
                            codec="snapshot.v2",
                            payload=phantom_payload,
                            byte_length=len(phantom_payload),
                            sha256=sha256(phantom_payload).hexdigest(),
                        )
                    )
                    conn.execute(
                        insert(checkpoints).values(
                            session_id=str(running.claim.session_id),
                            operation_id=running.claim.operation_id,
                            checkpoint_id=phantom_id,
                            kind="transition",
                            cursor=admitted_row.cursor + 1,
                            snapshot_chunk_key=phantom_key,
                            frame_stack_version=1,
                            created_at=terminal_row.created_at,
                        )
                    )
                    with pytest.raises(AuditIntegrityError, match="single terminal append"):
                        read_original()
                finally:
                    savepoint.rollback()
                assert read_original() == result

                savepoint = conn.begin_nested()
                try:
                    conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == str(running.claim.session_id),
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == result.result_chunk_key,
                        )
                        .values(codec="composer.primary-no-sdk-refusal.v1")
                    )
                    with pytest.raises(AuditIntegrityError):
                        read_classifier_no_sdk_refusal_on_connection(
                            conn,
                            session_id=running.claim.session_id,
                            operation_id=running.claim.operation_id,
                            effect_key=result.effect_key,
                        )
                finally:
                    savepoint.rollback()
                assert (
                    read_classifier_no_sdk_refusal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=result.effect_key,
                    )
                    == result
                )
                conn.execute(update(usage).where(usage.c.entry_id == attempt.ledger_entry_id).values(prompt_tokens=1))
                with pytest.raises(AuditIntegrityError, match="zero-use physical cancellation cohort"):
                    read_classifier_no_sdk_refusal_on_connection(
                        conn,
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        effect_key=result.effect_key,
                    )
            finally:
                transaction.rollback()
