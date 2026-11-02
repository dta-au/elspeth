"""Real V4 classifier no-SDK terminal keeps its two-frame source and zero-use cohort."""

from __future__ import annotations

import litellm
import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_classifier_primary_no_sdk import (
    ClassifierNoSdkRefusalReceipt,
    read_classifier_no_sdk_refusal_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_cold_restore as cold_restore
from tests.unit.web.sessions.test_continuation_classifier_primary import _classifier_case, _invoke
from tests.unit.web.sessions.test_continuation_classifier_primary import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_v4_classifier_pre_sdk_refusal_retains_original_two_frames_and_zero_use(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_v4_from_settings(
        continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576)
    )
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(cold_restore, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    async with _classifier_case(store, tmp_path, monkeypatch) as (_phases, running, runtime, issuer, prepared, prior_sdk):
        assert len(prior_sdk) == 1
        further_sdk = []

        async def unexpected_sdk(**_kwargs):
            further_sdk.append(1)
            raise AssertionError("classifier SDK must remain unentered")

        original = ProviderCallCustody.require_cost_dispatch

        def expire_after_admission(custody):
            if custody.continuation_classifier_v4_admission is not None:
                raise TimeoutError("injected after original classifier H, before SDK")
            return original(custody)

        monkeypatch.setattr(litellm, "acompletion", unexpected_sdk)
        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", expire_after_admission)
        result = await _invoke(issuer, runtime, prepared)
        assert type(result) is ClassifierNoSdkRefusalReceipt and not further_sdk
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
            cold = read_classifier_no_sdk_refusal_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                effect_key=result.effect_key,
            )
        assert cold == result
        assert effect.disposition == "refused" and effect.provider_attempt_id == attempt.attempt_id
        assert hold.state == "consumed" and attempt.settled_at is not None
        assert (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) == (0, 0, 0, 0)
        root = decode_continuation_snapshot(result.refused.payload_json)
        assert len(root.frames) == 2 and root.frames[0].frame_kind == "compose" and root.frames[1].frame_kind == "tool_batch"
        with store[0].connect() as conn:
            transaction = conn.begin()
            try:
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
