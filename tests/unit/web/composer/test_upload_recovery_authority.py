"""Available uploads must not turn a provider's explanation into a build request."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import litellm
import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_decision import AdvisorGateBlocked
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import (
    _authored_interpretation_requirement_id,
    _canonicalize_authored_interpretation_requirements,
    _options_with_default_llm_reviews,
)
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.protocol import CompositionStateData
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import _composer_service_with_session, _empty_state, _make_llm_response, _make_settings


@pytest.fixture(autouse=True)
def available_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", available)


async def _upload(service, session_id: str, data_dir: Path, content: bytes = b"Case Study _\nA synthetic case study\n"):
    sessions = service._require_sessions_service()
    blobs = BlobServiceImpl(sessions._engine, data_dir)
    with fenced_operation_context(sessions._engine, session_id, operation_kind=SessionOperationKind.CREATE) as context:
        return await blobs.create_blob(UUID(session_id), "cases.csv", content, "text/csv", session_operation_context=context)


def _advisor_reply():
    return _make_llm_response(content=json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}))


def _sdk_reply(model, *, content=None, tool_calls=None):
    """Keep the existing tool framing while supplying real SDK usage metadata."""
    message = _make_llm_response(content=content, tool_calls=tool_calls).choices[0].message
    wire_calls = [
        {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
        for call in (message.tool_calls or ())
    ]
    return litellm.ModelResponse(
        model=model,
        choices=[
            {
                "message": {"role": "assistant", "content": content, "tool_calls": wire_calls or None},
                "finish_reason": "tool_calls" if wire_calls else "stop",
            }
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


@contextmanager
def _observe_real_end(service, reviews, outcomes):
    """Observe and forward the real checkpoint, including its owned binding."""
    actual = service._advisor_checkpoint._run_advisor_checkpoint
    assert actual.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint
    assert actual.__module__ == "elspeth.web.composer.advisor_checkpoint"

    async def observe(*args, **kwargs):
        if kwargs["phase"] == "end":
            reviews.append(kwargs["publication_review"])
        verdict = await actual(*args, **kwargs)
        if kwargs["phase"] == "end":
            assert verdict.publication_binding == reviews[-1].binding
            outcomes.append(verdict)
        return verdict

    with patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", new=observe):
        yield


def _assert_end_packet_sent(kwargs, review, message):
    text = [
        part
        for item in kwargs["messages"]
        for part in ((item["content"],) if isinstance(item["content"], str) else tuple(part["text"] for part in item["content"]))
    ]
    assert any(review.provider_payload_json in part for part in text)
    payload = json.loads(review.payload_json)
    assert payload["current_request"] == message
    assert payload["saved_effects"]["saved_revision_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("uploaded", [False, True], ids=["without_upload", "with_upload"])
@pytest.mark.parametrize(
    ("message", "publications"),
    [
        ("Do not build anything. Just explain what a CSV source is.", ("ADMIT",)),
        ("Hello!", ("ADMIT",)),
        ("Build from my uploaded CSV", ("WITHHOLD", "WITHHOLD")),
    ],
    ids=["no_build", "greeting", "build_stall"],
)
async def test_upload_availability_does_not_force_construction(
    tmp_path: Path, uploaded: bool, message: str, publications: tuple[str, ...]
) -> None:
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    if uploaded:
        await _upload(service, session_id, tmp_path)
    observed = []
    reviews = []
    outcomes = []
    phases = []
    explanation = "I will only explain the options and leave the pipeline unchanged."
    findings = "The authorized build remains unstarted; upload availability alone is not evidence of a configured pipeline."

    async def complete(**kwargs: Any):
        if kwargs["model"] == service._settings.composer_advisor_model:
            assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
            assert len(reviews) <= len(publications)
            phases.append("end")
            _assert_end_packet_sent(kwargs, reviews[-1], message)
            assert json.loads(reviews[-1].payload_json)["candidate_reply"] == explanation
            publication = publications[len(reviews) - 1]
            return _sdk_reply(
                kwargs["model"],
                content=json.dumps(
                    {
                        "verdict": "CLEAN",
                        "category": "other",
                        "steps": [],
                        "findings": findings if publication == "WITHHOLD" else "",
                        "note": None,
                        "publication_verdict": publication,
                    }
                ),
            )
        assert kwargs["model"] == service._model
        phases.append("primary")
        observed.append(deepcopy(kwargs["messages"]))
        assert len(observed) <= len(publications), "Unexpected lexical PRIMARY retry"
        return _sdk_reply(kwargs["model"], content=explanation)

    initial = _empty_state()
    with _observe_real_end(service, reviews, outcomes), patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(message, [], initial, session_id=session_id)

    assert len(observed) == len(reviews) == len(outcomes) == len(publications)
    assert phases == [phase for _publication in publications for phase in ("primary", "end")]
    assert [outcome.publication_verdict for outcome in outcomes] == list(publications)
    assert len({review.binding for review in reviews}) == len(reviews)
    assert result.repair_turns_used == 0
    assert result.state == initial
    assert not result.tool_invocations
    assert all(
        "Continue by calling a build/edit tool" not in str(item["content"]) for turn in observed for item in turn if item["role"] == "user"
    )
    assert all(any(item["role"] == "user" and item["content"] == message for item in turn) for turn in observed)
    if len(publications) == 2:
        repair = observed[1][-1]["content"]
        assert findings in repair
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert any(item["role"] == "user" and item["content"] == message for item in observed[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("inspect_first", [False, True], ids=["no_tools", "discovery_before_stall"])
async def test_authorized_uploaded_build_recovers_through_provider_and_real_tools(tmp_path: Path, inspect_first: bool) -> None:
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    blob = await _upload(service, session_id, tmp_path)
    # The selected upload is named by the user, not supplied by a lexical retry.
    message = f"Build a CSV report from my uploaded file {blob.id}."
    proposal = {
        "source": {
            "blob_id": str(blob.id),
            "plugin": "csv",
            "on_success": "report",
            "on_validation_failure": "discard",
            "options": {"schema": {"mode": "observed"}},
        },
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "report",
                "plugin": "csv",
                "options": {
                    "path": str(tmp_path / "outputs" / session_id / "report.csv"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Uploaded CSV report"},
    }
    calls = 0
    advisor_calls = 0
    observed = []
    reviews = []
    outcomes = []
    phases = []
    findings = "The current request authorizes the uploaded CSV report, but no pipeline has been saved."

    async def complete(**kwargs: Any):
        nonlocal calls, advisor_calls
        if kwargs["model"] == service._settings.composer_advisor_model:
            advisor_calls += 1
            schema_name = kwargs["response_format"]["json_schema"]["name"]
            assert schema_name in {"advisor_checkpoint_response", "advisor_end_response"}
            phase = "end" if schema_name == "advisor_end_response" else "early"
            phases.append(phase)
            payload = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
            if phase == "end":
                assert len(reviews) <= 2
                first = len(reviews) == 1
                if first:
                    _assert_end_packet_sent(kwargs, reviews[-1], message)
                    assert json.loads(reviews[-1].payload_json)["candidate_reply"] == "I can build a report from your uploaded CSV."
                else:
                    assert any(
                        reviews[-1].provider_payload_json
                        in (item["content"] if isinstance(item["content"], str) else "".join(part["text"] for part in item["content"]))
                        for item in kwargs["messages"]
                    )
                    packet = json.loads(reviews[-1].payload_json)
                    assert packet["current_request"] == message
                    assert packet["candidate_reply"] == "The CSV report pipeline is configured."
                    assert packet["saved_effects"]["saved_revision_count"] > 0
                    assert packet["saved_effects"]["net_changed"] is True
                payload["publication_verdict"] = "WITHHOLD" if first else "ADMIT"
                payload["findings"] = findings if first else ""
            return _sdk_reply(kwargs["model"], content=json.dumps(payload))
        assert kwargs["model"] == service._model
        phases.append("primary")
        observed.append(deepcopy(kwargs["messages"]))
        index = calls
        calls += 1
        if inspect_first and index == 0:
            return _sdk_reply(
                kwargs["model"], tool_calls=[{"id": "inspect", "name": "inspect_source", "arguments": {"blob_id": str(blob.id)}}]
            )
        if index == int(inspect_first):
            return _sdk_reply(kwargs["model"], content="I can build a report from your uploaded CSV.")
        if index == int(inspect_first) + 1:
            repair = kwargs["messages"][-1]["content"]
            assert findings in repair
            assert len(reviews) == 1
            assert str(blob.id) in json.dumps(kwargs["messages"])
            assert any(item["role"] == "user" and item["content"] == message for item in kwargs["messages"])
            if inspect_first:
                assert "No tool has run" not in repair
                assert any(item["role"] == "tool" and item["tool_call_id"] == "inspect" for item in kwargs["messages"])
            return _sdk_reply(kwargs["model"], tool_calls=[{"id": "build", "name": "set_pipeline", "arguments": proposal}])
        assert index == int(inspect_first) + 2
        return _sdk_reply(kwargs["model"], content="The CSV report pipeline is configured.")

    with _observe_real_end(service, reviews, outcomes), patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(message, [], _empty_state(), session_id=session_id)

    assert calls == 3 + int(inspect_first)
    assert advisor_calls > 0
    assert phases == (["primary"] if inspect_first else []) + ["primary", "end", "primary", "early", "primary", "end"]
    assert advisor_calls == 3
    assert [outcome.publication_verdict for outcome in outcomes] == ["WITHHOLD", "ADMIT"]
    assert reviews[0].binding != reviews[1].binding
    assert result.repair_turns_used == 0
    assert result.state.sources["source"].options["blob_ref"] == str(blob.id)
    assert [output.name for output in result.state.outputs] == ["report"]
    assert any(invocation.tool_name == "set_pipeline" and invocation.status.value == "success" for invocation in result.tool_invocations)
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.readiness.authoring_valid is True
    assert result.runtime_preflight.readiness.execution_ready is True


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_authorized", [False, True], ids=["explain_without_changes", "repair_requested"])
@pytest.mark.parametrize("failure_kind", ["source_proof", "runtime_preflight"])
async def test_saved_validation_blocker_preserves_request_and_readiness(tmp_path: Path, repair_authorized: bool, failure_kind: str) -> None:
    """A saved invalid draft is evidence of a problem, not permission to edit."""
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    blob = await _upload(service, session_id, tmp_path, b"case_study,note\nA synthetic case study,extra field\n")
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="report",
            on_validation_failure="discard",
            options={
                "path": blob.storage_path,
                "blob_ref": str(blob.id),
                "mode": "bind_source",
                "schema": {"mode": "fixed" if failure_kind == "source_proof" else "flexible", "fields": ["case_study: str"]},
            },
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="report",
                plugin="csv",
                on_write_failure="discard",
                options={
                    "path": str(tmp_path / "outputs" / session_id / "report.csv"),
                    "schema": {"mode": "observed"},
                    "mode": "write" if failure_kind == "source_proof" else "invalid",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(name="Saved CSV draft"),
        version=1,
    )
    sessions = service._require_sessions_service()
    payload = state.to_dict()
    saved = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=state.validate().is_valid,
            validation_errors=(),
        ),
        provenance="session_seed",
    )
    message = (
        "Repair the saved CSV pipeline."
        if repair_authorized
        else "Do not build or change anything. Explain why the saved CSV draft cannot run."
    )
    calls = 0
    advisor_calls = 0
    observed = []

    async def complete(**kwargs: Any):
        nonlocal calls, advisor_calls
        if kwargs["model"] == service._settings.composer_advisor_model:
            advisor_calls += 1
            return _advisor_reply()
        observed.append(deepcopy(kwargs["messages"]))
        index = calls
        calls += 1
        if index == 1 and repair_authorized:
            return _make_llm_response(
                tool_calls=[
                    {
                        "id": "repair_schema",
                        "name": "patch_source_options" if failure_kind == "source_proof" else "patch_output_options",
                        "arguments": (
                            {"patch": {"schema": {"mode": "flexible", "fields": ["case_study: str"]}}}
                            if failure_kind == "source_proof"
                            else {"sink_name": "report", "patch": {"mode": "write"}}
                        ),
                    }
                ]
            )
        return _make_llm_response(content="The saved draft has an invalid contract and cannot run.")

    with patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(message, [], state, session_id=session_id, current_state_id=str(saved.id))

    assert calls == 3
    repair_message = observed[1][-1]["content"]
    if failure_kind == "source_proof":
        assert "csv_fixed_schema_omits_observed_columns" in repair_message
    else:
        assert "runtime preflight" in repair_message
        assert "contract violation" in repair_message
    assert "Do not respond to the user yet; resolve these first" not in repair_message
    assert "authorizes changes" in repair_message
    assert "without changing the pipeline" in repair_message
    assert any(item["role"] == "user" and item["content"] == message for item in observed[1])
    assert result.runtime_preflight is not None
    if repair_authorized:
        assert advisor_calls > 0
        assert result.state.sources["source"].options["schema"]["mode"] == "flexible"
        expected_tool = "patch_source_options" if failure_kind == "source_proof" else "patch_output_options"
        assert any(invocation.tool_name == expected_tool for invocation in result.tool_invocations)
        assert result.runtime_preflight.readiness.execution_ready is True
    else:
        assert result.state == state
        assert not result.tool_invocations
        assert result.runtime_preflight.readiness.execution_ready is False
        assert result.runtime_preflight.readiness.completion_ready is False
        current = await sessions.get_current_state(UUID(session_id))
        assert current is not None
        assert current.id == saved.id


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_authorized", [False, True], ids=["explain_without_changes", "repair_requested"])
async def test_saved_advisor_findings_preserve_request_and_completion_gate(tmp_path: Path, repair_authorized: bool) -> None:
    from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END

    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=_make_settings(data_dir=tmp_path))
    blob = await _upload(service, session_id, tmp_path)
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="report",
            on_validation_failure="discard",
            options={"path": blob.storage_path, "blob_ref": str(blob.id), "mode": "bind_source", "schema": {"mode": "observed"}},
        ),
        nodes=(),
        edges=(),
        outputs=(
            OutputSpec(
                name="report",
                plugin="csv",
                on_write_failure="discard",
                options={
                    "path": str(tmp_path / "outputs" / session_id / "report.csv"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(name="Saved CSV report"),
        version=1,
    )
    sessions = service._require_sessions_service()
    payload = state.to_dict()
    saved = await sessions.save_composition_state(
        UUID(session_id),
        CompositionStateData(
            sources=payload["sources"],
            nodes=payload["nodes"],
            edges=payload["edges"],
            outputs=payload["outputs"],
            metadata_=payload["metadata"],
            is_valid=state.validate().is_valid,
            validation_errors=(),
        ),
        provenance="session_seed",
    )
    message = (
        "Make case_study required in the saved CSV report pipeline."
        if repair_authorized
        else "Do not change anything. Explain the saved CSV report pipeline."
    )
    primary = []
    advisor_calls = 0
    reviews = []

    async def complete(**kwargs: Any):
        nonlocal advisor_calls
        if kwargs["model"] == service._settings.composer_advisor_model:
            advisor_calls += 1
            assert advisor_calls <= 2, "Only two END responses are supplied"
            review_json = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(review_json.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            reviews.append(review)
            assert review["current_request"] == message
            repaired = repair_authorized and advisor_calls == 2
            assert review["candidate_reply"] == (
                "case_study is required in the CSV source contract." if repaired else "The CSV source feeds the report output."
            )
            assert review["saved_effects"]["saved_revision_count"] == int(repaired)
            assert review["saved_effects"]["net_changed"] is repaired
            if repaired:
                current = await sessions.get_current_state(UUID(session_id))
                assert current is not None and current.sources is not None
                assert current.sources["source"]["options"]["schema"] == {"mode": "flexible", "fields": ("case_study: str",)}
            return _make_llm_response(
                content=json.dumps(
                    {
                        "verdict": "CLEAN" if repaired else "FLAGGED",
                        "category": "other",
                        "steps": [],
                        "findings": "" if repaired else "The saved report has no explicit required case_study runtime contract.",
                        "note": None,
                        "publication_verdict": "ADMIT" if repaired else "WITHHOLD",
                    }
                )
            )
        assert kwargs["model"] == service._model, "No response is supplied for another provider role"
        primary.append(deepcopy(kwargs["messages"]))
        assert len(primary) <= (3 if repair_authorized else 2), "No additional primary response is supplied"
        if len(primary) == 2 and repair_authorized:
            return _make_llm_response(
                tool_calls=[
                    {
                        "id": "repair_contract",
                        "name": "patch_source_options",
                        "arguments": {"patch": {"schema": {"mode": "flexible", "fields": ["case_study: str"]}}},
                    }
                ]
            )
        return _make_llm_response(
            content=(
                "case_study is required in the CSV source contract."
                if repair_authorized and len(primary) == 3
                else "The CSV source feeds the report output."
            )
        )

    with patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(message, [], state, session_id=session_id, current_state_id=str(saved.id))

    assert advisor_calls == 2
    assert len(primary) == (3 if repair_authorized else 2)
    assert len(result.llm_calls) == len(primary) + advisor_calls
    assert (reviews[0]["current_graph_hash"] != reviews[1]["current_graph_hash"]) is repair_authorized
    stored = await sessions.get_messages(UUID(session_id), limit=None)
    passes = [
        row.tool_calls[0]["pass"]
        for row in stored
        if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    assert [(item["verdict"], item["publication_verdict"], item["provider_attempts"]) for item in passes] == [
        ("flagged", "WITHHOLD", 1),
        ("clean", "ADMIT", 1) if repair_authorized else ("flagged", "WITHHOLD", 1),
    ]
    assert all(item["phase"] == "end" and item["source"] == "model" and item["first_attempt_accepted"] is True for item in passes)
    assert all(type(item["publication_binding"]) is str and len(item["publication_binding"]) == 64 for item in passes)
    assert passes[0]["publication_binding"] != passes[1]["publication_binding"]
    repair_message = primary[1][-1]["content"]
    assert "Completion advisory review" in repair_message
    assert "active request authorizes" in repair_message
    assert "without changing the pipeline" in repair_message
    assert any(item["role"] == "user" and item["content"] == message for item in primary[1])
    assert result.runtime_preflight is not None
    if repair_authorized:
        from elspeth.web.composer.no_tool_policy import AssistantTextSegment, TrustedSystemNoticeSegment, visible_message_segments

        raw = "case_study is required in the CSV source contract."
        notice = (
            "Checkpoint save status: Verified checkpoint evidence covers 1 saved draft revisions. "
            "There are net authored changes from the starting draft. Checkpoint evidence does not establish task completion."
        )
        assert result.raw_assistant_content == raw
        assert result.message == raw + "\n\n---\n\n[ELSPETH-SYSTEM] " + notice
        assert visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
            AssistantTextSegment(raw),
            TrustedSystemNoticeSegment(notice),
        )
        assert result.state.sources["source"].options["schema"] == {"mode": "flexible", "fields": ("case_study: str",)}
        assert any(
            invocation.tool_name == "patch_source_options" and invocation.status.value == "success"
            for invocation in result.tool_invocations
        )
        assert result.runtime_preflight.readiness.execution_ready is True
    else:
        assert result.state == state
        assert not result.tool_invocations
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert result.runtime_preflight.readiness.completion_ready is False
        current = await sessions.get_current_state(UUID(session_id))
        assert current is not None
        assert current.id == saved.id


@pytest.mark.asyncio
@pytest.mark.parametrize("repair_authorized", [False, True], ids=["explain_without_changes", "repair_requested"])
@pytest.mark.parametrize("capped", [False, True], ids=["pending_orphan", "rate_capped_orphan"])
@pytest.mark.parametrize("wired", [False, True], ids=["unresolvable_wiring", "resolvable_handoff"])
async def test_saved_orphan_recovery_preserves_request_and_acknowledgement(
    tmp_path: Path, repair_authorized: bool, capped: bool, wired: bool
) -> None:
    settings = _make_settings(data_dir=tmp_path, composer_interpretation_rate_limit_per_session_day=1 if capped else 20)
    service, session_id = _composer_service_with_session(catalog=create_catalog_service(), settings=settings)
    blob = await _upload(service, session_id, tmp_path)
    node = NodeSpec(
        id="assess",
        node_type="transform",
        plugin="llm",
        input="cases",
        on_success="report",
        on_error="discard",
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        options={
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "region_name": "us-east-1",
            "system_prompt": "Assess case-study quality against the supplied rubric.",
            "prompt_template": "Assess {{ row.case_study }} in a pending interpretation tone.",
            "prompt_template_parts": [
                {"kind": "text", "text": "Assess {{ row.case_study }} in a "},
                {"kind": "interpretation_ref", "requirement_id": "tone_review"},
                {"kind": "text", "text": " tone."},
            ],
            "required_input_fields": ["case_study"],
            "response_field": "assessment",
            "schema": {"mode": "observed"},
            "interpretation_requirements": [
                {"id": "tone_review", "kind": "vague_term", "user_term": "tone", "draft": "professional", "status": "pending"}
            ],
        },
    )
    options = dict(node.options)
    options["interpretation_requirements"] = [{"kind": "vague_term", "user_term": "tone", "draft": "professional"}]
    prompt_parts = [dict(part) for part in options["prompt_template_parts"]]
    prompt_parts[1]["requirement_id"] = _authored_interpretation_requirement_id(component_id="assess", user_term="tone")
    options["prompt_template_parts"] = prompt_parts
    options = dict(
        _options_with_default_llm_reviews(
            node_id="assess",
            plugin="llm",
            options=_canonicalize_authored_interpretation_requirements(options, component_id="assess"),
        )
    )
    if not wired:
        del options["prompt_template_parts"]
    node = replace(node, options=options)
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="cases",
            on_validation_failure="discard",
            options={
                "path": blob.storage_path,
                "blob_ref": str(blob.id),
                "mode": "bind_source",
                "schema": {"mode": "flexible", "fields": ["case_study: str"]},
            },
        ),
        nodes=(node,),
        edges=(),
        outputs=(
            OutputSpec(
                name="report",
                plugin="csv",
                on_write_failure="discard",
                options={
                    "path": str(tmp_path / "outputs" / session_id / "report.csv"),
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
            ),
        ),
        metadata=PipelineMetadata(name="Saved assessment draft"),
        version=1,
    )
    sessions = service._require_sessions_service()

    async def save(candidate: CompositionState):
        payload = candidate.to_dict()
        return await sessions.save_composition_state(
            UUID(session_id),
            CompositionStateData(
                sources=payload["sources"],
                nodes=payload["nodes"],
                edges=payload["edges"],
                outputs=payload["outputs"],
                metadata_=payload["metadata"],
                is_valid=candidate.validate().is_valid,
                validation_errors=(),
            ),
            provenance="session_seed",
        )

    if capped:
        prior_state = replace(state, nodes=(replace(node, options={**node.options, "prompt_template_parts": prompt_parts}),))
        prior = await save(prior_state)
        with fenced_operation_context(sessions._engine, session_id, operation_kind=SessionOperationKind.COMPOSE) as context:
            await sessions.create_pending_interpretation_event(
                session_id=UUID(session_id),
                composition_state_id=prior.id,
                affected_node_id="assess",
                tool_call_id="prior_review",
                user_term="tone",
                kind=InterpretationKind.VAGUE_TERM,
                llm_draft="professional",
                model_identifier=settings.composer_model,
                model_version="test",
                provider="test",
                composer_skill_hash=service._composer_skill_hash,
                session_operation_context=context,
            )
        # Remove the original site so its real pending event is superseded, then
        # restore a saved orphan on a new branch. It still consumes today's cap.
        await save(replace(state, nodes=(), version=2))
    saved = await save(state)
    message = (
        "Repair the saved assessment pipeline."
        if repair_authorized
        else "Do not change anything. Explain why the saved assessment draft cannot run."
    )
    observed = []

    async def complete(**kwargs: Any):
        if kwargs["model"] == settings.composer_advisor_model:
            return _advisor_reply()
        observed.append(deepcopy(kwargs["messages"]))
        if repair_authorized and not capped and not wired and len(observed) == 2:
            return _make_llm_response(
                tool_calls=[
                    {
                        "id": "wire_orphan",
                        "name": "patch_node_options",
                        "arguments": {"node_id": "assess", "patch": {"prompt_template_parts": prompt_parts}},
                    }
                ]
            )
        review_turn = 3 if not capped and not wired else 2
        if len(observed) == review_turn and repair_authorized:
            tool = (
                {
                    "id": "repair_orphan",
                    "name": "patch_node_options",
                    "arguments": {
                        "node_id": "assess",
                        "patch": {
                            "prompt_template": "Assess {{ row.case_study }} in a professional tone.",
                            "prompt_template_parts": None,
                            "interpretation_requirements": [],
                        },
                    },
                }
                if capped
                else {
                    "id": "review_orphan",
                    "name": "request_interpretation_review",
                    "arguments": {"affected_node_id": "assess", "kind": "vague_term", "user_term": "tone"},
                }
            )
            return _make_llm_response(
                tool_calls=[
                    tool,
                    {
                        "id": "review_model",
                        "name": "request_interpretation_review",
                        "arguments": {"affected_node_id": "assess", "kind": "llm_model_choice", "user_term": "llm_model_choice:assess"},
                    },
                ]
            )
        return _make_llm_response(content="The saved draft has an unresolved tone interpretation.")

    with patch("litellm.acompletion", side_effect=complete):
        result = await service.compose(message, [], state, session_id=session_id, current_state_id=str(saved.id))

    repair_message = observed[1][-1]["content"]
    assert "vague_term:assess:tone" in repair_message
    if capped:
        assert "interpretation request limit" in repair_message
    assert "Do not reply to the user yet" not in repair_message
    assert "active request authorizes" in repair_message
    assert "without changing the pipeline" in repair_message
    assert any(item["role"] == "user" and item["content"] == message for item in observed[1])
    assert result.runtime_preflight is not None
    if repair_authorized:
        expected_tool = "patch_node_options" if capped else "request_interpretation_review"
        assert any(invocation.tool_name == expected_tool and invocation.status.value == "success" for invocation in result.tool_invocations)
        assert all(invocation.status.value == "success" for invocation in result.tool_invocations), [
            (invocation.tool_name, invocation.error_message) for invocation in result.tool_invocations
        ]
        assert all(json.loads(invocation.result_canonical)["success"] is True for invocation in result.tool_invocations), [
            invocation.result_canonical for invocation in result.tool_invocations
        ]
        assert all(blocker.code != "interpretation_review_orphaned" for blocker in result.runtime_preflight.readiness.blockers), (
            result.runtime_preflight
        )
        pending = await sessions.list_interpretation_events(UUID(session_id), status="pending")
        assert pending
        assert result.runtime_preflight.readiness.execution_ready is False
        if not capped:
            assert any(event.kind is InterpretationKind.VAGUE_TERM and event.user_term == "tone" for event in pending)
    else:
        assert result.state == state
        assert not result.tool_invocations
        assert result.runtime_preflight.readiness.execution_ready is False
        missing = await service._interpretation_surfacing._missing_pending_interpretation_review_sites(result.state, session_id=session_id)
        pending = await sessions.list_interpretation_events(UUID(session_id), status="pending")
        if wired:
            assert missing == ()
            assert {event.kind for event in pending} == {
                InterpretationKind.VAGUE_TERM,
                InterpretationKind.LLM_MODEL_CHOICE,
                InterpretationKind.LLM_PROMPT_TEMPLATE,
            }
            assert all(event.tool_call_id.startswith("backend_auto_surface:") for event in pending)
            assert result.runtime_preflight.readiness.completion_ready is True
        else:
            assert ("assess", "tone", InterpretationKind.VAGUE_TERM) in missing
            assert not any(event.kind is InterpretationKind.VAGUE_TERM for event in pending)
            assert result.runtime_preflight.readiness.completion_ready is False
            assert any(blocker.code == "interpretation_review_orphaned" for blocker in result.runtime_preflight.readiness.blockers)
        current = await sessions.get_current_state(UUID(session_id))
        assert current is not None
        assert current.id == saved.id
