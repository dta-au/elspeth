"""Composer LLM-call audit tests for the async LiteLLM path."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.core.canonical import stable_hash
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.composer import llm_response_parsing as llm_response_parsing_module
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import build_llm_call_record, token_usage_from_response
from elspeth.web.composer.protocol import ComposerConvergenceError, ComposerServiceError
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.config import WebSettings
from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult
from tests.unit.web.composer._helpers import (
    _stub_advisor_end_gate_clean,  # noqa: F401  (autouse end-gate CLEAN stub)
)


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeToolCall:
    id: str
    function: _FakeFunction


@dataclass
class _FakeMessage:
    content: str | None
    tool_calls: list[_FakeToolCall] | None
    reasoning: str | None = None
    reasoning_content: str | None = None
    reasoning_details: list[dict[str, Any]] | None = None
    thinking_blocks: list[dict[str, Any]] | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeLLMResponse:
    choices: list[_FakeChoice]
    model: str | None = "provider/model-returned"
    usage: Any = None
    id: str | None = "chatcmpl-123"


def _empty_state() -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _passing_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


def _mock_catalog() -> MagicMock:
    catalog = MagicMock(spec=CatalogService)
    catalog.list_sources.return_value = [
        PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[]),
    ]
    catalog.list_transforms.return_value = []
    catalog.list_sinks.return_value = []
    catalog.get_schema.return_value = PluginSchemaInfo(
        name="csv",
        plugin_type="source",
        description="CSV source",
        json_schema={"title": "Config", "properties": {}},
        knob_schema={"fields": []},
    )
    return catalog


def _make_settings(**overrides: Any) -> WebSettings:
    defaults: dict[str, Any] = {
        "data_dir": Path("/data"),
        "composer_model": "openrouter/openai/gpt-5.5",
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "composer_prompt_guard_mode": "debug-when-unconfigured",
        "shareable_link_signing_key": b"\x00" * 32,
    }
    defaults.update(overrides)
    return WebSettings(**defaults)


def _make_llm_response(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    model: str | None = "provider/model-returned",
    prompt_tokens: int | None = 11,
    completion_tokens: int | None = 7,
    total_tokens: int | None = None,
    request_id: str | None = "chatcmpl-123",
    cost: float | int | str | None = None,
    reasoning_tokens: int | None = None,
    reasoning: str | None = None,
    reasoning_content: str | None = None,
    reasoning_details: list[dict[str, Any]] | None = None,
    thinking_blocks: list[dict[str, Any]] | None = None,
) -> _FakeLLMResponse:
    fake_tool_calls: list[_FakeToolCall] | None = None
    if tool_calls:
        fake_tool_calls = [
            _FakeToolCall(
                id=tc["id"],
                function=_FakeFunction(name=tc["name"], arguments=json.dumps(tc["arguments"])),
            )
            for tc in tool_calls
        ]
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost=cost,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
    )
    return _FakeLLMResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=content,
                    tool_calls=fake_tool_calls,
                    reasoning=reasoning,
                    reasoning_content=reasoning_content,
                    reasoning_details=reasoning_details,
                    thinking_blocks=thinking_blocks,
                )
            )
        ],
        model=model,
        usage=usage,
        id=request_id,
    )


def _captured_llm_calls(exc: BaseException) -> Sequence[ComposerLLMCall]:
    calls = exc.__dict__["llm_calls"]
    assert isinstance(calls, tuple)
    return cast(tuple[ComposerLLMCall, ...], calls)


def test_llm_response_parser_does_not_call_dynamic_getattr() -> None:
    import inspect

    assert "getattr(" not in inspect.getsource(llm_response_parsing_module)


def test_token_usage_ignores_provider_properties_and_reads_own_fields() -> None:
    class _ProviderUsage:
        def __init__(self) -> None:
            self.completion_tokens = 7

        @property
        def prompt_tokens(self) -> int:
            raise AssertionError("provider property should not be invoked")

    usage = token_usage_from_response(SimpleNamespace(usage=_ProviderUsage()))

    assert usage.prompt_tokens is None
    assert usage.completion_tokens == 7


def test_token_usage_reads_real_litellm_pydantic_extra_fields() -> None:
    """LiteLLM ModelResponse keeps ``usage`` in ``__pydantic_extra__``, not ``__dict__``.

    Pydantic v2 models with ``extra="allow"`` store undeclared fields outside
    ``vars()``; the parser must still see them or every real provider response
    audits as unknown token counts with no provider cost.
    """
    from litellm.types.utils import ModelResponse, Usage

    usage_obj = Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    usage_obj.cost = 0.0123
    response = ModelResponse(usage=usage_obj)
    assert "usage" not in vars(response)  # the storage split this test pins

    usage = token_usage_from_response(response)

    assert usage.prompt_tokens == 11
    assert usage.completion_tokens == 7
    assert usage.reported_total == 18

    record = build_llm_call_record(
        model_requested="openrouter/openai/gpt-5.5",
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )
    assert record.prompt_tokens == 11
    assert record.provider_cost == 0.0123


def _record_with_reasoning_artifact(artifact: object) -> ComposerLLMCall:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(reasoning_details=artifact, thinking_blocks=None))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    return build_llm_call_record(
        model_requested="provider/model",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )


def test_provider_reasoning_cycle_degrades_to_fixed_sentinel() -> None:
    artifact: dict[str, object] = {}
    artifact["self"] = artifact

    record = _record_with_reasoning_artifact(artifact)

    assert record.reasoning_details == "<provider-artifact-unavailable>"


def _nested_json_lists(depth: int) -> object:
    value: object = 0
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "artifact",
    [
        pytest.param({"deep": _nested_json_lists(65)}, id="deep"),
        pytest.param({"wide": [None] * 10_001}, id="wide"),
        pytest.param({"oversized": ["x" * 65_500] * 17}, id="oversized"),
    ],
)
def test_provider_reasoning_artifact_budgets_degrade_to_fixed_sentinel(artifact: object) -> None:
    record = _record_with_reasoning_artifact(artifact)

    assert record.reasoning_details == "<provider-artifact-unavailable>"


def test_provider_reasoning_does_not_invoke_serializer_methods() -> None:
    calls: list[str] = []

    class _ThrowingSerializer:
        __slots__ = ()

        def model_dump(self, **_kwargs: object) -> object:
            calls.append("model_dump")
            raise RuntimeError("provider-controlled serializer ran")

    record = _record_with_reasoning_artifact(_ThrowingSerializer())

    assert record.reasoning_details == "<provider-artifact-unavailable>"
    assert calls == []


def test_provider_reasoning_does_not_invoke_provider_descriptors() -> None:
    calls: list[str] = []

    class _ThrowingDescriptor:
        def __init__(self) -> None:
            self.safe = "value"

        @property
        def __pydantic_extra__(self) -> object:
            calls.append("descriptor")
            raise RuntimeError("provider-controlled descriptor ran")

    record = _record_with_reasoning_artifact(_ThrowingDescriptor())

    assert record.reasoning_details == {"safe": "value"}
    assert calls == []


def test_provider_artifact_admits_real_litellm_tool_call() -> None:
    """A real LiteLLM tool call must audit as an artifact, not as the sentinel.

    ``litellm.types.utils.ChatCompletionMessageToolCall`` declares no pydantic
    model fields: ``id``/``type``/``function`` live in ``__pydantic_extra__``
    and its ``__dict__`` is empty. Reading ``__dict__`` alone yields no
    data-only representation, so the whole artifact silently degrades to
    ``PROVIDER_ARTIFACT_UNAVAILABLE`` in the durable audit record
    (ADR-032; same defect class as elspeth-9ea866438b).
    """
    from litellm.types.utils import ChatCompletionMessageToolCall, Function

    tool_call = ChatCompletionMessageToolCall(
        id="call_1",
        type="function",
        function=Function(name="do_thing", arguments='{"a": 1}'),
    )
    assert object.__getattribute__(tool_call, "__dict__") == {}  # the storage split this test pins

    record = _record_with_reasoning_artifact(tool_call)

    assert record.reasoning_details == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "do_thing", "arguments": '{"a": 1}'},
    }


def test_provider_artifact_admits_pydantic_extra_allow_model() -> None:
    """Undeclared fields on an ``extra="allow"`` model must reach the audit record."""
    import pydantic

    class _ProviderArtifact(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")

        declared: str

    artifact = _ProviderArtifact(declared="visible", undeclared="also visible")

    record = _record_with_reasoning_artifact(artifact)

    assert record.reasoning_details == {"declared": "visible", "undeclared": "also visible"}


def test_provider_artifact_without_data_fields_still_degrades_to_sentinel() -> None:
    """The sentinel fallback is preserved, not removed, by the extras merge."""

    class _Opaque:
        __slots__ = ()

    record = _record_with_reasoning_artifact(_Opaque())

    assert record.reasoning_details == "<provider-artifact-unavailable>"


def test_provider_reasoning_content_degrades_when_oversized() -> None:
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(reasoning="x" * 65_537))],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )

    record = build_llm_call_record(
        model_requested="provider/model",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=response,
    )

    assert record.reasoning_content == "<provider-artifact-unavailable>"


def test_llm_call_record_redacts_raw_provider_error_detail() -> None:
    openai_key = "sk-" + ("A" * 48)
    bearer_token = "Bearer " + ("x" * 32)
    raw_error = (
        "ProviderError headers={'Authorization': '"
        + bearer_token
        + "'} api_key="
        + openai_key
        + " prompt_template='classify {email}' "
        + "sample_rows=[{'email':'person@example.com','ssn':'123-45-6789'}] "
        + ("row detail " * 80)
    )

    call = build_llm_call_record(
        model_requested="provider/model",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.API_ERROR,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
        error_class="ProviderError",
        error_message=raw_error,
    )

    stored = call.error_message
    assert stored is not None
    assert stored.startswith("provider error detail redacted")
    assert "raw_error_hash=" in stored
    assert len(stored) <= 512
    for leaked in (
        openai_key,
        bearer_token,
        "person@example.com",
        "123-45-6789",
        "classify {email}",
        "row detail",
    ):
        assert leaked not in stored


@pytest.fixture(autouse=True)
def _composer_available_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    def _available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)


def test_pydantic_extra_unset_slot_reads_as_no_extras_without_raising() -> None:
    """A declared-but-unset ``__pydantic_extra__`` slot is third-party state a
    partially constructed provider object can legitimately carry: the boundary
    answers None ("no extras"), it never propagates the AttributeError."""
    from elspeth.core.llm_pricing import _pydantic_extra_fields

    class _UnsetSlot:
        __slots__ = ("__pydantic_extra__",)

    assert _pydantic_extra_fields(_UnsetSlot()) is None


# ---------------------------------------------------------------------------
# Per-call audit records at the shared PRIMARY owner.
#
# Continuation's PRIMARY call and the turn-classification repair calls both go
# through ProviderGateway's audited call body; ComposerServiceImpl.
# _call_llm_before_deadline is the deadline/retry wrapper that attaches the
# buffered records to a raised exception. These tests call that owner directly.
# ---------------------------------------------------------------------------


class _PrimaryOwner:
    """A real fenced COMPOSE context, required-work turn binding and provider owner."""

    def __init__(self, tmp_path: Path) -> None:
        import structlog

        from elspeth.web.coordination.contracts import SessionOperationKind
        from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.schema import initialize_session_schema
        from elspeth.web.sessions.service import SessionServiceImpl
        from elspeth.web.sessions.telemetry import build_sessions_telemetry
        from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user

        self.engine = create_session_engine(f"sqlite:///{tmp_path / 'llm-audit-sessions.db'}")
        initialize_session_schema(self.engine)
        with self.engine.begin() as connection:
            ensure_test_identity(connection, identity_id="alice")
            grant_test_pipeline_user(connection, identity_id="alice")
        self.authority = SQLiteLocalSessionOperationAuthority(self.engine)
        self.sessions = SessionServiceImpl(
            self.engine,
            telemetry=build_sessions_telemetry(),
            log=structlog.get_logger("test.llm-audit"),
            session_operation_authority=self.authority,
        )
        session = self.authority.create_session_with_initial_fence(
            user_id="alice", title="LLM audit", auth_provider_type="local", owner_instance_id="llm-audit-owner", lease_seconds=300
        )
        self.context = self.authority.acquire(
            session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="llm-audit-owner", lease_seconds=300
        )

    def service(self, **settings_overrides: Any) -> ComposerServiceImpl:
        return ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(**settings_overrides),
            sessions_service=self.sessions,
            session_engine=self.engine,
        )

    def provider_owner(self) -> Any:
        from elspeth.web.composer.provider_quota import ProviderInvocationOwner
        from elspeth.web.required_work import (
            RequiredAuthorityKind,
            RequiredWorkAuthority,
            RequiredWorkBinding,
            RequiredWorkCoordinator,
            RequiredWorkRole,
        )

        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, self.context, invocation_id=str(uuid4()))
        )
        return ProviderInvocationOwner(service=self.sessions, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))

    def close(self) -> None:
        self.authority.release(self.context)
        self.engine.dispose()


@pytest.fixture
def primary(tmp_path: Path):
    owner = _PrimaryOwner(tmp_path)
    try:
        yield owner
    finally:
        owner.close()


async def _primary_call(
    primary: _PrimaryOwner, *, user: str = "Build a CSV pipeline", **settings_overrides: Any
) -> tuple[Any, BufferingRecorder]:
    service = primary.service(**settings_overrides)
    recorder = BufferingRecorder()
    state = _empty_state()
    completion = await service._call_llm_before_deadline(
        [{"role": "user", "content": user}],
        composer_loop_tool_definitions(service._planner_dialect),
        state,
        state.version,
        asyncio.get_running_loop().time() + 30,
        recorder,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        provider_owner=primary.provider_owner(),
    )
    return completion, recorder


@pytest.mark.asyncio
async def test_text_only_success_records_llm_call_metadata(primary: _PrimaryOwner) -> None:
    llm_response = _make_llm_response(content="Done.")
    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> _FakeLLMResponse:
        requests.append(deepcopy(kwargs))
        return llm_response

    with patch("litellm.acompletion", new_callable=AsyncMock, side_effect=complete) as mock_acomp:
        _completion, recorder = await _primary_call(primary, composer_pricing_model="openai/gpt-4o-2024-08-06")

    # Every physical call retains its exact request and provider metadata.
    assert mock_acomp.call_count == len(recorder.llm_calls) == 1
    for call, request_kwargs in zip(recorder.llm_calls, requests, strict=True):
        assert call.status is ComposerLLMCallStatus.SUCCESS
        assert call.model_requested == "openrouter/openai/gpt-5.5"
        assert call.model_returned == "provider/model-returned"
        assert call.prompt_tokens == 11
        assert call.completion_tokens == 7
        assert call.total_tokens == 18
        assert call.pricing_model == "openai/gpt-4o-2024-08-06"
        assert call.provider_cost == pytest.approx(0.0000975)
        assert call.provider_cost_source == "litellm.cost_per_token"
        assert call.provider_request_id == "chatcmpl-123"
        assert call.messages_hash == stable_hash(request_kwargs["messages"])
        assert call.tools_spec_hash == stable_hash(request_kwargs["tools"])


@pytest.mark.asyncio
async def test_success_records_provider_cost_from_usage_metadata(primary: _PrimaryOwner) -> None:
    llm_response = _make_llm_response(content="Done.", cost=0.0037)

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=llm_response):
        _completion, recorder = await _primary_call(primary)

    call = recorder.llm_calls[0]
    assert call.provider_cost == 0.0037
    assert call.provider_cost_source == "response_usage.cost"


@pytest.mark.asyncio
async def test_success_records_provider_reasoning_metadata(primary: _PrimaryOwner) -> None:
    reasoning_details = [{"type": "reasoning.text", "text": "selected set_pipeline"}]
    thinking_blocks = [{"type": "thinking", "thinking": "checked required output options", "signature": "synthetic-native-signature"}]
    llm_response = _make_llm_response(
        content="Done.",
        reasoning_tokens=12,
        reasoning="openrouter native reasoning",
        reasoning_content="litellm normalized reasoning",
        reasoning_details=reasoning_details,
        thinking_blocks=thinking_blocks,
    )

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=llm_response):
        _completion, recorder = await _primary_call(primary)

    call = recorder.llm_calls[0]
    payload = call.to_dict()
    assert call.reasoning_tokens == 12
    assert call.reasoning_content == "openrouter native reasoning"
    assert payload["reasoning_details"] == reasoning_details
    assert payload["thinking_blocks"] == thinking_blocks


@pytest.mark.asyncio
async def test_malformed_provider_cost_is_recorded_as_unavailable(primary: _PrimaryOwner) -> None:
    llm_response = _make_llm_response(content="Done.", cost="not-a-number")

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=llm_response):
        _completion, recorder = await _primary_call(primary)

    call = recorder.llm_calls[0]
    assert call.provider_cost is None
    assert call.provider_cost_source == "not_available"


@pytest.mark.asyncio
async def test_unset_sampling_is_omitted_and_reflected_in_audit(primary: _PrimaryOwner) -> None:
    llm_response = _make_llm_response(content="Done.")

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=llm_response) as mock_acomp:
        _completion, recorder = await _primary_call(primary, composer_model="anthropic/claude-3-5-sonnet-20241022")

    request_kwargs = mock_acomp.call_args.kwargs
    assert "temperature" not in request_kwargs
    assert "seed" not in request_kwargs
    assert recorder.llm_calls[0].temperature is None
    assert recorder.llm_calls[0].seed is None


@pytest.mark.asyncio
async def test_deadline_timeout_records_llm_call_on_convergence_error(primary: _PrimaryOwner) -> None:
    async def timeout_llm(*_args: Any, **_kwargs: Any) -> _FakeLLMResponse:
        raise TimeoutError

    with (
        patch("litellm.acompletion", side_effect=timeout_llm),
        pytest.raises(ComposerConvergenceError) as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    assert exc_info.value.budget_exhausted == "timeout"
    assert len(exc_info.value.llm_calls) == 1
    assert exc_info.value.llm_calls[0].status is ComposerLLMCallStatus.TIMEOUT


@pytest.mark.asyncio
async def test_bad_request_records_redacted_llm_call_on_service_error(primary: _PrimaryOwner) -> None:
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

    bad_request = LiteLLMBadRequestError(message="bad request leaked detail", model="bad", llm_provider="test")

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=bad_request),
        pytest.raises(ComposerServiceError) as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    assert str(exc_info.value) == "LLM request rejected (BadRequestError)"
    llm_calls = _captured_llm_calls(exc_info.value)
    assert len(llm_calls) == 1
    call = llm_calls[0]
    assert call.status is ComposerLLMCallStatus.BAD_REQUEST_ERROR
    assert call.error_class == "BadRequestError"
    assert call.error_message == "BadRequestError"


@pytest.mark.asyncio
async def test_unclassified_provider_exception_records_api_error_call(primary: _PrimaryOwner) -> None:
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=ValueError("unexpected codec failure")),
        pytest.raises(ValueError, match="unexpected codec failure") as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    llm_calls = _captured_llm_calls(exc_info.value)
    assert len(llm_calls) == 1
    call = llm_calls[0]
    assert call.status is ComposerLLMCallStatus.API_ERROR
    assert call.error_class == "ValueError"
    assert call.error_message == "ValueError"


@pytest.mark.asyncio
async def test_empty_choices_records_malformed_response(primary: _PrimaryOwner) -> None:
    empty_response = _FakeLLMResponse(choices=[], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=None, total_tokens=3))

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, return_value=empty_response),
        pytest.raises(ComposerServiceError) as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    llm_calls = _captured_llm_calls(exc_info.value)
    assert len(llm_calls) == 1
    call = llm_calls[0]
    assert call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE
    assert call.prompt_tokens == 3
    assert call.completion_tokens is None
    assert call.total_tokens == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "choice"),
    [
        ("missing-message", SimpleNamespace()),
        ("non-string-content", SimpleNamespace(message=SimpleNamespace(content=["part-a", "part-b"], tool_calls=None))),
        ("dict-content", SimpleNamespace(message=SimpleNamespace(content={"text": "hi"}, tool_calls=None))),
        ("int-content", SimpleNamespace(message=SimpleNamespace(content=7, tool_calls=None))),
        ("missing-content-attr", SimpleNamespace(message=SimpleNamespace(tool_calls=None))),
        ("non-sequence-tool-calls", SimpleNamespace(message=SimpleNamespace(content=None, tool_calls={"id": "x"}))),
        ("missing-tool-calls-attr", SimpleNamespace(message=SimpleNamespace(content="hi"))),
        (
            "tool-call-missing-id",
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(function=SimpleNamespace(name="set_metadata", arguments="{}"))],
                )
            ),
        ),
        (
            "tool-call-non-string-function-metadata",
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(id="tc_1", function=SimpleNamespace(name=42, arguments="{}"))],
                )
            ),
        ),
    ],
)
async def test_malformed_nonempty_response_records_malformed_not_success(primary: _PrimaryOwner, case: str, choice: Any) -> None:
    """Regression for elspeth-b6be9e991f: a nonempty-but-malformed provider
    response must classify as MALFORMED_RESPONSE before the audit wrapper
    records SUCCESS.
    """
    malformed = _FakeLLMResponse(choices=[choice], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=None, total_tokens=3))

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, return_value=malformed),
        pytest.raises(ComposerServiceError) as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    llm_calls = _captured_llm_calls(exc_info.value)
    assert len(llm_calls) == 1
    call = llm_calls[0]
    assert call.status is ComposerLLMCallStatus.MALFORMED_RESPONSE, case
    assert call.error_message == "malformed_response"


@pytest.mark.asyncio
async def test_cancelled_model_call_records_cancelled_status(primary: _PrimaryOwner) -> None:
    cancelled = asyncio.CancelledError()

    with (
        patch("litellm.acompletion", new_callable=AsyncMock, side_effect=cancelled),
        pytest.raises(asyncio.CancelledError) as exc_info,
    ):
        await _primary_call(primary, user="Hello")

    llm_calls = _captured_llm_calls(exc_info.value)
    assert len(llm_calls) == 1
    assert llm_calls[0].status is ComposerLLMCallStatus.CANCELLED


@pytest.mark.asyncio
async def test_tool_call_then_final_response_records_both_llm_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.no_tool_policy import AssistantTextSegment, TrustedSystemNoticeSegment, visible_message_segments
    from tests.helpers.continuation_app import ScriptedSDK, continuation_app, end_admit, install_sdk, submit_and_settle, tool_call

    sdk = ScriptedSDK(
        [
            ("primary", lambda app: (None, [tool_call(app, "call-1", "set_metadata", {"patch": {"name": "After tool"}})])),
            ("primary", ("Pipeline updated.", None)),
            ("end", end_admit()),
        ]
    )
    install_sdk(monkeypatch, sdk)
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sdk.app_ref.append(harness.app)
        snapshot = await submit_and_settle(harness, {"content": "Set a name", "operation_id": str(uuid4()), "state_id": None})
        assert snapshot.status == "completed", (snapshot, sdk.roles)
        assert snapshot.result is not None
        raw = "Pipeline updated."
        notice = (
            "Checkpoint save status: Verified checkpoint evidence covers 1 saved draft revisions. "
            "There are net authored changes from the starting draft. Checkpoint evidence does not establish task completion."
        )
        message = snapshot.result.message
        assert message.raw_content == raw
        assert message.content == raw + "\n\n---\n\n[ELSPETH-SYSTEM] " + notice
        assert visible_message_segments(content=message.content, raw_content=message.raw_content) == (
            AssistantTextSegment(raw),
            TrustedSystemNoticeSegment(notice),
        )
        rows = await harness.app.state.session_service.get_messages(harness.session_id, limit=None)
        calls = [
            envelope["call"]
            for row in rows
            for envelope in (row.tool_calls or ())
            if envelope["_kind"] == "llm_call_audit" and envelope["call"]["model_requested"] == "test/model"
        ]
        primary_requests = [request for request in sdk.requests if request["role"] == "primary"]
        assert len(calls) == len(primary_requests) == 2
        assert [call["status"] for call in calls] == ["success", "success"]
        assert calls[0]["messages_hash"] != calls[1]["messages_hash"]
        for call, request_kwargs in zip(calls, primary_requests, strict=True):
            assert call["messages_hash"] == stable_hash(request_kwargs["messages"])
            assert call["tools_spec_hash"] == stable_hash(request_kwargs["tools"])
