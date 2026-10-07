"""Freeform planner repair, exhaustion, and policy-rejection regressions.

The provider is scripted while the planner feedback, candidate validation,
proposal acceptance, and durable failure disposition remain real.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.composer_graphs import assert_isomorphic
from tests.helpers.composer_operations import submit_and_settle

from .conftest import (
    PARITY_FIXTURES,
    ParityEnv,
    _empty_state,
    _ScriptedCompletion,
    emit_proposal_response,
    rewrite_source_paths,
)

# A simple source-transform-sink fixture keeps repair checks about planner behavior.
_LINEAR = next(fixture for fixture in PARITY_FIXTURES if fixture["class"] == "linear_transform")

# A real transform plugin that is installed in the catalog but NOT admitted by the
# parity web policy (absent from ``_PARITY_ALLOWLIST`` and ``REQUIRED_WEB_PLUGIN_IDS``),
# so a candidate that names it is a genuine policy denial.
_POLICY_DENIED_TRANSFORM = "truncate"


# --------------------------------------------------------------------------- #
# Planner helpers                                                            #
# --------------------------------------------------------------------------- #


def _valid_pipeline(env: ParityEnv, fixture: dict[str, Any], session_id: str) -> dict[str, Any]:
    """The fixture's canonical pipeline with source paths bound under the S2 allowlist."""
    return rewrite_source_paths(fixture["canonical_arguments"], env.data_dir, session_id)


def _malformed_missing_edges(pipeline: dict[str, Any]) -> dict[str, Any]:
    """A schema-malformed terminal: drop the required top-level ``edges`` key.

    ``SetPipelineArgumentsModel`` requires ``edges``; omitting it fails the
    terminal schema check (``_canonical_schema_feedback``) — a genuine malformed
    terminal, not a candidate-validation rejection.
    """
    return {key: value for key, value in pipeline.items() if key != "edges"}


def _policy_denied_variant(pipeline: dict[str, Any]) -> dict[str, Any]:
    """A shape-valid terminal that names a policy-denied transform plugin.

    Passes ``SetPipelineArgumentsModel`` (structurally a valid transform node) but
    fails ``build_set_pipeline_candidate`` authorization, so the planner receives
    the allowlisted *candidate* feedback (``ValidationError``), not schema feedback.
    """
    denied = copy.deepcopy(pipeline)
    denied["nodes"][0]["plugin"] = _POLICY_DENIED_TRANSFORM
    return denied


def _last_tool_feedback(request: dict[str, Any]) -> dict[str, Any]:
    """Parse the last ``role="tool"`` message the planner fed back into a request."""
    messages = request["messages"]
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    if not tool_messages:
        raise AssertionError("scripted request carried no tool-role feedback message")
    return json.loads(tool_messages[-1]["content"])


async def _drive_freeform_scripted(
    env: ParityEnv,
    fixture: dict[str, Any],
    completion: _ScriptedCompletion,
    session: Any,
) -> tuple[Any, Any]:
    """Run the real freeform ``compose`` empty-build path under ``completion``, then accept.

    Mirrors ``ParityEnv.drive_freeform`` but injects a caller-supplied multi-response
    completion (the positive driver scripts exactly one). Returns the committed
    ``CompositionState`` and the accepted proposal record.
    """
    env.monkeypatch.setattr("litellm.acompletion", completion)
    await env.sessions.update_composer_preferences(
        session.id,
        trust_mode="explicit_approve",
        density_default="high",
        actor="test",
    )
    user_message = await env.sessions.add_message(
        session.id,
        "user",
        fixture["intent"],
        writer_principal="route_user_message",
    )
    await env.composer.compose(
        fixture["intent"],
        [],
        _empty_state(),
        session_id=str(session.id),
        user_id="alice",
        user_message_id=str(user_message.id),
    )
    proposals = await env.sessions.list_composition_proposals(session.id, status="pending")
    if len(proposals) != 1:
        raise AssertionError(f"freeform scripted repair staged {len(proposals)} proposals, expected exactly one")
    proposal = proposals[0]
    async with env._client() as client:
        response = await client.post(
            f"/api/sessions/{session.id}/proposals/{proposal.id}/accept",
            json={"draft_hash": proposal.pipeline_metadata.draft_hash},
        )
    if response.status_code != 200:
        raise AssertionError(f"freeform scripted repair accept failed ({response.status_code}): {response.text}")
    return await env._committed_state(session.id), proposal


