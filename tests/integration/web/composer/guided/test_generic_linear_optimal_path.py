"""Real guided-service acceptance for the generic linear optimal path."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    RAW_HTML_CLEANUP_REVIEW_DRAFT,
    RAW_HTML_CLEANUP_USER_TERM,
)


class _OptimalPathCompletion:
    def __init__(self, pipeline: dict[str, object]) -> None:
        self.pipeline = pipeline
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> SimpleNamespace:
        self.requests.append(kwargs)
        calls = [("transform", "web_scrape"), ("transform", "llm"), ("transform", "field_mapper")] if len(self.requests) == 1 else []
        tool_calls = [
            SimpleNamespace(
                id=f"schema-{index}",
                function=SimpleNamespace(
                    name="get_plugin_schema",
                    arguments=json.dumps({"plugin_type": plugin_type, "name": name}),
                ),
            )
            for index, (plugin_type, name) in enumerate(calls, start=1)
        ]
        if not tool_calls:
            tool_calls = [
                SimpleNamespace(
                    id="proposal",
                    function=SimpleNamespace(
                        name="emit_pipeline_proposal",
                        arguments=json.dumps({"pipeline": self.pipeline}),
                    ),
                )
            ]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=tool_calls))],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01},
            model="provider/planner-v1",
            id=f"optimal-{len(self.requests)}",
        )


def _document_abstract_pipeline(session_id: str) -> dict[str, object]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "documents",
            "options": {
                "path": f"blobs/{session_id}/documents.csv",
                "schema": {
                    "mode": "flexible",
                    "fields": ["document_uri: str"],
                    "guaranteed_fields": ["document_uri"],
                },
            },
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "fetch_document",
                "node_type": "transform",
                "plugin": "web_scrape",
                "input": "documents",
                "on_success": "fetched_documents",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "url_field": "document_uri",
                    "content_field": "document_content",
                    "fingerprint_field": "document_fingerprint",
                    "http": {
                        "abuse_contact": "data-steward@agency.gov.au",
                        "scraping_reason": "Retrieve user-requested documents",
                    },
                },
            },
            {
                "id": "write_abstract",
                "node_type": "transform",
                "plugin": "llm",
                "input": "fetched_documents",
                "on_success": "abstracted_documents",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "profile": "task-role",
                    "prompt_template": "Write an abstract of {{ row.document_content }}",
                    "required_input_fields": ["document_content"],
                    "response_field": "abstract",
                },
            },
            {
                "id": "retain_public_fields",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "abstracted_documents",
                "on_success": "result",
                "on_error": "discard",
                "options": {
                    "schema": {
                        "mode": "flexible",
                        "fields": ["document_uri: str", "abstract: str"],
                        "guaranteed_fields": ["document_uri", "abstract"],
                    },
                    "mapping": {"document_uri": "document_uri", "abstract": "abstract"},
                    "select_only": True,
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": "pipeline_decision",
                            "user_term": RAW_HTML_CLEANUP_USER_TERM,
                            "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
                        }
                    ],
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "result",
                "plugin": "json",
                "options": {
                    "path": "outputs/document_abstracts.json",
                    "schema": {"mode": "fixed", "fields": ["document_uri: str", "abstract: str"]},
                    "format": "json",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Document abstract pipeline"},
    }


def test_real_guided_service_discovers_once_then_commits_canonical_document_abstract_proposal(
    composer_test_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = composer_test_client.post("/api/sessions", json={"title": "document abstracts"}).json()
    app = composer_test_client.app
    completion = _OptimalPathCompletion(_document_abstract_pipeline(str(session["id"])))
    real_service = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=app.state.session_service,
    )
    real_service._availability = ComposerAvailability(
        available=True,
        model="openrouter/planner-under-test",
        provider="openrouter",
    )
    app.state.composer_service = real_service
    monkeypatch.setattr("elspeth.web.composer.service._litellm_acompletion", completion)

    planned = composer_test_client.post(
        f"/api/sessions/{session['id']}/guided/plan",
        json={
            "operation_id": "00000000-0000-4000-8000-000000000099",
            "intent": (
                "Fetch each document_uri, write an abstract, and retain only document_uri and abstract. "
                "Use data-steward@agency.gov.au as the abuse contact and "
                "'Retrieve user-requested documents' as the scraping reason."
            ),
        },
    )

    assert planned.status_code == 200, planned.text
    pending = planned.json()
    assert pending["status"] == "pending"
    assert pending["pipeline_metadata"]["repair_count"] == 0
    assert len(completion.requests) == 2
    second_messages = completion.requests[1]["messages"]
    discovery_assistant = next(message for message in second_messages if message["role"] == "assistant")
    assert len(discovery_assistant["tool_calls"]) == 3
    assert {call["function"]["name"] for call in discovery_assistant["tool_calls"]} == {"get_plugin_schema"}

    accepted = composer_test_client.post(
        f"/api/sessions/{session['id']}/proposals/{pending['id']}/accept",
        json={"draft_hash": pending["pipeline_metadata"]["draft_hash"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "committed"
    current = asyncio.run(app.state.session_service.get_current_state(UUID(str(session["id"]))))
    assert current is not None
    assert [node["id"] for node in current.nodes] == ["fetch_document", "write_abstract", "retain_public_fields"]
    mapper = current.nodes[2]["options"]
    assert mapper["mapping"] == {"document_uri": "document_uri", "abstract": "abstract"}
    assert mapper["select_only"] is True
