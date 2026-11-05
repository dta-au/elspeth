"""Real V4 classifier no-SDK terminal keeps its two-frame source and zero-use cohort."""

from __future__ import annotations

from dataclasses import replace

import litellm
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import (
    ClassifierNoSdkRefusalReceipt,
    read_classifier_no_sdk_refusal_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
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
            cold = read_classifier_no_sdk_refusal_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=result.effect_key,
            )
        assert cold == result
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