def _disposition_rows(engine: Any) -> list[Any]:
    """The durable ``planner_failure_disposition`` audit rows (freeform terminalization)."""
    with engine.connect() as conn:
        rows = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.tool_calls)).all()
    return [
        row for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0].get("_kind") == "planner_failure_disposition"
    ]


def _assert_allowlisted_feedback_shape(feedback: dict[str, Any], *, error_class: str) -> None:
    """The repair feedback is the redaction-safe structured projection only."""
    # "guidance" rides candidate-rejection feedback only (not the canonical
    # schema projection) and is a STATIC usage line — never per-request data —
    # so it does not widen the redaction boundary this allowlist protects.
    # "truncation_notice" rides a rejection the candidate builder capped
    # (elspeth-4fad98a453). Its only variable part is the integer count of
    # defective components the builder did not list — a number ELSPETH
    # derived from its own gates, never candidate content — so the closed
    # equality below is what keeps it at zero egress.
    assert {"success", "validation"} <= set(feedback) <= {"success", "validation", "guidance", "truncation_notice"}, feedback
    if "guidance" in feedback:
        assert feedback["guidance"] == ("To expand any code, call explain_validation_error with the exact code string.")
    if "truncation_notice" in feedback:
        withheld_count = re.fullmatch(
            r"(\d+) further component\(s\) of this candidate also failed validation and are not listed here\. "
            r"Repair every component named above and re-emit; the remaining failures are reported on the next turn\.",
            feedback["truncation_notice"],
        )
        assert withheld_count is not None, feedback["truncation_notice"]
        assert int(withheld_count.group(1)) > 0, feedback["truncation_notice"]
    assert feedback["success"] is False
    validation = feedback["validation"]
    assert set(validation) == {"is_valid", "errors"}, validation
    assert validation["is_valid"] is False
    assert validation["errors"], "expected at least one structured error"
    for entry in validation["errors"]:
        # The four structured fields are always present. A closed error_code may
        # additionally carry the STATIC (explanation, suggested_fix) catalogue
        # text keyed by the code (tools.generation.explain_validation_code) — a
        # public constant, never per-request data — so the redaction-safe
        # allowlist widens by exactly those two paired keys. A schema-contract
        # rejection may additionally carry the structured "contract" facts
        # (producer/consumer component ids + schema FIELD NAMES from validated
        # config — pipeline metadata, never row content; see
        # SchemaContractDetail in composer.state). Nothing else may ride.
        # A canonical-schema rejection may additionally carry
        # "schema_violations": the JSON path of each structural violation, the
        # violated keyword, and a scalar constraint from the schema the SAME
        # provider was advertised — location and rule only, never the rejected
        # value (elspeth-4fad98a453). Nothing else may ride.
        assert {"component", "severity", "error_code", "error_class"} <= set(entry), entry
        assert set(entry) <= {
            "component",
            "severity",
            "error_code",
            "error_class",
            "explanation",
            "suggested_fix",
            "contract",
            "schema_violations",
            "schema_violations_withheld",
        }, entry
        assert ("explanation" in entry) == ("suggested_fix" in entry), entry
        if "contract" in entry:
            assert set(entry["contract"]) <= {"producer", "consumer", "missing_fields", "extra_fields"}, entry
        for violation in entry["schema_violations"] if "schema_violations" in entry else ():
            assert {"path", "rule"} <= set(violation), violation
            assert set(violation) <= {"path", "rule", "constraint", "detail"}, violation
        assert entry["error_class"] == error_class
    # No provider prose, plugin name, option value, or raw message may ride
    # the feedback. The static catalogue enrichment legitimately mentions the
    # WORD "options" (e.g. "patch_node_options"), so the leak canary checks
    # the redaction boundary itself: no "message" key (raw validation text)
    # and no denied-plugin identifier anywhere in the projection.
    blob = json.dumps(feedback)
    assert _POLICY_DENIED_TRANSFORM not in blob
    assert '"message"' not in blob


