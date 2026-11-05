"""Supported V4 transitions through actual app admission and driver scheduling.

These are acceptance paths, not primitive-owner fixtures. Provider replies are
scripted; original action/segment clocks and the worker protocol are untouched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web import app as app_module
from elspeth.web.composer.continuation_iteration_transitions import ITERATION_EFFECT_OWNER
from elspeth.web.composer.continuation_policy_settings import continuation_policy_v4_from_settings
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    observe_supported_path,
    semantic_primary_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ("read", "read_end_retry", "mutation_early"))
async def test_supported_driver_reaches_original_paid_transitions(tmp_path, monkeypatch, route):
    def v4_policy(settings):
        policy = continuation_policy_v4_from_settings(settings)
        assert policy is not None
        assert policy.actions.advisor.preparation == 5.0
        return policy

    monkeypatch.setattr(app_module, "continuation_policy_from_settings", v4_policy)
    operation_id = str(uuid4())
    checkpoint_dispatches = []
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
        if route == "mutation_early":
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
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == [("early", 0)]
            return {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The provider's draft is coherent.", "note": None}

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
        if route == "mutation_early":
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
        with observe_supported_path(monkeypatch, operation_id=operation_id, session_id=harness.session_id, artifact=artifact) as observed:
            terminal = await submit_and_settle(
                harness,
                message_body("Inspect or build the requested pipeline, then explain the result.", operation_id=operation_id),
            )
            assert terminal.status == "completed", f"Original driver predecessor failed; safe metadata: {artifact}"
            observed.require_driver_stages()
            assert sdk.roles == [role for role, _payload in steps] and not sdk.steps
            expected_checkpoints = [("early", 0)] if route == "mutation_early" else []
            expected_checkpoints.append(("end", 0))
            if route == "read_end_retry":
                expected_checkpoints.append(("end", 1))
            assert [(phase, ordinal) for phase, ordinal, _attempt in checkpoint_dispatches] == expected_checkpoints
            assert len({attempt for _phase, _ordinal, attempt in checkpoint_dispatches}) == len(expected_checkpoints)
            retained = {row.effect_key: row for row in require_predecessor()}
            assert retained["provider:0:0"].disposition == retained["provider:0:1"].disposition == "committed"
            with harness.app.state.session_engine.connect() as conn:
                attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all())
            assert len(attempts) == len(steps)
            assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
            assert len({attempt.attempt_id for attempt in attempts}) == len(steps)
        assert "PRIVATE_SUPPORTED_END_REPLY" not in artifact.read_text(encoding="utf-8")
