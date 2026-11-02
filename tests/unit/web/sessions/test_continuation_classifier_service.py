"""One real service classifier handoff through the Continuation worker."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.web.sessions.continuation_classifier_primary import read_original_classifier_primary_on_connection
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    ADVISOR_MODEL,
    PRIMARY_MODEL,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    sdk_reply,
    semantic_stop_response,
    submit_and_settle,
    tool_call,
)


def _draft_with_review_term() -> dict[str, object]:
    """The planner's unwired draft; its real tool result must stage review."""
    return {
        "source": {"plugin": "null", "on_success": "rows", "on_validation_failure": "discard", "options": {}},
        "nodes": [
            {
                "id": "rate_node",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "scored_rows",
                "on_error": "discard",
                "options": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4o",
                    "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                    "prompt_template": "Rate how {{interpretation:cool}} this row is.",
                    "schema": {"mode": "observed"},
                },
            }
        ],
        "edges": [],
        "outputs": [],
    }


@pytest.mark.asyncio
async def test_staged_review_classifier_uses_paid_primary_then_next_shared_end(tmp_path, monkeypatch):
    operation_id = str(uuid4())
    calls: list[dict[str, object]] = []
    async with continuation_app(tmp_path, monkeypatch) as harness:

        async def provider(**request):
            calls.append(request)
            if len(calls) == 1:
                assert request["model"] == PRIMARY_MODEL and request["tools"]
                return sdk_reply(
                    "Create the draft and ask for the user's interpretation.",
                    [
                        tool_call(harness.app, "draft_graph", "set_pipeline", _draft_with_review_term()),
                        tool_call(
                            harness.app,
                            "pending_review",
                            "request_interpretation_review",
                            {
                                "affected_node_id": "rate_node",
                                "kind": "vague_term",
                                "user_term": "cool",
                                "llm_draft": "modern, useful, engaging, and clear for the public.",
                            },
                        ),
                    ],
                )
            if len(calls) == 2:
                assert request["model"] == ADVISOR_MODEL and request["response_format"]
                return sdk_reply(
                    json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "The draft is coherent.", "note": None}),
                    model=ADVISOR_MODEL,
                )
            if len(calls) == 3:
                assert request["model"] == PRIMARY_MODEL and request["tools"]
                return sdk_reply("The draft is ready for your interpretation review.")
            if len(calls) == 4:
                assert request["model"] == ADVISOR_MODEL and request["response_format"]
                return sdk_reply(json.dumps(end_admit()), model=ADVISOR_MODEL)
            if len(calls) == 5:
                assert request["model"] == ADVISOR_MODEL and not request.get("tools")
                return sdk_reply(json.dumps(semantic_stop_response(request)), model=ADVISOR_MODEL)
            raise AssertionError("classifier or END replayed a physical provider call")

        install_sdk(monkeypatch, provider)
        result = await submit_and_settle(
            harness,
            message_body("Create a draft that rates how cool pages are, then ask me to review the term.", operation_id=operation_id),
        )

        assert result.status == "completed", (result.error, [call["model"] for call in calls])
        assert [call["model"] for call in calls] == [
            PRIMARY_MODEL,
            ADVISOR_MODEL,
            PRIMARY_MODEL,
            ADVISOR_MODEL,
            ADVISOR_MODEL,
        ]
        rows = {row.effect_key: row for row in effects(harness.app, operation_id)}
        planned = rows["provider:0:0"]
        classifier = rows["provider:0:1"]
        ((_, end),) = ((key, row) for key, row in rows.items() if key.startswith("end:"))
        assert planned.disposition == classifier.disposition == end.disposition == "committed"
        assert planned.provider_attempt_id is not None
        assert classifier.provider_attempt_id is not None and classifier.provider_attempt_id != planned.provider_attempt_id
        with harness.app.state.session_engine.connect() as conn:
            retained = read_original_classifier_primary_on_connection(
                conn, session_id=harness.session_id, operation_id=operation_id, effect_key=classifier.effect_key
            )
            attempts = tuple(conn.execute(select(physical).where(physical.c.session_id == str(harness.session_id))).all())
            shared = conn.execute(select(ordinals.c.next_ordinal).where(ordinals.c.family == 0)).scalar_one()
        assert retained.effect_key == classifier.effect_key
        assert retained.branch == "staged_review"
        assert retained.completion.message.content == "The draft is ready for your interpretation review."
        assert len(attempts) == 5 and all(attempt.settled_at is not None for attempt in attempts)
        assert len({attempt.attempt_id for attempt in attempts}) == 5
        assert all(attempt.ledger_entry_id is not None for attempt in attempts)
        assert shared == 2
