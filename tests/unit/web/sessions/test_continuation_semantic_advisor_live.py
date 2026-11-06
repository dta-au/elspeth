"""Real app/worker/driver semantic consumer; only external SDK/guard are scripted."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from traceback import extract_tb
from uuid import uuid4

import pytest
from sqlalchemy import Text, cast, insert, or_, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.diagnostic_evidence import diagnostic_evidence_identity
from elspeth.web.sessions import continuation_checkpoint_end_complete_review_cold as end_review_cold
from elspeth.web.sessions.composer_operations import ComposerOperationCancel
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.continuation_semantic_actions import read_original_committed_semantic_read_on_connection
from elspeth.web.sessions.continuation_semantic_advisor import read_original_prescribed_primary_source_on_connection
from elspeth.web.sessions.models import chat_messages_table as audit_messages
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ScriptedSDK,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    semantic_primary_response,
    semantic_read_response,
    semantic_review_response,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)
from tests.helpers.continuation_end_profile import driver_end_preparation_profile
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store

operation_store = _operation_store


def _checkpoint_private_audit_candidates(conn, *, session_id, attempt_id):
    """Repeat original candidate discovery, projecting only IDs and equality bits."""
    request_id = diagnostic_evidence_identity("provider-request", attempt_id)
    failure_id = diagnostic_evidence_identity("provider-failure", attempt_id)
    text = cast(audit_messages.c.tool_calls, Text)
    outer = audit_messages.c.tool_calls[0]
    inner = outer["evidence"]
    rows = conn.execute(
        select(
            audit_messages.c.id,
            (outer["tool_call_id"].as_string() == attempt_id).label("outer_call"),
            outer["evidence_id"].as_string().in_((request_id, failure_id)).label("outer_evidence"),
            (inner["tool_call_id"].as_string() == attempt_id).label("inner_call"),
            inner["evidence_id"].as_string().in_((request_id, failure_id)).label("inner_evidence"),
        )
        .where(
            audit_messages.c.session_id == session_id,
            audit_messages.c.role == "audit",
            audit_messages.c.content == "Private credential-filtered diagnostic facts",
            or_(text.like(f"%{attempt_id}%"), text.like(f"%{request_id}%"), text.like(f"%{failure_id}%")),
        )
        .limit(3)
    ).all()
    return tuple(
        {
            "id": row.id,
            "outer_call": row.outer_call is True,
            "outer_evidence": row.outer_evidence is True,
            "inner_call": row.inner_call is True,
            "inner_evidence": row.inner_evidence is True,
        }
        for row in rows
    )


def _observe_original_driver(monkeypatch, operation):
    """Payload-free delegation until the actual first live failure is known."""
    observations = []
    original_advance, original_settle = _SegmentDriver.advance, _SegmentDriver.settle
    original_run = _SegmentDriver.run
    original_cancel = ContinuationLivePhases.cancel_business
    original_private_audit = end_review_cold.require_checkpoint_paid_private_audit_on_connection

    def private_audit(conn, *, source, attempt_id, private_sha256, incomplete):
        try:
            return original_private_audit(conn, source=source, attempt_id=attempt_id, private_sha256=private_sha256, incomplete=incomplete)
        except AuditIntegrityError as error:
            if source.operation_id == operation and error.args == ("checkpoint private audit lost its exact original member count",):
                observed = {"boundary": "checkpoint_private_audit_count", "attempt_id": attempt_id, "expected": 2 if incomplete else 1}
                observations.append(observed)
                try:
                    observed["candidates"] = _checkpoint_private_audit_candidates(conn, session_id=source.session_id, attempt_id=attempt_id)
                except Exception as observation_error:
                    observed["observation_error_class"] = type(observation_error).__name__
            raise

    def failure_data(error):
        return {
            "class": type(error).__name__,
            "cancel_reasons": tuple(arg.reason.value for arg in error.args if type(arg) is ComposerOperationCancel),
            "trace": tuple((Path(frame.filename).name, frame.name, frame.lineno) for frame in extract_tb(error.__traceback__)),
            "members": tuple(failure_data(member) for member in error.exceptions) if isinstance(error, BaseExceptionGroup) else (),
        }

    async def advance(driver, stored, selected):
        if driver.running.claim.operation_id != operation:
            return await original_advance(driver, stored, selected)
        frame = selected.stack.frames[-1]
        now = time.monotonic()
        observed = {
            "boundary": "advance",
            "cursor": stored.cursor,
            "kind": frame.frame_kind,
            "phase": frame.phase if frame.frame_kind != "initialize" else frame.ingress_phase,
            "remaining": driver.sql_deadline.remaining_action(monotonic_now=now),
        }
        observations.append(observed)
        try:
            return await original_advance(driver, stored, selected)
        except BaseException as error:
            observed["error"] = failure_data(error)
            raise
        finally:
            observed["elapsed"] = time.monotonic() - now

    async def settle(driver, record, original=None, *, expired_failure=None):
        if driver.running.claim.operation_id == operation:
            observations.append(
                {
                    "boundary": "settle",
                    "cursor": record.journal_cursor,
                    "original": None if original is None else failure_data(original),
                    "expired": None if expired_failure is None else expired_failure.failure_code,
                }
            )
        return await original_settle(driver, record, original, expired_failure=expired_failure)

    async def run(driver, adoption_failure):
        if driver.running.claim.operation_id != operation:
            return await original_run(driver, adoption_failure)
        observed = {"boundary": "driver.run"}
        observations.append(observed)
        try:
            return await original_run(driver, adoption_failure)
        except BaseException as error:
            observed["error"] = failure_data(error)
            raise

    def cancel(phases, reason):
        if phases.work.running is not None and phases.work.running.claim.operation_id == operation:
            observations.append({"boundary": "cancel_business", "reason": reason.reason.value})
        return original_cancel(phases, reason)

    monkeypatch.setattr(_SegmentDriver, "advance", advance)
    monkeypatch.setattr(_SegmentDriver, "settle", settle)
    monkeypatch.setattr(_SegmentDriver, "run", run)
    monkeypatch.setattr(ContinuationLivePhases, "cancel_business", cancel)
    monkeypatch.setattr(end_review_cold, "require_checkpoint_paid_private_audit_on_connection", private_audit)
    return observations


def test_checkpoint_private_audit_candidate_summary_distinguishes_nested_text(operation_store):
    engine, _repository, _authority, _service, session_id = operation_store
    attempt = str(uuid4())
    request = diagnostic_evidence_identity("provider-request", attempt)
    failure = diagnostic_evidence_identity("provider-failure", attempt)
    member = {"tool_call_id": attempt, "evidence_id": request, "evidence": {"tool_call_id": attempt, "evidence_id": request}}
    nested = {
        "tool_call_id": "another-call",
        "evidence_id": "another-evidence",
        "evidence": {
            "tool_call_id": "another-call",
            "evidence_id": "another-evidence",
            "arguments": {"content": f"{attempt} {request} {failure}"},
        },
    }
    identifiers = tuple(str(uuid4()) for _ in range(3))
    with engine.begin() as conn:
        for sequence, (identifier, envelope) in enumerate(zip(identifiers, (member, nested, {"unrelated": "negative"}), strict=True), 1):
            conn.execute(
                insert(audit_messages).values(
                    id=identifier,
                    session_id=str(session_id),
                    role="audit",
                    content="Private credential-filtered diagnostic facts",
                    tool_calls=[envelope],
                    sequence_no=sequence,
                    writer_principal="compose_loop",
                    created_at=datetime.now(UTC),
                )
            )
        summary = _checkpoint_private_audit_candidates(conn, session_id=str(session_id), attempt_id=attempt)
        assert {row["id"]: row for row in summary} == {
            identifiers[0]: {"id": identifiers[0], "outer_call": True, "outer_evidence": True, "inner_call": True, "inner_evidence": True},
            identifiers[1]: {
                "id": identifiers[1],
                "outer_call": False,
                "outer_evidence": False,
                "inner_call": False,
                "inner_evidence": False,
            },
        }
        assert _checkpoint_private_audit_candidates(conn, session_id=str(session_id), attempt_id=str(uuid4())) == ()


@pytest.mark.asyncio
async def test_actual_driver_runs_paid_semantic_advisor_before_prescribed_primary_and_reproves_cold_source(tmp_path, monkeypatch):
    instruction = "Use the retained inspection to explain why the current pipeline is empty."
    actual_decisions = []

    def semantic(request):
        answer = semantic_primary_response(request, instruction=instruction)
        actual_decisions.append(answer)
        return answer

    def semantic_review(request):
        answer = semantic_review_response(request)
        actual_decisions.append(answer)
        return answer

    def semantic_stop(request):
        answer = semantic_stop_response(request)
        actual_decisions.append(answer)
        return answer

    app_ref = []
    sdk = ScriptedSDK(
        [
            ("primary", lambda app: ("Inspecting the current graph.", [tool_call(app, "inspect_live_k7", "get_pipeline_state", {})])),
            ("semantic", semantic),
            ("primary", ("The current pipeline has no configured nodes.", None)),
            ("semantic", semantic_review),
            ("end", end_admit()),
            ("semantic", semantic_stop),
        ],
        app_ref=app_ref,
    )
    install_sdk(monkeypatch, sdk)
    operation = str(uuid4())
    original_observations = _observe_original_driver(monkeypatch, operation)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        app_ref.append(harness.app)
        result = await submit_and_settle(
            harness, message_body("Explain the current pipeline after inspecting its state.", operation_id=operation)
        )
        if result.status != "completed":
            # Preserve every original cancellation/settlement observation.
            # Pytest shortens nested list reprs inside assertion messages.
            print("K7 original driver observations: " + canonical_json(original_observations))
        assert result.status == "completed", (result.error, original_observations)
        assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"] and not sdk.steps
        assert len(actual_decisions) == 3
        assert "inspect_live_k7" in canonical_json(sdk.requests[1]["messages"])
        assert any(message["role"] == "assistant" and message.get("content") == instruction for message in sdk.requests[2]["messages"])
        assert [check["role"] for check in _checks(harness.app.state.session_engine)] == [
            "composer",
            "advisor",
            "composer",
            "advisor",
            "advisor",
            "advisor",
        ]
        final_document = json.loads(sdk.requests[-1]["messages"][-1]["content"])
        assert final_document["review_result"]["message"] == "The current pipeline has no configured nodes."
        retained = {row.effect_key: row for row in effects(harness.app, operation)}
        semantic_effect = retained["provider:1:0"]
        prescribed = retained["provider:0:1"]
        assert semantic_effect.disposition == prescribed.disposition == "committed"
        assert semantic_effect.provider_attempt_id is not None and prescribed.provider_attempt_id is not None
        assert semantic_effect.provider_attempt_id != prescribed.provider_attempt_id
        assert json.loads(prescribed.action_json) == actual_decisions[0]["prescribed_action"]
        with harness.app.state.session_engine.connect() as conn:
            attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all())
            assert len(attempts) == 6 and all(attempt.settled_at is not None for attempt in attempts)
            source = read_original_prescribed_primary_source_on_connection(
                conn,
                session_id=harness.session_id,
                operation_id=operation,
                action_json=prescribed.action_json,
                effect_key=prescribed.effect_key,
            )
            assert (source.effect_key, source.input_sha256) == (semantic_effect.effect_key, semantic_effect.input_sha256)
            substituted = json.loads(prescribed.action_json)
            substituted["instruction"] = "An instruction the actual semantic provider never produced."
            with pytest.raises(AuditIntegrityError):
                read_original_prescribed_primary_source_on_connection(
                    conn,
                    session_id=harness.session_id,
                    operation_id=operation,
                    action_json=canonical_json(substituted),
                    effect_key=prescribed.effect_key,
                )
            assert tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all()) == attempts
        assert sdk.roles == ["primary", "semantic", "primary", "semantic", "end", "semantic"]


@pytest.mark.asyncio
async def test_actual_driver_read_result_reaches_next_advisor_then_real_review_and_whole_stop(tmp_path, monkeypatch):
    from elspeth.web.sessions import continuation_semantic_advisor as semantic_sql

    original_document_check = semantic_sql._require_retained_input_document
    review_documents = []

    def observe_document(conn, sid, operation, root, physical_input, retained, *, env_ref_names, evidence_document=None):
        original_document_check(
            conn,
            sid,
            operation,
            root,
            physical_input,
            retained,
            env_ref_names=env_ref_names,
            evidence_document=evidence_document,
        )
        if retained.review is not None and not review_documents:
            review_documents.append((sid, operation, root, physical_input, retained, env_ref_names))

    monkeypatch.setattr(semantic_sql, "_require_retained_input_document", observe_document)
    operation = str(uuid4())
    original_answer = "The current pipeline has no configured nodes."
    sdk = ScriptedSDK(
        [
            ("primary", (original_answer, None)),
            ("semantic", semantic_read_response),
            ("semantic", semantic_review_response),
            ("end", end_admit()),
            ("semantic", semantic_stop_response),
        ]
    )
    install_sdk(monkeypatch, sdk)
    observations = _observe_original_driver(monkeypatch, operation)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        try:
            with driver_end_preparation_profile(
                monkeypatch, harness.app.state.session_engine, session_id=harness.session_id, operation_id=operation
            ):
                # Observe this complete multi-action scenario beyond the general
                # 240-poll wait; this is not a macro-latency qualification.
                result = await submit_and_settle(
                    harness,
                    message_body("Inspect the current pipeline and explain what it contains.", operation_id=operation),
                    rounds=720,
                )
        except BaseException:
            print("K7 original read driver observations: " + canonical_json(observations))
            raise
        if result.status != "completed":
            print("K7 original read driver observations: " + canonical_json(observations))
        assert result.status == "completed", result.error
        assert sdk.roles == ["primary", "semantic", "semantic", "end", "semantic"] and not sdk.steps
        before_read = json.loads(sdk.requests[1]["messages"][-1]["content"])
        after_read = json.loads(sdk.requests[2]["messages"][-1]["content"])
        assert "read_results" not in before_read and len(after_read["read_results"]) == 1
        actual_read = after_read["read_results"][0]
        assert actual_read["tool_name"] == "get_pipeline_state"
        assert json.loads(actual_read["response_json"])["success"] is True
        assert "prescribing_provider_call_id" not in actual_read
        assert before_read["source"]["premise"]["input_hash"] == after_read["source"]["premise"]["input_hash"]
        final_document = json.loads(sdk.requests[-1]["messages"][-1]["content"])
        assert len(review_documents) == 1
        sid, actual_operation, root, physical_input, retained, env_ref_names = review_documents[0]
        assert actual_operation == operation and retained.review is not None
        full_review = json.loads(retained.review.result_json)
        assert final_document["review_result"] == json.loads(retained.review.provider_json)
        assert full_review["raw_assistant_content"] == original_answer
        assert (
            final_document["review_result"]["message"]
            == full_review["message"]
            == (
                original_answer + "\n\n---\n\n"
                "[ELSPETH-SYSTEM] Checkpoint save status: "
                "Verified checkpoint evidence covers 0 saved draft revisions. "
                "There is no net authored change from the starting draft. "
                "Checkpoint evidence does not establish task completion."
            )
        )
        rows = effects(harness.app, operation)
        reads = tuple(row for row in rows if row.owner == "read")
        assert len(reads) == 1 and reads[0].disposition == "committed" and reads[0].provider_attempt_id is None
        with harness.app.state.session_engine.connect() as conn:
            bridge = json.loads(
                read_chunk(
                    conn, session_id=str(harness.session_id), operation_id=operation, key=f"semantic-read-source:{reads[0].effect_key}"
                ).decode("utf-8", errors="strict")
            )
            original = read_original_committed_semantic_read_on_connection(
                conn,
                session_id=harness.session_id,
                operation_id=operation,
                commit_checkpoint_id=bridge["commit_checkpoint_id"],
                semantic_effect_key=bridge["semantic_effect_key"],
            )
            assert original.committed.original_result.tool_name == actual_read["tool_name"]
            assert original.committed.original_result.response_json == actual_read["response_json"]
            attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all())
            assert len(attempts) == 5 and all(attempt.settled_at is not None for attempt in attempts)
        assert [check["role"] for check in _checks(harness.app.state.session_engine)] == [
            "composer",
            "advisor",
            "advisor",
            "advisor",
            "advisor",
        ]
        original_document = json.loads(physical_input.messages[-1]["content"])
        assert full_review["llm_calls"] and original_document["review_result"]["llm_calls"]
        assert any(call["private_request"] is not None for call in full_review["llm_calls"])
        assert all("private_request" not in call for call in original_document["review_result"]["llm_calls"])
        # Exercise the actual SQL document comparator after the original action
        # clock. These are its real admitted arguments, not invented authority.
        from elspeth.web.sessions import continuation_semantic_review_result as review_codec

        original_review_decoder = review_codec.decode_semantic_review_result
        repeated_review_decodes = []

        def observe_review_decode(encoded):
            repeated_review_decodes.append(encoded)
            return original_review_decoder(encoded)

        monkeypatch.setattr(review_codec, "decode_semantic_review_result", observe_review_decode)
        # Calibrate the observer against the real complete cold source.
        assert review_codec.decode_semantic_review_result(retained.review.result_json).result.llm_calls
        assert json.loads(retained.review.provider_json) == original_document["review_result"]
        assert repeated_review_decodes == [retained.review.result_json]
        repeated_review_decodes.clear()
        with harness.app.state.session_engine.connect() as conn:
            original_document_check(conn, sid, operation, root, physical_input, retained, env_ref_names=env_ref_names)
            for alteration in ("message", "publication_binding", "omission", "private_request"):
                changed_document = json.loads(physical_input.messages[-1]["content"])
                changed_review = changed_document["review_result"]
                if alteration == "message":
                    changed_review["message"] += " A result the original owner never returned."
                elif alteration == "publication_binding":
                    assert changed_review["publication_binding"] != "0" * 64
                    changed_review["publication_binding"] = "0" * 64
                elif alteration == "omission":
                    del changed_review["runtime_preflight"]
                else:
                    changed_review["llm_calls"][0]["private_request"] = full_review["llm_calls"][0]["private_request"]
                changed_input = physical_input.model_copy(
                    update={
                        "messages": (
                            *physical_input.messages[:-1],
                            {**physical_input.messages[-1], "content": canonical_json(changed_document)},
                        )
                    }
                )
                with pytest.raises(AuditIntegrityError, match="complete original source/result DATA"):
                    original_document_check(conn, sid, operation, root, changed_input, retained, env_ref_names=env_ref_names)
            assert tuple(conn.execute(select(physical).where(physical.c.session_id == sid)).all()) == attempts
        assert repeated_review_decodes == []
