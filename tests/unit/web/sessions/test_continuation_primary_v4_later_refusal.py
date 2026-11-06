"""A real later PRIMARY refusal preserves settled history in its cold proof."""

from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import ITERATION_EFFECT_OWNER
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.sessions.continuation import load_snapshot, read_chunk
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt
from elspeth.web.sessions.continuation_primary_paid_overflow_refusal_terminal import read_v4_primary_no_sdk_refusal_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    install_sdk,
    observe_original_driver_failures,
    semantic_primary_response,
    submit_and_settle,
    tool_call,
)


@pytest.mark.asyncio
async def test_later_primary_zero_use_refusal_cold_proves_complete_historical_frontier(tmp_path, monkeypatch):
    sdk = ScriptedSDK(
        [
            ("primary", lambda app: ("Inspecting the current state.", [tool_call(app, "prior_read", "get_pipeline_state", {})])),
            ("semantic", semantic_primary_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    refused, returned = [], []
    actual_dispatch = ProviderCallCustody.require_cost_dispatch
    actual_advance = _SegmentDriver.advance
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        sid = str(harness.session_id)
        engine = harness.app.state.session_engine

        def history(conn, current_effect):
            rows = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id)).all()
            return {
                row.effect_key: (
                    tuple(row),
                    None
                    if row.result_chunk_key is None
                    else read_chunk(conn, session_id=sid, operation_id=operation_id, key=row.result_chunk_key),
                )
                for row in rows
                if row.effect_key != current_effect
            }

        def refuse_later_after_admission(custody):
            admission = custody.continuation_primary_v4_admission
            if admission is None or admission.hold.operation_id != operation_id or admission.hold.effect_key != "provider:0:1":
                return actual_dispatch(custody)
            assert not refused
            with engine.connect() as conn:
                selected = load_snapshot(conn, session_id=sid, operation_id=operation_id)
                root = decode_continuation_snapshot(selected.payload_json)
                assert type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4
                prior = history(conn, admission.hold.effect_key)
                advance = conn.execute(
                    select(effects).where(
                        effects.c.session_id == sid,
                        effects.c.operation_id == operation_id,
                        effects.c.owner == ITERATION_EFFECT_OWNER,
                    )
                ).one()
            assert advance.disposition == "committed" and advance.receipt_key is not None
            assert "provider:0:0" in prior and any(key.startswith("tool:") for key in prior)
            assert len(root.effect_frontier) > 1
            assert all(entry.disposition in ("committed", "refused") for entry in root.effect_frontier if entry.effect_id in prior)
            refused.append((admission, prior))
            raise TimeoutError("D10 original later PRIMARY refused before SDK")

        async def observe_advance(driver, *args, **kwargs):
            result = await actual_advance(driver, *args, **kwargs)
            if driver.running.claim.operation_id == operation_id and type(result) is PrimaryNoSdkRefusalReceipt:
                returned.append(result)
            return result

        monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", refuse_later_after_admission)
        monkeypatch.setattr(_SegmentDriver, "advance", observe_advance)
        driver_failures = observe_original_driver_failures(monkeypatch, operation_id, session_id=harness.session_id)
        terminal = await submit_and_settle(harness, message_body("Inspect the pipeline, then explain it.", operation_id=operation_id))
        assert terminal.status == "failed" and terminal.error is not None
        if terminal.error.error_type != "composer_admission_refused":
            diagnostic_path = tmp_path / "d10-safe-driver-failures.json"
            diagnostic_path.write_text(json.dumps(driver_failures, indent=2), encoding="utf-8")
            pytest.fail(f"Expected composer_admission_refused, got {terminal.error.error_type}; safe driver metadata: {diagnostic_path}")
        assert sdk.roles == ["primary", "semantic"] and not sdk.steps
        assert len(refused) == len(returned) == 1
        admission, prior = refused[0]
        result = returned[0]
        assert result.effect_key == admission.hold.effect_key == "provider:0:1"

        with locked_session_transaction(engine, sid) as conn:

            def cold():
                return read_v4_primary_no_sdk_refusal_on_connection(
                    conn, session_id=harness.session_id, operation_id=operation_id, effect_key=result.effect_key
                )

            assert cold() == result and history(conn, result.effect_key) == prior
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == admission.attempt.attempt_id)).one()
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one()
            ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
            assert hold.state == "consumed" and hold.consumed_at is not None and attempt.settled_at is not None
            assert ledger.prompt_tokens == ledger.completion_tokens == ledger.cached_prompt_tokens == ledger.reasoning_tokens == 0
            selected = load_snapshot(conn, session_id=sid, operation_id=operation_id)
            root = decode_continuation_snapshot(selected.payload_json)
            checkpoint = conn.execute(select(checkpoints).where(checkpoints.c.checkpoint_id == selected.checkpoint_id)).one()
            original_effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == "provider:0:0"
                )
            ).one()
            assert original_effect.disposition == "committed" and original_effect.provider_attempt_id != admission.attempt.attempt_id

            for changed in ("historical_receipt", "missing_history", "current_receipt", "current_attempt"):
                savepoint = conn.begin_nested()
                try:
                    if changed in ("historical_receipt", "missing_history"):
                        frontier = tuple(
                            entry.model_copy(update={"receipt_id": str(uuid4())})
                            if entry.effect_id == "provider:0:0" and changed == "historical_receipt"
                            else entry
                            for entry in root.effect_frontier
                            if entry.effect_id != "provider:0:0" or changed != "missing_history"
                        )
                        payload = encode_continuation_snapshot(root.model_copy(update={"effect_frontier": frontier})).encode("utf-8")
                        altered = conn.execute(
                            update(chunks)
                            .where(
                                chunks.c.session_id == sid,
                                chunks.c.operation_id == operation_id,
                                chunks.c.chunk_key == checkpoint.snapshot_chunk_key,
                            )
                            .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
                        )
                        expected = "changed its original bounded tail"
                    else:
                        values = (
                            {"receipt_key": str(uuid4())}
                            if changed == "current_receipt"
                            else {"provider_attempt_id": original_effect.provider_attempt_id}
                        )
                        altered = conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == sid,
                                effects.c.operation_id == operation_id,
                                effects.c.effect_key == result.effect_key,
                            )
                            .values(**values)
                        )
                        expected = "changed its original terminal/effect/H"
                    assert altered.rowcount == 1
                    with pytest.raises(AuditIntegrityError, match=expected):
                        cold()
                finally:
                    savepoint.rollback()
                assert cold() == result and history(conn, result.effect_key) == prior
        assert sdk.roles == ["primary", "semantic"]
