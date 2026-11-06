"""Supported V4 transitions through actual app admission and driver scheduling.

These are acceptance paths, not primitive-owner fixtures. Provider replies are
scripted; original action/segment clocks and the worker protocol are untouched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import ITERATION_EFFECT_OWNER
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ACTOR,
    ADVISOR_MODEL,
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    observe_supported_path,
    sdk_reply,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.helpers.continuation_early_profile import semantic_preparation_profile


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("read", "read_end_retry", "mutation_early", "mutation_early_retry"))
async def test_supported_driver_reaches_original_paid_transitions(tmp_path, monkeypatch, route):
    operation_id = str(uuid4())
    checkpoint_dispatches = []
    early_action_cuts = []
    original_dispatch = ProviderCallCustody.require_cost_dispatch

    def observe_checkpoint_dispatch(custody):
        result = original_dispatch(custody)
        prepared = custody.continuation_checkpoint_v4
        admission = custody.continuation_checkpoint_v4_admission
        if prepared is not None and admission is not None and prepared.running.claim.operation_id == operation_id:
            checkpoint_dispatches.append((prepared.prepared_source.phase, prepared.retry_ordinal, admission.attempt.attempt_id))
        return result

    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", observe_checkpoint_dispatch)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        policy = harness.app.state.composer_async_operation_authority._continuation_policy
        assert policy is not None and policy.actions.advisor.preparation == 5.0
        if route in ("mutation_early", "mutation_early_retry"):
            from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

            blob = tmp_path / "blobs" / str(harness.session_id)
            blob.mkdir(parents=True, exist_ok=True)
            (blob / "input.csv").write_text("name\nAda\n", encoding="utf-8")
            (tmp_path / "outputs" / str(harness.session_id)).mkdir(parents=True, exist_ok=True)
            await harness.app.state.session_service.update_composer_preferences(
                harness.session_id, trust_mode="auto_commit", density_default="high", actor="test"
            )

            def first_primary(app):
                return "Building the requested draft.", [
                    tool_call(app, "supported_author", "set_pipeline", _pipeline(tmp_path, str(harness.session_id)))
                ]

        else:

            def first_primary(app):
                return "Inspecting the current pipeline.", [tool_call(app, "supported_read", "get_pipeline_state", {})]

        def require_predecessor():
            retained = effects(harness.app, operation_id)
            assert any(row.owner == "tool_dispatch" and row.disposition == "committed" for row in retained)
            assert any(row.effect_key.endswith(":p4") and row.disposition == "committed" for row in retained)
            return retained

        def early_reply(_request):
            require_predecessor()
            expected = [("early", 0), ("early", 1)] if route == "mutation_early_retry" else [("early", 0)]
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == expected
            record_early_action()
            return {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The provider's draft is coherent.", "note": None}

        def record_early_action():
            entries = [event for event in observed.events if event["boundary"] == "advance" and event["event"] == "entry"]
            current = entries[-1]
            assert current["action_kind"] == "advisor" and current["clock_bound"] and current["sql_owner_bound"]
            early_action_cuts.append((current["segment"], current["generation"], current["attempt"], current["action_ordinal_before"]))
            assert len(set(early_action_cuts)) == len(early_action_cuts)

        def malformed_early(_request):
            # A genuine external format failure after the real mutation/P4
            # predecessor and admitted first physical EARLY call.
            require_predecessor()
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == [("early", 0)]
            assert sdk.roles == ["primary", "early"]
            record_early_action()
            return "PRIVATE_SUPPORTED_EARLY_REPLY: complete provider reply with malformed EARLY grammar."

        def next_primary(request):
            require_predecessor()
            return semantic_primary_response(request)

        def review(request):
            retained = {row.effect_key: row for row in require_predecessor()}
            assert retained["provider:0:1"].disposition == "committed"
            assert any(row.owner == ITERATION_EFFECT_OWNER and row.disposition == "committed" for row in retained.values())
            return semantic_review_response(request)

        def malformed_end(_request):
            # The fault is the external reply grammar, after a real Review and
            # original END dispatch; a predecessor failure cannot pass this test.
            require_predecessor()
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == [("end", 0)]
            assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end"]
            return "PRIVATE_SUPPORTED_END_REPLY: complete provider reply with malformed END grammar."

        steps = [("primary", first_primary)]
        if route == "mutation_early_retry":
            steps.append(("early", malformed_early))
        if route in ("mutation_early", "mutation_early_retry"):
            steps.append(("early", early_reply))
        steps.extend(
            [
                ("semantic", next_primary),
                ("primary", ("The inspected pipeline is ready for the requested explanation.", None)),
                ("semantic", review),
            ]
        )
        if route == "read_end_retry":
            steps.append(("end", malformed_end))
        steps.extend([("end", end_admit()), ("semantic", semantic_stop_response)])
        sdk = ScriptedSDK(steps, app_ref=[harness.app])
        install_sdk(monkeypatch, sdk)
        artifact = tmp_path / f"supported-{route}-driver.json"
        with (
            semantic_preparation_profile(
                monkeypatch,
                harness.app.state.session_engine,
                output_key=f"supported-{route}-semantic",
                driver_operation=(operation_id, harness.session_id),
            ),
            observe_supported_path(monkeypatch, operation_id=operation_id, session_id=harness.session_id, artifact=artifact) as observed,
        ):
            terminal = await submit_and_settle(
                harness,
                message_body("Inspect or build the requested pipeline, then explain the result.", operation_id=operation_id),
                # Observe this complete multi-action script; production stage
                # clocks and the helper's default short-script bound stay fixed.
                rounds=720,
            )
            assert terminal.status == "completed", f"Original driver predecessor failed; safe metadata: {artifact}"
            observed.require_driver_stages()
            assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
            expected_checkpoints = [("early", 0)] if route in ("mutation_early", "mutation_early_retry") else []
            if route == "mutation_early_retry":
                expected_checkpoints.append(("early", 1))
            expected_checkpoints.append(("end", 0))
            if route == "read_end_retry":
                expected_checkpoints.append(("end", 1))
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == expected_checkpoints
            assert len(early_action_cuts) == sum(phase == "early" for phase, _ordinal in expected_checkpoints)
            assert len({attempt for _phase, _ordinal, attempt in checkpoint_dispatches}) == len(expected_checkpoints)
            retained = {row.effect_key: row for row in require_predecessor()}
            assert retained["provider:0:0"].disposition == retained["provider:0:1"].disposition == "committed"
            with harness.app.state.session_engine.connect() as conn:
                attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all())
            assert len(attempts) == len(steps)
            assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
            assert len({attempt.attempt_id for attempt in attempts}) == len(steps)
        assert "PRIVATE_SUPPORTED_END_REPLY" not in artifact.read_text(encoding="utf-8")
        assert "PRIVATE_SUPPORTED_EARLY_REPLY" not in artifact.read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("cost,stop", ((0.01, False), (None, False), (0.01, True)), ids=("known-cost", "unknown-cost", "stop-after-entry"))
async def test_supported_driver_closes_original_paid_malformed_semantic_return(tmp_path, monkeypatch, cost, stop):
    operation_id = str(uuid4())
    original_text = "PRIVATE_MALFORMED_SEMANTIC_REPLY: the provider returned this complete non-JSON response."
    admissions, completed, reached = [], [], []
    original_dispatch = ProviderCallCustody.require_cost_dispatch
    original_complete = SessionServiceImpl.complete_v4_semantic_result_sync

    def dispatch(custody):
        result = original_dispatch(custody)
        prepared = custody.continuation_semantic_v4
        admission = custody.continuation_semantic_v4_admission
        if prepared is not None and prepared.binding.running.claim.operation_id == operation_id:
            assert admission is not None
            admissions.append(admission)
        return result

    def complete(self, **values):
        paid = original_complete(self, **values)
        prepared = values["prepared"]
        if prepared.binding.running.claim.operation_id == operation_id:
            completed.append((prepared, values["observation"], paid))
        return paid

    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", dispatch)
    monkeypatch.setattr(SessionServiceImpl, "complete_v4_semantic_result_sync", complete)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        policy = harness.app.state.composer_async_operation_authority._continuation_policy
        assert policy is not None and policy.actions.advisor.preparation == 5.0

        def malformed(_request):
            retained = effects(harness.app, operation_id)
            assert any(row.owner == "tool_dispatch" and row.disposition == "committed" for row in retained)
            assert any(row.effect_key.endswith(":p4") and row.disposition == "committed" for row in retained)
            assert sdk.roles == ["primary", "semantic"] and len(admissions) == 1
            with harness.app.state.session_engine.connect() as conn:
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == admissions[0].attempt.attempt_id)).one()
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == hold.provider_attempt_id)).one()
            assert hold.state == "active" and attempt.settled_at is None and attempt.ledger_entry_id is None
            reached.append(attempt.attempt_id)
            if stop:
                cancelled = harness.app.state.composer_async_operation_authority.request_owner_cancel(
                    session_id=harness.session_id, operation_id=operation_id, actor_user_id=ACTOR, auth_provider_type="local"
                )
                assert cancelled is not None and cancelled.cancel_requested_at is not None
            return sdk_reply(original_text, model=ADVISOR_MODEL, cost=cost)

        sdk = ScriptedSDK(
            [
                ("primary", lambda app: ("Inspecting the pipeline.", [tool_call(app, "malformed_predecessor", "get_pipeline_state", {})])),
                ("semantic", malformed),
            ],
            app_ref=[harness.app],
        )
        install_sdk(monkeypatch, sdk)
        artifact = tmp_path / "supported-malformed-semantic-driver.json"
        with observe_supported_path(monkeypatch, operation_id=operation_id, session_id=harness.session_id, artifact=artifact) as observed:
            terminal = await submit_and_settle(
                harness, message_body("Inspect the pipeline and explain its current state.", operation_id=operation_id), rounds=720
            )
            observed.require_driver_stages()
            assert len(reached) == len(completed) == 1
            assert sdk.roles == ["primary", "semantic"] and not sdk.steps
            prepared, observation, paid = completed[0]
            assert paid.outcome is semantic_sql.SemanticAdvisorProviderOutcome.PAID_INVALID
            assert isinstance(paid.original_error, _MalformedLLMResponseError)
            assert paid.original_error is observation.original_failure
            assert paid.decision is None and paid.response is not None
            assert decode_continuation_response(paid.response).message.content == original_text
            assert terminal.status == "failed" and terminal.result is None and terminal.error is not None
            if stop:
                assert terminal.error.http_status == 499 and terminal.error.error_type == "request_cancelled"
                assert terminal.error.failure_code == "request_cancelled" and terminal.error.diagnostic_id is None
            else:
                assert terminal.error.http_status == 500 and terminal.error.error_type == "operation_failed"
                assert terminal.error.failure_code == "operation_failed" and terminal.error.diagnostic_id is not None
            assert original_text not in terminal.model_dump_json()
            with harness.app.state.session_engine.connect() as conn:
                attempts = conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()
                attempt = conn.execute(select(physical).where(physical.c.attempt_id == reached[0])).one()
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == reached[0])).one()
                ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
                cold, _input, _root = semantic_sql._paid_result_data(
                    conn,
                    str(harness.session_id),
                    operation_id,
                    paid.snapshot,
                    decode_continuation_snapshot(paid.snapshot.payload_json),
                    prepared.binding.effect_key,
                )
            assert attempt.settled_at is not None and hold.state == "consumed"
            assert len(attempts) == 2 and all(item.settled_at is not None for item in attempts)
            assert ledger.prompt_tokens == 7 and ledger.completion_tokens == 3
            assert paid.audit_call.provider_cost == cost
            assert cold.outcome is semantic_sql.SemanticAdvisorProviderOutcome.PAID_INVALID
            assert cold.audit_call == paid.audit_call and cold.response == paid.response and cold.decision is None
            retained = effects(harness.app, operation_id)
            effect = next(row for row in retained if row.effect_key == prepared.binding.effect_key)
            assert effect.disposition == "refused" and effect.provider_attempt_id == reached[0]
            assert not any(row.effect_key == "provider:0:1" for row in retained)
        assert original_text not in artifact.read_text(encoding="utf-8")
