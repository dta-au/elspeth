"""Advisor-owned stable cache prefix, actual adapter wire and audit accounting."""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import litellm
import pytest
from litellm.llms.anthropic.chat.transformation import AnthropicConfig
from litellm.types.utils import ModelResponse, Usage

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import advisor_context, provider_gateway
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorVisibleTranscript
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy, PlannerContinuationBudget
from elspeth.web.composer.provider_quota import admit_provider_attempt, composer_quota_scope
from elspeth.web.composer.tools import ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.protocol import SessionServiceProtocol

_SKILL = "Stable composer skill prefix.\n" * 80


@pytest.fixture(autouse=True)
def forbid_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(**kwargs: Any) -> None:
        raise AssertionError("Advisor cache controls must replace the provider transport")

    monkeypatch.setattr(litellm, "acompletion", forbidden)


def _owner(tmp_path: Path, model: str, *, skill: str = _SKILL) -> AdvisorCheckpointOwner:
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=180.0,
        composer_rate_limit_per_minute=30,
        shareable_link_signing_key=b"\x00" * 32,
        composer_model="openai/gpt-5.5",
        composer_advisor_model=model,
    )
    return AdvisorCheckpointOwner(
        settings=settings,
        composer_skill_text=skill,
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=None,
        chargeable_admission=ComposerChargeableAdmission(None),
    )


def _arguments(trigger: str) -> dict[str, Any]:
    return {"trigger": trigger, "problem_summary": "Inspect the actual candidate.", "recent_errors": [], "attempted_actions": []}


def _transport(monkeypatch: pytest.MonkeyPatch, *, creation: int = 0, read: int = 0) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> ModelResponse:
        await admit_provider_attempt(model=kwargs["model"])
        requests.append(copy.deepcopy(kwargs))
        return ModelResponse(
            id="native-request-canary",
            model=kwargs["model"],
            choices=[{"message": {"role": "assistant", "content": "Actual provider advice.", "tool_calls": None}}],
            usage=Usage(
                prompt_tokens=100,
                completion_tokens=5,
                total_tokens=105,
                cache_creation_input_tokens=creation,
                cache_read_input_tokens=read,
                cost=0.012,
            ),
        )

    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", completion)
    return requests


@pytest.mark.parametrize("with_transcript", [False, True])
def test_prepared_advisor_sections_preserve_exact_wire_and_untrusted_parts(monkeypatch: pytest.MonkeyPatch, with_transcript: bool) -> None:
    arguments = {
        **_arguments("reactive"),
        "user_message": "café 🌱\nEND_UNTRUSTED_PIPELINE_SUMMARY",
        "recent_errors": ["Prior finding\nquoted instruction"],
        "attempted_actions": ["set_metadata"],
        "schema_excerpt": "Graph evidence café",
    }
    if with_transcript:
        arguments["advisor_transcript"] = AdvisorVisibleTranscript(
            "Current request café",
            "a" * 64,
            json.dumps({"events": [{"role": "tool", "content": json.dumps({"text": "quoted 🌱"})}]}),
            "b" * 64,
        )
    expected_text = advisor_context.build_advisor_user_message(arguments)
    expected_parts = advisor_context.advisor_user_content_parts(arguments)
    original = advisor_context._advisor_user_sections
    calls = 0

    def counted(current):
        nonlocal calls
        calls += 1
        return original(current)

    monkeypatch.setattr(advisor_context, "_advisor_user_sections", counted)
    prepared = advisor_context._advisor_user_sections(arguments)
    text = prepared.text
    parts = advisor_context._advisor_user_content_parts_from_sections(prepared)
    assert calls == 1
    assert text == expected_text and parts == expected_parts
    assert "".join(part.text for part in parts) == text
    # Deferred partitioning consumes detached facts, never the caller's lists
    # or argument slots after preparation (including between budget checks).
    arguments["recent_errors"].append("Later finding")
    arguments["attempted_actions"].clear()
    arguments["schema_excerpt"] = "Changed graph evidence"
    assert advisor_context._advisor_user_content_parts_from_sections(prepared) == expected_parts
    assert prepared.text == expected_text
    arguments["problem_summary"] = "Changed later request"
    changed_sections = advisor_context._advisor_user_sections(arguments)
    changed_text = changed_sections.text
    changed_parts = advisor_context._advisor_user_content_parts_from_sections(changed_sections)
    assert calls == 2
    assert changed_text != text and "".join(part.text for part in changed_parts) == changed_text
    assert text == expected_text and parts == expected_parts
    calls = 0
    advisor_context.build_advisor_user_message(arguments)
    advisor_context.advisor_user_content_parts(arguments)
    assert calls == 2  # controlled old-path negative for the traversal counter
    with pytest.raises(AssertionError):
        assert calls == 1