# --------------------------------------------------------------------------- #
# Freeform: repair success / exhaustion / policy rejection                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_freeform_one_repair_converges_to_reference_graph(parity_env: ParityEnv) -> None:
    """A malformed terminal, then a valid one: converges with repair_count == 1."""
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    completion = _ScriptedCompletion(
        emit_proposal_response(_malformed_missing_edges(valid)),
        emit_proposal_response(valid),
    )

    committed, proposal = await _drive_freeform_scripted(parity_env, _LINEAR, completion, session)

    assert_isomorphic(committed, reference, left="freeform-one-repair:linear_transform", right="reference")
    assert proposal.pipeline_metadata.repair_count == 1
    assert len(completion.requests) == 2, "exactly one malformed attempt plus one valid attempt"
    # The repair feedback that provoked the second attempt is the schema projection.
    _assert_allowlisted_feedback_shape(_last_tool_feedback(completion.requests[1]), error_class="SchemaValidationError")


@pytest.mark.asyncio
async def test_freeform_repair_exhaustion_is_translated_to_a_safe_disposition(parity_env: ParityEnv) -> None:
    """Every terminal malformed: REPAIR_EXHAUSTED → planner_repair_exhausted 500 + one closed disposition row."""
    async with parity_env._client() as client:
        created = await client.post("/api/sessions", json={"title": "freeform repair exhaustion"})
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        valid = _valid_pipeline(parity_env, _LINEAR, session_id)
        malformed = emit_proposal_response(_malformed_missing_edges(valid))
        # Budget is 2 (parity settings inherit the WebSettings default); the third
        # malformed terminal makes repair_count == 3 > 2, which now engages the
        # escape-hatch overtime turn on the advisor model. A fourth malformed
        # terminal spends the hatch, so the original REPAIR_EXHAUSTED stands.
        completion = _ScriptedCompletion(malformed, malformed, malformed, malformed)
        parity_env.monkeypatch.setattr("litellm.acompletion", completion)
        settled = await submit_and_settle(
            client,
            parity_env.app,
            path=f"/api/sessions/{session_id}/messages",
            body={"content": _LINEAR["intent"], "operation_id": str(uuid4()), "state_id": None},
        )

    status, body = settled.error()
    assert status == 500, settled.final.text
    detail = body["detail"]
    assert detail["error_type"] == "composer_planner_failure"
    assert detail["failure_code"] == "planner_repair_exhausted"
    assert len(completion.requests) == 4, "repair_budget + 1 primary calls, then the spent escape-hatch turn"
    assert completion.requests[3]["model"] != completion.requests[0]["model"], "overtime turn runs on the advisor model"

    rows = _disposition_rows(parity_env.app.state.session_engine)
    assert len(rows) == 1, "exactly one durable closed failure-disposition audit row"
    envelope = rows[0].tool_calls[0]
    assert envelope["failure_code"] == "planner_repair_exhausted"
    assert envelope["surface"] == "freeform"
    # The disposition names the wall: the last rejection's closed validation
    # codes, so a live exhaustion 500 is diagnosable from the DB alone.
    assert envelope["rejection_codes"], "exhaustion disposition must carry the last rejection codes"


@pytest.mark.asyncio
async def test_freeform_policy_denied_candidate_is_rejected_with_allowlisted_shape(parity_env: ParityEnv) -> None:
    """A policy-denied plugin is rejected with the allowlisted candidate feedback, then repaired."""
    reference = parity_env.reference_state(_LINEAR)
    session = await parity_env.sessions.create_session("alice", "Alice", "local")
    valid = _valid_pipeline(parity_env, _LINEAR, str(session.id))
    completion = _ScriptedCompletion(
        emit_proposal_response(_policy_denied_variant(valid)),
        emit_proposal_response(valid),
    )

    committed, proposal = await _drive_freeform_scripted(parity_env, _LINEAR, completion, session)

    assert_isomorphic(committed, reference, left="freeform-policy-rejection:linear_transform", right="reference")
    assert proposal.pipeline_metadata.repair_count == 1
    assert len(completion.requests) == 2
    # Candidate authorization rejection → ValidationError projection (NOT the schema
    # projection the one-repair case trips), carrying no denied-plugin leak.
    _assert_allowlisted_feedback_shape(_last_tool_feedback(completion.requests[1]), error_class="ValidationError")