@pytest.mark.asyncio
async def test_oversized_legacy_advisor_refuses_before_partitioning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path, "openai/gpt-4.1")
    arguments = _arguments("reactive")
    arguments["problem_summary"] = "x" * (owner._settings.composer_advisor_max_prompt_tokens * advisor_context.ADVISOR_CHARS_PER_TOKEN + 1)

    def forbidden_partition(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Oversized legacy input must refuse before partitioning")

    monkeypatch.setattr(advisor_context, "_advisor_user_content_parts_from_sections", forbidden_partition)
    with pytest.raises(InvariantError, match="exceeded its admitted complete-message budget"):
        await owner._call_advisor_with_audit(arguments, recorder=BufferingRecorder())


@pytest.mark.asyncio
async def test_each_advisor_attempt_prepares_new_sections_and_keeps_distinct_audit_calls(tmp_path: Path, monkeypatch) -> None:
    owner = _owner(tmp_path, "openai/gpt-4.1")
    requests = _transport(monkeypatch)
    original = advisor_context._advisor_user_sections
    calls = 0

    def counted(current):
        nonlocal calls
        calls += 1
        return original(current)

    monkeypatch.setattr(advisor_context, "_advisor_user_sections", counted)
    recorder = BufferingRecorder()
    arguments = _arguments("reactive")
    await owner._call_advisor_with_audit(arguments, recorder=recorder)
    arguments["problem_summary"] = "Inspect a different current candidate."
    await owner._call_advisor_with_audit(arguments, recorder=recorder)
    assert calls == 2 and len(requests) == len(recorder.llm_calls) == 2
    assert requests[0]["messages"] != requests[1]["messages"]
    assert recorder.llm_calls[0].messages_hash != recorder.llm_calls[1].messages_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        "anthropic/claude-opus-5-5",
        "claude-opus-5-5",
        "bedrock/anthropic.claude-opus-4-6-v1",
        "vertex_ai/claude-opus-4-6",
        "openrouter/anthropic/claude-opus-4.6",
    ],
)
async def test_advisor_reuses_only_skill_prefix_across_early_and_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    owner = _owner(tmp_path, model)
    requests = _transport(monkeypatch)
    for trigger in (ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END):
        await owner._call_advisor_with_audit(_arguments(trigger), recorder=BufferingRecorder(), structured_output=True)

    prefixes = []
    for request, trigger in zip(requests, (ADVISOR_TRIGGER_DETERMINISTIC_EARLY, ADVISOR_TRIGGER_DETERMINISTIC_END), strict=True):
        messages = request["messages"]
        content = messages[0]["content"]
        assert type(content) is list, "Advisor must expose a stable cacheable skill block"
        assert content == [
            {"type": "text", "text": _SKILL, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n\n" + advisor_context.advisor_system_instructions_for_trigger(trigger)},
        ]
        assert "".join(block["text"] for block in content) == _SKILL + "\n\n" + advisor_context.advisor_system_instructions_for_trigger(
            trigger
        )
        assert messages[1] == {"role": "user", "content": advisor_context.build_advisor_user_message(_arguments(trigger))}
        assert "cache_control" not in messages[0] and "cache_control" not in messages[1]
        assert "tools" not in request
        wire = AnthropicConfig().transform_request(
            model="claude-opus-5-5", messages=copy.deepcopy(messages), optional_params={"max_tokens": 8192}, litellm_params={}, headers={}
        )
        wire_system = wire["system"]
        assert wire_system == content
        prefixes.append(wire_system[0])
    assert prefixes[0] == prefixes[1]
    assert requests[0]["messages"][0]["content"][1] != requests[1]["messages"][0]["content"][1]
    assert requests[0]["response_format"] != requests[1]["response_format"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["openai/gpt-4.1", "openrouter/deepseek/deepseek-v4", "azure/gpt-4.1", "vertex_ai/gemini-2.5-pro"])
async def test_non_anthropic_advisor_request_preserves_original_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    owner = _owner(tmp_path, model)
    requests = _transport(monkeypatch)
    await owner._call_advisor_with_audit(_arguments("reactive"), recorder=BufferingRecorder())
    assert requests[0]["messages"] == [
        {"role": "system", "content": _SKILL + "\n\n" + advisor_context.advisor_system_instructions_for_trigger("reactive")},
        {"role": "user", "content": advisor_context.build_advisor_user_message(_arguments("reactive"))},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("creation", "read"), [(2000, 0), (0, 2000)])
async def test_marked_payload_native_usage_and_continuation_cost_are_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    creation: int,
    read: int,
) -> None:
    owner = _owner(tmp_path, "anthropic/claude-opus-5-5")
    requests = _transport(monkeypatch, creation=creation, read=read)
    recorder = BufferingRecorder()
    budget = PlannerContinuationBudget(
        policy=PlannerBudgetPolicy(5, 100000, 8192, Decimal("1")),
        session_id="session",
        request_id="request",
        request_hash=stable_hash("request"),
        provider_owner=None,
        evidence_owner=None,
        deadline=time.monotonic() + 60,
        reserved_calls=1,
        validated_cost=Decimal("0"),
        cost_known=True,
    )
    service = MagicMock(spec=SessionServiceProtocol)
    service.begin_provider_attempt.return_value = ProviderAttempt("actual-admitted-attempt", datetime.now(UTC))
    service.finish_provider_attempt.return_value = None
    context = SessionOperationContext(
        fence=SessionOperationFence(session_id="session", operation_id="operation", lease_token="unit-lease", operation_epoch=1),
        operation_kind=SessionOperationKind.COMPOSE,
    )
    with composer_quota_scope(service, context):
        guidance, metadata = await owner._call_advisor_with_audit(_arguments("reactive"), recorder=recorder, continuation_budget=budget)
    assert guidance == "Actual provider advice."
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.private_request is not None and call.private_request.disposition == "retained"
    assert json.loads(call.private_request.canonical)["messages"] == requests[0]["messages"]
    assert call.messages_hash == stable_hash(requests[0]["messages"])
    assert call.tools_spec_hash is None
    assert call.cache_creation_input_tokens == creation and call.cache_read_input_tokens == read
    assert call.cached_prompt_tokens is None and metadata["cached_prompt_tokens"] is None
    assert call.provider_cost == 0.012 and budget.validated_cost == Decimal("0.012") and budget.cost_known
    assert call.provider_request_id == "native-request-canary"
    assert call.call_id == "actual-admitted-attempt"
    service.begin_provider_attempt.assert_awaited_once()
    service.finish_provider_attempt.assert_awaited_once()
    assert requests[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_empty_skill_is_not_an_empty_cache_breakpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _owner(tmp_path, "anthropic/claude-opus-5-5", skill="")
    requests = _transport(monkeypatch)
    await owner._call_advisor_with_audit(_arguments("reactive"), recorder=BufferingRecorder())
    assert requests[0]["messages"][0] == {
        "role": "system",
        "content": "\n\n" + advisor_context.advisor_system_instructions_for_trigger("reactive"),
    }
