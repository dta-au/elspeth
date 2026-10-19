"""Tests for ComposerServiceImpl — LLM tool-use loop with mock LLM."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import threading
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import UUID, uuid4

import litellm
import pytest
import structlog
from httpx import MockTransport, Request, Response
from litellm.exceptions import APIError as LiteLLMAPIError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import (
    PromptGuardClient,
    PromptGuardContentPart,
    PromptGuardDecision,
    PromptGuardRequest,
    PromptGuardToolArgumentParts,
)
from elspeth.core.canonical import canonical_json
from elspeth.web import async_workers
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer import no_tool_policy as _no_tool_policy_module
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_audit import persist_advisor_checkpoint_pass
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner, AdvisorCheckpointVerdict
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.composition_completion import _MAX_REPAIR_TURNS, _compose_preflight_repair_message
from elspeth.web.composer.planning_application import (
    _freeform_planner_conversation_context,
)
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import (
    COMPOSER_HISTORY_USER_AUTHORED_KEY,
    ComposerAdmissionRefused,
    ComposerConvergenceError,
    ComposerPluginCrashError,
    ComposerResult,
    ComposerRuntimePreflightError,
    ComposerServiceError,
    PipelineCommitIntent,
    RetainedOperationContext,
    ToolArgumentError,
)
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationSummary,
)
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools import _registry as tool_registry
from elspeth.web.composer.tools import execute_tool as _strict_execute_tool
from elspeth.web.composer.turn_effects import AdvisorPublicationReview, TurnEvidence
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.preflight import runtime_preflight_settings_hash
from elspeth.web.execution.schemas import (
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
)
from elspeth.web.execution.schemas import (
    ValidationResult as ValidationResultModel,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)
from elspeth.web.sessions._persist_payload import CommittedCheckpointWitness
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_states_table,
    quota_provider_attempts_table,
    sessions_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.routes._helpers import _persist_turn_audit_cohort
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.audit_hashing import fake_sha256
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.required_executor import assert_owned_executor_fixture_settled
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import (
    FakeChoice,
    FakeFunction,
    FakeLLMResponse,
    FakeMessage,
    FakeToolCall,
    _empty_state,
    _make_settings,
    _mock_catalog,
    _stub_advisor_end_gate_clean,  # noqa: F401  (autouse end-gate CLEAN stub)
)
from tests.unit.web.composer._helpers import (
    _make_llm_response as _make_raw_llm_response,
)
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

_REAL_RUN_ADVISOR_CHECKPOINT = AdvisorCheckpointOwner._run_advisor_checkpoint


def _assert_checkpoint_reply(result: ComposerResult, base: str, *, count: int, changed: bool = False, raw: str | None = None) -> None:
    """Preserve the complete prior reply and require one final factual receipt."""
    expected_raw = base if raw is None else raw
    scope = f"Verified checkpoint evidence covers {count} saved draft revisions. " + (
        "There are net authored changes from the starting draft." if changed else "There is no net authored change from the starting draft."
    )
    notice = f"Checkpoint save status: {scope} Checkpoint evidence does not establish task completion."
    boundary = "\n\n---\n\n[ELSPETH-SYSTEM] " if base == expected_raw else "\n\n"
    assert result.raw_assistant_content == expected_raw
    assert result.message == base + boundary + notice
    assert _no_tool_policy_module.visible_message_segments(content=result.message, raw_content=expected_raw) == (
        *_no_tool_policy_module.visible_message_segments(content=base, raw_content=expected_raw),
        _no_tool_policy_module.TrustedSystemNoticeSegment(notice),
    )


@pytest.mark.parametrize("availability", ["available", "unavailable", "unknown"])
def test_retained_operation_context_distinguishes_empty_unknown_and_unavailable(
    availability: Literal["available", "unavailable", "unknown"],
) -> None:
    context = RetainedOperationContext(session_id=uuid4(), actor_user_id="test-user", availability=availability)
    projected = json.loads(context.provider_message())["retained_operation_history"]
    assert projected["availability"] == availability
    assert projected["outcomes"] == [] and projected["has_more"] is False
    assert "not requested edits" in projected["interpretation"]
    assert "Unknown attribution is not zero" in projected["interpretation"]
    with pytest.raises(AuditIntegrityError):
        context.validate_for(session_id=str(uuid4()), actor_user_id="test-user")
    with pytest.raises(AuditIntegrityError):
        context.validate_for(session_id=str(context.session_id), actor_user_id="another-user")


@pytest.mark.asyncio
async def test_retained_operation_context_reaches_real_primary_and_reviewed_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
    context = RetainedOperationContext(session_id=UUID(session_id), actor_user_id="test-user", availability="unavailable")
    calls = []

    async def completion(**kwargs):
        calls.append(deepcopy(kwargs))
        content = (
            json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"})
            if "response_format" in kwargs
            else "Prior operation history is unavailable; I cannot establish what happened."
        )
        return litellm.ModelResponse(
            choices=[{"message": {"content": content, "tool_calls": None, "role": "assistant"}, "finish_reason": "stop", "index": 0}],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )

    # Both primary and the newly reachable END use this explicit finite SDK seam.
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _REAL_RUN_ADVISOR_CHECKPOINT)
    with fenced_operation_context(service._session_engine, session_id) as authority:
        result = await service.compose(
            "What happened to the earlier action?",
            [],
            _empty_state(),
            session_id=session_id,
            user_id="test-user",
            session_operation_context=authority,
            retained_operation_context=context,
        )
    primary = [call for call in calls if "response_format" not in call]
    reviews = [call for call in calls if "response_format" in call]
    assert len(primary) == len(reviews) == 1
    assert any(row["content"] == context.provider_message() for row in primary[0]["messages"])
    assert "retained_operation_history" in json.dumps(reviews[0]["messages"])
    assert "unavailable" in json.dumps(reviews[0]["messages"])
    _assert_checkpoint_reply(result, "Prior operation history is unavailable; I cannot establish what happened.", count=0)


def _make_llm_response(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> _AdmittedLLMCompletion:
    """Model the admitted result returned by the gateway's _call_llm seam."""
    return _admit_composer_llm_completion(_make_raw_llm_response(content=content, tool_calls=tool_calls))


def _advisor_checkpoint_reply(*, verdict: Literal["CLEAN", "FLAGGED"], findings: str, note: str | None) -> str:
    return json.dumps({"verdict": verdict, "category": "other", "steps": [], "findings": findings, "note": note})


class _ReconciliationLLMScript:
    """Finite provider replies with prompts captured before the loop appends to them."""

    def __init__(self, *responses: _AdmittedLLMCompletion) -> None:
        self.responses = responses
        self._responses = iter(responses)
        self.prompts: list[list[dict[str, Any]]] = []

    async def respond(
        self,
        messages: list[dict[str, Any]],
        _tools: list[dict[str, Any]],
        *,
        provider_custody: object | None = None,
        credential_state: object | None = None,
        prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
        prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
        prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
    ) -> _AdmittedLLMCompletion:
        self.prompts.append(deepcopy(messages))
        return next(self._responses)


def _assert_reconciliation_turn(mock_llm: AsyncMock, script: _ReconciliationLLMScript) -> None:
    """Require exact tool feedback without the retired lexical PRIMARY retry."""
    assert mock_llm.await_count == len(script.responses) == 2
    assert len(script.prompts) == 2
    prompt = script.prompts[1][-1]
    assert prompt["role"] == "tool"
    calls = script.responses[0].tool_batch.calls
    assert len(calls) == 1
    assert prompt["tool_call_id"] == calls[0].id


class _SemanticRecoveryLLMScript:
    """Finite SDK responses; the real checkpoint owner parses and binds every review."""

    def __init__(
        self,
        service: ComposerServiceImpl,
        request: str,
        primary_responses: tuple[_AdmittedLLMCompletion, ...],
        *,
        phases: tuple[Literal["primary", "early", "end"], ...],
        first_findings: str,
    ) -> None:
        self.service = service
        self.request = request
        self.primary_responses = primary_responses
        self.expected_phases = phases
        self.first_findings = first_findings
        self.phases: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.primary_requests: list[dict[str, Any]] = []
        self.end_inputs: list[AdvisorPublicationReview] = []
        self.end_outputs: list[AdvisorCheckpointVerdict] = []
        self.mints: list[tuple[ProviderInvocationOwner, ProviderInvocationFamily, Any]] = []
        self.attempt_ids: list[str] = []

    async def checkpoint(self, *args: Any, **kwargs: Any) -> AdvisorCheckpointVerdict:
        if kwargs["phase"] == "end":
            assert kwargs["user_message"] == self.request
            review = kwargs["publication_review"]
            assert isinstance(review, AdvisorPublicationReview)
            self.end_inputs.append(review)
        verdict = await _REAL_RUN_ADVISOR_CHECKPOINT(self.service._advisor_checkpoint, *args, **kwargs)
        if kwargs["phase"] == "end":
            assert verdict.publication_binding == review.binding
            self.end_outputs.append(verdict)
        return verdict

    async def complete(self, **kwargs: Any) -> Any:
        if "response_format" not in kwargs:
            phase = "primary"
            assert kwargs["model"] == self.service._model
        else:
            schema = kwargs["response_format"]["json_schema"]
            assert schema["name"] in {"advisor_checkpoint_response", "advisor_end_response"}
            phase = "end" if schema["name"] == "advisor_end_response" else "early"
            assert kwargs["model"] == self.service._settings.composer_advisor_model
        assert len(self.phases) < len(self.expected_phases), "Unexpected extra provider transition"
        assert phase == self.expected_phases[len(self.phases)]
        owner, family, custody = self.mints[-1]
        assert owner.service is self.service._require_sessions_service()
        assert family is (ProviderInvocationFamily.PRIMARY if phase == "primary" else ProviderInvocationFamily.ADVISOR)
        assert custody.attempt is not None
        self.attempt_ids.append(custody.attempt.attempt_id)
        self.phases.append(phase)
        self.requests.append(deepcopy(kwargs))
        tool_calls = None
        if phase == "primary":
            response = self.primary_responses[len(self.primary_requests)]
            self.primary_requests.append(deepcopy(kwargs))
            content = response.message.content
            if response.tool_batch.calls:
                tool_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in response.tool_batch.calls
                ]
        else:
            payload: dict[str, Any] = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
            if phase == "end":
                review = self.end_inputs[-1]
                texts = [
                    message["content"] if isinstance(message["content"], str) else "".join(part["text"] for part in message["content"])
                    for message in kwargs["messages"]
                ]
                assert any(review.provider_payload_json in text for text in texts)
                evidence = json.loads(review.payload_json)
                assert evidence["current_request"] == self.request
                assert evidence["candidate_reply"] == self.primary_responses[len(self.primary_requests) - 1].message.content
                first = len(self.end_inputs) == 1
                payload["publication_verdict"] = "WITHHOLD" if first else "ADMIT"
                payload["findings"] = self.first_findings if first else ""
            content = json.dumps(payload)
        return litellm.ModelResponse(
            model=kwargs["model"],
            choices=[
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    def assert_finished(self) -> None:
        assert tuple(self.phases) == self.expected_phases
        assert len(self.primary_requests) == len(self.primary_responses)
        assert [verdict.publication_verdict for verdict in self.end_outputs] == ["WITHHOLD", "ADMIT"]
        assert self.end_inputs[0].binding != self.end_inputs[1].binding
        assert len(self.attempt_ids) == len(set(self.attempt_ids)) == len(self.expected_phases)
        assert len(self.mints) == len(self.expected_phases)
        assert len({id(owner.required_work.coordinator) for owner, _family, _custody in self.mints}) == 1
        self.mints[0][0].required_work.coordinator.assert_completed()
        with self.service._session_engine.connect() as conn:
            attempts = conn.execute(
                select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.attempt_id.in_(self.attempt_ids))
            ).all()
            session_id = self.mints[0][2].context.fence.session_id
            audit_rows = conn.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.session_id == session_id, chat_messages_table.c.role == "audit")
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
            ledger = conn.execute(
                select(token_usage_ledger_table).where(
                    token_usage_ledger_table.c.entry_id.in_(attempt.ledger_entry_id for attempt in attempts)
                )
            ).all()
        assert len(attempts) == len(self.attempt_ids)
        assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
        assert len(ledger) == len(attempts)
        assert all(row.prompt_tokens == row.completion_tokens == 10 for row in ledger)
        calls = [envelope["call"] for row in audit_rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "llm_call_audit"]
        assert len(calls) == len(self.attempt_ids) and {call["call_id"] for call in calls} == set(self.attempt_ids)
        assert all(call["status"] == "success" for call in calls)
        guards = [
            envelope["check"] for row in audit_rows for envelope in (row.tool_calls or ()) if envelope["_kind"] == "prompt_guard_audit"
        ]
        assert [(check["role"], check["disposition"]) for check in guards] == [
            ("composer" if phase == "primary" else "advisor", "debug_bypass") for phase in self.expected_phases
        ]
        assert any(
            row.content == self.primary_responses[0].message.content
            and any(
                envelope["_kind"] == COMPOSER_WITHHELD_REPLY_KIND and envelope["origin"] == "repair_gate_superseded"
                for envelope in (row.tool_calls or ())
            )
            for row in audit_rows
        )
        repair = self.primary_requests[1]["messages"]
        assert self.first_findings in json.dumps(repair)
        assert any(message["role"] == "assistant" and message["content"] == self.primary_responses[0].message.content for message in repair)
        assert not any(
            message["role"] == "user"
            and isinstance(message["content"], str)
            and message["content"].startswith(
                ("[composer-system] No tool has run this turn", "[composer-system] No composition-state mutation completed successfully")
            )
            for request in self.primary_requests
            for message in request["messages"]
        )


@contextlib.contextmanager
def _real_semantic_recovery_sdk(script: _SemanticRecoveryLLMScript) -> Iterator[None]:
    """Restore the real per-instance checkpoint and observe its actual mint owner."""
    real_mint = ProviderInvocationOwner.mint

    def mint(owner: ProviderInvocationOwner, family: ProviderInvocationFamily):
        custody = real_mint(owner, family)
        script.mints.append((owner, family, custody))
        return custody

    with (
        patch("litellm.acompletion", new=script.complete),
        patch.object(script.service._advisor_checkpoint, "_run_advisor_checkpoint", new=script.checkpoint),
        patch.object(ProviderInvocationOwner, "mint", new=mint),
    ):
        yield


def _advisor_checkpoint_responses(*replies: str) -> Callable[..., Awaitable[tuple[str, dict[str, object]]]]:
    responses = iter(replies)

    async def respond(
        *_args: object, on_provider_dispatch: Callable[[], None] | None = None, **_kwargs: object
    ) -> tuple[str, dict[str, object]]:
        if on_provider_dispatch is not None:
            on_provider_dispatch()
        return next(responses), {}

    return respond


def test_service_rejects_uninferrable_advisor_provider() -> None:
    settings = _make_settings(composer_advisor_model="custom-model-without-provider")

    with pytest.raises(ValueError, match="composer_advisor_model provider could not be inferred"):
        ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)


def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    state: CompositionState,
    catalog: Any,
    **kwargs: Any,
) -> ToolResult:
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return _strict_execute_tool(
        tool_name,
        arguments,
        state,
        PolicyCatalogView.for_trained_operator(catalog, snapshot),
        plugin_snapshot=snapshot,
        **kwargs,
    )


def _execution_ready() -> ValidationReadiness:
    return ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[])


def _not_authoring_ready(code: str = "test_blocker") -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=False,
        execution_ready=False,
        completion_ready=False,
        blockers=[
            ValidationReadinessBlocker(
                suggestion=None,
                note=None,
                code=code,
                component_id=None,
                component_type=None,
                detail=code,
            )
        ],
    )


def _pending_interpretation_readiness() -> ValidationReadiness:
    return ValidationReadiness(
        authoring_valid=True,
        execution_ready=False,
        completion_ready=True,
        blockers=[
            ValidationReadinessBlocker(
                suggestion=None,
                note=None,
                code=INTERPRETATION_REVIEW_PENDING_CODE,
                component_id="rate_node",
                component_type="transform",
                detail="rate_node:cool",
            )
        ],
    )


def ValidationResult(
    *,
    is_valid: bool,
    checks: list[ValidationCheck],
    errors: list[ValidationError],
    readiness: ValidationReadiness | None = None,
    **kwargs: Any,
) -> ValidationResultModel:
    return ValidationResultModel(
        is_valid=is_valid,
        checks=checks,
        errors=errors,
        readiness=readiness or (_execution_ready() if is_valid else _not_authoring_ready()),
        **kwargs,
    )


def _assert_no_mutation_empty_state_blocker(
    result: ComposerResult,
    *,
    tool_name: str,
    expected_detail: str,
) -> None:
    """Assert the no-mutation empty-state augmentation contract (post elspeth-861b0c58f5).

    The new shape (vs. the old synthetic-replacement behavior):
    - The model's prose is preserved verbatim at the start of result.message
      (raw_assistant_content carries the same prose unaugmented).
    - A system-attributed suffix is appended carrying the concrete blocker.
    - The runtime_preflight ValidationResult records the state_exists=false
      check and the blocker detail for audit-trail attribution.
    """
    assert result.runtime_preflight is not None
    assert result.runtime_preflight.is_valid is False
    assert [check.name for check in result.runtime_preflight.checks] == ["state_exists"]
    assert result.raw_assistant_content is not None
    # Model's prose preserved verbatim at the start; system suffix appended.
    assert result.message.startswith(result.raw_assistant_content)
    # System suffix carries the operator-facing meta-narration.
    assert "[ELSPETH-SYSTEM]" in result.message
    assert "still empty" in result.message
    # Blocker detail surfaced both in suffix (for the user) and in the
    # runtime_preflight ValidationResult (for audit-trail attribution).
    assert tool_name in result.message
    assert expected_detail in result.message
    blocker_detail = result.runtime_preflight.checks[0].detail
    assert tool_name in blocker_detail
    assert expected_detail in blocker_detail


def _session_engine_with_session(*, user_id: str = "test-user") -> tuple[Any, str]:
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id=user_id)
        grant_test_pipeline_user(conn, identity_id=user_id)
        conn.execute(
            sessions_table.insert().values(
                id=session_id,
                user_id=user_id,
                auth_provider_type="local",
                title="Test Session",
                trust_mode="auto_commit",
                density_default="high",
                created_at=now,
                updated_at=now,
            )
        )
    return engine, session_id


def _insert_user_message(engine: Any, session_id: str, content: str) -> str:
    """Persist a user chat message for tests that create verbatim blobs."""
    user_message_id = str(uuid4())
    with engine.begin() as conn:
        latest = conn.execute(
            select(chat_messages_table.c.sequence_no)
            .where(chat_messages_table.c.session_id == session_id)
            .order_by(chat_messages_table.c.sequence_no.desc())
        ).first()
        sequence_no = 1 if latest is None else latest.sequence_no + 1
        conn.execute(
            chat_messages_table.insert().values(
                id=user_message_id,
                session_id=session_id,
                role="user",
                content=content,
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=sequence_no,
                writer_principal="route_user_message",
                created_at=datetime.now(UTC),
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    return user_message_id


def _verbatim_blob_context(engine: Any, session_id: str, content: str) -> dict[str, str]:
    user_message_content = f"Use this exact content:\n{content}"
    return {
        "user_message_id": _insert_user_message(engine, session_id, user_message_content),
        "user_message_content": user_message_content,
    }


def _test_sessions_service(engine: Any, data_dir: Path | None = None) -> SessionServiceImpl:
    return FencedSessionServiceHarness(
        engine,
        data_dir=data_dir,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.composer.sessions"),
    )


def _composer_service_with_session(catalog: CatalogService, settings: Any) -> tuple[ComposerServiceImpl, str]:
    engine, session_id = _session_engine_with_session()
    service = ComposerServiceImpl.for_trained_operator(
        catalog=catalog,
        settings=settings,
        sessions_service=_test_sessions_service(engine, Path(settings.data_dir)),
        session_engine=engine,
    )
    return service, session_id


@contextlib.contextmanager
def _sampling_provider_owner(
    service: ComposerServiceImpl,
    session_id: str,
    *,
    expected_calls: int,
    expected_guard_mode: Literal["required", "debug-when-unconfigured"] = "debug-when-unconfigured",
) -> Iterator[tuple[ProviderInvocationOwner, BufferingRecorder]]:
    sessions = service._sessions_service
    engine = service._session_engine
    assert isinstance(sessions, SessionServiceImpl) and engine is not None
    assert service._settings.composer_prompt_guard_mode == expected_guard_mode
    with fenced_operation_context(engine, session_id) as context:
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
        )
        owner = ProviderInvocationOwner(service=sessions, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
        recorder = BufferingRecorder()
        try:
            yield owner, recorder
        finally:
            coordinator.assert_completed()
        with engine.connect() as connection:
            attempts = connection.execute(select(quota_provider_attempts_table)).all()
            rows = connection.execute(select(chat_messages_table)).all()
        checks = [
            envelope["check"]
            for row in rows
            if row.tool_calls is not None
            for envelope in row.tool_calls
            if envelope["_kind"] == "prompt_guard_audit"
        ]
        assert len(checks) == len(attempts) == len(recorder.llm_calls) == expected_calls
        if expected_guard_mode == "required":
            assert all(
                check["role"] == "composer"
                and check["mode"] == "required"
                and check["adapter"] == "azure_prompt_shields"
                and check["disposition"] == "allow"
                and check["reason"] == "clean"
                and check["operation_id"] == context.fence.operation_id
                and check["operation_epoch"] == context.fence.operation_epoch
                for check in checks
            )
        else:
            assert all(
                check["role"] == "composer"
                and check["mode"] == "debug-when-unconfigured"
                and check["disposition"] == "debug_bypass"
                and check["operation_id"] == context.fence.operation_id
                for check in checks
            )
        assert {attempt.attempt_id for attempt in attempts} == {call.call_id for call in recorder.llm_calls}
        assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)


@pytest.fixture
def _sampling_required_guard(monkeypatch: pytest.MonkeyPatch) -> tuple[PromptGuardSettings, list[dict[str, Any]]]:
    """Keep the real required Azure guard and durable custody on direct SDK tests."""
    settings = PromptGuardSettings(endpoint="https://sampling-guard.example.test", subscription_key="offline-sampling")
    requests: list[dict[str, Any]] = []

    def shield_response(request: Request) -> Response:
        assert request.url.host == "sampling-guard.example.test"
        assert request.method == "POST" and request.url.path == "/contentsafety/text:shieldPrompt"
        body = json.loads(request.content)
        requests.append(body)
        return Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in body["documents"]],
            },
        )

    def mount(config: PromptGuardSettings | None) -> PromptGuardMount:
        assert config is not None and config is settings
        return PromptGuardMount(AzurePromptGuardClient(config, http_transport=MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    return settings, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("verification_fails", [False, True], ids=["guard-only", "guard-and-integrity"])
@pytest.mark.parametrize("advisor_path", ["early", "manual"])
async def test_guard_refusal_retains_committed_effect_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verification_fails: bool,
    advisor_path: str,
) -> None:
    import litellm

    from elspeth.web.dependencies import create_catalog_service

    class CheckpointGuard(PromptGuardClient):
        def __init__(self, decision: PromptGuardDecision) -> None:
            self.decision = decision
            self.requests: list[PromptGuardRequest] = []

        @property
        def adapter(self) -> str:
            return "controlled-local"

        @property
        def policy_fingerprint(self) -> str:
            return "b" * 64

        @property
        def timeout_seconds(self) -> float:
            return 5.0

        async def inspect(self, request: PromptGuardRequest) -> PromptGuardDecision:
            self.requests.append(request)
            return self.decision

    primary_guard = CheckpointGuard(PromptGuardDecision("allow", "clean"))
    advisor_guard = CheckpointGuard(PromptGuardDecision("blocked", "policy_blocked"))
    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", lambda _settings: PromptGuardMount(primary_guard))
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", lambda _settings: PromptGuardMount(advisor_guard))
    monkeypatch.setattr(AdvisorCheckpointOwner, "_run_advisor_checkpoint", _REAL_RUN_ADVISOR_CHECKPOINT)
    service, session_id = _composer_service_with_session(
        create_catalog_service(), _make_settings(data_dir=tmp_path, composer_prompt_guard_mode="required")
    )
    sessions = service._require_sessions_service()
    engine = service._session_engine
    assert engine is not None
    blob_id = _create_session_blob_for_test(
        engine=engine, session_id=session_id, data_dir=tmp_path, filename="input.csv", content="name,score\nada,42\n"
    )
    with engine.connect() as connection:
        source_path = connection.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).scalar_one()
    source_arguments = {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": source_path, "schema": {"mode": "observed"}},
        "on_validation_failure": "discard",
    }
    pipeline = {
        "source": source_arguments,
        "nodes": [],
        "edges": [],
        "outputs": [
            {
                "sink_name": "rows",
                "plugin": "json",
                "options": {
                    "path": "outputs/result.jsonl",
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
    }
    sdk_calls: list[dict[str, Any]] = []
    tool_calls = [
        {
            "id": "source-early-1",
            "type": "function",
            "function": {"name": "set_pipeline", "arguments": json.dumps({"pipeline": pipeline})},
        }
    ]
    if advisor_path == "manual":
        tool_calls.append(
            {
                "id": "manual-advisor-1",
                "type": "function",
                "function": {
                    "name": "request_advisor_hint",
                    "arguments": json.dumps(
                        {
                            "trigger": "proactive_security_safety",
                            "problem_summary": "Review the supplied pipeline evidence.",
                            "recent_errors": [],
                            "attempted_actions": ["set_pipeline"],
                        }
                    ),
                },
            }
        )
    expected_tool_ids = tuple(call["id"] for call in tool_calls)

    async def completion(**kwargs: Any) -> litellm.ModelResponse:
        sdk_calls.append(deepcopy(kwargs))
        assert len(sdk_calls) == 1, [message for message in kwargs["messages"] if message["role"] == "tool"]
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    }
                }
            ],
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    original_advisor = (
        service._advisor_checkpoint._call_advisor_with_audit
        if advisor_path == "manual"
        else service._advisor_checkpoint._maybe_run_early_checkpoint
    )
    original_accept = TurnEvidence.accept_checkpoint
    guard_refusals: list[PromptGuardRefused] = []
    integrity_failures: list[AuditIntegrityError] = []
    committed_snapshots: list[tuple[list[Any], list[Any], str]] = []
    evidence_owners: list[TurnEvidence] = []
    observed_recorders: list[BufferingRecorder] = []

    async def observe_advisor(*args: Any, **kwargs: Any) -> Any:
        recorder = kwargs["recorder"]
        assert isinstance(recorder, BufferingRecorder)
        observed_recorders.append(recorder)
        try:
            return await original_advisor(*args, **kwargs)
        except PromptGuardRefused as refusal:
            guard_refusals.append(refusal)
            raise

    def verify_actual_checkpoint(
        evidence: TurnEvidence,
        witness: CommittedCheckpointWitness,
        *,
        expected_state: CompositionState,
        tool_call_ids: tuple[str, ...],
        assistant_message_id: str,
    ) -> None:
        with engine.connect() as connection:
            states = connection.execute(select(composition_states_table)).all()
            conversation = connection.execute(
                select(chat_messages_table)
                .where(chat_messages_table.c.role.in_(("assistant", "tool")))
                .order_by(chat_messages_table.c.sequence_no)
            ).all()
        assert len(states) == 1 and states[0].id == str(witness.state.id)
        assert [row.role for row in conversation] == ["assistant", *["tool"] * len(expected_tool_ids)]
        assert conversation[0].id == assistant_message_id
        assert tuple(row.tool_call_id for row in conversation[1:]) == expected_tool_ids
        committed_snapshots.append((states, conversation, assistant_message_id))
        evidence_owners.append(evidence)
        try:
            # Corrupt only the actual comparison input after the real P4
            # commit. The production verifier raises the integrity original;
            # neither the persisted rows nor the dispatched state is changed.
            original_accept(
                evidence,
                witness,
                expected_state=expected_state.with_metadata({"name": "controlled comparison mismatch"})
                if verification_fails
                else expected_state,
                tool_call_ids=tool_call_ids,
                assistant_message_id=assistant_message_id,
            )
        except AuditIntegrityError as failure:
            integrity_failures.append(failure)
            raise

    if advisor_path == "manual":
        monkeypatch.setattr(service._advisor_checkpoint, "_call_advisor_with_audit", observe_advisor)
    else:
        monkeypatch.setattr(service._advisor_checkpoint, "_maybe_run_early_checkpoint", observe_advisor)
    monkeypatch.setattr(TurnEvidence, "accept_checkpoint", verify_actual_checkpoint)
    try:
        with fenced_operation_context(engine, session_id) as context:
            coordinator = RequiredWorkCoordinator(
                RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
            )
            binding = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN)
            owner = ProviderInvocationOwner(service=sessions, required_work=binding)
            with pytest.raises((PromptGuardRefused, BaseExceptionGroup)) as caught:
                await service.compose(
                    "Add a CSV source and review it.",
                    [],
                    _empty_state(),
                    session_id=session_id,
                    user_id="test-user",
                    session_operation_context=context,
                    required_work=binding,
                    provider_owner=owner,
                )
            assert len(guard_refusals) == len(committed_snapshots) == len(evidence_owners) == 1
            assert len(primary_guard.requests) == len(advisor_guard.requests) == len(sdk_calls) == 1
            assert len(integrity_failures) == int(verification_fails)
            projection = project_composer_operation_error(caught.value, request_id="controlled-advisor-request")
            assert projection.http_status == (500 if verification_fails else 422)
            if advisor_path == "manual":
                assert len(observed_recorders) == 1
                advisor_invocation = next(
                    invocation for invocation in observed_recorders[0].invocations if invocation.tool_name == "request_advisor_hint"
                )
                assert advisor_invocation.status is ComposerToolStatus.SUCCESS
                assert advisor_invocation.result_canonical is not None
                refusal_payload = json.loads(advisor_invocation.result_canonical)
                assert refusal_payload["success"] is False and refusal_payload["outbound_call_made"] is False
                assert refusal_payload["error_type"] == "prompt_guard_blocked"
            leaves = caught.value.exceptions if isinstance(caught.value, BaseExceptionGroup) else (caught.value,)
            enriched = [failure for failure in leaves if isinstance(failure, PromptGuardRefused)]
            assert len(enriched) == 1
            refusal = enriched[0]
            assert refusal.__cause__ is guard_refusals[0]
            assert refusal.audit_id == guard_refusals[0].audit_id
            assert refusal.tool_invocations == () and refusal.partial_state is None
            assert len(refusal.llm_calls) == 1 and refusal.failed_turn is not None
            assert refusal.failed_turn.assistant_message_id == committed_snapshots[0][2]
            assert refusal.failed_turn.tool_calls_attempted == refusal.failed_turn.tool_responses_persisted == len(expected_tool_ids)
            if verification_fails:
                assert len(integrity_failures) == 1
                assert integrity_failures[0] in leaves
                assert integrity_failures[0].failed_turn is refusal.failed_turn
                assert evidence_owners[0].effects.verification == "unavailable"
                assert projection.http_status == 500 and projection.error_type == "audit_integrity_error"
                assert projection.body["failed_turn"]["assistant_message_id"] == refusal.failed_turn.assistant_message_id
                assert projection.body["failed_turn"]["tool_calls_attempted"] == len(expected_tool_ids)
                assert projection.body["failed_turn"]["tool_responses_persisted"] == len(expected_tool_ids)
            else:
                assert integrity_failures == []
                assert evidence_owners[0].effects.verification == "verified"
                assert projection.http_status == 422 and projection.error_type == "prompt_guard_blocked"
            # Drain only the remaining physical model-call audit as the route
            # does. The committed assistant/tool checkpoint must not replay.
            await _persist_turn_audit_cohort(
                sessions,
                UUID(session_id),
                refusal.tool_invocations,
                refusal.llm_calls,
                tool_composition_state_id=None,
                llm_composition_state_id=None,
                plugin_crash_pending=True,
                required_audit=True,
                required_work=coordinator,
                session_operation_context=context,
            )
            coordinator.assert_completed()
            with engine.connect() as connection:
                states_after = connection.execute(select(composition_states_table)).all()
                rows = connection.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
                attempts = connection.execute(select(quota_provider_attempts_table)).all()
            assert states_after == committed_snapshots[0][0]
            assert [row for row in rows if row.role in ("assistant", "tool")] == committed_snapshots[0][1]
            assert [
                row.tool_calls[0]["check"]["disposition"]
                for row in rows
                if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
            ] == ["allow", "blocked"]
            assert (
                len([row for row in rows if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == "llm_call_audit"])
                == len(attempts)
                == 1
            )
            assert attempts[0].attempt_id == refusal.llm_calls[0].call_id
            current = await sessions.get_current_state(UUID(session_id))
            assert current is not None and current.sources["source"]["plugin"] == "csv"
    finally:
        engine.dispose()


def _create_session_blob_for_test(
    *,
    engine: Any,
    session_id: str,
    data_dir: Path,
    filename: str = "seed.txt",
    content: str = "original content",
) -> str:
    provenance_context = _verbatim_blob_context(engine, session_id, content)
    with fenced_operation_context(engine, session_id) as context:
        result = _execute_tool(
            "create_blob",
            {
                "filename": filename,
                "mime_type": "text/plain",
                "content": content,
            },
            _empty_state(),
            _mock_catalog(),
            data_dir=str(data_dir),
            session_engine=engine,
            session_id=session_id,
            user_message_id=provenance_context["user_message_id"],
            user_message_content=provenance_context["user_message_content"],
            session_operation_context=context,
            session_operation_authority=SQLiteLocalSessionOperationAuthority(engine),
        )
    assert result.success is True, result.data
    return str(result.data["blob_id"])


def _blob_content_for_test(engine: Any, blob_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(select(blobs_table.c.storage_path).where(blobs_table.c.id == blob_id)).first()
    if row is None:
        return None
    return Path(row.storage_path).read_text(encoding="utf-8")


def test_effect_resolution_and_summaries_never_write_seeded_blob_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel

    pipeline_arguments = {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "on_validation_failure": "discard",
            "inline_blob": {"filename": "input.csv", "mime_type": "text/csv", "content": "SECRET_SENTINEL_RAW"},
        },
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    SetPipelineArgumentsModel.model_validate(pipeline_arguments)
    engine, session_id = _session_engine_with_session()
    blob_id = _create_session_blob_for_test(engine=engine, session_id=session_id, data_dir=tmp_path)
    with engine.connect() as conn:
        rows_before = conn.execute(select(blobs_table)).all()
    files_before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    declarations = dict(tool_registry._DECLARATIONS_BY_NAME)
    spies = []
    for name in ("create_blob", "update_blob", "delete_blob", "set_pipeline"):
        declaration = declarations[name]
        spy = create_autospec(declaration.handler, side_effect=AssertionError("effect resolution invoked mutation handler"))
        spies.append(spy)
        declarations[name] = replace(declaration, handler=spy, json_schema=deep_thaw(declaration.json_schema))
    monkeypatch.setattr(tool_registry, "_DECLARATIONS_BY_NAME", declarations)
    for name, arguments in (
        ("create_blob", {"filename": "SECRET_SENTINEL_RAW", "content": "SECRET_SENTINEL_RAW"}),
        ("update_blob", {"blob_id": blob_id, "content": "SECRET_SENTINEL_RAW"}),
        ("delete_blob", {"blob_id": blob_id}),
        ("set_pipeline", pipeline_arguments),
    ):
        owned = deep_freeze(arguments)
        assert tool_registry.resolve_tool_effects(name, owned).domains
        summary = build_tool_proposal_summary(tool_name=name, arguments=owned, redacted_arguments={"blob_id": "safe-file"})
        assert "SECRET_SENTINEL_RAW" not in repr(summary)
    for spy in spies:
        spy.assert_not_called()
    with engine.connect() as conn:
        assert conn.execute(select(blobs_table)).all() == rows_before
    assert {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == files_before


@pytest.fixture(autouse=True)
def _composer_available_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep service tests focused on compose behavior, not local API keys."""

    def _available(**kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=str(kwargs["model"]), provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)


@pytest.fixture(autouse=True)
def _composer_to_thread_uses_test_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run composer to_thread seams through a deterministic test worker.

    These unit tests assert the composer offloads synchronous tool and audit
    work; they do not need to test asyncio's executor implementation. The shim
    keeps the off-event-loop-thread property visible while avoiding local
    executor hangs from masking composer behavior.
    """

    async def test_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        import threading

        result: list[Any] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                result.append(func(*args, **kwargs))
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=run, name="composer-test-worker")
        worker.start()
        while worker.is_alive():
            await asyncio.sleep(0.001)
        worker.join()
        if failures:
            raise failures[0]
        if result:
            return result[0]
        return None

    monkeypatch.setattr("asyncio.to_thread", test_to_thread)


def test_freeform_planner_context_preserves_every_authenticated_request() -> None:
    messages = [
        {
            "role": "user",
            "content": f"request-{index}",
            COMPOSER_HISTORY_USER_AUTHORED_KEY: True,
        }
        for index in range(10)
    ]

    context = _freeform_planner_conversation_context("Build the requested pipeline.", messages)

    assert context is not None
    assert [(request.history_index, request.content) for request in context.prior_user_requests] == [
        (0, "request-0"),
        (1, "request-1"),
        (2, "request-2"),
        (3, "request-3"),
        (4, "request-4"),
        (5, "request-5"),
        (6, "request-6"),
        (7, "request-7"),
        (8, "request-8"),
        (9, "request-9"),
    ]
    assert context.additional_prior_user_requests_omitted == 0


@pytest.mark.parametrize(
    "message",
    [
        "Build the requested pipeline.",
        "Could you build the requested pipeline?",
        "Can you please build the requested workflow?",
        "I want you to build the requested pipeline.",
        "Let's build the requested pipeline.",
        "Build!",
        "Build this pipeline.",
        "Update this pipeline.",
        "Change this workflow.",
        "Run this pipeline.",
        "Build the pipeline.",
        "Change this node.",
        "Route this input.",
        "Save this output.",
    ],
)
def test_freeform_planner_context_reaches_every_admitted_referential_form(message: str) -> None:
    context = _freeform_planner_conversation_context(
        message,
        [
            {
                "role": "user",
                "content": "Route rows with amount > 500 to high_value.",
                COMPOSER_HISTORY_USER_AUTHORED_KEY: True,
            }
        ],
    )

    assert context is not None
    assert [request.content for request in context.prior_user_requests] == ["Route rows with amount > 500 to high_value."]


@pytest.mark.parametrize(
    "message",
    [
        "Build a CSV to JSONL pipeline.",
        "Build a pipeline that routes every row to both sinks.",
        "Fan every row out to both sinks.",
        "Remove the threshold and build a JSONL pipeline.",
    ],
)
def test_freeform_planner_context_does_not_override_self_contained_current_intent(message: str) -> None:
    context = _freeform_planner_conversation_context(
        message,
        [{"role": "user", "content": "Route rows with amount > 500 to high_value."}],
    )

    assert context is None


def test_freeform_planner_context_present_but_invalid_authorship_marker_crashes() -> None:
    """The marker is NotRequired[Literal[True]]: absence skips the entry, but a
    PRESENT non-True value (e.g. None) is a broken first-party contract and must
    raise instead of being folded into the absent path by a ``.get()`` default."""
    from elspeth.web.composer.invariants import InvariantError

    with pytest.raises(InvariantError, match="user-authorship marker is malformed"):
        _freeform_planner_conversation_context(
            "Build the requested pipeline.",
            [
                {
                    "role": "user",
                    "content": "Route rows with amount > 500 to high_value.",
                    COMPOSER_HISTORY_USER_AUTHORED_KEY: None,  # type: ignore[typeddict-item]
                }
            ],
        )


class TestComposerTextOnlyResponse:
    @pytest.mark.asyncio
    async def test_state_credentials_are_refused_before_prompt_serialization_or_provider(self) -> None:
        service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="output",
                options={"path": "input.csv", "password": "low-entropy-secret"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        with (
            patch("elspeth.web.composer.service.build_messages") as build,
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as provider,
            pytest.raises(CredentialMaterialRefused) as caught,
        ):
            await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert caught.value.surface == "composer_state_before_provider"
        build.assert_not_called()
        provider.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_build_text_only_returns_immediately(self) -> None:
        """Non-build text-only replies still terminate without mutation."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        llm_response = _make_llm_response(content="I'll help you build a pipeline!")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            result = await service.compose("What can this composer do?", [], state, session_id=session_id)

        assert isinstance(result, ComposerResult)
        assert result.raw_assistant_content == "I'll help you build a pipeline!"
        assert result.message.startswith(result.raw_assistant_content)
        assert _no_tool_policy_module.visible_message_segments(content=result.message, raw_content=result.raw_assistant_content) == (
            _no_tool_policy_module.AssistantTextSegment(result.raw_assistant_content),
            _no_tool_policy_module.TrustedSystemNoticeSegment(
                "Checkpoint save status: Verified checkpoint evidence covers 0 saved draft revisions. "
                "There is no net authored change from the starting draft. Checkpoint evidence does not establish task completion."
            ),
        )
        assert mock_llm.await_count == 1
        assert result.state.version == 1  # unchanged

    @pytest.mark.asyncio
    async def test_build_request_text_only_reaches_real_end_without_fabricated_mutation_blocker(self) -> None:
        """Actual END withholds unsupported prose; zero attempts do not invent a tool failure."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()
        model_prose = "I set up the workflow conceptually and can continue."
        truthful = "No workflow has been configured. Please supply leads_q3.csv."
        request = 'Build the workflow described by the accepted request: "Read leads_q3.csv and write JSON".'
        user_message_id = _insert_user_message(service._session_engine, session_id, request)
        script = _SemanticRecoveryLLMScript(
            service,
            request,
            (_make_llm_response(content=model_prose), _make_llm_response(content=truthful)),
            phases=("primary", "end", "primary", "end"),
            first_findings="The reply claims a configured workflow without any saved composition work.",
        )
        with _real_semantic_recovery_sdk(script):
            result = await service.compose(request, [], state, session_id=session_id, user_id="test-user", user_message_id=user_message_id)

        script.assert_finished()
        assert result.state.version == state.version
        assert not result.tool_invocations
        assert result.runtime_preflight is None
        assert result.repair_turns_used == 0
        _assert_checkpoint_reply(result, truthful, count=0)
        assert model_prose not in result.message
        assert "still empty" not in result.message
        assert "without calling any build/edit tool" not in result.message
        assert all(json.loads(review.payload_json)["saved_effects"]["saved_revision_count"] == 0 for review in script.end_inputs)

    @pytest.mark.asyncio
    async def test_failed_mutation_then_empty_state_reply_names_blocking_tool_error(self) -> None:
        """A failed mutation followed by final prose must surface the tool error."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()
        failed_set_pipeline = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad_pipeline",
                    "name": "set_pipeline",
                    "arguments": {
                        "source": {"on_success": "rows", "options": {}},
                        "nodes": [],
                        "edges": [],
                        "outputs": [],
                    },
                }
            ],
        )
        final_prose = "I tried to build it, but the workflow is still conceptual."
        text_response = _make_llm_response(content=final_prose)

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(failed_set_pipeline, text_response)
            mock_llm.side_effect = script.respond
            result = await service.compose("Build the CSV workflow now.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        assert result.state.version == state.version
        assert result.raw_assistant_content == final_prose
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is False
        # New contract: model prose preserved + system suffix with blocker.
        assert result.message.startswith(final_prose)
        assert "[ELSPETH-SYSTEM]" in result.message
        assert "still empty" in result.message
        assert "set_pipeline failed before mutation" in result.message
        # S0: the required-path gate records the ToolArgumentError it builds
        # (was the hand label "MissingRequiredPaths", a class that never existed).
        assert "(ToolArgumentError: missing: " in result.message
        assert "source.plugin" in result.message


class TestComposerSingleToolCall:
    @pytest.mark.asyncio
    async def test_tool_dispatch_receives_configured_blob_quota(self) -> None:
        """Composer dispatch must thread WebSettings blob quota into tool execution."""

        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(),
            settings=_make_settings(max_blob_storage_per_session_bytes=3),
        )
        state = _empty_state()
        captured_quota: list[int | None] = []

        def fake_execute_tool(
            _tool_name: str,
            _arguments: dict[str, Any],
            _state: CompositionState,
            _catalog: CatalogService,
            *args: Any,
            max_blob_storage_per_session_bytes: int | None = None,
            **kwargs: Any,
        ) -> ToolResult:
            del args, kwargs
            captured_quota.append(max_blob_storage_per_session_bytes)
            return ToolResult(
                success=True,
                updated_state=_state.with_metadata({"name": "captured"}),
                validation=ValidationSummary(is_valid=True, errors=()),
                affected_nodes=("metadata",),
            )

        turn = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_set_metadata",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "captured"}},
                }
            ],
        )
        done = _make_llm_response(content="Done.")

        with (
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=fake_execute_tool),
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.side_effect = [turn, done]
            await service.compose("Update metadata", [], state, session_id=session_id)

        assert captured_quota == [3]

    @pytest.mark.asyncio
    async def test_tool_dispatch_receives_composer_source_provenance_context(self) -> None:
        """Sync tool dispatch receives the user message and audited composer provenance."""

        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(),
            settings=_make_settings(composer_model="gpt-5.5"),
        )
        service._availability = ComposerAvailability(  # type: ignore[misc]
            available=True,
            model="gpt-5.5",
            provider="test-provider",
        )
        state = _empty_state()
        captured_kwargs: dict[str, Any] = {}
        arguments = {"patch": {"name": "captured"}}

        def fake_execute_tool(
            _tool_name: str,
            _arguments: dict[str, Any],
            _state: CompositionState,
            _catalog: CatalogService,
            *args: Any,
            **kwargs: Any,
        ) -> ToolResult:
            del args, _arguments, _catalog
            captured_kwargs.update(kwargs)
            return ToolResult(
                success=True,
                updated_state=_state.with_metadata({"name": "captured"}),
                validation=ValidationSummary(is_valid=True, errors=()),
                affected_nodes=("metadata",),
            )

        turn = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_set_metadata",
                    "name": "set_metadata",
                    "arguments": arguments,
                }
            ],
        )
        done = _make_llm_response(content="Done.")

        with (
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=fake_execute_tool),
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.side_effect = [turn, done]
            await service.compose(
                "Create generated source content.", [], state, user_message_id="11111111-1111-1111-1111-111111111111", session_id=session_id
            )

        assert captured_kwargs["user_message_id"] == "11111111-1111-1111-1111-111111111111"
        assert captured_kwargs["user_message_content"] == "Create generated source content."
        assert captured_kwargs["composer_model_identifier"] == "gpt-5.5"
        assert captured_kwargs["composer_model_version"] == "gpt-5.5"
        assert captured_kwargs["composer_provider"] == "test-provider"
        assert captured_kwargs["composer_skill_hash"] == service._composer_skill_hash
        assert captured_kwargs["tool_arguments_hash"] == stable_hash(arguments)

    @pytest.mark.asyncio
    async def test_explicit_approve_mutating_tool_creates_pending_proposal_without_state_mutation(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
        fake_llm_one_set_pipeline_tool_call: Any,
    ) -> None:
        """Explicit approval mode stores mutating tool calls as pending proposals."""
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)
        state = _empty_state()

        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )

        result = await composer_service_with_real_sessions._run_one_turn_for_test(
            llm=fake_llm_one_set_pipeline_tool_call,
            session_id=result_session_id,
            initial_state=state,
        )

        proposals = await sessions_service.list_composition_proposals(session_uuid)
        assert len(proposals) == 1
        assert proposals[0].tool_call_id == "call_set_pipeline"
        assert proposals[0].tool_name == "set_pipeline"
        assert proposals[0].status == "pending"
        assert "Replace the pipeline" in proposals[0].summary
        assert proposals[0].composer_model_identifier == composer_service_with_real_sessions._model
        assert proposals[0].composer_model_version == composer_service_with_real_sessions._model
        assert proposals[0].composer_provider == "test"
        assert proposals[0].composer_skill_hash == composer_service_with_real_sessions._composer_skill_hash
        assert proposals[0].tool_arguments_hash == stable_hash(proposals[0].arguments_json)
        assert "pipeline" not in proposals[0].arguments_json
        assert {"source", "nodes", "edges", "outputs", "metadata"}.issubset(proposals[0].arguments_json)
        assert result.tool_outcomes[0].post_version == state.version

    @pytest.mark.asyncio
    async def test_explicit_approve_proposal_uses_profile_aware_validation(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
        fake_llm_one_set_pipeline_tool_call: Any,
    ) -> None:
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        await sessions_service.update_composer_preferences(
            UUID(result_session_id),
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )
        state = _empty_state()

        result = await composer_service_with_real_sessions._run_one_turn_for_test(
            llm=fake_llm_one_set_pipeline_tool_call,
            session_id=result_session_id,
            initial_state=state,
        )

        proposal_result = result.tool_outcomes[0].response
        assert isinstance(proposal_result, ToolResult)
        catalog = _mock_catalog()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        expected = PolicyCatalogView.for_trained_operator(
            catalog,
            snapshot,
        ).validate_composition_state(state)
        assert proposal_result.validation == expected.validation

    @pytest.mark.asyncio
    async def test_create_composition_proposal_normalizes_composer_provenance(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
    ) -> None:
        """Proposal provenance is normalized before it becomes audit-bearing state."""
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)

        proposal = await sessions_service.create_composition_proposal(
            session_id=session_uuid,
            tool_call_id="call_set_pipeline",
            tool_name="set_pipeline",
            summary="Replace the pipeline.",
            rationale="Composer proposed a pipeline update.",
            affects=["graph"],
            arguments_json={"source": {"plugin": "csv", "options": {}}},
            arguments_redacted_json={"source": {"plugin": "csv", "options": {}}},
            base_state_id=None,
            actor="assistant",
            composer_model_identifier=" openai/gpt-5-mini ",
            composer_model_version=" gpt-5-mini-2026-05-01 ",
            composer_provider=" openai ",
            composer_skill_hash=f" {fake_sha256('composer-skill')} ",
            tool_arguments_hash=f" {fake_sha256('tool-arguments')} ",
        )

        assert proposal.composer_model_identifier == "openai/gpt-5-mini"
        assert proposal.composer_model_version == "gpt-5-mini-2026-05-01"
        assert proposal.composer_provider == "openai"
        assert proposal.composer_skill_hash == fake_sha256("composer-skill")
        assert proposal.tool_arguments_hash == fake_sha256("tool-arguments")

    @pytest.mark.asyncio
    async def test_create_composition_proposal_rejects_blank_composer_provenance(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
    ) -> None:
        """Blank proposal provenance fails before a partial audit row can persist."""
        from elspeth.contracts.errors import AuditIntegrityError

        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)

        with pytest.raises(AuditIntegrityError, match=r"composer provenance.*composer_provider"):
            await sessions_service.create_composition_proposal(
                session_id=session_uuid,
                tool_call_id="call_set_pipeline",
                tool_name="set_pipeline",
                summary="Replace the pipeline.",
                rationale="Composer proposed a pipeline update.",
                affects=["graph"],
                arguments_json={"source": {"plugin": "csv", "options": {}}},
                arguments_redacted_json={"source": {"plugin": "csv", "options": {}}},
                base_state_id=None,
                actor="assistant",
                composer_model_identifier="openai/gpt-5-mini",
                composer_model_version="gpt-5-mini-2026-05-01",
                composer_provider="\t ",
                composer_skill_hash=fake_sha256("composer-skill"),
                tool_arguments_hash=fake_sha256("tool-arguments"),
            )

        assert await sessions_service.list_composition_proposals(session_uuid) == []

    @pytest.mark.asyncio
    async def test_explicit_approve_does_not_intercept_create_blob(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
        fake_llm_create_blob_then_set_pipeline: Any,
    ) -> None:
        """Regression for staging session 986fabf6-a723-4eb3-84de-2db1b7ae4e96:
        under trust_mode=explicit_approve the previous behaviour intercepted
        both create_blob and set_pipeline as proposals. The create_blob
        proposal was structurally unacceptable — the accept endpoint
        requires CompositionState.version to advance, but create_blob
        never advances state (it is a blob-store side effect). So users
        clicked 'Accept' on the proposal and got HTTP 409 'did not change
        composition state'.

        After the fix, create_blob executes immediately; only
        composition-mutation tools (set_pipeline here) become pending
        proposals."""
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)
        state = _empty_state()

        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )

        await composer_service_with_real_sessions._run_one_turn_for_test(
            llm=fake_llm_create_blob_then_set_pipeline,
            session_id=result_session_id,
            initial_state=state,
        )

        proposals = await sessions_service.list_composition_proposals(session_uuid)
        proposal_tools = sorted(p.tool_name for p in proposals)
        # create_blob is NOT intercepted — it executes immediately and the
        # set_pipeline is independently prevalidated before proposal creation.
        assert "create_blob" not in proposal_tools
        # set_pipeline IS still intercepted — it advances composition state
        # and represents the meaningful operator approval.
        assert "set_pipeline" in proposal_tools

    @pytest.mark.asyncio
    async def test_explicit_approve_intercepts_update_blob_without_mutating_blob(
        self,
        tmp_path: Path,
    ) -> None:
        """update_blob is a destructive blob-store mutation and must require approval."""
        engine, session_id = _session_engine_with_session()
        sessions_service = _test_sessions_service(engine, tmp_path)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(data_dir=tmp_path),
            sessions_service=sessions_service,
            session_engine=engine,
        )
        session_uuid = UUID(session_id)
        blob_id = _create_session_blob_for_test(
            engine=engine,
            session_id=session_id,
            data_dir=tmp_path,
            content="original content",
        )
        with engine.connect() as conn:
            blob_before = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )
        turn = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_update_blob",
                    "name": "update_blob",
                    "arguments": {"blob_id": blob_id, "content": "mutated content"},
                }
            ],
        )
        done = _make_llm_response(content="Update is pending approval.")
        responses = [turn, done]

        async def _llm(
            _messages: Any,
            _tools: Any,
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            return responses.pop(0)

        await service._run_one_turn_for_test(
            llm=_llm,
            session_id=session_id,
            initial_state=_empty_state(),
        )

        proposals = await sessions_service.list_composition_proposals(session_uuid)
        assert [proposal.tool_name for proposal in proposals] == ["update_blob"]
        assert proposals[0].status == "pending"
        assert proposals[0].affects == ("blob_store",)
        assert proposals[0].summary.startswith("Overwrite session file")
        with engine.connect() as conn:
            assert conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one() == blob_before
        assert _blob_content_for_test(engine, blob_id) == "original content"

    @pytest.mark.asyncio
    async def test_explicit_approve_intercepts_delete_blob_without_deleting_blob(
        self,
        tmp_path: Path,
    ) -> None:
        """delete_blob is a destructive blob-store mutation and must require approval."""
        engine, session_id = _session_engine_with_session()
        sessions_service = _test_sessions_service(engine, tmp_path)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=_mock_catalog(),
            settings=_make_settings(data_dir=tmp_path),
            sessions_service=sessions_service,
            session_engine=engine,
        )
        session_uuid = UUID(session_id)
        blob_id = _create_session_blob_for_test(
            engine=engine,
            session_id=session_id,
            data_dir=tmp_path,
            content="original content",
        )
        with engine.connect() as conn:
            blob_before = conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one()
        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )
        turn = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_delete_blob",
                    "name": "delete_blob",
                    "arguments": {"blob_id": blob_id},
                }
            ],
        )
        done = _make_llm_response(content="Delete is pending approval.")
        responses = [turn, done]

        async def _llm(
            _messages: Any,
            _tools: Any,
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            return responses.pop(0)

        await service._run_one_turn_for_test(
            llm=_llm,
            session_id=session_id,
            initial_state=_empty_state(),
        )

        proposals = await sessions_service.list_composition_proposals(session_uuid)
        assert [proposal.tool_name for proposal in proposals] == ["delete_blob"]
        assert proposals[0].status == "pending"
        assert proposals[0].affects == ("blob_store",)
        assert proposals[0].summary.startswith("Delete session file")
        with engine.connect() as conn:
            assert conn.execute(select(blobs_table).where(blobs_table.c.id == blob_id)).one() == blob_before
        assert _blob_content_for_test(engine, blob_id) == "original content"

    @pytest.mark.asyncio
    async def test_explicit_approve_invalid_arguments_do_not_crash_compose_loop(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
        fake_llm_set_pipeline_with_misplaced_schema: Any,
    ) -> None:
        """Regression for staging session 100dc5cb-fd66-400b-8041-a1c165cbd8bd:
        under trust_mode=explicit_approve the proposal-interception path
        called redact_tool_call_arguments() with no exception handler. When
        the LLM produced structurally-invalid arguments (schema at the node
        body level instead of inside options), the Pydantic ValidationError
        propagated up, crashed the compose request with HTTP 500, and the
        frontend rendered a bare 'retry' button with no diagnostic.

        After the fix, the redaction failure is caught, the proposal block
        is skipped, and the normal dispatch path produces a clean
        ToolArgumentError that the compose loop surfaces to the model as
        a tool message — letting the model self-correct rather than
        crashing the request."""
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)
        state = _empty_state()

        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="explicit_approve",
            density_default="high",
            actor="user:alice",
        )

        # Must not raise — the previous behavior raised a 500 here.
        result = await composer_service_with_real_sessions._run_one_turn_for_test(
            llm=fake_llm_set_pipeline_with_misplaced_schema,
            session_id=result_session_id,
            initial_state=state,
        )

        # No proposal was created — the LLM arguments were invalid.
        proposals = await sessions_service.list_composition_proposals(session_uuid)
        assert proposals == []
        # The set_pipeline outcome surfaces as a tool argument failure that
        # the loop carries into the model's next turn for self-correction.
        outcome_tool_names = [o.call.function.name for o in result.tool_outcomes]
        assert "set_pipeline" in outcome_tool_names
        invalid_outcome = next(o for o in result.tool_outcomes if o.call.function.name == "set_pipeline")
        assert invalid_outcome.error_class is not None
        # State did not advance — invalid arguments are not committed.
        assert invalid_outcome.post_version == state.version

    @pytest.mark.asyncio
    async def test_auto_commit_mutating_tool_preserves_existing_state_mutation_path(
        self,
        composer_service_with_real_sessions: ComposerServiceImpl,
        result_session_id: str,
        fake_llm_one_set_pipeline_tool_call: Any,
    ) -> None:
        """Auto-commit mode still executes mutating tools through the existing path."""
        sessions_service = composer_service_with_real_sessions._sessions_service
        assert sessions_service is not None
        session_uuid = UUID(result_session_id)
        state = _empty_state()

        await sessions_service.update_composer_preferences(
            session_uuid,
            trust_mode="auto_commit",
            density_default="high",
            actor="user:alice",
        )

        result = await composer_service_with_real_sessions._run_one_turn_for_test(
            llm=fake_llm_one_set_pipeline_tool_call,
            session_id=result_session_id,
            initial_state=state,
        )

        proposals = await sessions_service.list_composition_proposals(session_uuid)
        assert proposals == []
        assert result.tool_outcomes[0].post_version > state.version

    @pytest.mark.asyncio
    async def test_single_tool_call_then_text(self) -> None:
        """LLM makes one tool call, then responds with text."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        # Turn 1: tool call to set_source
        tool_response = _make_llm_response(
            content=None,
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "t1",
                        "options": {
                            "path": f"/data/blobs/{session_id}/data.csv",
                            "schema": {"mode": "observed"},
                        },
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        # Turn 2: text response
        text_response = _make_llm_response(content="I've set up a CSV source.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [tool_response, text_response]
            result = await service.compose("Use CSV as source", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "I've set up a CSV source.", count=1, changed=True)
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].plugin == "csv"
        assert result.state.version == 2

    @pytest.mark.asyncio
    async def test_progress_reports_visible_boundaries_without_tool_payloads(self) -> None:
        """Progress summaries derive from visible lifecycle/tool names, not raw args."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        progress_events: list[ComposerProgressEvent] = []

        async def record_progress(event: ComposerProgressEvent) -> None:
            progress_events.append(event)

        tool_response = _make_llm_response(
            content=None,
            tool_calls=[
                {
                    "id": "call_secret",
                    "name": "validate_secret_ref",
                    "arguments": {"name": "OPENROUTER_TOP_SECRET_VALUE"},
                }
            ],
        )
        text_response = _make_llm_response(content="I checked the available credentials.")

        async def inline_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch("elspeth.web.composer.service.asyncio.to_thread", side_effect=inline_to_thread),
        ):
            mock_llm.side_effect = [tool_response, text_response]
            result = await service.compose("Use my OpenRouter secret", [], state, progress=record_progress, session_id=session_id)

        _assert_checkpoint_reply(result, "I checked the available credentials.", count=0)
        phases = [event.phase for event in progress_events]
        assert phases[:2] == ["starting", "calling_model"]
        assert "using_tools" in phases
        assert "complete" in phases

        progress_text = "\n".join(line for event in progress_events for line in (event.headline, event.likely_next or "", *event.evidence))
        assert "OPENROUTER_TOP_SECRET_VALUE" not in progress_text
        assert '"name"' not in progress_text
        assert "checking available secret references" in progress_text.lower()

    @pytest.mark.asyncio
    async def test_inline_complete_pipeline_incremental_replay_uses_atomic_tool_shape(self, tmp_path: Path) -> None:
        """Incremental inline full replacement retains the atomic tool shape."""
        from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
        from elspeth.web.composer.turn_effects import authored_state_hash

        catalog = _mock_catalog()
        engine, session_id = _session_engine_with_session()
        settings = _make_settings(data_dir=tmp_path)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(engine, tmp_path),
            session_engine=engine,
        )

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="existing_rows",
                options={"path": str(tmp_path / "existing.csv"), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(name="Existing pipeline"),
            version=1,
        )
        user_message_content = "I want a pipeline that takes the string 'hello' and appends ' world' to it."
        user_message_id = _insert_user_message(engine, session_id, user_message_content)
        output_path = tmp_path / "outputs" / session_id / "append.csv"
        pipeline_args = {
            "source": {
                "plugin": "text",
                "on_success": "source_out",
                "options": {
                    "column": "text",
                    "schema": {"mode": "flexible", "fields": ["text: str"], "guaranteed_fields": ["text"]},
                },
                "inline_blob": {
                    "filename": "input.txt",
                    "mime_type": "text/plain",
                    "content": "hello",
                },
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "append_world",
                    "node_type": "transform",
                    "plugin": "value_transform",
                    "input": "source_out",
                    "on_success": "main",
                    "on_error": "discard",
                    "options": {
                        "schema": {
                            "mode": "observed",
                            "guaranteed_fields": ["text"],
                            "required_fields": ["text"],
                        },
                        "operations": [{"target": "text", "expression": "row['text'] + ' world'"}],
                    },
                }
            ],
            "edges": [
                {"id": "source_to_append", "from_node": "source", "to_node": "append_world", "edge_type": "on_success"},
                {"id": "append_to_main", "from_node": "append_world", "to_node": "main", "edge_type": "on_success"},
            ],
            "outputs": [
                {
                    "sink_name": "main",
                    "plugin": "csv",
                    "options": {
                        "path": str(output_path),
                        "schema": {"mode": "observed", "required_fields": ["text"]},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {"name": "Append literal text"},
        }
        build_turn = _make_raw_llm_response(
            tool_calls=[
                {
                    "id": "call_build",
                    "name": "set_pipeline",
                    "arguments": pipeline_args,
                }
            ],
        )
        preview_turn = _make_raw_llm_response(tool_calls=[{"id": "call_preview", "name": "preview_pipeline", "arguments": {}}])
        final_turn = _make_raw_llm_response(content="Pipeline configured.")
        primary_requests = []
        reviews = []

        async def completion(**kwargs: Any):
            if kwargs["model"] == service._model:
                primary_requests.append(deepcopy(kwargs))
                assert len(primary_requests) <= 3, "Only the original build/preview/final primary responses are supplied"
                return (build_turn, preview_turn, final_turn)[len(primary_requests) - 1]
            assert kwargs["model"] == settings.composer_advisor_model, "No response is supplied for another role"
            assert not reviews, "Only one independent END SDK response is supplied"
            review_json = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(review_json.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            reviews.append(review)
            assert review["candidate_reply"] == "Pipeline configured."
            assert review["current_request"] == user_message_content
            assert review["saved_effects"]["saved_revision_count"] == 1
            assert review["saved_effects"]["net_changed"] is True
            return _make_raw_llm_response(
                content=json.dumps(
                    {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
                )
            )

        with (
            patch("litellm.acompletion", side_effect=completion) as mock_llm,
            patch.object(
                service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                new=_REAL_RUN_ADVISOR_CHECKPOINT.__get__(service._advisor_checkpoint, AdvisorCheckpointOwner),
            ),
            patch.object(
                service._preflight,
                "cached_runtime_preflight",
                new_callable=AsyncMock,
                return_value=ValidationResult(is_valid=True, checks=[], errors=[]),
            ),
        ):
            result = await service.compose(
                user_message_content,
                [],
                state,
                session_id=session_id,
                user_message_id=user_message_id,
            )

        _assert_checkpoint_reply(result, "Pipeline configured.", count=1, changed=True)
        assert len(primary_requests) == 3 and len(reviews) == 1
        assert mock_llm.call_count == 4
        assert reviews[0]["current_graph_hash"] == authored_state_hash(result.state)
        tool_names = [inv.tool_name for inv in result.tool_invocations]
        assert tool_names == ["set_pipeline", "preview_pipeline"]
        assert "create_blob" not in tool_names
        assert "set_source_from_blob" not in tool_names
        assert "upsert_node" not in tool_names
        assert "set_output" not in tool_names
        assert len(result.llm_calls) == 4
        assert result.state.sources["source"] is not None
        assert "blob_ref" in result.state.sources["source"].options
        stored = await service._require_sessions_service().get_messages(UUID(session_id), limit=None)
        passes = [
            row.tool_calls[0]["pass"]
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
        assert len(passes) == 1
        assert passes[0]["phase"] == "end" and passes[0]["source"] == "model"
        assert passes[0]["publication_verdict"] == "ADMIT" and passes[0]["first_attempt_accepted"] is True
        assert passes[0]["provider_attempts"] == 1
        assert type(passes[0]["publication_binding"]) is str and len(passes[0]["publication_binding"]) == 64


class TestComposerMultiTurnToolCalls:
    @pytest.mark.asyncio
    async def test_multi_turn_state_accumulates(self) -> None:
        """Multiple tool calls across turns — state accumulates."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        # Turn 1: set_source
        turn1 = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "t1",
                        "options": {
                            "path": f"/data/blobs/{session_id}/input.csv",
                            "schema": {"mode": "observed"},
                        },
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        # Turn 2: set_metadata
        turn2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "My Pipeline"}},
                }
            ],
        )
        # Turn 3: text
        turn3 = _make_llm_response(content="Pipeline configured.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [turn1, turn2, turn3]
            result = await service.compose("Build a pipeline", [], state, session_id=session_id)

        assert result.state.sources["source"] is not None
        assert result.state.metadata.name == "My Pipeline"
        assert result.state.version == 3  # two mutations

    @pytest.mark.asyncio
    async def test_flagged_to_clean_repair_cycle_elides_advisor_exchange_before_finalize_call(self) -> None:
        """R2-F12 Step 3 (elspeth-bff8fe6864, belt-and-braces): once a repair
        tool call has landed following a FLAGGED END advisor pass, the
        injected advisor-findings message must not still be in context for
        the LLM call that produces the eventual CLEAN finalize reply —
        otherwise that reply can anchor on (quote/rebut) text the real user
        never saw. The repair turn's own tool-call/tool-result messages
        (which carry the real state change) must NOT be elided."""
        from elspeth.web.composer.turn_effects import AdvisorPublicationReview

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        # Turn 1: no tool calls -> END advisor gate runs, FLAGGED.
        turn1 = _make_llm_response(content="Looks ready to me.")
        # Turn 2: a genuine repair tool call.
        turn2 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}],
        )
        # Turn 3: no tool calls again -> END advisor gate re-runs, CLEAN -> finalize.
        turn3 = _make_llm_response(content="The pipeline name is now Repaired.")
        reviews: list[AdvisorPublicationReview] = []

        async def _fake_advisor_checkpoint(*_args: object, **kwargs: Any) -> AdvisorCheckpointVerdict:
            assert kwargs["phase"] == "end"
            review = kwargs["publication_review"]
            assert type(review) is AdvisorPublicationReview
            reviews.append(review)
            assert len(reviews) <= 2, "No additional review allowance is supplied by this fixture"
            flagged = len(reviews) == 1
            return AdvisorCheckpointVerdict(
                ok=True,
                blocking=flagged,
                findings_text="FLAGGED: sink omits rating" if flagged else "CLEAN",
                publication_verdict="WITHHOLD" if flagged else "ADMIT",
                publication_binding=review.binding,
            )

        responses = iter([turn1, turn2, turn3])
        captured_messages: list[list[dict[str, Any]]] = []

        async def _fake_call_llm(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            captured_messages.append(list(messages))
            return next(responses)

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", autospec=True, side_effect=_fake_advisor_checkpoint),
        ):
            mock_llm.side_effect = _fake_call_llm
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert len(reviews) == 2 and reviews[0].binding != reviews[1].binding
        assert result.state.metadata.name == "Repaired"
        _assert_checkpoint_reply(result, "The pipeline name is now Repaired.", count=1, changed=True)
        assert result.raw_assistant_content == "The pipeline name is now Repaired."
        assert len(captured_messages) == 3
        # Turn 2 (the repair call) saw the injected advisor findings.
        assert any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[1])
        # Turn 3 (the finalize call) must NOT — the elision boundary. The
        # repair's own tool-call/tool-result messages must survive.
        assert not any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[2])
        assert any(m.get("role") == "tool" for m in captured_messages[2])
        # The repair call answered an exchange the model could see in full:
        # its own superseded reply, then the advisor injection.
        superseded_reply = {"role": "assistant", "content": "Looks ready to me."}
        assert captured_messages[1][-2] == superseded_reply
        assert "Completion advisory review" in captured_messages[1][-1]["content"]
        # Surgical removal, not an accidental match on the wrong entry: turn
        # 3's list is turn 2's list with exactly that exchange removed — the
        # advisor message AND the reply it answered, so the repair's assistant
        # tool-call message never directly follows another assistant message —
        # and exactly the repair's two messages (assistant tool-call + tool
        # result) appended — net zero, not some other count.
        assert len(captured_messages[2]) == len(captured_messages[1])
        assert superseded_reply not in captured_messages[2]
        # Every message present in turn 2's context OTHER than that exchange
        # survives verbatim (by identity) into turn 3's context — the drain
        # removed exactly the pair and touched nothing else.
        turn2_minus_exchange = captured_messages[1][:-2]
        assert captured_messages[2][: len(turn2_minus_exchange)] == turn2_minus_exchange
        roles = [m.get("role") for m in captured_messages[2]]
        assert all(pair != ("assistant", "assistant") for pair in itertools.pairwise(roles))

    @pytest.mark.asyncio
    async def test_flagged_repair_clean_replaces_every_public_and_persisted_echo_surface(self) -> None:
        """Advisor findings are internal even when the primary model parrots them.

        This is the real compose-loop/persistence regression for D3: pass one
        FLAGs, the primary emits an interleaved repair tool turn containing the
        hidden findings, pass two returns CLEAN, and the primary's terminal
        prose parrots the same hidden material again.  Tool calls and state
        still land, but no human/transcript field may retain model-authored
        prose produced from the advisor repair context.
        """
        from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        from elspeth.web.composer.advisor_context import (
            _ADVISOR_UNTRUSTED_PRIOR_FINDINGS_BEGIN,
            _ADVISOR_UNTRUSTED_PRIOR_FINDINGS_END,
            _ADVISOR_UNTRUSTED_SUMMARY_BEGIN,
            _ADVISOR_UNTRUSTED_SUMMARY_END,
        )
        from elspeth.web.composer.advisor_policy import (
            _ADVISOR_FINDINGS_UNTRUSTED_BEGIN,
            _ADVISOR_FINDINGS_UNTRUSTED_END,
        )
        from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
        from elspeth.web.sessions.routes._helpers import _composer_conversation_or_tool_messages

        service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
        service._advisor_checkpoint._run_advisor_checkpoint = _REAL_RUN_ADVISOR_CHECKPOINT.__get__(
            service._advisor_checkpoint, AdvisorCheckpointOwner
        )  # type: ignore[method-assign]
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        false_claim = "the source schema is missing and text_field, page_count_field, and on_error are not wired"
        foreign_repair = "Repair: {transform: numeric_route, options: {mode: gate}}"
        sentinels = (
            _ADVISOR_FINDINGS_UNTRUSTED_BEGIN,
            _ADVISOR_FINDINGS_UNTRUSTED_END,
            _ADVISOR_UNTRUSTED_PRIOR_FINDINGS_BEGIN,
            _ADVISOR_UNTRUSTED_PRIOR_FINDINGS_END,
            _ADVISOR_UNTRUSTED_SUMMARY_BEGIN,
            _ADVISOR_UNTRUSTED_SUMMARY_END,
        )
        findings = "\n".join((f"FLAGGED: {false_claim}", foreign_repair, *sentinels))
        echoed_primary_prose = f"The advisor says {false_claim}. {foreign_repair} {' '.join(sentinels)}"
        responses = [
            _make_llm_response(content="The pipeline is ready."),
            _make_llm_response(
                content=echoed_primary_prose,
                tool_calls=[{"id": "repair-1", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}],
            ),
            _make_llm_response(content=echoed_primary_prose),
        ]
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        reviews: list[dict[str, Any]] = []

        async def review_echo(
            arguments: dict[str, Any], *, on_provider_dispatch: Callable[[], None] | None = None, **_kwargs: Any
        ) -> tuple[str, dict[str, object]]:
            review = json.loads(arguments["publication_review"].payload_json)
            reviews.append(review)
            assert len(reviews) <= 2, "Only two END responses are supplied"
            assert review["current_request"] == "Review this pipeline"
            assert review["candidate_reply"] == ("The pipeline is ready.", echoed_primary_prose)[len(reviews) - 1]
            assert review["saved_effects"]["saved_revision_count"] == len(reviews) - 1
            assert review["saved_effects"]["net_changed"] is (len(reviews) == 2)
            if on_provider_dispatch is not None:
                on_provider_dispatch()
            flagged = len(reviews) == 1
            return json.dumps(
                {
                    "verdict": "FLAGGED" if flagged else "CLEAN",
                    "category": "other",
                    "steps": [],
                    "findings": findings if flagged else "Candidate repeats unsupported advisor findings and internal sentinel markers.",
                    "note": "Review the configured source options." if flagged else None,
                    # A CLEAN graph judgment cannot authorize false quoted
                    # findings and internal sentinels in the candidate reply.
                    "publication_verdict": "WITHHOLD",
                }
            ), {}

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as primary_call,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(
                service._advisor_checkpoint,
                "_call_advisor_with_audit",
                new_callable=AsyncMock,
                side_effect=review_echo,
            ) as advisor_call,
        ):
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        engine = service._session_engine
        assert engine is not None
        with engine.connect() as conn:
            persisted_assistant_rows = (
                conn.execute(
                    select(chat_messages_table).where(
                        chat_messages_table.c.session_id == session_id,
                        chat_messages_table.c.role == "assistant",
                        chat_messages_table.c.writer_principal == "compose_loop",
                    )
                )
                .mappings()
                .all()
            )

        assert primary_call.await_count == 3 and advisor_call.await_count == len(reviews) == 2
        assert reviews[0]["current_graph_hash"] != reviews[1]["current_graph_hash"]
        saved_scope = "Verified checkpoint evidence covers 1 saved draft revisions. There are net authored changes from the starting draft."
        assert result.message.startswith(saved_scope)
        assert result.raw_assistant_content == saved_scope
        assert (
            len(
                [
                    segment
                    for segment in _no_tool_policy_module.visible_message_segments(content=result.message, raw_content=saved_scope)
                    if isinstance(segment, _no_tool_policy_module.TrustedSystemNoticeSegment)
                    and segment.content.startswith("Checkpoint save status: Verified checkpoint evidence covers 1 saved draft revisions.")
                ]
            )
            == 1
        )
        assert result.state.metadata.name == "Repaired"
        assert len(persisted_assistant_rows) == 1
        assert persisted_assistant_rows[0]["content"] == "ELSPETH is applying a pipeline correction."
        assert persisted_assistant_rows[0]["raw_content"] is None
        assert persisted_assistant_rows[0]["tool_calls"] is not None
        sessions = service._require_sessions_service()
        stored = await sessions.get_messages(UUID(session_id), limit=None)
        withheld = [
            (row.tool_calls[0]["origin"], row.content)
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == COMPOSER_WITHHELD_REPLY_KIND
        ]
        assert withheld == [
            ("repair_gate_superseded", "The pipeline is ready."),
            ("advisor_repair_tool_turn", echoed_primary_prose),
            ("advisor_publication_withheld", echoed_primary_prose),
        ]
        passes = [
            row.tool_calls[0]["pass"]
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
        assert [(item["verdict"], item["publication_verdict"], item["provider_attempts"]) for item in passes] == [
            ("flagged", "WITHHOLD", 1),
            ("clean", "WITHHOLD", 1),
        ]
        assert all(item["first_attempt_accepted"] is True for item in passes)
        assert all(type(item["publication_binding"]) is str and len(item["publication_binding"]) == 64 for item in passes)
        assert passes[0]["publication_binding"] != passes[1]["publication_binding"]
        public_rows = _composer_conversation_or_tool_messages(
            await sessions.get_messages(UUID(session_id), limit=None, include_private_evidence=False)
        )

        runtime_preflight = result.runtime_preflight
        assert runtime_preflight is not None
        public_surfaces = [
            result.message,
            result.raw_assistant_content or "",
            *(row["content"] for row in persisted_assistant_rows),
            *((row["raw_content"] or "") for row in persisted_assistant_rows),
            *(row.content or "" for row in public_rows),
            *(row.raw_content or "" for row in public_rows),
            *(check.detail for check in runtime_preflight.checks),
            *(error.message for error in runtime_preflight.errors),
            *((error.suggestion or "") for error in runtime_preflight.errors),
            *(blocker.detail for blocker in runtime_preflight.readiness.blockers),
        ]
        for surface in public_surfaces:
            assert false_claim not in surface
            assert foreign_repair not in surface
            assert "Repair:" not in surface
            for sentinel in sentinels:
                assert sentinel not in surface

    @pytest.mark.asyncio
    async def test_advisor_rereview_carries_prior_finding_mutation_and_current_evidence(self) -> None:
        from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND

        catalog = _mock_catalog()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=_make_settings())
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        llm_responses = [
            _make_llm_response(content="Looks ready."),
            _make_llm_response(tool_calls=[{"id": "repair-1", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}]),
            _make_llm_response(content="The pipeline name is now Repaired."),
        ]
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        prior_finding = "FLAGGED: sink omits rating"
        service._advisor_checkpoint._run_advisor_checkpoint = _REAL_RUN_ADVISOR_CHECKPOINT.__get__(
            service._advisor_checkpoint, AdvisorCheckpointOwner
        )  # type: ignore[method-assign]
        reviews = []

        async def rereview(
            arguments: dict[str, Any], *, on_provider_dispatch: Callable[[], None] | None = None, **_kwargs: Any
        ) -> tuple[str, dict[str, object]]:
            review = json.loads(arguments["publication_review"].payload_json)
            reviews.append(review)
            assert len(reviews) <= 2, "Only the original two review responses are supplied"
            assert review["current_request"] == "Review this pipeline"
            assert review["candidate_reply"] == ("Looks ready.", "The pipeline name is now Repaired.")[len(reviews) - 1]
            assert review["saved_effects"]["saved_revision_count"] == len(reviews) - 1
            assert review["saved_effects"]["net_changed"] is (len(reviews) == 2)
            if on_provider_dispatch is not None:
                on_provider_dispatch()
            flagged = len(reviews) == 1
            return json.dumps(
                {
                    "verdict": "FLAGGED" if flagged else "CLEAN",
                    "category": "other",
                    "steps": [],
                    "findings": prior_finding if flagged else "",
                    "note": "Add the rating field to the sink output." if flagged else None,
                    "publication_verdict": "WITHHOLD" if flagged else "ADMIT",
                }
            ), {}

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=llm_responses) as primary_call,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(
                service._advisor_checkpoint,
                "_call_advisor_with_audit",
                new_callable=AsyncMock,
                side_effect=rereview,
            ) as advisor_call,
        ):
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert result.runtime_preflight is None or result.runtime_preflight.is_valid
        assert primary_call.await_count == 3 and advisor_call.await_count == len(reviews) == 2
        assert result.state.metadata.name == "Repaired"
        _assert_checkpoint_reply(result, "The pipeline name is now Repaired.", count=1, changed=True)
        assert reviews[0]["current_graph_hash"] != reviews[1]["current_graph_hash"]
        first_arguments = advisor_call.await_args_list[0].args[0]
        second_arguments = advisor_call.await_args_list[1].args[0]
        assert first_arguments["recent_errors"] == []
        assert first_arguments["attempted_actions"] == []
        assert any(prior_finding in item for item in second_arguments["recent_errors"])
        assert any("set_metadata" in item for item in second_arguments["attempted_actions"])
        assert "review pass 2" in second_arguments["problem_summary"].lower()
        assert "assess the current evidence independently" in second_arguments["problem_summary"].lower()
        assert "Pipeline: Repaired" in second_arguments["schema_excerpt"]
        stored = await service._require_sessions_service().get_messages(UUID(session_id), limit=None)
        passes = [
            row.tool_calls[0]["pass"]
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
        assert [(item["verdict"], item["publication_verdict"], item["provider_attempts"]) for item in passes] == [
            ("flagged", "WITHHOLD", 1),
            ("clean", "ADMIT", 1),
        ]
        assert all(item["first_attempt_accepted"] is True for item in passes)
        assert all(type(item["publication_binding"]) is str and len(item["publication_binding"]) == 64 for item in passes)
        assert passes[0]["publication_binding"] != passes[1]["publication_binding"]

    @pytest.mark.asyncio
    async def test_advisor_rereview_without_mutation_is_explicit_not_a_blind_replay(self) -> None:
        from elspeth.web.composer.advisor_decision import AdvisorGateBlocked

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        service._advisor_checkpoint._run_advisor_checkpoint = _REAL_RUN_ADVISOR_CHECKPOINT.__get__(
            service._advisor_checkpoint, AdvisorCheckpointOwner
        )  # type: ignore[method-assign]
        reviewed: list[dict[str, Any]] = []

        async def rereview(
            arguments: dict[str, Any], *, on_provider_dispatch: Callable[[], None] | None = None, **_kwargs: Any
        ) -> tuple[str, dict[str, object]]:
            review = json.loads(arguments["publication_review"].payload_json)
            reviewed.append(review)
            assert len(reviewed) <= 2, "Only two END responses are supplied"
            assert review["current_request"] == "Review this pipeline"
            assert review["candidate_reply"] == ("Looks ready.", "The pipeline has no configured output.")[len(reviewed) - 1]
            assert review["saved_effects"]["saved_revision_count"] == 0
            assert review["saved_effects"]["net_changed"] is False
            if on_provider_dispatch is not None:
                on_provider_dispatch()
            return json.dumps(
                {
                    "verdict": "FLAGGED",
                    "category": "other",
                    "steps": [],
                    "findings": "FLAGGED: missing output",
                    "note": "Configure a pipeline output.",
                    "publication_verdict": "WITHHOLD",
                }
            ), {}

        with (
            patch.object(
                service._provider_gateway,
                "_call_llm",
                new_callable=AsyncMock,
                side_effect=[
                    _make_llm_response(content="Looks ready."),
                    _make_llm_response(content="The pipeline has no configured output."),
                ],
            ) as primary_call,
            patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
            patch.object(
                service._advisor_checkpoint,
                "_call_advisor_with_audit",
                new_callable=AsyncMock,
                side_effect=rereview,
            ) as advisor_call,
        ):
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert primary_call.await_count == advisor_call.await_count == len(reviewed) == 2
        assert reviewed[0]["current_graph_hash"] == reviewed[1]["current_graph_hash"]
        assert result.state == state
        assert result.tool_invocations == ()
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert "Looks ready." not in result.message
        assert "The pipeline has no configured output." not in result.message
        second_arguments = advisor_call.await_args_list[1].args[0]
        assert any("FLAGGED: missing output" in item for item in second_arguments["recent_errors"])
        assert second_arguments["attempted_actions"] == ["No successful pipeline mutation occurred since the prior advisor pass."]
        assert "current evidence identity" in second_arguments["problem_summary"].lower()
        assert "prior semantic repair evidence identity" in second_arguments["problem_summary"].lower()

    @staticmethod
    def _wired_source_only_state() -> CompositionState:
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    @pytest.mark.asyncio
    async def test_superseded_no_tool_reply_stays_in_model_context_for_the_repair_turn(self) -> None:
        """A no-tool reply answered by a repair injection must remain in the
        provider context. Without it the next call shows a user-role repair
        message answering an assistant turn the model cannot see."""
        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        replies = [_make_llm_response(content="Looks ready."), _make_llm_response(content="Still ready.")]
        provider_contexts: list[list[dict[str, Any]]] = []

        async def scripted_call_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            provider_contexts.append([dict(item) for item in messages])
            return replies[len(provider_contexts) - 1]

        with (
            patch.object(service._provider_gateway, "_call_llm", side_effect=scripted_call_llm),
            patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
            patch.object(
                service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                new_callable=AsyncMock,
                side_effect=[
                    AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: missing output"),
                    AdvisorCheckpointVerdict(ok=True, blocking=False, findings_text="CLEAN"),
                ],
            ),
        ):
            await service.compose("Review this pipeline", [], self._wired_source_only_state(), session_id=session_id)

        assert len(provider_contexts) == 2
        repair_context = provider_contexts[1]
        assert repair_context[-1]["role"] == "user"
        assert "[Completion advisory review — BLOCKING." in repair_context[-1]["content"]
        assert repair_context[-2] == {"role": "assistant", "content": "Looks ready."}

    @pytest.mark.asyncio
    async def test_superseded_no_tool_reply_is_recoverable_from_a_withheld_reply_audit_row(self) -> None:
        """The reply a repair gate supersedes is never published, so it must be
        kept as a non-rendered audit row: ``ComposerLLMCall`` stores no response
        text, and without this row the model's words exist nowhere."""
        from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
        from elspeth.web.sessions.routes._helpers import _composer_chat_history, _composer_conversation_messages

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())

        with (
            patch.object(
                service._provider_gateway,
                "_call_llm",
                new_callable=AsyncMock,
                side_effect=[_make_llm_response(content="Looks ready."), _make_llm_response(content="Still ready.")],
            ),
            patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
            patch.object(
                service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                new_callable=AsyncMock,
                side_effect=[
                    AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: missing output"),
                    AdvisorCheckpointVerdict(ok=True, blocking=False, findings_text="CLEAN"),
                ],
            ),
        ):
            await service.compose("Review this pipeline", [], self._wired_source_only_state(), session_id=session_id)

        assert service._sessions_service is not None
        stored = await service._sessions_service.get_messages(UUID(session_id))
        withheld = [
            row for row in stored if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == COMPOSER_WITHHELD_REPLY_KIND
        ]
        superseded = [row for row in withheld if row.tool_calls[0]["origin"] == "repair_gate_superseded"]
        assert [row.content for row in superseded] == ["Looks ready."]
        # Recoverable is not rendered and not replayed: the row stays out of the
        # chat view and out of later provider history.
        assert withheld[0] not in _composer_conversation_messages(stored)
        assert all(item["content"] != "Looks ready." for item in _composer_chat_history(stored))

    @pytest.mark.asyncio
    async def test_advisor_outage_on_the_first_pass_does_not_delete_the_models_reply(self) -> None:
        """An unreviewed reply survives privately while public copy states saved facts."""
        from hashlib import sha256

        from elspeth.web.composer.advisor_audit import ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND
        from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked
        from elspeth.web.composer.turn_effects import AdvisorPublicationReview
        from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND, COMPOSER_WITHHELD_REPLY_SCHEMA
        from elspeth.web.sessions.routes._helpers import _composer_chat_history, _composer_conversation_or_tool_messages

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        outage = AdvisorCheckpointVerdict(ok=False, blocking=False, findings_text="advisor unavailable", failure_class="unavailable")
        state = self._wired_source_only_state()

        with (
            patch.object(
                service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=[_make_llm_response(content="Looks ready.")]
            ) as primary_call,
            patch.object(
                service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])
            ) as preflight,
            patch.object(
                service._advisor_checkpoint, "_run_advisor_checkpoint", new_callable=AsyncMock, return_value=outage
            ) as advisor_call,
        ):
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert primary_call.await_count == 1
        assert advisor_call.await_count == service._settings.composer_advisor_checkpoint_max_passes == 2
        assert [call.kwargs["pass_index"] for call in advisor_call.await_args_list] == [1, 2]
        reviews = [call.kwargs["publication_review"] for call in advisor_call.await_args_list]
        assert type(reviews[0]) is AdvisorPublicationReview and reviews[1] is reviews[0]
        evidence = json.loads(reviews[0].payload_json)
        assert evidence["candidate_reply"] == "Looks ready."
        assert evidence["current_request"] == "Review this pipeline"
        assert evidence["saved_effects"]["saved_revision_count"] == 0
        assert evidence["saved_effects"]["net_changed"] is False
        preflight.assert_not_called()
        assert result.state == state
        assert result.tool_invocations == ()
        saved_scope = (
            "Verified checkpoint evidence covers 0 saved draft revisions. There is no net authored change from the starting draft."
        )
        assert result.raw_assistant_content == saved_scope
        _assert_checkpoint_reply(
            result,
            saved_scope + _no_tool_policy_module._ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX,
            raw=saved_scope,
            count=0,
        )
        assert "Looks ready." not in result.message
        assert _no_tool_policy_module.visible_message_segments(content=result.message, raw_content=result.raw_assistant_content)[:-1] == (
            _no_tool_policy_module.AssistantTextSegment(saved_scope),
            # No mutation this turn, so no preflight ran: the absent-shape twin.
            _no_tool_policy_module.TrustedSystemNoticeSegment(
                _no_tool_policy_module._ADVISOR_SIGNOFF_UNAVAILABLE_UNVERIFIED_PUBLISHED_NOTICE
            ),
        )
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.readiness.completion_ready is False
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert result.advisor_gate_decision.fact.cause is AdvisorBlockCause.UNAVAILABLE
        stored = await service._require_sessions_service().get_messages(UUID(session_id), limit=None)
        withheld = [
            row for row in stored if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == COMPOSER_WITHHELD_REPLY_KIND
        ]
        assert len(withheld) == 1
        assert withheld[0].session_id == UUID(session_id) and withheld[0].writer_principal == "compose_loop"
        assert withheld[0].content == "Looks ready."
        assert len(withheld[0].tool_calls) == 1
        assert withheld[0].tool_calls[0] == {
            "_kind": COMPOSER_WITHHELD_REPLY_KIND,
            "schema": COMPOSER_WITHHELD_REPLY_SCHEMA,
            "origin": "advisor_publication_withheld",
            "content_hash": sha256(b"Looks ready.").hexdigest(),
        }
        public_rows = _composer_conversation_or_tool_messages(stored)
        assert withheld[0] not in public_rows
        assert all(row.content != "Looks ready." and row.raw_content != "Looks ready." for row in public_rows)
        assert all(item["content"] != "Looks ready." for item in _composer_chat_history(stored))
        terminal = [
            row
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND
        ]
        assert len(terminal) == 1
        assert terminal[0].tool_calls[0] == {
            "_kind": ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND,
            "publication": {
                "branch": "terminal_block",
                "reason": "unavailable",
                "preflight_shape": "absent",
                "findings_backend_authored": False,
            },
        }
        assert json.loads(terminal[0].content) == terminal[0].tool_calls[0]

    @pytest.mark.asyncio
    async def test_advisor_cohort_keeps_every_replaced_reply_recoverable(self) -> None:
        """Verified request effects replace completion prose without losing any private reply."""
        from elspeth.web.composer.turn_effects import AdvisorPublicationReview
        from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        responses = [
            _make_llm_response(content="Looks ready to me."),
            _make_llm_response(
                content="Renaming it as the review asked.",
                tool_calls=[{"id": "c1", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}],
            ),
            _make_llm_response(content="The pipeline name is now Repaired."),
        ]
        reviews: list[AdvisorPublicationReview] = []

        async def review_reply(*_args, **kwargs):
            assert kwargs["phase"] == "end"
            review = kwargs["publication_review"]
            assert type(review) is AdvisorPublicationReview
            reviews.append(review)
            assert len(reviews) <= 2, "No additional review allowance is supplied by this fixture"
            flagged = len(reviews) == 1
            return AdvisorCheckpointVerdict(
                ok=True,
                blocking=flagged,
                findings_text="FLAGGED: sink omits rating" if flagged else "CLEAN",
                publication_verdict="WITHHOLD" if flagged else "ADMIT",
                publication_binding=review.binding,
            )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses),
            patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
            patch.object(
                service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                autospec=True,
                side_effect=review_reply,
            ),
        ):
            result = await service.compose("Review this pipeline", [], self._wired_source_only_state(), session_id=session_id)

        assert len(reviews) == 2 and reviews[0].binding != reviews[1].binding
        _assert_checkpoint_reply(result, "The pipeline name is now Repaired.", count=1, changed=True)
        assert result.raw_assistant_content == "The pipeline name is now Repaired."

        assert service._sessions_service is not None
        stored = await service._sessions_service.get_messages(UUID(session_id))
        withheld = [
            (row.tool_calls[0]["origin"], row.content)
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == COMPOSER_WITHHELD_REPLY_KIND
        ]
        assert withheld == [
            ("repair_gate_superseded", "Looks ready to me."),
            ("advisor_repair_tool_turn", "Renaming it as the review asked."),
        ]
        assert all(row.content != "Renaming it as the review asked." for row in stored if row.role == "assistant")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mutate_after_discovery", [True, False], ids=["rename_after_discovery", "discovery_only"])
    async def test_flagged_discovery_only_tool_call_does_not_elide_advisor_message(self, mutate_after_discovery: bool) -> None:
        """Discovery neither saves a revision nor drains a FLAGGED exchange.

        A later metadata rename saves one revision and drains the exchange;
        it does not repair the missing output or authorize completion prose.
        """
        import litellm

        from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND, ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND
        from elspeth.web.composer.advisor_context import _ADVISOR_UNTRUSTED_SUMMARY_BEGIN, _ADVISOR_UNTRUSTED_SUMMARY_END
        from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked
        from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
        from elspeth.web.sessions.models import composition_states_table
        from elspeth.web.sessions.routes._helpers import _composer_chat_history, _composer_conversation_or_tool_messages
        from elspeth.web.sessions.state_envelope import unwrap_state_column

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        turn1 = _make_llm_response(content="Looks ready to me.")
        # Turn 2: discovery only — no mutation. Must NOT drain.
        turn2 = _make_llm_response(tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}])
        # Turn 3: a metadata mutation drains the exchange, but cannot repair
        # the missing output or authorize a completion claim.
        turn3 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}],
        )
        # The final no-tool reply requires a new independent END review.
        turn4 = _make_llm_response(content="Pipeline is ready.")
        replies = [turn1, turn2, *([turn3] if mutate_after_discovery else []), turn4]
        responses = iter(replies)
        captured_messages: list[list[dict[str, Any]]] = []
        sessions = service._require_sessions_service()
        reviews: list[dict[str, Any]] = []
        revision_snapshots: list[tuple[tuple[str, int, dict[str, Any]], ...]] = []

        def saved_revisions() -> tuple[tuple[str, int, dict[str, Any]], ...]:
            with sessions._engine.connect() as conn:
                return tuple(
                    (row.id, row.version, unwrap_state_column(row.metadata_))
                    for row in conn.execute(
                        select(composition_states_table.c.id, composition_states_table.c.version, composition_states_table.c.metadata_)
                        .where(composition_states_table.c.session_id == session_id)
                        .order_by(composition_states_table.c.version)
                    )
                )

        async def _fake_call_llm(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            assert len(captured_messages) < len(replies), "Only the original finite primary responses are supplied"
            captured_messages.append(deepcopy(messages))
            revision_snapshots.append(saved_revisions())
            return next(responses)

        async def review_completion(**kwargs: Any) -> Any:
            assert kwargs["model"] == settings.composer_advisor_model
            assert len(reviews) < 2, "Only the original two independent END responses are supplied"
            assert "tools" not in kwargs and kwargs["num_retries"] == kwargs["max_retries"] == 0
            assert kwargs["response_format"]["json_schema"]["name"] == "advisor_end_response"
            review_json = kwargs["messages"][1]["content"].rsplit(_ADVISOR_UNTRUSTED_SUMMARY_BEGIN + "\n", 1)[1]
            review = json.loads(review_json.split("\n" + _ADVISOR_UNTRUSTED_SUMMARY_END, 1)[0])
            reviews.append(review)
            assert review["current_request"] == "Review this pipeline"
            assert review["candidate_reply"] == ("Looks ready to me.", "Pipeline is ready.")[len(reviews) - 1]
            saved_count = int(mutate_after_discovery and len(reviews) == 2)
            assert review["saved_effects"]["saved_revision_count"] == saved_count == len(saved_revisions())
            assert review["saved_effects"]["net_changed"] is bool(saved_count)
            if saved_count:
                assert review["saved_effects"]["latest_committed_state_id"] == saved_revisions()[0][0]
                # This fixture has no seeded durable state: SQL allocates its
                # first version as 1, independently of the in-memory version.
                assert saved_revisions()[0][1:] == (1, {"name": "Repaired", "description": ""})
            return litellm.ModelResponse(
                model=kwargs["model"],
                choices=[
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "verdict": "FLAGGED",
                                    "category": "request_not_met",
                                    "steps": [],
                                    "findings": "The pipeline has no configured output. The completion claim is unsupported.",
                                    "note": None,
                                    "publication_verdict": "WITHHOLD",
                                }
                            ),
                        },
                    }
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 20},
            )

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight) as preflight,
            patch.object(
                service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                side_effect=_REAL_RUN_ADVISOR_CHECKPOINT.__get__(service._advisor_checkpoint, AdvisorCheckpointOwner),
            ) as checkpoints,
            patch.object(litellm, "acompletion", side_effect=review_completion) as advisor_sdk,
        ):
            mock_llm.side_effect = _fake_call_llm
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        assert len(captured_messages) == mock_llm.await_count == 3 + int(mutate_after_discovery)
        assert checkpoints.await_count == advisor_sdk.await_count == len(reviews) == settings.composer_advisor_checkpoint_max_passes == 2
        bindings = [call.kwargs["publication_review"].binding for call in checkpoints.await_args_list]
        assert all(call.kwargs["phase"] == "end" for call in checkpoints.await_args_list)
        owned_reviews = [json.loads(call.kwargs["publication_review"].payload_json) for call in checkpoints.await_args_list]
        # Provider text is scrubbed without changing owned metadata identities.
        assert [json.loads(call.kwargs["publication_review"].provider_payload_json) for call in checkpoints.await_args_list] == reviews
        assert [review["current_graph_hash"] for review in reviews] == [review["current_graph_hash"] for review in owned_reviews]
        assert (owned_reviews[0]["current_graph_hash"] != owned_reviews[1]["current_graph_hash"]) is mutate_after_discovery
        assert revision_snapshots[:3] == [(), (), ()], "Neither initial review nor list_sources saves a revision"
        assert result.state.outputs == ()
        assert result.state.metadata.name == ("Repaired" if mutate_after_discovery else state.metadata.name)
        assert result.state.version == state.version + int(mutate_after_discovery)
        if mutate_after_discovery:
            assert revision_snapshots[3] == saved_revisions() and len(saved_revisions()) == 1
            preflight.assert_called_once()
        else:
            assert saved_revisions() == () and result.state == state
            preflight.assert_not_called()
        saved_scope = (
            "Verified checkpoint evidence covers 1 saved draft revisions. There are net authored changes from the starting draft."
            if mutate_after_discovery
            else "Verified checkpoint evidence covers 0 saved draft revisions. There is no net authored change from the starting draft."
        )
        suffix = (
            _no_tool_policy_module._ADVISOR_SIGNOFF_PENDING_PUBLISHED_FINALIZE_SUFFIX
            if mutate_after_discovery
            else _no_tool_policy_module._ADVISOR_SIGNOFF_UNVERIFIED_PUBLISHED_FINALIZE_SUFFIX
        )
        _assert_checkpoint_reply(
            result,
            saved_scope + suffix,
            raw=saved_scope,
            count=int(mutate_after_discovery),
            changed=mutate_after_discovery,
        )
        assert result.runtime_preflight is not None and result.runtime_preflight.readiness.completion_ready is False
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert result.advisor_gate_decision.fact.cause is AdvisorBlockCause.GRAPH_REJECTED
        # Turn 2 (discovery-only) still sees it — not drained by a non-mutating turn.
        assert any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[1])
        # The next primary call (metadata mutation or final reply) still sees
        # the finding: discovery cannot consume the injected exchange.
        assert any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[2])
        assert [m for m in captured_messages[1] if "Completion advisory review" in (m.get("content") or "")] == [
            m for m in captured_messages[2] if "Completion advisory review" in (m.get("content") or "")
        ]
        # The final call drains that exchange only after the real rename.
        assert any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[-1]) is not mutate_after_discovery
        assert [inv.tool_name for inv in result.tool_invocations] == ["list_sources", *(["set_metadata"] if mutate_after_discovery else [])]
        assert all(inv.status == "success" for inv in result.tool_invocations)
        assert {m["tool_call_id"] for m in captured_messages[-1] if m["role"] == "tool"} == {
            "c1",
            *(["c2"] if mutate_after_discovery else []),
        }
        assert result.tool_invocations[0].version_before == result.tool_invocations[0].version_after == state.version
        if mutate_after_discovery:
            assert result.tool_invocations[1].version_before == state.version
            assert result.tool_invocations[1].version_after == state.version + 1
        stored = await sessions.get_messages(UUID(session_id), limit=None)
        withheld = [
            row for row in stored if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == COMPOSER_WITHHELD_REPLY_KIND
        ]
        assert [(row.tool_calls[0]["origin"], row.content) for row in withheld] == [
            ("repair_gate_superseded", "Looks ready to me."),
            ("advisor_publication_withheld", "Pipeline is ready."),
        ]
        public_rows = _composer_conversation_or_tool_messages(stored)
        assert all(row not in public_rows for row in withheld)
        for candidate in ("Looks ready to me.", "Pipeline is ready."):
            assert candidate not in result.message
            assert all(row.content != candidate and row.raw_content != candidate for row in public_rows)
            assert all(item["content"] != candidate for item in _composer_chat_history(stored))
        passes = [
            row.tool_calls[0]["pass"]
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
        ]
        assert [(item["verdict"], item["publication_verdict"], item["provider_attempts"]) for item in passes] == [
            ("flagged", "WITHHOLD", 1),
            ("flagged", "WITHHOLD", 1),
        ]
        assert all(item["first_attempt_accepted"] is True for item in passes)
        assert all(type(item["publication_binding"]) is str and len(item["publication_binding"]) == 64 for item in passes)
        assert [item["publication_binding"] for item in passes] == bindings
        assert passes[0]["publication_binding"] != passes[1]["publication_binding"]
        terminal = [
            row.tool_calls[0]["publication"]
            for row in stored
            if row.role == "audit" and row.tool_calls and row.tool_calls[0]["_kind"] == ADVISOR_TERMINAL_PUBLICATION_AUDIT_KIND
        ]
        assert terminal == [
            {
                "branch": "terminal_block",
                "reason": "flagged_final_pass",
                "preflight_shape": "green" if mutate_after_discovery else "absent",
                "findings_backend_authored": False,
            }
        ]

    @pytest.mark.asyncio
    async def test_irrelevant_mutation_does_not_clear_a_fresh_final_flag(self) -> None:
        """A real but irrelevant mutation cannot mint advisor clearance."""
        catalog = _mock_catalog()
        settings = _make_settings()  # composer_advisor_checkpoint_max_passes default 2
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

        turn1 = _make_llm_response(content="Looks ready to me.")
        turn2 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "set_metadata", "arguments": {"patch": {"name": "Repaired"}}}],
        )
        turn3 = _make_llm_response(content="Should be fine now.")

        verdicts = iter(
            [
                AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: PASS_ONE_SINK_ISSUE"),
                AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: PASS_TWO_STILL_BROKEN"),
            ]
        )

        async def _fake_advisor_checkpoint(*_args: object, **_kwargs: object) -> AdvisorCheckpointVerdict:
            return next(verdicts)

        responses = iter([turn1, turn2, turn3])
        captured_messages: list[list[dict[str, Any]]] = []

        async def _fake_call_llm(
            messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
            *,
            provider_custody: object | None = None,
            credential_state: object | None = None,
            prompt_guard_trusted_message_indexes: frozenset[int] = frozenset(),
            prompt_guard_content_parts: tuple[tuple[PromptGuardContentPart, ...] | None, ...] | None = None,
            prompt_guard_tool_argument_parts: tuple[tuple[PromptGuardToolArgumentParts, ...] | None, ...] | None = None,
        ) -> Any:
            captured_messages.append(list(messages))
            return next(responses)

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", side_effect=_fake_advisor_checkpoint),
        ):
            mock_llm.side_effect = _fake_call_llm
            result = await service.compose("Review this pipeline", [], state, session_id=session_id)

        # Completion remains withheld, while the already-green runtime shape
        # stays truthful about authoring and execution readiness.
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is True
        assert result.runtime_preflight.readiness.authoring_valid is True
        assert result.runtime_preflight.readiness.execution_ready is True
        assert result.runtime_preflight.readiness.completion_ready is False
        # Neither pass's raw advisor prose reaches the public result.
        assert "PASS_TWO_STILL_BROKEN" not in result.message
        assert "PASS_ONE_SINK_ISSUE" not in result.message
        assert "PASS_TWO_STILL_BROKEN" not in result.runtime_preflight.model_dump_json()
        assert "PASS_ONE_SINK_ISSUE" not in result.runtime_preflight.model_dump_json()

        # The composer saw pass 1 only long enough to make the mutation; the
        # public/final turn no longer anchors on or exposes that exchange.
        assert len(captured_messages) == 3
        assert any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[1])
        assert not any("Completion advisory review" in (m.get("content") or "") for m in captured_messages[2])


def test_advisor_repair_none_preflight_publishes_unverified_not_ready() -> None:
    """elspeth-88592f5be7 regression (repro_88592f5be7.py): a no-mutation
    advisor-repair turn carries ``runtime_preflight=None`` — validation never
    ran this turn — and used to ride the success disjunct, publishing and
    persisting "The pipeline is configured and ready." while discarding the
    model's contradicting prose. ``None`` is UNKNOWN readiness: the published
    wording must not assert readiness, and the repair-cohort prose stays
    withheld."""
    from elspeth.web.composer.composition_completion import (
        _ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE,
        _replace_advisor_repair_public_result,
    )

    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=7, sources={})
    prose = "I did not change anything; the pipeline still has no output wired the way you asked."

    out = _replace_advisor_repair_public_result(ComposerResult(message=prose, state=state, runtime_preflight=None))

    assert out.message == _ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE
    assert "configured and ready" not in out.message
    assert prose not in out.message
    assert out.raw_assistant_content is None


def test_none_preflight_reads_unknown_fail_closed_in_both_advisor_consumers() -> None:
    """elspeth-88592f5be7 disagreement test: ONE sentinel,
    ``runtime_preflight is None``, has two consumers thousands of lines
    apart. The END advisor gate documents ``None`` as "unknown for this turn;
    fails closed to the fully blocking shape", and the advisor-repair public
    substitution must read the SAME sentinel in the SAME direction — never as
    an affirmative readiness claim. If a future unification flips either
    consumer, this fails loudly."""
    from elspeth.web.composer.composition_completion import (
        _ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE,
        _ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE,
        _replace_advisor_repair_public_result,
    )

    # Consumer 1 — the END advisor gate: None preflight fails closed to the
    # fully blocking validation shape (every readiness axis withheld).
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    blocked = service._advisor_checkpoint._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: still wrong"),
        state=_empty_state(),
        assistant_message=None,
        recorder=BufferingRecorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=None,
        outstanding_findings=None,
    )
    blocked_preflight = blocked.runtime_preflight
    assert blocked_preflight is not None
    assert blocked_preflight.is_valid is False
    assert blocked_preflight.readiness.authoring_valid is False
    assert blocked_preflight.readiness.execution_ready is False
    assert blocked_preflight.readiness.completion_ready is False

    # Consumer 2 — the advisor-repair public substitution: the same None
    # sentinel must not publish the affirmative readiness assertion.
    out = _replace_advisor_repair_public_result(ComposerResult(message="model prose", state=_empty_state(), runtime_preflight=None))
    assert out.message == _ADVISOR_REPAIR_UNVERIFIED_PUBLIC_MESSAGE
    assert out.message != _ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE


class TestComposerConvergence:
    @pytest.mark.asyncio
    async def test_discovery_budget_exceeded_raises(self) -> None:
        """Discovery-only turns exhaust the discovery budget."""
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_discovery_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Two different discovery tools to avoid cache hits
        disc1 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
        )
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "list_transforms", "arguments": {}}],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [disc1, disc2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Loop forever", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "discovery"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", [None, "CLARIFY: Which input?"])
    async def test_composition_budget_exceeded_raises(self, content: str | None) -> None:
        """Mutation turns exhaust the composition budget."""
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_composition_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        mut = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "test"}},
                }
            ],
        )
        # Bonus call also returns tool calls — convergence error
        mut2 = _make_llm_response(
            content=content,
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "test2"}},
                }
            ],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [mut, mut2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Keep mutating", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "composition"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["", "CLARIFY: "])
    async def test_self_correction_on_final_composition_turn_succeeds(self, marker: str) -> None:
        """B-4D-3: LLM makes mutation on final turn, then text on bonus call."""
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_composition_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        mut = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "Final"}},
                }
            ],
        )
        text = _make_llm_response(content=marker + "Done after final correction.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [mut, text]
            result = await service.compose("Do it", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "Done after final correction.", count=1, changed=True)
        assert result.state.metadata.name == "Final"

    @pytest.mark.asyncio
    async def test_mixed_turns_charge_correct_budgets(self) -> None:
        """Mixed discovery/mutation turns are classified independently.

        Discovery turns charge discovery budget, mutation turns charge
        composition budget. Neither exhausts the other.
        """
        catalog = _mock_catalog()
        settings = _make_settings(
            composer_max_composition_turns=2,
            composer_max_discovery_turns=2,
        )
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Turn 1: discovery (list_sources) — discovery counter = 1
        disc = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
        )
        # Turn 2: mutation (set_metadata) — composition counter = 1
        mut = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "Works"}},
                }
            ],
        )
        # Turn 3: text response — loop terminates
        text = _make_llm_response(content="Done.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [disc, mut, text]
            result = await service.compose("Build", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "Done.", count=1, changed=True)
        assert result.state.metadata.name == "Works"


class TestFailedMutationBudgetClassification:
    """Failed mutation tool calls must charge composition budget, not discovery."""

    @pytest.mark.asyncio
    async def test_failed_mutation_charges_composition_budget(self) -> None:
        """A mutation tool that fails with KeyError/TypeError should exhaust
        composition budget, not discovery budget."""
        catalog = _mock_catalog()
        settings = _make_settings(
            composer_max_composition_turns=1,
            composer_max_discovery_turns=10,
        )
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Turn 1: set_source with missing required key → KeyError
        # This is a mutation tool, so even though it fails, it should
        # charge composition budget (1/1 → exhausted).
        bad_mutation = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {"plugin": "csv"},  # missing on_success
                }
            ],
        )
        # Bonus call (composition exhausted gives one last chance) also
        # returns a tool call → convergence error
        bad_mutation2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_source",
                    "arguments": {"plugin": "csv"},
                }
            ],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [bad_mutation, bad_mutation2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Setup source", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "composition"

    @pytest.mark.asyncio
    async def test_failed_mutation_json_parse_charges_composition_budget(self) -> None:
        """Mutation tool with unparseable JSON arguments should still
        charge composition budget."""
        catalog = _mock_catalog()
        settings = _make_settings(
            composer_max_composition_turns=1,
            composer_max_discovery_turns=10,
        )
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Build a tool call with invalid JSON manually
        call = FakeToolCall(
            id="c1",
            function=FakeFunction(
                name="set_source",
                arguments="{invalid json",
            ),
        )
        msg = FakeMessage(content=None, tool_calls=[call])
        response = _admit_composer_llm_completion(FakeLLMResponse(choices=[FakeChoice(message=msg)]))

        # Bonus call also fails
        call2 = FakeToolCall(
            id="c2",
            function=FakeFunction(
                name="set_source",
                arguments="{still invalid",
            ),
        )
        msg2 = FakeMessage(content=None, tool_calls=[call2])
        response2 = _admit_composer_llm_completion(FakeLLMResponse(choices=[FakeChoice(message=msg2)]))

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [response, response2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Setup source", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "composition"

    @pytest.mark.asyncio
    async def test_failed_discovery_does_not_charge_composition_budget(self) -> None:
        """A failed discovery tool should still charge discovery budget,
        not composition budget."""
        catalog = _mock_catalog()
        settings = _make_settings(
            composer_max_composition_turns=10,
            composer_max_discovery_turns=1,
        )
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # list_sources with invalid JSON → still a discovery turn
        call = FakeToolCall(
            id="c1",
            function=FakeFunction(
                name="list_sources",
                arguments="{bad json",
            ),
        )
        msg = FakeMessage(content=None, tool_calls=[call])
        response = _admit_composer_llm_completion(FakeLLMResponse(choices=[FakeChoice(message=msg)]))

        # Turn 2: another discovery call
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "list_transforms", "arguments": {}}],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [response, disc2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Explore", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "discovery"


class TestComposerErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_to_llm(self) -> None:
        """Unknown tool name returns error message, LLM can retry."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Turn 1: invalid tool
        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "nonexistent_tool",
                    "arguments": {},
                }
            ],
        )
        # Turn 2: text response (self-corrected)
        text = _make_llm_response(content="Sorry, let me try again.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [bad_call, text]
            result = await service.compose("Do something", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "Sorry, let me try again.", count=0)
        # State unchanged — the bad tool call didn't modify anything
        assert result.state.version == 1

    @pytest.mark.asyncio
    async def test_malformed_arguments_returns_error(self) -> None:
        """Malformed tool arguments return error, not crash."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        supplied_arguments = {"plugin": "csv"}
        declared_required = tuple(tool_registry._DECLARATIONS_BY_NAME["set_source"].json_schema["required"])
        missing = tuple(name for name in declared_required if name not in supplied_arguments)
        assert missing == ("on_success", "options")
        missing_detail = ", ".join(missing)

        # Turn 1: set_source with missing required field
        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "set_source",
                    "arguments": supplied_arguments,
                }
            ],
        )
        # Turn 2: text
        text = _make_llm_response(content="No pipeline changes were saved.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_source",
            expected_detail=missing_detail,
        )
        assert len(result.tool_invocations) == 1
        invocation = result.tool_invocations[0]
        assert invocation.error_class == "ToolArgumentError"
        assert invocation.error_message == f"missing: {missing_detail}"
        assert result.runtime_preflight.checks[0].detail == (
            "No composition-state mutation completed successfully; state_exists=false. Blocking result: "
            f"set_source failed before mutation (ToolArgumentError: missing: {missing_detail})."
        )

    @pytest.mark.asyncio
    async def test_wrong_type_tool_arg_returns_error(self) -> None:
        """ToolArgumentError from Tier 3 type guard in tool handler is caught, not crash.

        Tool handlers validate LLM-provided argument types at the Tier 3
        boundary, raising ToolArgumentError for wrong types (e.g. int where
        str expected). The compose loop catches this typed exception and
        feeds the error back to the LLM so it can retry with a corrected
        argument.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Turn 1: tool call that triggers ToolArgumentError from Tier 3 type guard
        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        # Turn 2: LLM self-corrects
        text = _make_llm_response(content="Fixed.")

        with (
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ToolArgumentError(
                    argument="content",
                    expected="a string",
                    actual_type="int",
                ),
            ),
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
        ):
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_source",
            expected_detail="'content' must be a string, got int",
        )

    @pytest.mark.asyncio
    async def test_malformed_set_pipeline_missing_top_level_required_field_returns_error(self) -> None:
        """Top-level required field omission in set_pipeline returns a Tier-3 arg error.

        Renamed from test_malformed_set_pipeline_nested_required_field_returns_error
        — the body always tested top-level ``source.plugin`` omission, not a nested
        required field. Coverage of nested-optional + inner-required semantics lives
        in the two tests below.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "set_pipeline",
                    "arguments": {
                        "source": {
                            "on_success": "source_out",
                            "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                        },
                        "nodes": [
                            {
                                "id": "t1",
                                "node_type": "transform",
                                "plugin": "passthrough",
                                "input": "source_out",
                                "on_success": "main",
                                "options": {},
                            }
                        ],
                        "edges": [
                            {
                                "id": "e1",
                                "from_node": "source",
                                "to_node": "t1",
                                "edge_type": "on_success",
                            }
                        ],
                        "outputs": [
                            {
                                "sink_name": "main",
                                "plugin": "csv",
                                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                            }
                        ],
                    },
                }
            ],
        )
        text = _make_llm_response(content="Recovered.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_pipeline",
            expected_detail="source.plugin",
        )
        tool_msg = script.prompts[1][-1]
        error_content = json.loads(tool_msg["content"])
        assert "source.plugin" in error_content["error"]
        assert "missing required" in error_content["error"].lower()
        # Regression guard: the conditional-on-presence walker must NOT
        # surface false-positive errors for the optional ``source.inline_blob``
        # branch when no inline blob was supplied. See elspeth-4e79436719 §Bug A.
        assert "inline_blob" not in error_content["error"]

    @pytest.mark.asyncio
    async def test_set_pipeline_without_inline_blob_passes_predispatch_validation(self) -> None:
        """No-inline set_pipeline payloads must reach the handler.

        Regression guard for elspeth-4e79436719 Bug A: the required-paths
        walker previously lifted ``source.inline_blob.{filename,mime_type,content}``
        into top-level required paths because it recursed into nested
        ``properties`` blocks unconditionally — even when the containing
        property (``inline_blob``) was optional at its parent. Correct
        JSON-Schema semantics: nested ``required`` applies only when the
        containing object is present in the value.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        good_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_ok",
                    "name": "set_pipeline",
                    "arguments": {
                        "sources": {
                            "primary": {
                                "plugin": "csv",
                                "on_success": "t1",
                                "on_validation_failure": "discard",
                                "options": {"path": "/data/blobs/in.csv", "schema": {"mode": "observed"}},
                            }
                        },
                        "nodes": [
                            {
                                "id": "t1",
                                "node_type": "transform",
                                "plugin": "passthrough",
                                "input": "main",
                                "on_success": "main",
                                "on_error": "discard",
                                "options": {},
                            }
                        ],
                        "edges": [
                            {
                                "id": "e1",
                                "from_node": "source",
                                "to_node": "t1",
                                "edge_type": "on_success",
                            }
                        ],
                        "outputs": [
                            {
                                "sink_name": "main",
                                "plugin": "csv",
                                "on_write_failure": "discard",
                                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                            }
                        ],
                    },
                }
            ],
        )
        text = _make_llm_response(content="No pipeline changes were saved.")
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            script = _ReconciliationLLMScript(good_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Setup", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        # The pre-dispatch validator must not have rejected the call.
        # Verify by inspecting the tool-result message content: the
        # required-path gate produces a JSON object whose
        # ``error`` field starts with ``Tool 'set_pipeline' missing required
        # argument(s):``. A no-inline payload must not trigger that path,
        # regardless of what the tool handler returns afterwards.
        tool_msg = script.prompts[1][-1]
        assert tool_msg["role"] == "tool"
        error_content = json.loads(tool_msg["content"])
        assert type(error_content) is dict
        error_text = error_content.get("error", "")
        assert type(error_text) is str
        assert "missing required argument" not in error_text.lower()
        assert "inline_blob" not in error_text
        assert len(result.tool_invocations) == 1
        assert result.tool_invocations[0].tool_call_id == "call_ok"
        assert result.tool_invocations[0].error_category is not ToolArgumentErrorCategory.MISSING_REQUIRED_PATH

    @pytest.mark.asyncio
    async def test_set_pipeline_with_partial_inline_blob_returns_tier3_arg_error(self) -> None:
        """When ``inline_blob`` is present but incomplete, the inner required fields ARE enforced.

        Conditional-on-presence semantics: an absent optional object skips
        its inner ``required`` checks; a present-but-incomplete optional
        object surfaces them as Tier-3 arg errors. See elspeth-4e79436719
        §Bug A.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        partial_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_partial",
                    "name": "set_pipeline",
                    "arguments": {
                        "source": {
                            "plugin": "csv",
                            "on_success": "t1",
                            "options": {"path": "/data/blobs/in.csv", "schema": {"mode": "observed"}},
                            "inline_blob": {"filename": "data.csv"},
                        },
                        "nodes": [
                            {
                                "id": "t1",
                                "node_type": "transform",
                                "plugin": "passthrough",
                                "input": "main",
                                "on_success": "main",
                                "options": {},
                            }
                        ],
                        "edges": [
                            {
                                "id": "e1",
                                "from_node": "source",
                                "to_node": "t1",
                                "edge_type": "on_success",
                            }
                        ],
                        "outputs": [
                            {
                                "sink_name": "main",
                                "plugin": "csv",
                                "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                            }
                        ],
                    },
                }
            ],
        )
        text = _make_llm_response(content="Adjusted.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(partial_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_pipeline",
            expected_detail="source.inline_blob.mime_type, source.inline_blob.content",
        )
        tool_msg = script.prompts[1][-1]
        error_content = json.loads(tool_msg["content"])
        assert "source.inline_blob.mime_type" in error_content["error"]
        assert "source.inline_blob.content" in error_content["error"]
        # filename WAS supplied — must not be reported.
        assert "source.inline_blob.filename" not in error_content["error"]

    @pytest.mark.asyncio
    async def test_internal_key_error_is_not_swallowed(self) -> None:
        """KeyError from tool handler internals must crash, not be sent to LLM.

        Previously, KeyError from missing LLM arguments and KeyError from
        internal bugs were both caught and fed back to the LLM. Internal
        bugs should crash immediately — the LLM cannot self-correct our code.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Provide all required arguments so pre-validation passes,
        # but patch execute_tool to raise a KeyError from "internal logic"
        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=KeyError("internal_state_key"),
            ),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=session_id)
        # The underlying KeyError is preserved on the wrapper so callers
        # (server logs, route handler, capture_logs assertions) can still
        # identify the original plugin-internal class.
        assert isinstance(exc_info.value.original_exc, KeyError)
        assert exc_info.value.exc_class == "KeyError"
        assert "internal_state_key" in str(exc_info.value.original_exc)

    @pytest.mark.asyncio
    async def test_missing_args_error_message_lists_keys(self) -> None:
        """Missing required arguments should produce a clear error listing the keys."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # set_source requires plugin, on_success, options, on_validation_failure
        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {"plugin": "csv"},  # missing 3 required args
                }
            ],
        )
        text = _make_llm_response(content="Ok.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            await service.compose("Setup", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        # Verify the error message sent back to the LLM mentions the missing keys
        tool_msg = script.prompts[1][-1]  # last message in second call
        error_content = json.loads(tool_msg["content"])
        assert "on_success" in error_content["error"]
        assert "missing required" in error_content["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [[], None, "oops"])
    async def test_top_level_non_object_mutation_arguments_return_tool_error(self, arguments: Any) -> None:
        """Non-object mutation arguments are Tier-3 tool errors, not plugin crashes."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "set_source",
                    "arguments": arguments,
                }
            ],
        )
        text = _make_llm_response(content="Recovered.")

        with (
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                wraps=_execute_tool,
            ) as mock_execute_tool,
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
        ):
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_source",
            expected_detail="arguments (",
        )
        mock_execute_tool.assert_not_called()
        tool_msg = script.prompts[1][-1]
        error_content = json.loads(tool_msg["content"])
        assert "arguments must be a JSON object" in error_content["error"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("arguments", [[], None, "oops"])
    async def test_top_level_non_object_discovery_arguments_return_tool_error(self, arguments: Any) -> None:
        """Non-object discovery arguments are rejected before cache lookup or execution."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "list_sources",
                    "arguments": arguments,
                }
            ],
        )
        text = _make_llm_response(content="Recovered.")

        with (
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                wraps=_execute_tool,
            ) as mock_execute_tool,
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.side_effect = [bad_call, text]
            result = await service.compose("Explore", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "Recovered.", count=0)
        mock_execute_tool.assert_not_called()
        tool_msg = mock_llm.call_args_list[1][0][0][-1]
        error_content = json.loads(tool_msg["content"])
        assert "arguments must be a JSON object" in error_content["error"]


class TestProviderCacheTokenAudit:
    """Audit-write capture of provider prompt-cache statistics (elspeth-4e79436719 Bug C).

    Provider responses can carry cache-hit information in two distinct
    shapes:
    - OpenAI / OpenRouter: ``usage.prompt_tokens_details.cached_tokens``
    - Anthropic: ``usage.cache_creation_input_tokens`` and
      ``usage.cache_read_input_tokens`` as siblings on usage

    Both must land on the ``ComposerLLMCall`` audit record with their
    canonical names, so the audit DB records what the provider actually
    reported. A missing field must remain ``None`` rather than be coerced to
    zero: absence is evidence, and a fabricated zero is indistinguishable from
    a measured zero.

    LiteLLM-shape deduplication: when LiteLLM is the wire to an
    Anthropic-family provider (Anthropic direct, Bedrock-Claude,
    Vertex-Claude/Gemini), it populates BOTH the nested OpenAI shape and
    the Anthropic siblings on the same response, deriving
    ``prompt_tokens_details.cached_tokens`` from the sibling
    ``cache_read_input_tokens``. The two are aliases, not independent
    signals — recording both would double-count cache hits in the audit
    sidecar, and creation-only responses (which carry ``cached_tokens=0``
    by LiteLLM default) would fabricate a zero into ``cached_prompt_tokens``
    that the provider never asserted. Presence of an Anthropic sibling is
    the provenance signal that the nested shape is LiteLLM-synthesized.
    """

    @staticmethod
    def _response_with_usage(usage: dict[str, Any]) -> FakeLLMResponse:
        """Build a fake response carrying a usage object as a Mapping.

        FakeLLMResponse only carries .choices; add .usage so the audit
        extractor's Mapping branch fires. Using a separate dataclass keeps
        the typed FakeLLMResponse intact for the rest of the test suite.
        """

        @dataclass
        class FakeResponseWithUsage:
            choices: list[FakeChoice]
            usage: dict[str, Any]
            model: str = "openrouter/openai/gpt-5.5"
            id: str = "chatcmpl-test"

        text = _make_raw_llm_response(content="Done.")
        return FakeResponseWithUsage(choices=text.choices, usage=usage)  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_openai_nested_cached_tokens_lands_on_audit_record(self) -> None:
        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = self._response_with_usage(
            {
                "prompt_tokens": 1200,
                "completion_tokens": 80,
                "total_tokens": 1280,
                "prompt_tokens_details": {"cached_tokens": 1024},
            }
        )
        usage = token_usage_from_response(response)
        assert usage.prompt_tokens == 1200
        assert usage.cached_prompt_tokens == 1024
        assert usage.cache_creation_input_tokens is None
        assert usage.cache_read_input_tokens is None

    @pytest.mark.asyncio
    async def test_anthropic_sibling_cache_fields_land_on_audit_record(self) -> None:
        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = self._response_with_usage(
            {
                "prompt_tokens": 8200,
                "completion_tokens": 120,
                "cache_creation_input_tokens": 7000,
                "cache_read_input_tokens": 1100,
            }
        )
        usage = token_usage_from_response(response)
        assert usage.cache_creation_input_tokens == 7000
        assert usage.cache_read_input_tokens == 1100
        assert usage.cached_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_litellm_normalized_anthropic_does_not_double_record_cache_hit(self) -> None:
        """LiteLLM-normalized Anthropic-family response: nested shape is suppressed.

        Mirrors the wire shape produced by
        ``litellm/llms/anthropic/chat/transformation.py:1294-1317`` —
        ``prompt_tokens_details.cached_tokens`` and ``cache_read_input_tokens``
        carry the SAME value because LiteLLM derives the former from the
        latter. The audit row must record only the Anthropic-shape signal.
        """
        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = self._response_with_usage(
            {
                "prompt_tokens": 8200,
                "completion_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 1100},
                "cache_creation_input_tokens": 7000,
                "cache_read_input_tokens": 1100,
            }
        )
        usage = token_usage_from_response(response)
        assert usage.cache_creation_input_tokens == 7000
        assert usage.cache_read_input_tokens == 1100
        assert usage.cached_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_litellm_creation_only_does_not_leak_zero_into_cached_prompt_tokens(self) -> None:
        """LiteLLM creation-only Anthropic response: nested cached_tokens=0 is suppressed.

        ``PromptTokensDetailsWrapper`` preserves ``cached_tokens=0`` (verified
        empirically), so a cache-creation-only Anthropic call still emits a
        nested shape with ``cached_tokens=0``. Without dedup, the audit row
        would carry ``cached_prompt_tokens=0`` — a fabricated zero that
        misleads the auditor into thinking the provider reported a zero-hit
        cache read, when in fact the provider only reported cache creation.
        """
        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = self._response_with_usage(
            {
                "prompt_tokens": 7000,
                "completion_tokens": 80,
                "prompt_tokens_details": {"cached_tokens": 0},
                "cache_creation_input_tokens": 7000,
                "cache_read_input_tokens": 0,
            }
        )
        usage = token_usage_from_response(response)
        assert usage.cache_creation_input_tokens == 7000
        assert usage.cache_read_input_tokens == 0
        assert usage.cached_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_litellm_normalized_dual_shape_is_deduped_on_attribute_branch(self) -> None:
        """Attribute-shaped usage object also dedups when siblings present.

        Uses REAL litellm objects, not a dataclass fake. A fake could not
        express the shape under test: litellm declares neither ``usage`` on
        ``ModelResponse`` nor the Anthropic sibling counters on ``Usage`` —
        all three live in ``__pydantic_extra__``, so a dataclass double that
        exposes them as ordinary attributes exercises a reading path
        production does not take (elspeth-6664a00cb0). The full reconciliation
        of this path against real litellm lives in
        ``test_llm_usage_real_litellm.py``.
        """
        from litellm.types.utils import (
            Choices,
            Message,
            ModelResponse,
            PromptTokensDetailsWrapper,
            Usage,
        )

        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = ModelResponse(
            choices=[Choices(message=Message(content="Done.", role="assistant"), finish_reason="stop")],
            model="anthropic/claude-3-5-sonnet",
            id="msg_test",
            usage=Usage(
                prompt_tokens=8200,
                completion_tokens=120,
                total_tokens=8320,
                prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=1100),  # type: ignore[no-untyped-call]
                cache_creation_input_tokens=7000,
                cache_read_input_tokens=1100,
            ),
        )
        usage = token_usage_from_response(response)
        assert usage.cache_creation_input_tokens == 7000
        assert usage.cache_read_input_tokens == 1100
        assert usage.cached_prompt_tokens is None

    @pytest.mark.asyncio
    async def test_no_cache_metadata_leaves_fields_none(self) -> None:
        """Absent cache metadata must NOT be fabricated to zero."""
        from elspeth.web.composer.llm_response_parsing import token_usage_from_response

        response = self._response_with_usage({"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
        usage = token_usage_from_response(response)
        assert usage.cached_prompt_tokens is None
        assert usage.cache_creation_input_tokens is None
        assert usage.cache_read_input_tokens is None

    @pytest.mark.asyncio
    async def test_cache_fields_propagate_through_compose_to_llm_call_record(self) -> None:
        """End-to-end: cache fields appear on the buffered ComposerLLMCall record.

        Patches the LLM call to return a response with OpenAI-shape cache
        metadata, then checks the recorded ComposerLLMCall on the
        ``ComposerResult.llm_calls`` tuple.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        response = self._response_with_usage(
            {
                "prompt_tokens": 500,
                "completion_tokens": 12,
                "total_tokens": 512,
                "prompt_tokens_details": {"cached_tokens": 256},
            }
        )

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = response
            result = await service.compose("Hi", [], state, session_id=session_id)

        assert len(result.llm_calls) == 1
        call_record = result.llm_calls[0]
        assert call_record.prompt_tokens == 500
        assert call_record.cached_prompt_tokens == 256
        assert call_record.cache_creation_input_tokens is None
        assert call_record.cache_read_input_tokens is None


class TestBuildMessages:
    @pytest.mark.asyncio
    async def test_build_messages_returns_new_list(self) -> None:
        """_build_messages must return a new list on every call."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()

        msgs1 = service._build_messages([], state, "Hello")
        msgs2 = service._build_messages([], state, "Hello")

        assert msgs1 is not msgs2  # different list objects
        assert msgs1 == msgs2  # same content

    @pytest.mark.asyncio
    async def test_build_messages_oserror_redacts_filename(self) -> None:
        """OSError from deployment-skill loading MUST NOT leak its filename.

        ``str(OSError)`` expands to "[Errno N] <strerror>: '<absolute
        path>'".  That filename reveals the operator's data-dir layout
        and — when the deployment skill lives under a user-scoped
        subdirectory — the user identifier itself.  The wrapper
        ``ComposerServiceError`` flows into the 502 response body in
        ``sessions/routes.py::send_message`` and ``recompose``, so the
        message MUST contain only the exception class name.

        This test pins the redaction contract: the ``str(exc)`` of the
        raised ``ComposerServiceError`` contains no substring of the
        provoking OSError's filename or its strerror text.  Mirrors
        the regression assertion on the SQLAlchemy-family 422 path in
        ``_handle_convergence_error`` (web/sessions/routes.py).
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()

        secret_path = "/var/lib/elspeth/users/alice/skills/secret-deployment.md"

        def _raise_oserror(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
            raise PermissionError(13, "Permission denied", secret_path)

        with (
            patch("elspeth.web.composer.service.build_messages", side_effect=_raise_oserror),
            pytest.raises(ComposerServiceError) as excinfo,
        ):
            service._build_messages([], state, "Hi")

        body = str(excinfo.value)
        # The filename MUST NOT appear in the wrapper message. Test
        # against the full path AND its directory fragments, because
        # partial leaks (e.g. "/var/lib/elspeth/users/alice") are just
        # as damaging as the full path.
        assert secret_path not in body
        assert "alice" not in body
        assert "Permission denied" not in body
        # The class name IS part of the safe surface — operators
        # reading the 502 still need to distinguish PermissionError
        # from IsADirectoryError from FileNotFoundError.
        assert "PermissionError" in body
        # __cause__ preservation: full detail reaches server-side
        # machinery even though the HTTP body is redacted.
        assert isinstance(excinfo.value.__cause__, PermissionError)
        assert excinfo.value.__cause__.filename == secret_path


class TestComposerMultipleToolCallsPerTurn:
    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_single_turn(self) -> None:
        """LLM returns multiple tool calls in one response — all executed."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        # Turn 1: two tool calls in one response
        multi_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "t1",
                        "options": {
                            "path": f"/data/blobs/{session_id}/input.csv",
                            "schema": {"mode": "observed"},
                        },
                        "on_validation_failure": "quarantine",
                    },
                },
                {
                    "id": "call_2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "Dual Call Pipeline"}},
                },
            ],
        )
        # Turn 2: text
        text = _make_llm_response(content="Done.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [multi_call, text]
            result = await service.compose("Setup", [], state, session_id=session_id)

        assert result.state.sources["source"] is not None
        assert result.state.metadata.name == "Dual Call Pipeline"
        assert result.state.version == 3  # two mutations


class TestDiscoveryCache:
    """Tests for the discovery cache (F1)."""

    @pytest.mark.asyncio
    async def test_one_compose_call_threads_one_snapshot_object(self) -> None:
        """Prompt and every tool dispatch share one principal snapshot."""

        from elspeth.web.composer import service as service_module
        from elspeth.web.composer import tool_batch as tool_batch_module
        from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

        catalog = _mock_catalog()
        snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        factory_calls: list[str] = []
        prompt_snapshots: list[PluginAvailabilitySnapshot] = []
        tool_snapshots: list[PluginAvailabilitySnapshot] = []

        def snapshot_factory(user_id: str) -> PluginAvailabilitySnapshot:
            factory_calls.append(user_id)
            return snapshot

        settings = _make_settings()
        engine, session_id = _session_engine_with_session(user_id="user-1")
        service = ComposerServiceImpl(
            catalog=catalog,
            settings=settings,
            plugin_snapshot_factory=snapshot_factory,
            operator_profile_registry=MagicMock(spec=OperatorProfileRegistry),
            sessions_service=_test_sessions_service(engine, Path(settings.data_dir)),
            session_engine=engine,
        )
        real_build_messages = service_module.build_messages
        real_execute_tool = tool_batch_module.execute_tool

        def capture_prompt(*args: Any, **kwargs: Any) -> Any:
            prompt_snapshots.append(kwargs["plugin_snapshot"])
            return real_build_messages(*args, **kwargs)

        def capture_tool(*args: Any, **kwargs: Any) -> Any:
            tool_snapshots.append(kwargs["plugin_snapshot"])
            return real_execute_tool(*args, **kwargs)

        discovery = _make_llm_response(tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}])
        final = _make_llm_response(content="Done.")
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=[discovery, final]),
            patch.object(service_module, "build_messages", side_effect=capture_prompt),
            patch.object(tool_batch_module, "execute_tool", side_effect=capture_tool),
        ):
            await service.compose("List sources", [], _empty_state(), user_id="user-1", session_id=session_id)

        assert factory_calls == ["user-1"]
        assert prompt_snapshots == [snapshot]
        assert tool_snapshots == [snapshot]
        assert prompt_snapshots[0] is tool_snapshots[0]

    @pytest.mark.asyncio
    async def test_cacheable_tool_returns_cached_result(self) -> None:
        """Repeated cacheable discovery calls return cached results
        without incrementing any budget counter."""
        from elspeth.web.composer import tool_batch as tool_batch_module

        catalog = _mock_catalog()
        settings = _make_settings(composer_max_discovery_turns=2)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Turn 1: list_sources (first call — executes, charges discovery: 1/2)
        # Turn 2: list_sources AGAIN (cache hit — no budget charge)
        # Turn 3: text
        disc1 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
        )
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "list_sources", "arguments": {}}],
        )
        text = _make_llm_response(content="Found sources.")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
        ):
            mock_llm.side_effect = [disc1, disc2, text]
            result = await service.compose("List sources", [], state, session_id=session_id)

        # Should NOT have raised — second list_sources was a cache hit
        _assert_checkpoint_reply(result, "Found sources.", count=0)
        assert dispatch.call_count == 1
        # Catalog list_sources is called once by snapshot construction
        # (before any PolicyCatalogView exists) and once more by the
        # PolicyCatalogView itself, which memoizes its unrestricted listing
        # per instance (PolicyCatalogView._full_items) — build_messages's own
        # list_sources() + capability_groups() call and execute_tool's first
        # discovery dispatch all reuse that single cached fetch instead of
        # re-deriving every PluginSummary three times over.
        # The second discovery call is a composer tool-cache hit — no catalog call.
        # Total: 2, not 4.
        assert catalog.list_sources.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change_state", [False, True])
    async def test_failed_cacheable_discovery_preserves_rejection_on_each_call(self, change_state: bool) -> None:
        """A failed schema lookup must execute twice and retain its rejection code."""
        from elspeth.web.composer import tool_batch as tool_batch_module
        from elspeth.web.composer.discovery_cache import cached_discovery_payload

        catalog = _mock_catalog()
        service, session_id = _composer_service_with_session(catalog, _make_settings())
        arguments = {"plugin_type": "source", "name": "missing_plugin"}
        responses = [
            _make_llm_response(tool_calls=[{"id": call_id, "name": "get_plugin_schema", "arguments": arguments}])
            for call_id in ("c1", "c2")
        ]
        if change_state:
            responses.insert(
                1,
                _make_llm_response(
                    tool_calls=[
                        {
                            "id": "mutation",
                            "name": "set_source",
                            "arguments": {
                                "plugin": "csv",
                                "on_success": "out",
                                "options": {"path": f"/data/blobs/{session_id}/input.csv", "schema": {"mode": "observed"}},
                                "on_validation_failure": "quarantine",
                            },
                        }
                    ]
                ),
            )
        responses.append(_make_llm_response(content="That plugin is unavailable."))
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
            patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
            patch.object(tool_batch_module, "_cached_discovery_payload", wraps=cached_discovery_payload) as cache_payload,
            patch.object(service._preflight, "runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
        ):
            result = await service.compose("Describe the missing plugin", [], _empty_state(), session_id=session_id)

        assert dispatch.call_count == 2 + int(change_state)
        cache_payload.assert_not_called()
        tool_messages = [
            message
            for message in completion.call_args_list[-1][0][0]
            if message["role"] == "tool" and message["tool_call_id"] in ("c1", "c2")
        ]
        assert len(tool_messages) == 2
        entries = []
        for message in tool_messages:
            payload = json.loads(message["content"])
            assert payload["success"] is False
            assert "data" not in payload
            entry = payload["validation"]["errors"][0]
            assert entry["component"] == "rejected_mutation"
            assert entry["error_code"] == "plugin_not_installed"
            assert entry["message"] == (
                "source plugin selection is unavailable (plugin_not_installed): no plugin with this name is installed in this deployment"
            )
            entries.append(entry)
        assert entries[0] == entries[1]
        first_payload, second_payload = [json.loads(message["content"]) for message in tool_messages]
        assert first_payload["version"] == 1
        assert second_payload["version"] == result.state.version == 1 + int(change_state)
        if change_state:
            assert any(entry.get("error_code") == "no_source_configured" for entry in first_payload["validation"]["errors"])
            assert all(entry.get("error_code") != "no_source_configured" for entry in second_payload["validation"]["errors"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["list_sources", "get_expression_grammar", "get_audit_info"])
    async def test_cache_hit_rebuilds_result_envelope_from_current_state(self, tool_name: str) -> None:
        """Cacheable discovery data is reused, but validation/version stay current."""
        from elspeth.web.composer import tool_batch as tool_batch_module

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        disc1 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": tool_name, "arguments": {}}],
        )
        mutate = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {
                            "path": f"/data/blobs/{session_id}/input.csv",
                            "schema": {"mode": "observed"},
                        },
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c3", "name": tool_name, "arguments": {}}],
        )
        text = _make_llm_response(content="Done.")

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
        ):
            mock_llm.side_effect = [disc1, mutate, disc2, text]
            result = await service.compose("Build", [], state, session_id=session_id)

        assert result.state.version == 2
        assert dispatch.call_count == 2  # one discovery miss and one mutation
        cached_tool_message = mock_llm.call_args_list[3][0][0][-1]
        cached_payload = json.loads(cached_tool_message["content"])
        first_message = next(message for message in mock_llm.call_args_list[3][0][0] if message.get("tool_call_id") == "c1")
        first_payload = json.loads(first_message["content"])
        assert first_payload["version"] == 1
        assert cached_payload["data"] == first_payload["data"]
        assert cached_payload["version"] == 2
        expected_validation = ToolResult(
            success=True,
            updated_state=result.state,
            validation=result.state.validate(),
            affected_nodes=(),
        ).to_dict()["validation"]
        assert cached_payload["validation"] == expected_validation
        # See test_cacheable_tool_returns_cached_result: PolicyCatalogView
        # memoizes its unrestricted listing per instance, so repeated
        # internal list_sources()/capability_groups() reads within one
        # compose() collapse into a single real catalog call.
        assert catalog.list_sources.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["get_expression_grammar", "get_audit_info"])
    async def test_selected_response_rejects_malformed_producer_before_success(self, tool_name: str) -> None:
        """Malformed owned output is a crash before audit success or model egress."""
        from elspeth.contracts.composer_audit import ComposerToolStatus
        from elspeth.web.composer import tool_batch as tool_batch_module

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        state = _empty_state()
        malformed = ToolResult(
            success=True, updated_state=state, validation=state.validate(), affected_nodes=(), data={"unexpected": "producer bug"}
        )
        responses = [
            _make_llm_response(tool_calls=[{"id": "bad-response", "name": tool_name, "arguments": {}}]),
            _make_llm_response(content="This must never see malformed output."),
        ]
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
            patch.object(tool_batch_module, "execute_tool", return_value=malformed),
            patch.object(tool_batch_module, "_cached_discovery_payload") as cache_payload,
            patch.object(BufferingRecorder, "record", autospec=True, side_effect=BufferingRecorder.record) as record,
            pytest.raises(ComposerPluginCrashError) as caught,
        ):
            await service.compose("Describe the pipeline language", [], state, session_id=session_id)

        assert isinstance(caught.value.original_exc, FrameworkBugError)
        assert completion.call_count == 1
        cache_payload.assert_not_called()
        assert [call.args[1].status for call in record.call_args_list] == [ComposerToolStatus.PLUGIN_CRASH]
        # Session-backed compose persists this failed turn before raising;
        # the exception must not ask the route to write the same audit twice.
        assert caught.value.tool_invocations == ()
        assert service._sessions_service is not None
        stored_messages = await service._sessions_service.get_messages(UUID(session_id))
        stored_tool_rows = [message for message in stored_messages if message.role == "tool"]
        assert len(stored_tool_rows) == 1
        assert stored_tool_rows[0].tool_call_id == "bad-response"
        stored_failure = json.loads(stored_tool_rows[0].content)
        assert stored_failure["_redaction_status"] == "plugin_crash"
        assert "data" not in stored_failure
        assert "producer bug" not in stored_tool_rows[0].content
        stored_assistant_rows = [message for message in stored_messages if message.role == "assistant"]
        assert len(stored_assistant_rows) == 1
        assert stored_tool_rows[0].parent_assistant_id == stored_assistant_rows[0].id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ["get_expression_grammar", "get_audit_info"])
    async def test_selected_response_caller_error_remains_uncached_argument_error(self, tool_name: str) -> None:
        """Invalid caller arguments retain their Tier-3 audit status on each attempt."""
        from elspeth.contracts.composer_audit import ComposerToolStatus
        from elspeth.web.composer import tool_batch as tool_batch_module

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        responses = [
            _make_llm_response(tool_calls=[{"id": call_id, "name": tool_name, "arguments": {"unexpected": 1}}])
            for call_id in ("invalid-1", "invalid-2")
        ]
        responses.append(_make_llm_response(content="The arguments were invalid."))
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
            patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
            patch.object(tool_batch_module, "_cached_discovery_payload") as cache_payload,
        ):
            result = await service.compose("Describe the pipeline language", [], _empty_state(), session_id=session_id)

        assert dispatch.call_count == 2
        cache_payload.assert_not_called()
        assert [entry.status for entry in result.tool_invocations] == [ComposerToolStatus.ARG_ERROR, ComposerToolStatus.ARG_ERROR]
        messages = [message for message in completion.call_args_list[-1][0][0] if message["role"] == "tool"]
        assert len(messages) == 2
        assert all("error" in json.loads(message["content"]) for message in messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("corruption", ["owned_value", "bound_contract"])
    async def test_selected_response_cache_corruption_crashes_before_hit_egress(self, corruption: str) -> None:
        """A real miss followed by a corrupt hit records one crash and stops egress."""
        from elspeth.contracts.composer_audit import ComposerToolStatus
        from elspeth.web.composer import tool_batch as tool_batch_module
        from elspeth.web.composer.discovery_cache import cached_discovery_payload
        from elspeth.web.composer.tools.generation import EXPRESSION_GRAMMAR_RESPONSE_CONTRACT, ExpressionGrammarResponse

        def corrupt_cache(result: Any) -> Any:
            cached = cached_discovery_payload(result)
            assert cached.admitted_response is not None
            if corruption == "owned_value":
                admitted = replace(cached.admitted_response, value=ExpressionGrammarResponse(grammar=cast(str, 17)))
            else:
                # Equivalent functions still belong to a different declaration identity.
                admitted = replace(cached.admitted_response, contract=replace(EXPRESSION_GRAMMAR_RESPONSE_CONTRACT))
            return replace(cached, admitted_response=admitted)

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        responses = [
            _make_llm_response(tool_calls=[{"id": call_id, "name": "get_expression_grammar", "arguments": {}}])
            for call_id in ("miss", "corrupt-hit")
        ]
        responses.append(_make_llm_response(content="This must never see corrupt cached output."))
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
            patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
            patch.object(tool_batch_module, "_cached_discovery_payload", side_effect=corrupt_cache) as cache_payload,
            patch.object(BufferingRecorder, "record", autospec=True, side_effect=BufferingRecorder.record) as record,
            pytest.raises(FrameworkBugError),
        ):
            await service.compose("Describe the pipeline language", [], _empty_state(), session_id=session_id)

        assert dispatch.call_count == 1
        assert cache_payload.call_count == 1
        assert completion.call_count == 2
        assert [call.args[1].status for call in record.call_args_list] == [ComposerToolStatus.SUCCESS, ComposerToolStatus.PLUGIN_CRASH]
        messages = [message for message in completion.call_args_list[-1][0][0] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in messages] == ["miss"]

    @pytest.mark.asyncio
    async def test_selected_response_semantic_failure_is_never_cached(self) -> None:
        """A selected handler's unsuccessful result keeps its absent-data envelope."""
        from elspeth.web.composer import tool_batch as tool_batch_module

        def fail_discovery(*args: Any, **kwargs: Any) -> ToolResult:
            return replace(_strict_execute_tool(*args, **kwargs), success=False, data=None)

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        responses = [
            _make_llm_response(tool_calls=[{"id": call_id, "name": "get_expression_grammar", "arguments": {}}])
            for call_id in ("failed-1", "failed-2")
        ]
        responses.append(_make_llm_response(content="Discovery was unavailable."))
        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
            patch.object(tool_batch_module, "execute_tool", side_effect=fail_discovery) as dispatch,
            patch.object(tool_batch_module, "_cached_discovery_payload") as cache_payload,
        ):
            await service.compose("Describe the pipeline language", [], _empty_state(), session_id=session_id)

        assert dispatch.call_count == 2
        cache_payload.assert_not_called()
        messages = [message for message in completion.call_args_list[-1][0][0] if message["role"] == "tool"]
        assert len(messages) == 2
        for message in messages:
            envelope = json.loads(message["content"])
            assert envelope["success"] is False
            assert "data" not in envelope

    @pytest.mark.asyncio
    async def test_selected_response_cache_encoder_failure_is_audited_before_success(self) -> None:
        """A cache hit's encoder is inside the same crash-audit boundary as admission."""
        from elspeth.contracts.composer_audit import ComposerToolStatus
        from elspeth.web.composer import tool_batch as tool_batch_module
        from elspeth.web.composer.discovery_cache import admitted_result_from_cached_discovery_payload

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        responses = [
            _make_llm_response(tool_calls=[{"id": call_id, "name": "get_expression_grammar", "arguments": {}}])
            for call_id in ("miss", "encoder-failure")
        ]
        responses.append(_make_llm_response(content="This must never see failed encoder output."))
        with contextlib.ExitStack() as patches:

            def fail_hit_encoder(*args: Any, **kwargs: Any) -> Any:
                admitted = admitted_result_from_cached_discovery_payload(*args, **kwargs)
                assert admitted.response is not None
                patches.enter_context(patch.object(type(admitted.response), "to_wire", side_effect=FrameworkBugError("encoder bug")))
                return admitted

            with (
                patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock, side_effect=responses) as completion,
                patch.object(tool_batch_module, "execute_tool", wraps=_strict_execute_tool) as dispatch,
                patch.object(tool_batch_module, "admitted_result_from_cached_discovery_payload", side_effect=fail_hit_encoder),
                patch.object(BufferingRecorder, "record", autospec=True, side_effect=BufferingRecorder.record) as record,
                pytest.raises(FrameworkBugError, match="encoder bug"),
            ):
                await service.compose("Describe the pipeline language", [], _empty_state(), session_id=session_id)

        assert dispatch.call_count == 1
        assert completion.call_count == 2
        assert [call.args[1].status for call in record.call_args_list] == [ComposerToolStatus.SUCCESS, ComposerToolStatus.PLUGIN_CRASH]
        messages = [message for message in completion.call_args_list[-1][0][0] if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in messages] == ["miss"]

    @pytest.mark.asyncio
    async def test_cache_key_includes_arguments(self) -> None:
        """Different arguments = different cache entries = both execute."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        schema1 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "get_plugin_schema",
                    "arguments": {"plugin_type": "source", "name": "csv"},
                }
            ],
        )
        schema2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "get_plugin_schema",
                    "arguments": {"plugin_type": "source", "name": "json"},
                }
            ],
        )
        text = _make_llm_response(content="Got schemas.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [schema1, schema2, text]
            await service.compose("Get schemas", [], state, session_id=session_id)

        # Pure policy admission precedes one direct schema read per request.
        assert [(entry.args, entry.kwargs) for entry in catalog.get_schema.call_args_list] == [
            (("source", "csv"), {}),
            (("source", "json"), {}),
        ]

    @pytest.mark.asyncio
    async def test_mutation_tools_never_cached(self) -> None:
        """Mutation tool results are never cached — always execute."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        mut1 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "X"}},
                }
            ],
        )
        mut2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "Y"}},
                }
            ],
        )
        text = _make_llm_response(content="Done.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [mut1, mut2, text]
            result = await service.compose("Update metadata", [], state, session_id=session_id)

        assert result.state.metadata.name == "Y"


class TestComposeTimeout:
    """Tests for the server-side compose timeout (F1)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("error_class", "expected_failure_class", "expected_attempts"),
        (("ServiceUnavailableError", "unavailable", 2), ("Timeout", "unavailable", 1), ("APIError", "unavailable", 2)),
    )
    async def test_end_advisor_classifies_real_sdk_failure_by_type(
        self, monkeypatch: pytest.MonkeyPatch, error_class: str, expected_failure_class: str, expected_attempts: int
    ) -> None:
        from litellm.exceptions import APIError, ServiceUnavailableError, Timeout

        sdk_error = (
            APIError(status_code=503, message="provider secret", llm_provider="test", model="test/advisor")
            if error_class == "APIError"
            else {"ServiceUnavailableError": ServiceUnavailableError, "Timeout": Timeout}[error_class](
                message="provider secret", llm_provider="test", model="test/advisor"
            )
        )
        service, _ = _composer_service_with_session(_mock_catalog(), _make_settings())
        checkpoint_persist = AsyncMock(spec=persist_advisor_checkpoint_pass)
        monkeypatch.setattr("elspeth.web.composer.advisor_checkpoint.persist_advisor_checkpoint_pass", checkpoint_persist)
        physical_calls = 0

        async def fail(*_args: object, on_provider_dispatch: Callable[[], None] | None = None, **_kwargs: object) -> Any:
            nonlocal physical_calls
            physical_calls += 1
            if on_provider_dispatch is not None:
                on_provider_dispatch()
            raise sdk_error

        with patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", side_effect=fail):
            verdict = await _REAL_RUN_ADVISOR_CHECKPOINT(
                service._advisor_checkpoint, phase="end", state=_empty_state(), session_id=None, recorder=None
            )

        assert physical_calls == expected_attempts
        assert verdict.failure_class == expected_failure_class
        assert verdict.findings_text == (
            "advisor model was unavailable" if expected_failure_class == "unavailable" else "advisor response was malformed"
        )
        assert "provider secret" not in repr(verdict)
        assert checkpoint_persist.await_count == 1
        assert checkpoint_persist.await_args.kwargs["record"].provider_attempts == expected_attempts

    @pytest.mark.asyncio
    async def test_end_advisor_first_party_same_named_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = _composer_service_with_session(_mock_catalog(), _make_settings())
        checkpoint_persist = AsyncMock(spec=persist_advisor_checkpoint_pass)
        monkeypatch.setattr("elspeth.web.composer.advisor_checkpoint.persist_advisor_checkpoint_pass", checkpoint_persist)
        impostor_class = type("ServiceUnavailableError", (Exception,), {})
        impostor = impostor_class("first-party defect")
        with (
            patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", side_effect=impostor) as advisor,
            pytest.raises(impostor_class) as raised,
        ):
            await _REAL_RUN_ADVISOR_CHECKPOINT(
                service._advisor_checkpoint, phase="end", state=_empty_state(), session_id=None, recorder=None
            )

        assert raised.value is impostor
        assert advisor.await_count == 1
        checkpoint_persist.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_end_advisor_generic_api_error_with_outage_name_reports_unavailable(self) -> None:
        """A provider API failure establishes unavailability, without response evidence."""
        service, _ = _composer_service_with_session(_mock_catalog(), _make_settings())
        named_generic_api_error = type("ServiceUnavailableError", (LiteLLMAPIError,), {})
        failure = named_generic_api_error(status_code=503, message="provider secret", llm_provider="test", model="test/advisor")

        with patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", side_effect=failure):
            verdict = await _REAL_RUN_ADVISOR_CHECKPOINT(
                service._advisor_checkpoint, phase="end", state=_empty_state(), session_id=None, recorder=None
            )

        assert verdict.failure_class == "unavailable"
        assert verdict.findings_text == "advisor model was unavailable"
        assert "provider secret" not in repr(verdict)

    @pytest.mark.asyncio
    async def test_timeout_raises_convergence_error(self) -> None:
        """Exceeding composer_timeout_seconds raises ComposerConvergenceError
        with budget_exhausted='timeout'."""
        import asyncio

        catalog = _mock_catalog()
        settings = _make_settings(composer_timeout_seconds=0.1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        async def slow_llm(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(1.0)
            return _make_llm_response(content="Too late.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = slow_llm
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Slow pipeline", [], state, session_id=session_id)
            assert exc_info.value.budget_exhausted == "timeout"

    @pytest.mark.asyncio
    async def test_advisor_zero_attempt_deadline_uses_compose_timeout_contract(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An expired compose budget is not a fabricated advisor outage."""
        import asyncio

        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(),
            settings=_make_settings(composer_timeout_seconds=0.005),
        )
        service._advisor_checkpoint._run_advisor_checkpoint = _REAL_RUN_ADVISOR_CHECKPOINT.__get__(
            service._advisor_checkpoint, AdvisorCheckpointOwner
        )  # type: ignore[method-assign]
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv"},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        progress_events: list[ComposerProgressEvent] = []
        checkpoint_persist = AsyncMock(spec=persist_advisor_checkpoint_pass)
        monkeypatch.setattr("elspeth.web.composer.advisor_checkpoint.persist_advisor_checkpoint_pass", checkpoint_persist)

        async def terminal_response_after_deadline(*_args: object, **_kwargs: object) -> Any:
            await asyncio.sleep(0.01)
            return _make_llm_response(content="The pipeline is ready.")

        async def record_progress(event: ComposerProgressEvent) -> None:
            progress_events.append(event)

        with (
            patch.object(service, "_call_llm_before_deadline", new_callable=AsyncMock, side_effect=terminal_response_after_deadline),
            patch.object(service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", new_callable=AsyncMock) as advisor_call,
            pytest.raises(ComposerConvergenceError) as exc_info,
        ):
            await service.compose("Review this pipeline", [], state, progress=record_progress, session_id=session_id)

        assert advisor_call.await_count == 0
        checkpoint_persist.assert_not_awaited()
        assert exc_info.value.budget_exhausted == "timeout"
        assert exc_info.value.reason == "convergence_wall_clock_timeout"
        assert exc_info.value.llm_calls == ()
        assert exc_info.value.tool_invocations == ()
        assert "advisor model was unavailable after retry" not in str(exc_info.value).lower()
        failed_events = [event for event in progress_events if event.phase == "failed"]
        assert len(failed_events) == 1
        assert failed_events[0].reason == "convergence_wall_clock_timeout"
        assert "timed out" in failed_events[0].headline.lower()

    @pytest.mark.asyncio
    async def test_early_advisor_zero_attempt_deadline_preserves_mutation_audit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EARLY expiry publishes the completed tool turn before timeout."""
        import time

        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(),
            settings=_make_settings(composer_timeout_seconds=1.0),
        )
        service._advisor_checkpoint._run_advisor_checkpoint = _REAL_RUN_ADVISOR_CHECKPOINT.__get__(
            service._advisor_checkpoint, AdvisorCheckpointOwner
        )  # type: ignore[method-assign]
        checkpoint_persist = AsyncMock(spec=persist_advisor_checkpoint_pass)
        monkeypatch.setattr("elspeth.web.composer.advisor_checkpoint.persist_advisor_checkpoint_pass", checkpoint_persist)
        source_arguments = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "/data/input.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        }

        def mutate_source(
            _tool_name: str,
            _arguments: dict[str, Any],
            current_state: CompositionState,
            _catalog: Any,
            **_kwargs: Any,
        ) -> ToolResult:
            updated_state = current_state.with_source(SourceSpec(**source_arguments))
            # The mutation completes after expiry; the advisor must still retain its audit.
            time.sleep(1.01)
            return ToolResult(
                success=True,
                updated_state=updated_state,
                validation=updated_state.validate(),
                affected_nodes=("source",),
                data=None,
            )

        async def mutation_response(*_args: object, **_kwargs: object) -> Any:
            return _make_llm_response(
                tool_calls=[
                    {
                        "id": "source-1",
                        "name": "set_source",
                        "arguments": source_arguments,
                    }
                ]
            )

        with (
            patch.object(service, "_call_llm_before_deadline", new_callable=AsyncMock, side_effect=mutation_response),
            patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", new_callable=AsyncMock) as advisor_call,
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=mutate_source),
            pytest.raises(ComposerConvergenceError) as exc_info,
        ):
            await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

        assert advisor_call.await_count == 0
        checkpoint_persist.assert_not_awaited()
        assert exc_info.value.budget_exhausted == "timeout"
        assert exc_info.value.llm_calls == ()
        # Durable P4 audit must not be replayed from the exception payload.
        assert exc_info.value.tool_invocations == ()
        sessions = service._require_sessions_service()
        with sessions._engine.connect() as conn:
            tool_rows = conn.execute(
                select(chat_messages_table.c.tool_call_id, chat_messages_table.c.composition_state_id).where(
                    chat_messages_table.c.session_id == session_id,
                    chat_messages_table.c.role == "tool",
                )
            ).all()
        assert len(tool_rows) == 1
        assert tool_rows[0].tool_call_id == "source-1"
        assert tool_rows[0].composition_state_id is not None
        current = await sessions.get_current_state(UUID(session_id))
        assert current is not None
        assert current.sources["source"]["plugin"] == "csv"
        assert exc_info.value.partial_state is not None
        assert exc_info.value.partial_state.sources["source"].plugin == "csv"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error_type", [AuditIntegrityError, FrameworkBugError, RuntimeError])
    async def test_early_advisor_internal_failure_preserves_completed_tool_audit(
        self, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
    ) -> None:
        """A failed advisor cannot discard a tool mutation already executed."""
        service, session_id = _composer_service_with_session(_mock_catalog(), _make_settings())
        sessions = service._require_sessions_service()
        await sessions.update_composer_preferences(UUID(session_id), trust_mode="auto_commit", density_default="high", actor="user:test")
        monkeypatch.setattr(
            service._advisor_checkpoint,
            "_run_advisor_checkpoint",
            _REAL_RUN_ADVISOR_CHECKPOINT.__get__(service._advisor_checkpoint, AdvisorCheckpointOwner),
        )
        failure = error_type("advisor internal failure")
        checkpoint_persist = AsyncMock(spec=persist_advisor_checkpoint_pass)
        monkeypatch.setattr("elspeth.web.composer.advisor_checkpoint.persist_advisor_checkpoint_pass", checkpoint_persist)
        source_arguments = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": "/data/input.csv", "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        }

        def mutate_source(
            _tool_name: str, _arguments: dict[str, Any], current_state: CompositionState, _catalog: Any, **_kwargs: Any
        ) -> ToolResult:
            updated_state = current_state.with_source(SourceSpec(**source_arguments))
            return ToolResult(
                success=True, updated_state=updated_state, validation=updated_state.validate(), affected_nodes=("source",), data=None
            )

        with (
            patch.object(
                service,
                "_call_llm_before_deadline",
                new_callable=AsyncMock,
                return_value=_make_llm_response(tool_calls=[{"id": "source-1", "name": "set_source", "arguments": source_arguments}]),
            ),
            patch.object(service._advisor_checkpoint, "_call_advisor_with_audit", new_callable=AsyncMock, side_effect=failure) as advisor,
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=mutate_source),
            pytest.raises(error_type) as raised,
        ):
            await service.compose("Build a CSV pipeline", [], _empty_state(), session_id=session_id)

        assert raised.value is failure
        assert advisor.await_count == 1
        checkpoint_persist.assert_not_awaited()
        engine = service._session_engine
        assert engine is not None
        with engine.connect() as conn:
            rows = conn.execute(
                select(chat_messages_table.c.role, chat_messages_table.c.tool_call_id, chat_messages_table.c.composition_state_id).where(
                    chat_messages_table.c.session_id == session_id
                )
            ).all()
        tool_rows = [row for row in rows if row.role == "tool"]
        assert len(tool_rows) == 1
        assert tool_rows[0].tool_call_id == "source-1"
        assert tool_rows[0].composition_state_id is not None
        assert any(row.role == "assistant" for row in rows)
        current = await sessions.get_current_state(UUID(session_id))
        assert current is not None
        assert current.sources["source"]["plugin"] == "csv"

    @pytest.mark.asyncio
    async def test_mutation_tool_state_preserved_on_timeout(self) -> None:
        """Mutation tools that complete before timeout must have their
        state reflected in partial_state.

        Regression test for the cancel-safety concern: with cooperative
        timeout, the deadline is checked AFTER tool execution completes,
        so side effects and state publication are never split. The
        partial_state must include the mutation that completed.
        """
        import time

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        call_count = 0

        def _slow_mutation_tool(
            _tool_name: str,
            _arguments: dict[str, Any],
            current_state: CompositionState,
            _catalog: Any,
            **kwargs: Any,
        ) -> ToolResult:
            # Simulate a blob mutation that takes time
            time.sleep(0.2)
            from elspeth.web.composer.state import SourceSpec

            new_state = current_state.with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="out",
                    options={"path": "/data/blobs/f.csv", "schema": {"mode": "observed"}},
                    on_validation_failure="quarantine",
                )
            )
            return ToolResult(
                success=True,
                updated_state=new_state,
                validation=new_state.validate(),
                affected_nodes=("source",),
                data=None,
            )

        async def first_tool_then_timeout_llm(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: return tool call (fast)
                return _make_llm_response(
                    tool_calls=[
                        {
                            "id": "c1",
                            "name": "set_source",
                            "arguments": {
                                "plugin": "csv",
                                "on_success": "out",
                                "options": {"path": "/data/blobs/f.csv", "schema": {"mode": "observed"}},
                                "on_validation_failure": "quarantine",
                            },
                        }
                    ],
                )
            # Second call: exercise _call_llm_before_deadline's timeout
            # capture path without depending on wall-clock scheduling.
            raise TimeoutError

        with (
            patch.object(service._provider_gateway, "_call_llm", new=first_tool_then_timeout_llm),
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=_slow_mutation_tool,
            ),
            pytest.raises(ComposerConvergenceError) as exc_info,
        ):
            await service.compose("Build pipeline", [], state, session_id=session_id)

        assert exc_info.value.budget_exhausted == "timeout"
        assert call_count == 2
        # The mutation tool completed BEFORE the timeout fired on the
        # second LLM call.  With cooperative timeout, partial_state must
        # reflect the completed mutation.
        assert exc_info.value.partial_state is not None, (
            "partial_state is None — mutation tool's state was lost on timeout. "
            "This is the cancel-safety regression: side effects committed but "
            "state was not published."
        )
        assert exc_info.value.partial_state.sources["source"] is not None
        assert exc_info.value.partial_state.sources["source"].plugin == "csv"


class TestConvergenceProgressDispatch:
    """Each ComposerConvergenceError sub-cause must surface a discriminated
    progress event through the sink — covering the contract that fixes
    elspeth-5030f7373d.

    The original symptom: wall-clock timeout, mutation-turn budget, and
    discovery-turn budget all collapsed into one generic ``phase: failed``
    event with the same headline / evidence / likely_next text. These tests
    end-to-end through ``compose()`` to confirm the sink receives the
    discriminated event for each budget value.
    """

    @pytest.mark.asyncio
    async def test_composition_budget_emits_distinct_failure_event(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_composition_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        progress_events: list[ComposerProgressEvent] = []

        async def record_progress(event: ComposerProgressEvent) -> None:
            progress_events.append(event)

        mut1 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "x"}},
                }
            ],
        )
        mut2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "y"}},
                }
            ],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [mut1, mut2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("loop forever", [], state, progress=record_progress, session_id=session_id)
        assert exc_info.value.budget_exhausted == "composition"

        failed_events = [e for e in progress_events if e.phase == "failed"]
        assert len(failed_events) == 1
        assert failed_events[0].reason == "convergence_composition_budget"
        assert "mutation turn budget" in failed_events[0].headline.lower()

    @pytest.mark.asyncio
    async def test_discovery_budget_emits_distinct_failure_event(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_discovery_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        progress_events: list[ComposerProgressEvent] = []

        async def record_progress(event: ComposerProgressEvent) -> None:
            progress_events.append(event)

        disc1 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
        )
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "list_transforms", "arguments": {}}],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [disc1, disc2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("discover forever", [], state, progress=record_progress, session_id=session_id)
        assert exc_info.value.budget_exhausted == "discovery"

        failed_events = [e for e in progress_events if e.phase == "failed"]
        assert len(failed_events) == 1
        assert failed_events[0].reason == "convergence_discovery_budget"
        assert "discovery turn budget" in failed_events[0].headline.lower()

    @pytest.mark.asyncio
    async def test_wall_clock_timeout_emits_distinct_failure_event(self) -> None:
        import asyncio

        catalog = _mock_catalog()
        settings = _make_settings(composer_timeout_seconds=0.1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        progress_events: list[ComposerProgressEvent] = []

        async def record_progress(event: ComposerProgressEvent) -> None:
            progress_events.append(event)

        async def slow_llm(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(1.0)
            return _make_llm_response(content="Too late.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = slow_llm
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("slow pipeline", [], state, progress=record_progress, session_id=session_id)
        assert exc_info.value.budget_exhausted == "timeout"

        failed_events = [e for e in progress_events if e.phase == "failed"]
        assert len(failed_events) == 1
        assert failed_events[0].reason == "convergence_wall_clock_timeout"
        assert "timed out" in failed_events[0].headline.lower()

    @pytest.mark.asyncio
    async def test_three_sub_causes_produce_three_distinct_progress_reasons(self) -> None:
        """Regression guard against UX-text collapse — the three convergence
        sub-causes must surface three distinct reason codes via the sink."""
        import asyncio
        import contextlib

        async def collect_failure_reason(
            settings: Any,
            llm_side_effect: Any,
        ) -> str | None:
            catalog = _mock_catalog()
            service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
            state = _empty_state()
            events: list[ComposerProgressEvent] = []

            async def record(event: ComposerProgressEvent) -> None:
                events.append(event)

            with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
                mock_llm.side_effect = llm_side_effect
                with contextlib.suppress(ComposerConvergenceError):
                    await service.compose("x", [], state, progress=record, session_id=session_id)
            failed = [e for e in events if e.phase == "failed"]
            return failed[-1].reason if failed else None

        composition_reason = await collect_failure_reason(
            _make_settings(composer_max_composition_turns=1),
            [
                _make_llm_response(tool_calls=[{"id": "c1", "name": "set_metadata", "arguments": {"patch": {"name": "x"}}}]),
                _make_llm_response(tool_calls=[{"id": "c2", "name": "set_metadata", "arguments": {"patch": {"name": "y"}}}]),
            ],
        )
        discovery_reason = await collect_failure_reason(
            _make_settings(composer_max_discovery_turns=1),
            [
                _make_llm_response(tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}]),
                _make_llm_response(tool_calls=[{"id": "c2", "name": "list_transforms", "arguments": {}}]),
            ],
        )

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(1.0)
            return _make_llm_response(content="late")

        timeout_reason = await collect_failure_reason(
            _make_settings(composer_timeout_seconds=0.1),
            slow,
        )

        codes = {composition_reason, discovery_reason, timeout_reason}
        assert len(codes) == 3, (
            "Convergence sub-causes collapsed into fewer than three distinct progress reasons "
            "— elspeth-5030f7373d regression. Got: " + repr(codes)
        )


class TestPartialStatePreservation:
    """Tests for partial state preservation on convergence failure (F2)."""

    @pytest.mark.asyncio
    async def test_convergence_includes_partial_state_when_mutated(self) -> None:
        """When mutations occurred before convergence failure,
        partial_state is attached to the exception."""
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_composition_turns=1)
        service, session_id = _composer_service_with_session(catalog, settings)
        state = _empty_state()

        # Turn 1: mutation (set_source) — composition budget exhausted (1/1)
        mut = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "t1",
                        "options": {
                            "path": f"/data/blobs/{session_id}/input.csv",
                            "schema": {"mode": "observed"},
                        },
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        # Bonus call also returns tool calls — convergence error
        mut2 = _make_llm_response(
            tool_calls=[
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "nope"}},
                }
            ],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [mut, mut2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Build pipeline", [], state, session_id=session_id)

            assert exc_info.value.partial_state is not None
            assert exc_info.value.partial_state.sources["source"] is not None
            assert exc_info.value.partial_state.sources["source"].plugin == "csv"
            assert exc_info.value.partial_state.version == 2

    @pytest.mark.asyncio
    async def test_convergence_no_partial_state_when_no_mutations(self) -> None:
        """When no mutations occurred, partial_state is None."""
        catalog = _mock_catalog()
        settings = _make_settings(composer_max_discovery_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        disc1 = _make_llm_response(
            tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
        )
        disc2 = _make_llm_response(
            tool_calls=[{"id": "c2", "name": "list_transforms", "arguments": {}}],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.side_effect = [disc1, disc2]
            with pytest.raises(ComposerConvergenceError) as exc_info:
                await service.compose("Just looking", [], state, session_id=session_id)

            assert exc_info.value.partial_state is None


class TestComposerSamplingConfig:
    """Composer LLM sampling must come from operator config.

    Default ``None`` means omit the provider parameter. Configured values are
    sent verbatim and recorded on each ComposerLLMCall, so a reviewer can
    correlate failures with the precise sampling regime.
    """

    @pytest.mark.asyncio
    async def test_freeform_tool_and_prose_calls_disable_hidden_sdk_retries(
        self, _sampling_required_guard: tuple[PromptGuardSettings, list[dict[str, Any]]]
    ) -> None:
        guard_settings, guard_requests = _sampling_required_guard
        service, session_id = _composer_service_with_session(
            catalog=_mock_catalog(),
            settings=_make_settings(
                composer_prompt_guard_mode="required",
                composer_prompt_guard=guard_settings,
                composer_advisor_prompt_guard=guard_settings,
            ),
        )
        completion = _make_raw_llm_response(content="acknowledged")

        with (
            _sampling_provider_owner(service, session_id, expected_calls=2, expected_guard_mode="required") as (owner, recorder),
            prompt_guard_user_scope("Hello"),
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=completion) as mock_acomp,
        ):
            await service._provider_gateway._call_llm_with_audit(
                [{"role": "user", "content": "Hello"}],
                [],
                timeout=5.0,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            )
            await service._provider_gateway._call_text_llm_with_audit(
                [{"role": "user", "content": "Hello"}],
                timeout=5.0,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            )

        assert mock_acomp.call_count == 2
        for call in mock_acomp.call_args_list:
            assert call.kwargs["num_retries"] == 0
            assert call.kwargs["max_retries"] == 0
        assert guard_requests == [
            {"userPrompt": "Hello", "documents": []},
            {"userPrompt": "", "documents": ['{"content":"Hello","role":"user"}']},
        ]

    @pytest.mark.asyncio
    async def test_call_llm_sends_configured_temperature_and_seed(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings(composer_temperature=0.0, composer_seed=42)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Single text-only response converges the loop immediately.
        completion = _make_raw_llm_response(content="acknowledged")

        # Service may reject because no pipeline was built; we only
        # care about what reached LiteLLM on the first (and possibly only) call.
        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                return_value=completion,
            ) as mock_acomp,
            contextlib.suppress(ComposerServiceError),
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        assert mock_acomp.call_count >= 1
        first_call_kwargs = mock_acomp.call_args_list[0].kwargs
        assert first_call_kwargs["temperature"] == 0.0
        assert first_call_kwargs["seed"] == 42

    @pytest.mark.asyncio
    async def test_call_llm_omits_sampling_when_operator_leaves_it_unset(
        self, _sampling_required_guard: tuple[PromptGuardSettings, list[dict[str, Any]]]
    ) -> None:
        guard_settings, guard_requests = _sampling_required_guard
        catalog = _mock_catalog()
        settings = _make_settings(
            composer_model="anthropic/claude-3-5-sonnet-20241022",
            composer_prompt_guard_mode="required",
            composer_prompt_guard=guard_settings,
            composer_advisor_prompt_guard=guard_settings,
        )
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        completion = _make_raw_llm_response(content="acknowledged")

        with (
            _sampling_provider_owner(service, session_id, expected_calls=1, expected_guard_mode="required") as (owner, recorder),
            prompt_guard_user_scope("Hello"),
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=completion) as mock_acomp,
        ):
            await service._provider_gateway._call_llm_with_audit(
                [{"role": "user", "content": "Hello"}],
                [],
                timeout=5.0,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            )

        kwargs = mock_acomp.call_args_list[0].kwargs
        assert "temperature" not in kwargs
        assert "seed" not in kwargs
        assert guard_requests == [{"userPrompt": "Hello", "documents": ['{"cache_control":{"type":"ephemeral"}}']}]

    @pytest.mark.asyncio
    async def test_call_text_llm_sends_configured_temperature_and_seed(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings(composer_temperature=0.0, composer_seed=42)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)

        completion = _make_raw_llm_response(content="diagnostic text")

        with (
            _sampling_provider_owner(service, session_id, expected_calls=1) as (owner, recorder),
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=completion) as mock_acomp,
        ):
            await service._provider_gateway._call_text_llm_with_audit(
                [{"role": "user", "content": "explain"}],
                timeout=5.0,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            )

        assert mock_acomp.call_count == 1
        kwargs = mock_acomp.call_args_list[0].kwargs
        assert kwargs["temperature"] == 0.0
        assert kwargs["seed"] == 42

    @pytest.mark.asyncio
    async def test_call_text_llm_omits_sampling_when_operator_leaves_it_unset(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings(composer_model="anthropic/claude-3-5-sonnet-20241022")
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        completion = _make_raw_llm_response(content="diagnostic text")

        with (
            _sampling_provider_owner(service, session_id, expected_calls=1) as (owner, recorder),
            patch("litellm.acompletion", new_callable=AsyncMock, return_value=completion) as mock_acomp,
        ):
            await service._provider_gateway._call_text_llm_with_audit(
                [{"role": "user", "content": "explain"}],
                timeout=5.0,
                recorder=recorder,
                provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
            )

        kwargs = mock_acomp.call_args_list[0].kwargs
        assert "temperature" not in kwargs
        assert "seed" not in kwargs


class TestEmptyChoicesValidation:
    """Tier 3 boundary: LiteLLM can return empty choices."""

    @pytest.mark.asyncio
    async def test_empty_choices_raises_service_error(self) -> None:
        """LiteLLM returning empty choices must raise ComposerServiceError.

        Empty choices can occur on content-filter blocks, rate-limit
        responses, or malformed upstream responses.  Without validation,
        this causes an IndexError at response.choices[0].message.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Patch the gateway transport so admission runs on the malformed raw response.
        empty_response = FakeLLMResponse(choices=[])
        with (
            patch(
                "elspeth.web.composer.provider_gateway._litellm_acompletion",
                new_callable=AsyncMock,
                return_value=empty_response,
            ),
            pytest.raises(ComposerServiceError, match="empty choices"),
        ):
            await service.compose("Hello", [], state, session_id=session_id)

    @pytest.mark.asyncio
    async def test_empty_choices_on_bonus_turn_raises_service_error(self) -> None:
        """Empty choices on the bonus turn (budget exhaustion) also raises.

        The bonus turn at composition budget exhaustion goes through the
        same _call_llm() path, so the validation protects both sites.
        """
        catalog = _mock_catalog()
        # Budget of 1 composition turn — first mutation exhausts it,
        # then the bonus _call_llm returns empty choices.
        settings = _make_settings(composer_max_composition_turns=1)
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # First call: valid response with a mutation tool call
        mutation_call = _make_raw_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        # Second call (bonus turn): empty choices
        empty_response = FakeLLMResponse(choices=[])

        with (
            patch(
                "elspeth.web.composer.provider_gateway._litellm_acompletion",
                new_callable=AsyncMock,
                side_effect=[mutation_call, empty_response],
            ) as mock_acomp,
            pytest.raises(ComposerServiceError, match="empty choices"),
        ):
            await service.compose("Setup CSV", [], state, session_id=session_id)

        # Confirm both LLM calls happened — the error is from the bonus
        # turn (second call), not from a tool handler fault on the first.
        assert mock_acomp.call_count == 2


@contextlib.contextmanager
def _mock_composer_retry_sleep() -> Iterator[AsyncMock]:
    """Observe service backoff without replacing worker or SDK scheduler sleeps."""
    retry_sleep = AsyncMock(spec=asyncio.sleep)
    retry_asyncio = SimpleNamespace(**(vars(asyncio) | {"sleep": retry_sleep}))
    with patch("elspeth.web.composer.service.asyncio", new=retry_asyncio):
        yield retry_sleep


class TestComposerAvailabilityAndBadRequest:
    """Readiness and LiteLLM bad-request failures are normalized by the service."""

    @pytest.mark.asyncio
    async def test_unavailable_composer_short_circuits_before_llm_call(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        service._availability = ComposerAvailability(
            available=False,
            model="bad-model",
            provider=None,
            reason="Composer model bad-model is unavailable.",
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            pytest.raises(ComposerServiceError, match="bad-model is unavailable"),
        ):
            mock_llm.return_value = _make_llm_response(content="unexpected")
            await service.compose("Hello", [], _empty_state())

        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_litellm_bad_request_raises_redacted_service_error(self) -> None:
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        bad_request = LiteLLMBadRequestError(
            message="bad model leaked-detail",
            model="bad-model",
            llm_provider="bad-provider",
        )

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=bad_request,
            ),
            pytest.raises(ComposerServiceError) as exc_info,
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        assert str(exc_info.value) == "LLM request rejected (BadRequestError)"
        assert "leaked-detail" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_bedrock_bad_request_raises_redacted_service_error(self) -> None:
        """Bedrock provider detail stays off every default/public surface."""
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
        from structlog.testing import capture_logs

        from elspeth.web.composer.provider_gateway import _BadRequestLLMError

        catalog = _mock_catalog()
        settings = _make_settings(composer_model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        access_key = "AKIA" + ("Z" * 16)
        sentinels = (
            "BEDROCK-RAW-DETAIL-SENTINEL",
            "arn:aws:bedrock:us-east-1:123456789012:inference-profile/example",
            "123456789012",
            "bedrock-request-7d3c2b1a",
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/example/invoke",
            access_key,
        )
        bad_request = LiteLLMBadRequestError(
            message=" ".join(sentinels),
            model="bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0",
            llm_provider="bedrock",
        )

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=bad_request,
            ),
            capture_logs() as captured_logs,
            pytest.raises(_BadRequestLLMError) as exc_info,
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        exc = exc_info.value
        assert str(exc) == "LLM request rejected (BadRequestError)"
        public_surfaces = [str(exc), repr(exc), repr(captured_logs)]
        llm_calls = exc.__dict__["llm_calls"]
        assert isinstance(llm_calls, tuple)
        assert len(llm_calls) == 1
        public_surfaces.extend(
            [
                llm_calls[0].error_class or "",
                llm_calls[0].error_message or "",
            ]
        )
        for sentinel in sentinels:
            assert all(sentinel not in surface for surface in public_surfaces)

        assert llm_calls[0].error_class == "BadRequestError"
        assert llm_calls[0].error_message == "BadRequestError"
        # Raw detail remains available only through the existing explicit
        # operator-facing opt-in path; it is not part of default rendering.
        assert exc.provider_detail is not None
        assert all(sentinel in exc.provider_detail for sentinel in sentinels)

    @pytest.mark.asyncio
    async def test_bad_request_llm_error_preserves_provider_detail(self) -> None:
        """``_BadRequestLLMError`` carries the underlying provider message.

        The wrap message (``str(exc)``) is intentionally redacted so the
        route layer does not leak provider details by default. The route
        layer's ``expose_provider_error=True`` path re-uses
        ``provider_detail`` to surface the raw message after scrubbing.

        Ticket: elspeth-9f7f9d5787.
        """
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        from elspeth.web.composer.provider_gateway import _BadRequestLLMError

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        bad_request = LiteLLMBadRequestError(
            message="provider says bad",
            model="gpt-4o",
            llm_provider="openai",
        )

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=bad_request,
            ),
            pytest.raises(_BadRequestLLMError) as exc_info,
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        # str(exc) unchanged from the existing redacted wrap message.
        assert str(exc_info.value) == "LLM request rejected (BadRequestError)"
        # New attributes carry the provider detail for the route layer.
        assert exc_info.value.provider_detail is not None
        assert "provider says bad" in exc_info.value.provider_detail
        assert exc_info.value.provider_status_code == 400

    @pytest.mark.asyncio
    async def test_bad_request_llm_error_empty_message_collapses_to_none(self) -> None:
        """Empty-string provider messages collapse to ``None`` so callers can
        distinguish "no detail" from "empty string" via truthiness checks.
        """
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        from elspeth.web.composer.provider_gateway import _BadRequestLLMError

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        # LiteLLM's BadRequestError formats the message into a longer string;
        # passing an empty message still yields a non-empty rendered str.
        # To exercise the empty-collapse path, raise the exception type
        # directly with an empty rendered form.
        bad_request = LiteLLMBadRequestError(message="", model="m", llm_provider="p")
        # Force str(exc) == "" so the ``or None`` collapse can fire.
        bad_request.args = ("",)
        bad_request.message = ""

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=bad_request,
            ),
            pytest.raises(_BadRequestLLMError) as exc_info,
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        # str(bad_request) is "" so provider_detail should collapse to None.
        # provider_status_code is still set from the .status_code attribute.
        assert exc_info.value.provider_detail is None
        assert exc_info.value.provider_status_code == 400

    @pytest.mark.asyncio
    async def test_litellm_api_error_is_retried_before_unavailable(self) -> None:
        from litellm.exceptions import APIError as LiteLLMAPIError

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        transient_error = LiteLLMAPIError(
            status_code=503,
            message="provider temporarily unavailable leaked-detail",
            model="openrouter/openai/gpt-5.5",
            llm_provider="openrouter",
        )
        success = _make_raw_llm_response(content="Recovered.")

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=[transient_error, success],
            ) as mock_llm,
            _mock_composer_retry_sleep() as mock_sleep,
        ):
            result = await service.compose("Hello", [], state, session_id=session_id)

        _assert_checkpoint_reply(result, "Recovered.", count=0)
        assert mock_llm.call_count == 2
        mock_sleep.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_real_service_unavailable_is_eligible_for_bounded_retry(self) -> None:
        from litellm.exceptions import ServiceUnavailableError

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        failure = ServiceUnavailableError(message="private upstream response", llm_provider="openrouter", model="openrouter/openai/gpt-5.5")
        with (
            patch(
                "litellm.acompletion", new_callable=AsyncMock, side_effect=[failure, _make_raw_llm_response(content="Recovered.")]
            ) as mock_llm,
            _mock_composer_retry_sleep() as mock_sleep,
        ):
            result = await service.compose("Hello", [], _empty_state(), session_id=session_id)

        _assert_checkpoint_reply(result, "Recovered.", count=0)
        assert mock_llm.call_count == 2
        mock_sleep.assert_awaited_once()
        mock_sleep.assert_awaited_once_with(1.0)

    @pytest.mark.asyncio
    async def test_real_bad_gateway_is_not_retried(self) -> None:
        from litellm.exceptions import BadGatewayError

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        failure = BadGatewayError(message="private upstream response", llm_provider="openrouter", model="openrouter/openai/gpt-5.5")
        with (
            patch("litellm.acompletion", new_callable=AsyncMock, side_effect=failure) as mock_llm,
            _mock_composer_retry_sleep() as mock_sleep,
            pytest.raises(BadGatewayError),
        ):
            await service.compose("Hello", [], _empty_state(), session_id=session_id)

        assert mock_llm.call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call_path", ("ordinary", "advisor", "text"))
    async def test_real_sdk_timeout_has_timeout_terminal_audit(self, call_path: str, monkeypatch) -> None:
        from litellm.exceptions import Timeout as LiteLLMTimeout

        from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus

        service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        recorder = BufferingRecorder()
        failure = LiteLLMTimeout(message="private timeout body", model="openrouter/openai/gpt-5.5", llm_provider="openrouter")

        async def fail_completion(*args, **kwargs):
            del args, kwargs
            raise failure

        if call_path == "ordinary":
            monkeypatch.setattr(service._provider_gateway, "_call_llm", fail_completion)
            call = service._provider_gateway._call_llm_with_audit([{"role": "user", "content": "Hello"}], [], timeout=5, recorder=recorder)
        else:
            monkeypatch.setattr("elspeth.web.composer.provider_gateway._litellm_acompletion", fail_completion)
            if call_path == "advisor":
                call = service._advisor_checkpoint._call_advisor_with_audit(
                    {
                        "trigger": "proactive_security_safety",
                        "problem_summary": "Need guidance.",
                        "recent_errors": [],
                        "attempted_actions": [],
                    },
                    recorder=recorder,
                )
            else:
                call = service._provider_gateway._call_text_llm_with_audit(
                    [{"role": "user", "content": "Explain this run"}], timeout=5, recorder=recorder
                )

        with pytest.raises(LiteLLMTimeout):
            await call
        assert len(recorder.llm_calls) == 1
        audited = recorder.llm_calls[0]
        assert audited.status is ComposerLLMCallStatus.TIMEOUT
        assert audited.error_class == "Timeout"
        assert audited.error_message == "Timeout"

    @pytest.mark.asyncio
    async def test_real_sdk_timeout_uses_existing_compose_timeout_outcome(self) -> None:
        from litellm.exceptions import Timeout as LiteLLMTimeout

        from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        failure = LiteLLMTimeout(message="private timeout body", model="openrouter/openai/gpt-5.5", llm_provider="openrouter")
        with (
            patch("litellm.acompletion", new_callable=AsyncMock, side_effect=failure) as mock_llm,
            _mock_composer_retry_sleep() as mock_sleep,
            pytest.raises(ComposerConvergenceError) as caught,
        ):
            await service.compose("Hello", [], _empty_state(), session_id=session_id)

        assert caught.value.budget_exhausted == "timeout"
        assert mock_llm.call_count == 1
        mock_sleep.assert_not_awaited()
        assert len(caught.value.llm_calls) == 1
        assert caught.value.llm_calls[0].status is ComposerLLMCallStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_bad_request_llm_error_is_not_retried(self) -> None:
        """Provider 400-class errors are deterministic — not transient — and
        must surface to the caller on the first attempt without consuming
        any retry budget. Pins the no-retry policy made mechanically visible
        in commit 361643809 (``except _BadRequestLLMError: raise`` between
        the ``LiteLLMAuthError`` and ``LiteLLMAPIError`` clauses). Without
        this negative-side assertion, a future refactor that either
        (a) changes ``_BadRequestLLMError`` to inherit from
        ``LiteLLMAPIError`` or (b) removes the explicit re-raise clause
        could silently re-route bad-requests through the retry loop and
        every existing positive-side test would still pass.
        """
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        from elspeth.web.composer.provider_gateway import _BadRequestLLMError

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        bad_request = LiteLLMBadRequestError(
            message="model gpt-foo does not exist",
            model="gpt-foo",
            llm_provider="openai",
        )

        with (
            patch(
                "litellm.acompletion",
                new_callable=AsyncMock,
                side_effect=bad_request,
            ) as mock_llm,
            _mock_composer_retry_sleep() as mock_sleep,
            pytest.raises(_BadRequestLLMError),
        ):
            await service.compose("Hello", [], state, session_id=session_id)

        # Pins the no-retry contract: exactly one provider call, and no
        # backoff sleep was awaited (the retry path's only observable
        # side-effect besides the call count).
        assert mock_llm.call_count == 1
        mock_sleep.assert_not_awaited()


class TestPluginBugCrashesFromToolExecution:
    """Plugin-internal TypeError/ValueError/UnicodeError must crash.

    The compose loop catches ONLY ToolArgumentError around execute_tool.
    Any other TypeError/ValueError/UnicodeError is a plugin bug — per
    docs/guides/data-trust-and-error-handling.md §Plugin Ownership, silently
    laundering a plugin bug as an LLM-argument error
    is worse than crashing, because the audit trail records a confident
    but wrong Tier-3 story.

    Mirrors test_internal_key_error_is_not_swallowed.
    """

    @pytest.mark.asyncio
    async def test_plugin_value_error_is_not_swallowed(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("invalid expression syntax — plugin bug"),
            ),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=session_id)
        # Crash on first tool call → no prior mutations → partial_state is None.
        assert exc_info.value.partial_state is None
        assert isinstance(exc_info.value.original_exc, ValueError)
        assert "plugin bug" in str(exc_info.value.original_exc)
        assert exc_info.value.exc_class == "ValueError"

    @pytest.mark.asyncio
    async def test_plugin_type_error_is_not_swallowed(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=TypeError("NoneType + int — plugin bug"),
            ),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=session_id)
        assert exc_info.value.partial_state is None
        assert isinstance(exc_info.value.original_exc, TypeError)
        assert "plugin bug" in str(exc_info.value.original_exc)
        assert exc_info.value.exc_class == "TypeError"

    @pytest.mark.asyncio
    async def test_plugin_unicode_error_is_not_swallowed(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "plugin bug"),
            ),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=session_id)
        assert exc_info.value.partial_state is None
        assert isinstance(exc_info.value.original_exc, UnicodeDecodeError)
        assert exc_info.value.exc_class == "UnicodeDecodeError"

    @pytest.mark.asyncio
    async def test_plugin_crash_after_successful_mutation_preserves_partial_state(
        self,
    ) -> None:
        """When a plugin crashes AFTER at least one prior mutation succeeded
        in the same request, ``ComposerPluginCrashError.partial_state`` MUST
        carry the accumulated post-mutation state so the route handler can
        persist it into composition_states.

        This closes the P1 regression flagged in review: the narrowed
        ``except`` in compose() used to re-raise bare exceptions without
        threading the loop-local ``state``, so any successful mutations
        prior to the crash were silently dropped and recompose restarted
        from the stale pre-request state.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()
        initial_version = state.version

        # Two tool calls in a single LLM turn: first succeeds (mutates
        # state), second raises a plugin-bug exception.
        two_calls = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                },
                {
                    "id": "c2",
                    "name": "set_metadata",
                    "arguments": {"patch": {"name": "after-mutation"}},
                },
            ],
        )

        mutated_state = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(name="after-mutation"),
            version=initial_version + 1,
        )
        successful_result = ToolResult(
            success=True,
            updated_state=mutated_state,
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
        )

        call_count = {"n": 0}

        def _fake_execute_tool(*args: Any, **kwargs: Any) -> ToolResult:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return successful_result
            raise ValueError("plugin bug: crash AFTER first mutation")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=_fake_execute_tool,
            ),
        ):
            mock_llm.return_value = two_calls
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=session_id)

        assert call_count["n"] == 2, "both tool calls should have been attempted"
        crash = exc_info.value
        assert crash.partial_state is not None, "partial_state MUST be populated when a mutation succeeded before the crash"
        assert crash.partial_state.version == initial_version + 1
        assert crash.partial_state.metadata.name == "after-mutation"
        assert isinstance(crash.original_exc, ValueError)
        assert crash.exc_class == "ValueError"

    @pytest.mark.asyncio
    async def test_tool_argument_error_is_caught_and_fed_to_llm(self) -> None:
        """Positive case: ToolArgumentError IS caught, error fed back for LLM retry."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        text = _make_llm_response(content="Got it, trying again.")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ToolArgumentError(
                    argument="plugin",
                    expected="a string",
                    actual_type="int",
                ),
            ),
        ):
            script = _ReconciliationLLMScript(valid_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Setup", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        assert isinstance(result, ComposerResult)
        second_call_messages = script.prompts[1]
        tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        error_payload = json.loads(tool_messages[0]["content"])
        assert "'plugin' must be a string, got int" in error_payload["error"]

    def test_tool_argument_error_subclass_cannot_leak_cause_to_llm(self) -> None:
        """A hostile ``__str__`` override cannot enter the prompt/audit path."""
        with pytest.raises(TypeError, match="does not support subclassing"):

            class LeakyToolArgumentError(ToolArgumentError):
                def __str__(self) -> str:
                    return f"{self.args[0]}: caused by {self.__cause__}"

    @pytest.mark.asyncio
    async def test_tool_argument_canary_is_absent_from_prompt_and_audit_channels(self) -> None:
        """Caller metadata is canonicalized before prompt and audit serialization."""
        from pydantic import BaseModel, ValidationError, field_validator

        canary = "TOOL_ARGUMENT_CHANNEL_CANARY_sk_live_4Pq8"
        loc_canary = "TOOL_ARGUMENT_LOC_CANARY_sk_live_9Vr6"

        class _HostileCauseModel(BaseModel):
            values: dict[str, int]
            secret: str

            @field_validator("secret")
            @classmethod
            def _reject_secret(cls, value: str) -> str:
                raise ValueError(f"rejected {canary} at /srv/private.key")

        cause_error: ValidationError | None = None
        try:
            _HostileCauseModel.model_validate(
                {
                    "values": {loc_canary: "not-an-int"},
                    "secret": "trigger",
                }
            )
        except ValidationError as cause:
            cause_error = cause
            raw_cause = json.dumps(cause.errors(), default=str)
            assert canary in raw_cause
            assert loc_canary in raw_cause
        else:
            raise AssertionError("model_validate should have raised")
        assert cause_error is not None

        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        state = _empty_state()
        tool_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        text = _make_llm_response(content="Got it.")
        leaky = ToolArgumentError(
            argument=f"field_{canary}",
            expected=f"the secret path /srv/{canary}",
            actual_type=f"ValueError({canary})",
        )
        leaky.__cause__ = cause_error
        BaseException.__setattr__(leaky, "safe_message", canary)
        BaseException.__setattr__(leaky, "args", (canary,))
        for private_name in ("_safe_argument", "_safe_expected", "_safe_actual_type", "_safe_code"):
            BaseException.__setattr__(leaky, private_name, canary)
        BaseException.__setattr__(leaky, "_tool_argument_error_sealed", False)

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=leaky),
        ):
            script = _ReconciliationLLMScript(tool_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Setup", [], state, session_id=session_id)

        _assert_reconciliation_turn(mock_llm, script)

        second_call_messages = script.prompts[1]
        prompt_blob = json.dumps(second_call_messages, sort_keys=True)
        audit_blob = json.dumps([invocation.to_dict() for invocation in result.tool_invocations], sort_keys=True)
        assert canary not in prompt_blob
        assert loc_canary not in prompt_blob
        all_prompts_blob = json.dumps(script.prompts, sort_keys=True)
        assert canary not in all_prompts_blob
        assert loc_canary not in all_prompts_blob
        assert canary not in audit_blob
        assert loc_canary not in audit_blob


class TestPluginCrashSessionPersistence:
    """Plugin-bug crash must leave a durable session-row breadcrumb.

    "No silent drops" for session records: a plugin crash that leaves
    the session in no recorded terminal state is as bad for audit
    integrity as the laundering behaviour this plan eliminates.

    Given the current sessions_table schema (no status / crashed_at /
    last_exc_class columns), the breadcrumb is a bump of updated_at.
    This test asserts that bump, plus the invariant that NO exception
    message leaks into any column. The follow-up archived issue tracks
    the schema migration that adds richer crash markers.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)

        self.session_id = str(uuid4())
        self.data_dir = tmp_path
        # Seed the sessions row with a DELIBERATELY OLD updated_at so the
        # crash-path bump is unambiguously distinguishable from the seed.
        self.seeded_at = datetime(2020, 1, 1, tzinfo=UTC)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")
            grant_test_pipeline_user(conn, identity_id="test-user")
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    trust_mode="auto_commit",
                    density_default="high",
                    created_at=self.seeded_at,
                    updated_at=self.seeded_at,
                )
            )

    @pytest.mark.asyncio
    async def test_plugin_crash_bumps_session_updated_at(self) -> None:
        from elspeth.web.sessions.models import sessions_table

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(self.engine, self.data_dir),
            session_engine=self.engine,
        )
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("plugin bug: /etc/secrets/bootstrap.key is bad"),
            ),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=self.session_id)
        # The underlying plugin exception is preserved on the wrapper.
        assert isinstance(exc_info.value.original_exc, ValueError)
        assert "plugin bug" in str(exc_info.value.original_exc)

        # Assertion 1: session row was touched on the crash path.
        with self.engine.begin() as conn:
            row = conn.execute(sessions_table.select().where(sessions_table.c.id == self.session_id)).one()

        # SQLite DateTime(timezone=True) strips tzinfo on read — normalize
        # both sides of the comparison to the same tz-naive representation.
        row_updated_at = row.updated_at
        if row_updated_at.tzinfo is None:
            seed_for_compare = self.seeded_at.replace(tzinfo=None)
        else:
            seed_for_compare = self.seeded_at
        assert row_updated_at > seed_for_compare, "crash path must bump updated_at as audit breadcrumb"

        # Assertion 2: NO column holds the exception message. Stringify
        # the entire row and verify secret fragments / class hints are
        # absent. This is the load-bearing audit-integrity invariant —
        # if a future refactor adds a 'last_error' column, the assertion
        # will catch any attempt to persist the raw message.
        row_text = " | ".join(str(v) for v in row._mapping.values())
        assert "plugin bug" not in row_text
        assert "/etc/secrets" not in row_text
        assert "ValueError" not in row_text

    @pytest.mark.asyncio
    async def test_crash_breadcrumb_failure_does_not_mask_plugin_bug(
        self,
    ) -> None:
        """If the fenced breadcrumb mutation raises a recoverable audit-path
        exception (SQLAlchemyError / OSError), slog.error fires and the
        original plugin-bug exception still propagates unchanged.

        Uses sqlalchemy.exc.OperationalError as the stand-in for a realistic
        DB-write failure (connection drop, locking timeout, disk I/O
        translated to SQLAlchemy layer). The catch at
        service.py ComposerServiceImpl.compose is narrowed to
        (SQLAlchemyError, OSError); substituting RuntimeError here would
        assert the wrong invariant because RuntimeError deliberately
        propagates past this catch (see the programmer-bug companion test).

        Two invariants asserted:
        1. The ORIGINAL ValueError reaches the caller (not the
           OperationalError from the persistence failure).
        2. slog.error is called with the `composer_crash_persistence_failed`
           event — guarantees that an accidental removal of Step 4a-pre's
           structlog import would be caught (without this assertion, a
           regression where slog.error silently fails as NameError would
           pass the test because the original exception still propagates).
        """
        from structlog.testing import capture_logs

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        sessions_service = _test_sessions_service(self.engine, self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=sessions_service,
            session_engine=self.engine,
        )
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        def fail_tool(*_args: Any, **_kwargs: Any) -> None:
            mutation.side_effect = OperationalError("UPDATE sessions", {}, Exception("db unavailable"))
            raise ValueError("original plugin bug")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=fail_tool,
            ),
            patch.object(
                sessions_service.session_operation_authority,
                "mutate",
                wraps=sessions_service.session_operation_authority.mutate,
            ) as mutation,
            capture_logs() as cap_logs,
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=self.session_id)
        # Original plugin exception survives the wrap.
        assert isinstance(exc_info.value.original_exc, ValueError)
        assert "original plugin bug" in str(exc_info.value.original_exc)

        # The crash-persistence-failure slog.error MUST fire. This closes
        # the regression risk where Step 4a-pre's structlog import is
        # accidentally removed — the method would then raise NameError
        # inside the except, masking the original ValueError.
        persistence_failure_events = [entry for entry in cap_logs if entry.get("event") == "composer_crash_persistence_failed"]
        assert len(persistence_failure_events) == 1, cap_logs
        event = persistence_failure_events[0]
        assert event["session_id"] == self.session_id
        assert event["original_exc_class"] == "ValueError"
        # audit_exc_class is the class of the *persistence* failure, not the
        # original plugin bug. Present so operators can distinguish "DB
        # write failed with IntegrityError" from "DB write failed with
        # OperationalError" without needing the traceback.
        assert event["audit_exc_class"] == "OperationalError"
        # No traceback / exception message fields — exc_info was deliberately
        # dropped from this slog call to prevent __cause__-chain secret
        # leakage into server logs.
        assert "exc_info" not in event
        assert "exception" not in event
        assert "stack_info" not in event
        # Exception messages MUST NOT appear anywhere in the structured
        # event (defense-in-depth against accidental re-addition of a
        # message= field in a future refactor).
        assert "original plugin bug" not in str(event)
        # The OperationalError carries its SQL statement and __cause__
        # ("db unavailable") — neither may appear in the structured event.
        assert "db unavailable" not in str(event)
        assert "UPDATE sessions" not in str(event)

    @pytest.mark.asyncio
    async def test_crash_breadcrumb_real_path_slog_emission(self) -> None:
        """Exercise the real fenced breadcrumb path (no patching of the
        authority).  If structlog is not imported in service.py, this test
        will surface the NameError that
        `test_crash_breadcrumb_failure_does_not_mask_plugin_bug` misses
        (because that test patches the mutation itself).

        The real mutation should succeed here (the sessions engine is live
        and the compose runs under a COMPOSE lease), so we assert the crash
        propagates without any persistence-failure slog event.
        """
        from structlog.testing import capture_logs

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(self.engine, self.data_dir),
            session_engine=self.engine,
        )
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("plugin bug"),
            ),
            capture_logs() as cap_logs,
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=self.session_id)
        assert isinstance(exc_info.value.original_exc, ValueError)
        assert "plugin bug" in str(exc_info.value.original_exc)

        # No persistence-failure event — the real path succeeded.
        persistence_failure_events = [entry for entry in cap_logs if entry.get("event") == "composer_crash_persistence_failed"]
        assert persistence_failure_events == [], cap_logs

    @pytest.mark.asyncio
    async def test_crash_breadcrumb_programmer_bug_propagates_past_catch(
        self,
    ) -> None:
        """Programmer-bug exceptions inside the fenced breadcrumb mutation
        MUST NOT be absorbed by the audit-cleanup catch in compose().

        This test is the guardrail for the narrowed catch at
        ComposerServiceImpl.compose: ``except (SQLAlchemyError, OSError,
        SessionOperationFenceLost)`` means AttributeError, TypeError,
        AssertionError, NameError and the like escape the handler.
        A future regression that re-widens the catch (e.g., "catch everything
        so audit never crashes the request") would silently pass the sibling
        ``test_crash_breadcrumb_failure_does_not_mask_plugin_bug``
        test because that path raises an audit-family exception. This test
        closes the loop by asserting AttributeError — a canonical Tier 1/2
        programmer bug — bubbles out of the compose() call unchanged, NOT
        wrapped as ComposerPluginCrashError and NOT logged as
        ``composer_crash_persistence_failed``.

        The original plugin-bug ValueError becomes the ``__context__`` of the
        escaping AttributeError because Python chains implicit exception
        context through the re-raise site; we do not assert on ``__context__``
        directly since that coupling is an implementation detail, but we do
        verify the headline exception type flipped from
        ComposerPluginCrashError to AttributeError.
        """
        from structlog.testing import capture_logs

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        sessions_service = _test_sessions_service(self.engine, self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=sessions_service,
            session_engine=self.engine,
        )
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("original plugin bug"),
            ),
            patch.object(
                sessions_service.session_operation_authority,
                "mutate",
                side_effect=AttributeError("sessions_table has no attribute 'c'"),
            ),
            capture_logs() as cap_logs,
        ):
            mock_llm.return_value = valid_call
            # AttributeError escapes the narrowed catch; the outer
            # ComposerPluginCrashError is never re-raised because the
            # audit-site AttributeError propagates first.
            with pytest.raises(AttributeError) as exc_info:
                await service.compose("Setup", [], state, session_id=self.session_id)

        assert "sessions_table" in str(exc_info.value)

        # No slog event — the catch did not fire, so the structured-logging
        # path was not reached. A regression that re-widens the catch would
        # cause this assertion to fail (the event would appear).
        persistence_failure_events = [entry for entry in cap_logs if entry.get("event") == "composer_crash_persistence_failed"]
        assert persistence_failure_events == [], cap_logs

    @pytest.mark.asyncio
    async def test_crash_breadcrumb_runs_off_event_loop(self) -> None:
        """The fenced breadcrumb mutation must execute in a worker thread,
        not on the event loop thread.

        ``authority.mutate`` performs a synchronous fenced transaction +
        UPDATE, which holds the GIL and (more importantly) blocks the
        asyncio event loop for the duration of the DB round-trip. Every
        other sync DB path in the compose flow is already offloaded
        through ``run_sync_in_worker``; the crash-path persistence was
        hoisted out of the main loop but not wrapped.

        Blast radius: a stalled persist blocks websocket heartbeats,
        rate-limit checks, and the per-session progress broadcasts for
        every concurrent request. Cold path, but the partial DoS
        matches the same class of regression that the tool-execution
        offloading test already guards against.
        """
        import threading

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        sessions_service = _test_sessions_service(self.engine, self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=sessions_service,
            session_engine=self.engine,
        )
        state = _empty_state()

        event_loop_thread = threading.current_thread()
        persist_thread: threading.Thread | None = None

        authority = sessions_service.session_operation_authority
        original_mutate = authority.mutate

        def capture_thread(context: Any, mutation: Any) -> Any:
            nonlocal persist_thread
            persist_thread = threading.current_thread()
            return original_mutate(context, mutation)

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("plugin bug"),
            ),
            patch.object(authority, "mutate", side_effect=capture_thread),
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError):
                await service.compose("Setup", [], state, session_id=self.session_id)

        assert persist_thread is not None, "the crash breadcrumb mutation was never called"
        assert persist_thread is not event_loop_thread, (
            "the crash breadcrumb mutation ran on the event loop thread — "
            "the synchronous fenced transaction blocks all concurrent "
            "requests. It must be offloaded via run_sync_in_worker(...)"
        )

    @pytest.mark.asyncio
    async def test_session_bound_compose_without_its_lease_never_writes_the_breadcrumb(self) -> None:
        """A session-bound compose without a COMPOSE context is refused
        before any provider call or crash breadcrumb write.

        The breadcrumb is a ``sessions`` write and every such write goes
        through the fenced authority (P4-D3 elspeth-f3bbe79753). Before D3
        this path fell back to an unfenced ``UPDATE sessions`` on the raw
        engine. Chargeable admission now rejects the missing authority
        with ``ComposerAdmissionRefused`` before the provider or tool can
        run. Nothing is logged as a crash-persistence failure, and the
        seeded ``updated_at`` stays untouched.

        The conftest lease adapter fences every legacy ``compose(session_id=
        ...)`` call, which is exactly the caller behaviour this test must
        NOT have, so the call goes to the real ``compose`` the adapter
        closed over. (``monkeypatch.undo()`` would also revert the
        environment fixtures the composer's availability depends on.)
        """
        import inspect

        from structlog.testing import capture_logs

        from elspeth.web.sessions.models import sessions_table

        adapter_closure = inspect.getclosurevars(ComposerServiceImpl.compose).nonlocals
        real_compose = adapter_closure.get("real_compose", ComposerServiceImpl.compose)

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(self.engine, self.data_dir),
            session_engine=self.engine,
        )
        state = _empty_state()

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=ValueError("plugin bug"),
            ),
            capture_logs() as cap_logs,
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerAdmissionRefused, match="session authority"):
                await real_compose(service, "Setup", [], state, session_id=self.session_id)

        mock_llm.assert_not_called()

        persistence_failure_events = [entry for entry in cap_logs if entry.get("event") == "composer_crash_persistence_failed"]
        assert persistence_failure_events == [], cap_logs

        with self.engine.begin() as conn:
            row = conn.execute(sessions_table.select().where(sessions_table.c.id == self.session_id)).one()
        row_updated_at = row.updated_at
        seeded = self.seeded_at.replace(tzinfo=None) if row_updated_at.tzinfo is None else self.seeded_at
        assert row_updated_at == seeded, "an unfenced compose must not bump updated_at"

    @pytest.mark.asyncio
    async def test_crash_breadcrumb_lost_fence_is_logged_and_never_written(self) -> None:
        """A COMPOSE fence lost by the time the crash arm runs refuses the write.

        This is the exception-set widening of P4-D3: the authority raises
        ``SessionOperationFenceLost`` when the context it is handed no
        longer holds the session fence, and the crash arm treats that like
        any other audit-path failure -- logged as
        ``composer_crash_persistence_failed`` with the fence-loss class,
        the original plugin crash still propagating, and NO ``updated_at``
        bump (nothing falls back to a raw-engine UPDATE).

        The loss is raised by the authority's ``mutate`` rather than by
        releasing a real lease mid-turn: the crashed tool's turn audit is
        persisted under the same fence BEFORE the crash arm runs, so a
        fence released inside the tool call is observed there first and
        never reaches the arm. The facet's own refusal of a context that
        does not hold the fence is pinned at the repository level
        (``test_operation_fence_wiring``).
        """
        from structlog.testing import capture_logs

        from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
        from elspeth.web.sessions.models import sessions_table

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        sessions_service = _test_sessions_service(self.engine, self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=sessions_service,
            session_engine=self.engine,
        )
        state = _empty_state()
        authority = sessions_service.session_operation_authority

        valid_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )

        def fail_tool(*_args: Any, **_kwargs: Any) -> None:
            mutation.side_effect = SessionOperationFenceLost(FenceLossReason.LEASE_EXPIRED)
            raise ValueError("plugin bug")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=fail_tool),
            patch.object(authority, "mutate", wraps=authority.mutate) as mutation,
            capture_logs() as cap_logs,
        ):
            mock_llm.return_value = valid_call
            with pytest.raises(ComposerPluginCrashError) as exc_info:
                await service.compose("Setup", [], state, session_id=self.session_id)

        assert isinstance(exc_info.value.original_exc, ValueError)
        persistence_failure_events = [entry for entry in cap_logs if entry.get("event") == "composer_crash_persistence_failed"]
        assert len(persistence_failure_events) == 1, cap_logs
        assert persistence_failure_events[0]["audit_exc_class"] == "SessionOperationFenceLost"
        assert persistence_failure_events[0]["original_exc_class"] == "ValueError"

        with self.engine.begin() as conn:
            row = conn.execute(sessions_table.select().where(sessions_table.c.id == self.session_id)).one()
        row_updated_at = row.updated_at
        seeded = self.seeded_at.replace(tzinfo=None) if row_updated_at.tzinfo is None else self.seeded_at
        assert row_updated_at == seeded, "a lost fence must not bump updated_at"


class TestToolExecutionThreadOffloading:
    """execute_tool() must run in a worker thread, not the event loop thread.

    Tests capture actual thread identity rather than checking whether
    asyncio.to_thread was called — testing the behavioral property
    (event loop not blocked) regardless of the offloading mechanism.
    """

    @staticmethod
    async def _assert_tool_runs_off_event_loop(
        tool_call_response: _AdmittedLLMCompletion,
        text_response: _AdmittedLLMCompletion,
        user_message: str,
    ) -> None:
        """Shared helper: verify a tool call executes in a worker thread."""
        import threading

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        event_loop_thread = threading.current_thread()
        tool_execution_thread: threading.Thread | None = None

        def _capture_thread(
            _tool_name: str,
            _arguments: dict[str, Any],
            current_state: CompositionState,
            _catalog: Any,
            **kwargs: Any,
        ) -> ToolResult:
            nonlocal tool_execution_thread
            tool_execution_thread = threading.current_thread()
            return _strict_execute_tool(_tool_name, _arguments, current_state, _catalog, **kwargs)

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=_capture_thread,
            ),
        ):
            mock_llm.side_effect = [tool_call_response, text_response]
            await service.compose(user_message, [], state, session_id=session_id)

        assert tool_execution_thread is not None, "execute_tool was never called"
        assert tool_execution_thread is not event_loop_thread, (
            "execute_tool ran on the event loop thread — must be offloaded to a worker thread to avoid blocking"
        )

    @pytest.mark.asyncio
    async def test_discovery_tool_runs_off_event_loop_thread(self) -> None:
        """Discovery tools run in a worker thread (read-only I/O)."""
        await self._assert_tool_runs_off_event_loop(
            tool_call_response=_make_llm_response(
                tool_calls=[{"id": "c1", "name": "list_sources", "arguments": {}}],
            ),
            text_response=_make_llm_response(content="Here are the sources."),
            user_message="List sources",
        )

    @pytest.mark.asyncio
    async def test_mutation_tool_runs_off_event_loop_thread(self) -> None:
        """Mutation tools run in a worker thread (blob/secret I/O).

        Previously only discovery tools were offloaded; mutation tools
        ran synchronously on the event loop, blocking all concurrent
        requests in the single-process server.
        """
        await self._assert_tool_runs_off_event_loop(
            tool_call_response=_make_llm_response(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "set_source",
                        "arguments": {
                            "plugin": "csv",
                            "on_success": "out",
                            "options": {"path": "/data/blobs/f.csv", "schema": {"mode": "observed"}},
                            "on_validation_failure": "quarantine",
                        },
                    }
                ],
            ),
            text_response=_make_llm_response(content="Source configured."),
            user_message="Set CSV source",
        )

    @pytest.mark.asyncio
    async def test_event_loop_not_blocked_during_tool_execution(self) -> None:
        """Heartbeat regression: compose() must not block the event loop.

        Runs compose() alongside an async heartbeat coroutine. If the
        heartbeat fires on schedule (not delayed by blocking tool work),
        the event loop was free during tool execution.
        """
        import time

        catalog = _mock_catalog()
        settings = _make_settings()
        service, session_id = _composer_service_with_session(catalog=catalog, settings=settings)
        state = _empty_state()

        # Blocking duration must be much larger than the gap threshold
        # to avoid false positives on slow/shared CI runners where OS
        # scheduler jitter can delay asyncio.sleep wakeups by 50-100ms.
        tool_block_seconds = 1.0
        heartbeat_interval = 0.05

        def _blocking_tool(
            _tool_name: str,
            _arguments: dict[str, Any],
            current_state: CompositionState,
            _catalog: Any,
            **kwargs: Any,
        ) -> ToolResult:
            time.sleep(tool_block_seconds)
            return ToolResult(
                success=True,
                updated_state=current_state,
                validation=current_state.validate(),
                affected_nodes=("source",),
                data=None,
            )

        heartbeat_times: list[float] = []

        async def heartbeat() -> None:
            while True:
                heartbeat_times.append(time.monotonic())
                await asyncio.sleep(heartbeat_interval)

        # Use a mutation tool — the original bug was specifically about
        # mutation tools running synchronously on the event loop.
        tool_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "c1",
                    "name": "set_source",
                    "arguments": {
                        "plugin": "csv",
                        "on_success": "out",
                        "options": {"path": "/data/blobs/f.csv", "schema": {"mode": "observed"}},
                        "on_validation_failure": "quarantine",
                    },
                }
            ],
        )
        text = _make_llm_response(content="Done.")

        with (
            patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=_blocking_tool,
            ),
        ):
            mock_llm.side_effect = [tool_call, text]
            hb_task = asyncio.create_task(heartbeat())
            try:
                await service.compose("List sources", [], state, session_id=session_id)
            finally:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task

        # With 1.0s block and 50ms interval we expect ~20 heartbeats.
        # Require at least 4 to catch partial stalls, not just total seizure.
        min_expected = int(tool_block_seconds / heartbeat_interval) - 2
        assert len(heartbeat_times) >= min(min_expected, 4), (
            f"Only {len(heartbeat_times)} heartbeat(s) fired during {tool_block_seconds}s tool execution — event loop was likely blocked"
        )

        # Check that no heartbeat interval exceeds a generous threshold.
        # If the event loop were blocked, one interval would be ≈ tool_block_seconds.
        # The 5x multiplier (250ms) gives wide margin for OS scheduler jitter
        # on shared CI runners while still catching a 1.0s event loop block.
        max_allowed_gap = heartbeat_interval * 5  # 250ms threshold vs 1.0s block (4x safety)
        for i in range(1, len(heartbeat_times)):
            gap = heartbeat_times[i] - heartbeat_times[i - 1]
            assert gap < max_allowed_gap, (
                f"Heartbeat gap {gap:.3f}s exceeds {max_allowed_gap:.3f}s — event loop was blocked (tool takes {tool_block_seconds}s)"
            )


class TestToolArgumentError:
    """ToolArgumentError is a composer-domain exception for Tier-3 boundary failures.

    It signals that a tool handler received arguments of the wrong type or
    with semantically invalid values that could not be coerced. The compose
    loop catches this and feeds the message back to the LLM for retry. Any
    OTHER exception escaping execute_tool is a plugin bug and must crash.

    The class is deliberately a structured DTO rather than a free-form
    ``Exception`` subclass: its composed message is echoed verbatim to
    the LLM API AND recorded in the Landscape audit trail, so any
    channel that could carry an LLM-supplied value would be a secret/PII
    leak pathway. Tests below lock in the "safe by construction" shape.
    """

    def test_inherits_from_exception_directly_not_composer_service_error(self) -> None:
        """ToolArgumentError must NOT inherit from ComposerServiceError.

        If it did, the route-level ``except ComposerServiceError`` block
        in ``send_message`` (sessions/routes.py) would silently absorb
        any escaped ToolArgumentError as a 502, recreating the
        silent-laundering channel this plan closes.
        Inheriting from Exception directly ensures an escaped
        ToolArgumentError (a compose-loop bug) surfaces loudly via FastAPI's
        default handler rather than being masked.
        """
        assert issubclass(ToolArgumentError, Exception)
        assert not issubclass(ToolArgumentError, ComposerServiceError)

    def test_structured_fields_compose_canonical_message(self) -> None:
        """Constructor composes args[0] deterministically from the three fields.

        The compose loop reads ``exc.args[0]`` to build the LLM-echo
        payload, so the composition template is a documented wire
        contract — a change here is a change to what the LLM and
        Landscape see.
        """
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        assert exc.argument == "content"
        assert exc.expected == "a string"
        assert exc.actual_type == "int"
        assert exc.args[0] == "'content' must be a string, got int"
        assert str(exc) == "'content' must be a string, got int"

    def test_untrusted_constructor_metadata_is_canonicalized_before_storage(self) -> None:
        """Arbitrary caller strings must not survive on the exception object."""
        canary = "TOOL_ARGUMENT_CANARY_sk_live_7eQ9_path_etc_shadow"
        exc = ToolArgumentError(
            argument=f"field_{canary}",
            expected=f"the secret at /srv/{canary}",
            actual_type=f"ValueError: rejected {canary}",
        )

        serialized = repr(
            {
                "args": exc.args,
                "argument": exc.argument,
                "expected": exc.expected,
                "actual_type": exc.actual_type,
                "safe_message": exc.safe_message,
            }
        )
        assert canary not in serialized
        assert len(exc.safe_message) <= 256

    def test_args_cannot_be_reassigned_after_construction(self) -> None:
        """The trusted BaseException wire payload must be immutable."""
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        original_args = exc.args

        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.args = ("TOOL_ARGUMENT_MUTATION_CANARY",)

        assert exc.args == original_args

    def test_code_is_closed_at_construction(self) -> None:
        """Unknown caller-controlled discriminants cannot enter the carrier."""
        with pytest.raises(ValueError, match="unsupported code"):
            ToolArgumentError(
                argument="content",
                expected="a string",
                actual_type="int",
                code="TOOL_ARGUMENT_CODE_CANARY",
            )

    @pytest.mark.parametrize(
        ("argument", "expected"),
        (
            ("name", "a string"),
            ("session_id", "a 12-character lowercase hex string"),
        ),
    )
    def test_composer_mcp_schema_labels_preserve_safe_diagnostics(self, argument: str, expected: str) -> None:
        """MCP's operator-owned schema labels remain actionable."""
        exc = ToolArgumentError(
            argument=argument,
            expected=expected,
            actual_type="invalid_session_id" if argument == "session_id" else "dict",
        )
        assert exc.argument == argument
        assert exc.expected == expected
        assert argument in exc.safe_message

    def test_constructor_is_keyword_only(self) -> None:
        """Positional construction must fail — structural leak prevention.

        The whole point of the DTO shape is that there is no way to
        sneak a raw LLM-supplied value into the message. A positional
        ``ToolArgumentError(f"bad: {user_input!r}")`` would defeat
        that. Making the constructor keyword-only forces every call
        site through the three-field safe channel.
        """
        with pytest.raises(TypeError):
            cast(Any, ToolArgumentError)("content must be a string, got int")

    def test_empty_argument_rejected(self) -> None:
        """Blank ``argument`` produces a nonsensical audit record and must be rejected."""
        with pytest.raises(ValueError, match="argument must be a non-empty"):
            ToolArgumentError(argument="", expected="a string", actual_type="int")

    def test_empty_expected_rejected(self) -> None:
        """Blank ``expected`` produces a nonsensical audit record and must be rejected."""
        with pytest.raises(ValueError, match="expected must be a non-empty"):
            ToolArgumentError(argument="content", expected="", actual_type="int")

    def test_empty_actual_type_rejected(self) -> None:
        """Blank ``actual_type`` produces a nonsensical audit record and must be rejected."""
        with pytest.raises(ValueError, match="actual_type must be a non-empty"):
            ToolArgumentError(argument="content", expected="a string", actual_type="")

    def test_declared_fields_frozen_after_construction(self) -> None:
        """Declared fields must not be mutable after construction.

        The exception flows into ``composition_states`` / LLM echo as
        an immutable audit artefact. Mirror the _FROZEN_ATTRS pattern
        used by ComposerConvergenceError and ComposerPluginCrashError
        so no intermediate layer can silently rewrite what downstream
        consumers see.
        """
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.argument = "other"
        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.expected = "a dict"
        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.actual_type = "str"

    def test_runtime_exception_dunders_remain_writable(self) -> None:
        """Runtime-required cause/context/traceback slots remain writable."""
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        cause = ValueError("deep cause")
        exc.__cause__ = cause
        exc.__context__ = cause
        exc.__suppress_context__ = True
        exc.__traceback__ = None
        assert exc.__cause__ is cause
        assert exc.__context__ is cause
        assert exc.__suppress_context__ is True
        assert exc.__traceback__ is None

    def test_unknown_attributes_and_free_form_notes_are_rejected(self) -> None:
        """Normal metadata channels close; the safe serializer ignores reflection."""
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        canary = "TOOL_ARGUMENT_ATTR_CANARY_sk_live_2Ks5_/etc/shadow"
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )

        with pytest.raises(AttributeError, match="closed after construction"):
            exc.untrusted_metadata = canary
        with pytest.raises(AttributeError, match="closed after construction"):
            exc.__notes__ = [canary]
        with pytest.raises(TypeError, match="does not accept free-form notes"):
            exc.add_note(canary)

        # Deliberate in-process reflection is outside the untrusted-input
        # boundary, but the production serializer must still ignore it.
        BaseException.__setattr__(exc, "forced_metadata", canary)
        serialized = json.dumps(arg_error_payload(exc, "set_metadata"), sort_keys=True)
        assert canary not in serialized

    def test_reflective_mutation_of_consumed_fields_cannot_leak(self) -> None:
        """Every public carrier read re-projects reflectively replaced fields."""
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        canary = "TOOL_ARGUMENT_REFLECTION_CANARY_sk_live_7Lq4_/root/.ssh/id_ed25519"
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )

        for name, value in (
            ("safe_message", canary),
            ("args", (canary,)),
            ("argument", canary),
            ("expected", canary),
            ("actual_type", canary),
            ("code", canary),
        ):
            BaseException.__setattr__(exc, name, value)

        serialized = repr(
            {
                "payload": arg_error_payload(exc, "set_metadata"),
                "args": exc.args,
                "message": str(exc),
                "repr": repr(exc),
                "vars": vars(exc),
                "argument": exc.argument,
                "expected": exc.expected,
                "actual_type": exc.actual_type,
                "code": exc.code,
            }
        )
        assert canary not in serialized
        assert exc.argument == "content"
        assert exc.expected == "a string"
        assert exc.actual_type == "int"
        assert exc.code is None
        assert exc.safe_message == "'content' must be a string, got int"
        assert exc.args == ("'content' must be a string, got int",)

    def test_private_backing_overwrite_and_seal_reset_cannot_leak(self) -> None:
        """Private reflection and a reset seal cannot bypass read projections."""
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        canary = "TOOL_ARGUMENT_PRIVATE_CANARY_sk_live_8Qw2_/srv/operator.key"
        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        for name in ("_safe_argument", "_safe_expected", "_safe_actual_type"):
            BaseException.__setattr__(exc, name, canary)
        BaseException.__setattr__(exc, "_tool_argument_error_sealed", False)
        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.safe_message = canary
        with pytest.raises(AttributeError, match="frozen after construction"):
            exc.args = (canary,)

        serialized = repr(
            {
                "payload": arg_error_payload(exc, "set_metadata"),
                "args": exc.args,
                "message": str(exc),
                "repr": repr(exc),
                "vars": vars(exc),
                "argument": exc.argument,
                "expected": exc.expected,
                "actual_type": exc.actual_type,
                "code": exc.code,
            }
        )
        assert canary not in serialized

    @pytest.mark.parametrize("replacement", (None, 7, ["not", "a", "scalar"]))
    def test_private_display_backing_missing_or_wrong_typed_uses_fixed_fallbacks(self, replacement: object) -> None:
        """Missing and malformed display text cannot expose raw input."""
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        exc = ToolArgumentError(
            argument="content",
            expected="a string",
            actual_type="int",
        )
        private_names = ("_safe_argument", "_safe_expected", "_safe_actual_type")
        if replacement is None:
            for name in private_names:
                BaseException.__delattr__(exc, name)
        else:
            for name in private_names:
                BaseException.__setattr__(exc, name, replacement)

        assert exc.argument == "tool argument"
        assert exc.expected == "a valid value"
        assert exc.actual_type == "invalid value"
        assert exc.code is None
        assert exc.args == ("'tool argument' must be a valid value, got invalid value",)
        assert arg_error_payload(exc, "set_metadata")["error"].endswith(exc.safe_message)

    @pytest.mark.parametrize("replacement", (None, 7, ["not", "a", "scalar"], "CODE_CORRUPTION_CANARY"))
    def test_corrupt_private_code_cannot_silently_erase_audit_classification(self, replacement: object) -> None:
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        exc = ToolArgumentError(
            argument="pipeline",
            expected="a mapping",
            actual_type="str",
            code="SCHEMA_VALIDATION",
            category=ToolArgumentErrorCategory.SCHEMA_SHAPE,
        )
        if replacement is None:
            BaseException.__delattr__(exc, "_safe_code")
        else:
            BaseException.__setattr__(exc, "_safe_code", replacement)
        # Display rendering remains safe even when classification must abort.
        assert "CODE_CORRUPTION_CANARY" not in str(exc)
        assert "CODE_CORRUPTION_CANARY" not in str(arg_error_payload(exc, "set_pipeline"))
        with pytest.raises(FrameworkBugError, match="classification code") as caught:
            _ = exc.code
        assert "CODE_CORRUPTION_CANARY" not in str(caught.value)

    @pytest.mark.parametrize(
        ("argument", "model_name"),
        (
            ("upsert_node arguments", "_UpsertNodeArgumentsModel"),
            ("splice_transform arguments", "SpliceTransformArgumentsModel"),
            ("upsert_edge arguments", "_UpsertEdgeArgumentsModel"),
            ("remove_node arguments", "_RemoveByIdArgumentsModel"),
            ("remove_edge arguments", "_RemoveByIdArgumentsModel"),
            ("set_metadata arguments", "_SetMetadataArgumentsModel"),
            ("set_output arguments", "_SetOutputArgumentsModel"),
            ("remove_output arguments", "_RemoveOutputArgumentsModel"),
            ("request_interpretation_review arguments", "_RequestInterpretationReviewArgumentsModel"),
        ),
    )
    def test_mutation_argument_producer_labels_remain_actionable(self, argument: str, model_name: str) -> None:
        """Every existing mutation validator label survives the closed projection."""
        exc = ToolArgumentError(
            argument=argument,
            expected=f"object conforming to {model_name}",
            actual_type="ValidationError",
        )
        assert exc.argument == argument
        assert exc.expected == (
            f"object conforming to {model_name}. Match the tool's declared JSON types. "
            "Supply object and array fields as actual JSON objects and arrays, not strings containing JSON."
        )
        assert argument in exc.safe_message

    def test_oversized_diagnostic_strings_take_early_fixed_fallback(self) -> None:
        """Oversized caller text is rejected before casefolding or scanning."""
        oversized = "Z" * 8_000_000

        tracemalloc.start()
        try:
            exc = ToolArgumentError(
                argument="content",
                expected=oversized,
                actual_type=oversized,
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert exc.expected == "a valid value"
        assert exc.actual_type == "invalid value"
        assert peak < 2 * 1024 * 1024

    def test_hostile_subclass_cannot_override_consumed_message_fields(self) -> None:
        """Subclassing is rejected, or its payload projection must remain closed."""
        from elspeth.web.composer.tool_error_payloads import arg_error_payload

        canary = "TOOL_ARGUMENT_SUBCLASS_CANARY_sk_live_5Rx9_/var/lib/private"

        try:

            class _HostileToolArgumentError(ToolArgumentError):
                def __init__(self) -> None:
                    Exception.__init__(self, canary)

                @property
                def safe_message(self) -> str:
                    return canary

                def __str__(self) -> str:
                    return canary

        except TypeError as exc:
            assert "does not support subclassing" in str(exc)
            return

        hostile = _HostileToolArgumentError()
        serialized = repr(
            {
                "payload": arg_error_payload(hostile, "set_metadata"),
                "args": hostile.args,
                "message": str(hostile),
            }
        )
        assert canary not in serialized

    def test_supports_exception_chaining(self) -> None:
        """raise ToolArgumentError(...) from exc must preserve __cause__.

        Audit-grade error reporting depends on the cause chain surviving
        asyncio.to_thread re-raise and the service-level catch. The
        cause is carried on ``__cause__`` for debug/audit but NEVER
        echoed to the LLM (see test_tool_argument_error_subclass_
        cannot_leak_cause_to_llm).
        """
        original = ValueError("bad input")
        try:
            try:
                raise original
            except ValueError as exc:
                raise ToolArgumentError(
                    argument="content",
                    expected="a string",
                    actual_type="int",
                ) from exc
        except ToolArgumentError as wrapped:
            assert wrapped.__cause__ is original


class TestToolArgumentErrorAcrossThreadBoundary:
    """End-to-end: ToolArgumentError raised inside the worker thread is caught
    correctly by the service-level catch, with message preserved.

    Closes the sleepy-assertion gap in the mocked service-level tests
    (which raise synchronously on the mock and never exercise the real
    asyncio.to_thread re-raise path).
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        from sqlalchemy.pool import StaticPool

        from elspeth.web.sessions.engine import create_session_engine
        from elspeth.web.sessions.models import sessions_table
        from elspeth.web.sessions.schema import initialize_session_schema

        self.engine = create_session_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        initialize_session_schema(self.engine)

        self.session_id = str(uuid4())
        self.data_dir = tmp_path
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            ensure_test_identity(conn, identity_id="test-user")
            grant_test_pipeline_user(conn, identity_id="test-user")
            conn.execute(
                sessions_table.insert().values(
                    id=self.session_id,
                    user_id="test-user",
                    auth_provider_type="local",
                    title="Test",
                    trust_mode="auto_commit",
                    density_default="high",
                    created_at=now,
                    updated_at=now,
                )
            )

    @pytest.mark.asyncio
    async def test_real_create_blob_type_guard_feeds_error_to_llm(self) -> None:
        """The real threaded schema boundary retains safe blob ARG_ERROR feedback.

        The flat registry schema rejects integer content before the handler's
        Pydantic parser. Blob-only failure remains audited and reaches the next
        PRIMARY, without manufacturing a failed CompositionState mutation.
        """
        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(self.engine, self.data_dir),
            session_engine=self.engine,
        )
        state = _empty_state()

        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "create_blob",
                    "arguments": {
                        "filename": "x.txt",
                        "mime_type": "text/plain",
                        "content": 42,  # wrong type
                    },
                }
            ],
        )
        text = _make_llm_response(content="Fixed.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=self.session_id)

        _assert_reconciliation_turn(mock_llm, script)

        assert result.state == state
        assert result.runtime_preflight is None
        assert len(result.tool_invocations) == 1
        invocation = result.tool_invocations[0]
        assert invocation.tool_call_id == "call_bad" and invocation.tool_name == "create_blob"
        assert invocation.status is ComposerToolStatus.ARG_ERROR
        assert invocation.error_class == "ToolArgumentError"
        assert invocation.error_category is ToolArgumentErrorCategory.SCHEMA_SHAPE
        _assert_checkpoint_reply(result, "Fixed.", count=0)
        assert "pipeline is still empty" not in result.message
        second_call_messages = script.prompts[1]
        tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        error_content = json.loads(tool_messages[0]["content"])
        # The safe schema message names the argument-bundle/model, excluding
        # the received value and the legacy type-guard wording.
        assert "create_blob arguments" in error_content["error"]
        assert "CreateBlobArgumentsModel" in error_content["error"]
        # The legacy raw type-guard wording must not survive into the LLM echo.
        assert "got int" not in error_content["error"]

    @pytest.mark.asyncio
    async def test_real_set_source_from_blob_options_guard_feeds_error_to_llm(self) -> None:
        """Tier-3 non-dict ``options`` routes through ARG_ERROR (Task 13 / Wave 2).

        Post-promotion (Task 13) ``set_source_from_blob`` validates via
        :class:`SetSourceFromBlobArgumentsModel` BEFORE any blob lookup.
        Pydantic rejects ``options: str`` and the handler re-raises
        :class:`pydantic.ValidationError` as :class:`ToolArgumentError`
        (pattern at ``tools.py:2320-2327``, rev-2 BLOCKER_A).  The
        LLM-facing payload is intentionally leak-safe: argument-bundle
        name + Pydantic model name, not per-field detail.

        Memory: ``feedback_locked_in_buggy_expectations`` — the prior
        assertion pinned the legacy in-handler isinstance message
        ``"'options' must be an object, got str"``; the Pydantic
        boundary now fires earlier and that message no longer reaches
        the LLM.
        """
        catalog = _mock_catalog()
        settings = _make_settings(data_dir=self.data_dir)
        service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(self.engine, self.data_dir),
            session_engine=self.engine,
        )
        state = _empty_state()

        with fenced_operation_context(self.engine, self.session_id) as context:
            create_result = _execute_tool(
                "create_blob",
                {"filename": "seed.txt", "mime_type": "text/plain", "content": "hello"},
                state,
                catalog,
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                **_verbatim_blob_context(self.engine, self.session_id, "hello"),
                session_operation_context=context,
                session_operation_authority=SQLiteLocalSessionOperationAuthority(self.engine),
            )
        blob_id = create_result.data["blob_id"]

        bad_call = _make_llm_response(
            tool_calls=[
                {
                    "id": "call_bad",
                    "name": "set_source_from_blob",
                    "arguments": {
                        "blob_id": blob_id,
                        "on_success": "out",
                        "options": "column=text",
                    },
                }
            ],
        )
        text = _make_llm_response(content="Fixed.")

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            script = _ReconciliationLLMScript(bad_call, text)
            mock_llm.side_effect = script.respond
            result = await service.compose("Set up the pipeline.", [], state, session_id=self.session_id)

        _assert_reconciliation_turn(mock_llm, script)

        _assert_no_mutation_empty_state_blocker(
            result,
            tool_name="set_source_from_blob",
            expected_detail="ToolArgumentError",
        )
        second_call_messages = script.prompts[1]
        tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        error_content = json.loads(tool_messages[0]["content"])
        # New (post-promotion) LLM-facing message names the argument-bundle
        # and the Pydantic model — no leak of the raw offending value or
        # its type (which the legacy handler echoed as "got str").
        assert "set_source_from_blob arguments" in error_content["error"]
        assert "SetSourceFromBlobArgumentsModel" in error_content["error"]
        assert "got str" not in error_content["error"]
        assert "column=text" not in error_content["error"]


class TestComposerErrorConstructionInvariants:
    """Type-level invariants for composer service exceptions.

    These exceptions flow into HTTP responses (as error_type/detail bodies)
    and into Landscape (via partial_state persistence in composition_states
    and structured-log exc_class correlation). Post-construction attribute
    reassignment would let any layer silently rewrite what downstream HTTP
    and audit consumers see. The class-level freeze and the ``capture()``
    classmethod encode the "partial_state only when state.version >
    initial_version" invariant mechanically rather than relying on each
    raise site to apply the rule by hand.
    """

    @pytest.mark.parametrize("budget", ["composition", "discovery"])
    def test_turn_budget_convergence_detail_names_the_tool_call_loop(self, budget: Literal["composition", "discovery"]) -> None:
        detail = str(ComposerConvergenceError(6, budget_exhausted=budget))

        assert detail.startswith(f"Composer did not converge within 6 turns (budget exhausted: {budget}).")
        assert "kept making tool calls without producing a final response" in detail

    def test_timeout_convergence_detail_does_not_claim_the_model_never_replied(self) -> None:
        """The detail is the HTTP body the user reads. A compose deadline can
        expire AFTER the model's final reply (the END gate's re-validation ran
        out of time), so "kept making tool calls without producing a final
        response" is false there. The timeout wording claims only what every
        timeout has in common."""
        detail = str(ComposerConvergenceError(6, budget_exhausted="timeout"))

        assert detail.startswith("Composer did not converge within 6 turns (budget exhausted: timeout).")
        assert "kept making tool calls" not in detail
        assert "ran out of time before the turn could be completed" in detail

    def test_plugin_crash_error_attributes_are_frozen_after_construction(self) -> None:
        exc = ComposerPluginCrashError(ValueError("boom"), partial_state=None)

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("original_exc", RuntimeError("replaced"))

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("partial_state", _empty_state())

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("exc_class", "PrettyException")

    def test_plugin_crash_error_allows_exception_chain_machinery(self) -> None:
        # __cause__, __context__, __suppress_context__, __traceback__, and
        # add_note() target BaseException-managed slots, not our declared
        # attrs. The freeze MUST NOT break `raise X from Y` or add_note.
        root = RuntimeError("underlying")
        exc = ComposerPluginCrashError(ValueError("boom"))
        exc.__cause__ = root
        exc.__suppress_context__ = True
        exc.add_note("operator triage hint")

        assert exc.__cause__ is root
        assert exc.__suppress_context__ is True
        assert "operator triage hint" in exc.__notes__

    def test_convergence_error_attributes_are_frozen_after_construction(self) -> None:
        exc = ComposerConvergenceError(
            max_turns=3,
            budget_exhausted="composition",
            partial_state=None,
        )

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("max_turns", 99)

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("budget_exhausted", "timeout")

        with pytest.raises(AttributeError, match="frozen"):
            exc.__setattr__("partial_state", _empty_state())

    def test_plugin_crash_capture_returns_none_when_state_not_mutated(self) -> None:
        # Invariant: partial_state is None when state.version == initial_version
        # (no tool call successfully mutated state before the crash).
        state = _empty_state()  # version=1
        exc = ComposerPluginCrashError.capture(
            KeyError("missing"),
            state=state,
            initial_version=state.version,
        )

        assert exc.partial_state is None
        assert exc.exc_class == "KeyError"

    def test_plugin_crash_capture_returns_state_when_mutated(self) -> None:
        # Invariant: partial_state IS the state when state.version moved
        # beyond initial_version (at least one tool call persisted).
        initial = _empty_state()  # version=1
        mutated = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=initial.version + 2,
        )
        exc = ComposerPluginCrashError.capture(
            ValueError("boom"),
            state=mutated,
            initial_version=initial.version,
        )

        assert exc.partial_state is mutated
        assert exc.exc_class == "ValueError"

    def test_convergence_capture_returns_none_when_state_not_mutated(self) -> None:
        state = _empty_state()
        exc = ComposerConvergenceError.capture(
            max_turns=5,
            budget_exhausted="composition",
            state=state,
            initial_version=state.version,
        )

        assert exc.partial_state is None
        assert exc.max_turns == 5
        assert exc.budget_exhausted == "composition"

    def test_convergence_capture_returns_state_when_mutated(self) -> None:
        initial = _empty_state()
        mutated = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=initial.version + 1,
        )
        exc = ComposerConvergenceError.capture(
            max_turns=7,
            budget_exhausted="timeout",
            state=mutated,
            initial_version=initial.version,
        )

        assert exc.partial_state is mutated
        assert exc.budget_exhausted == "timeout"


class TestComposerRuntimePreflightCacheAndTimeout:
    @pytest.mark.asyncio
    async def test_runtime_preflight_cache_reuses_same_state_version_and_settings_hash(self) -> None:
        service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        state = _empty_state()
        cache = service._preflight.new_cache()
        preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        with patch.object(service._preflight, "runtime_preflight", return_value=preflight) as mock_preflight:
            first = await service._preflight.cached_runtime_preflight(
                state,
                session_id=None,
                user_id="user-1",
                cache=cache,
                initial_version=state.version,
                session_scope="session:test",
            )
            second = await service._preflight.cached_runtime_preflight(
                state,
                session_id=None,
                user_id="user-1",
                cache=cache,
                initial_version=state.version,
                session_scope="session:test",
            )

        assert first is preflight
        assert second is preflight
        mock_preflight.assert_called_once_with(state, "user-1", None)

    @pytest.mark.asyncio
    async def test_unsaved_preflight_does_not_share_distinct_state_content_at_same_version(self) -> None:
        service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
        first_state = _empty_state()
        second_state = replace(
            first_state,
            metadata=PipelineMetadata(name="Distinct unsaved pipeline"),
        )
        assert first_state.version == second_state.version
        first_result = ValidationResult(is_valid=True, checks=[], errors=[])
        second_result = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="distinct",
                    component_type="pipeline",
                    message="Distinct state is invalid.",
                    suggestion="Use the matching preflight result.",
                    error_code="distinct_state",
                )
            ],
        )
        first_started = threading.Event()
        release_first = threading.Event()
        second_joined_coordinator = asyncio.Event()
        coordinator_calls = 0
        coordinator = service._preflight._coordinator
        original_run = coordinator.run

        def preflight(
            candidate: CompositionState,
            user_id: str | None,
            session_id: str | None,
        ) -> ValidationResultModel:
            assert user_id == "user-1"
            assert session_id is None
            if candidate is first_state:
                first_started.set()
                assert release_first.wait(timeout=5)
                return first_result
            assert candidate is second_state
            return second_result

        async def observed_run(*args: Any, **kwargs: Any) -> Any:
            nonlocal coordinator_calls
            coordinator_calls += 1
            if coordinator_calls == 2:
                second_joined_coordinator.set()
            return await original_run(*args, **kwargs)

        with (
            patch.object(service._preflight, "runtime_preflight", side_effect=preflight) as mock_preflight,
            patch.object(coordinator, "run", side_effect=observed_run),
        ):
            first_task = asyncio.create_task(
                service._preflight.cached_runtime_preflight(
                    first_state,
                    session_id=None,
                    user_id="user-1",
                    cache=service._preflight.new_cache(),
                    initial_version=first_state.version,
                    session_scope="session:unsaved",
                )
            )
            assert await asyncio.to_thread(first_started.wait, 5)
            second_task = asyncio.create_task(
                service._preflight.cached_runtime_preflight(
                    second_state,
                    session_id=None,
                    user_id="user-1",
                    cache=service._preflight.new_cache(),
                    initial_version=second_state.version,
                    session_scope="session:unsaved",
                )
            )
            await asyncio.wait_for(second_joined_coordinator.wait(), timeout=5)
            await asyncio.sleep(0)
            release_first.set()
            first, second = await asyncio.gather(first_task, second_task)

        assert first is first_result
        assert second is second_result
        assert mock_preflight.call_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hold_actual_return", [False, True], ids=["join-actual-worker", "release-only-cleanup-rejected"])
    async def test_runtime_preflight_timeout_is_cached_for_compose_call(self, hold_actual_return: bool) -> None:
        settings = _make_settings(composer_runtime_preflight_timeout_seconds=0.01)
        service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)
        state = _empty_state()
        cache = service._preflight.new_cache()
        started = threading.Event()
        release = threading.Event()
        actual_return = threading.Event()
        release_observed = asyncio.Event()
        loop = asyncio.get_running_loop()
        baseline = async_workers.outstanding_admissions()
        expected = ValidationResult(is_valid=True, checks=[], errors=[])
        if not hold_actual_return:
            actual_return.set()

        def slow_preflight(candidate: CompositionState, user_id: str | None, session_id: str | None) -> ValidationResult:
            started.set()
            release.wait(timeout=30)
            loop.call_soon_threadsafe(release_observed.set)
            assert actual_return.wait(timeout=30)
            return expected

        try:
            with patch.object(service._preflight, "runtime_preflight", side_effect=slow_preflight) as mock_preflight:
                with pytest.raises(ComposerRuntimePreflightError) as first:
                    await service._preflight.cached_runtime_preflight(
                        state,
                        session_id=None,
                        user_id="user-1",
                        cache=cache,
                        initial_version=state.version - 1,
                        session_scope="session:test",
                    )
                assert first.value.exc_class == "TimeoutError"
                assert started.is_set()
                key = service._preflight.key(state, session_scope="session:test", plugin_snapshot=None)
                actual_task = service._preflight._coordinator._inflight[key]
                assert not actual_task.done()

                with pytest.raises(ComposerRuntimePreflightError) as second:
                    await service._preflight.cached_runtime_preflight(
                        state,
                        session_id=None,
                        user_id="user-1",
                        cache=cache,
                        initial_version=state.version - 1,
                        session_scope="session:test",
                    )

                assert second.value.exc_class == "TimeoutError"
                mock_preflight.assert_called_once()
                if hold_actual_return:
                    # The old cleanup stops here: waking the thread is not
                    # proof of its actual Future's completion or slot release.
                    release.set()
                    await release_observed.wait()
                    assert not actual_task.done()
                    assert async_workers.outstanding_admissions() == baseline + 1
                    with pytest.raises(ExceptionGroup) as unsettled:
                        assert_owned_executor_fixture_settled(async_workers._GENERATION_CUSTODIAN)
                    assert [str(error) for error in unsettled.value.exceptions] == [
                        "Executor fixture retained charged invocation",
                        "Executor fixture retained incomplete actual Future",
                    ]
        finally:
            # Capture before releasing: the coordinator evicts completed work.
            pending = tuple(service._preflight._coordinator._inflight.values())
            release.set()
            actual_return.set()
            results = await asyncio.gather(*pending)
            assert all(result is expected for result in results)
        assert actual_task.done() and actual_task.result() is expected
        assert key not in service._preflight._coordinator._inflight
        assert async_workers.outstanding_admissions() == baseline
        assert_owned_executor_fixture_settled(async_workers._GENERATION_CUSTODIAN)

    @pytest.mark.asyncio
    async def test_timed_out_preflight_stays_admitted_so_a_retry_joins_it(self) -> None:
        """elspeth-5269b43bca: the caller's timeout must not release admission.

        A retry (a NEW compose call, so a fresh per-call cache) for the same
        state while the first sync preflight is still running must join that
        worker through the coordinator, not submit a second copy to the
        shared pool. Before the fix the timeout was the shared task's own
        outcome, so the in-flight entry was evicted while the thread still
        ran and every retry queued another hung worker.
        """
        settings = _make_settings(composer_runtime_preflight_timeout_seconds=0.02)
        service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)
        state = _empty_state()
        started = threading.Event()
        release = threading.Event()
        expected = ValidationResult(is_valid=True, checks=[], errors=[])

        def hung_preflight(candidate: CompositionState, user_id: str | None, session_id: str | None) -> ValidationResult:
            started.set()
            release.wait(timeout=30)
            return expected

        try:
            with patch.object(service._preflight, "runtime_preflight", side_effect=hung_preflight) as mock_preflight:
                for _ in range(3):
                    with pytest.raises(ComposerRuntimePreflightError) as failure:
                        await service._preflight.cached_runtime_preflight(
                            state,
                            session_id=None,
                            user_id="user-1",
                            cache=service._preflight.new_cache(),
                            initial_version=state.version - 1,
                            session_scope="session:test",
                        )
                    assert failure.value.exc_class == "TimeoutError"
                assert started.is_set()
                # One sync worker for three timed-out compose calls.
                mock_preflight.assert_called_once()
                key = service._preflight.key(state, session_scope="session:test", plugin_snapshot=None)
                assert key in service._preflight._coordinator._inflight

                # A retry that arrives with budget to spare joins the running
                # worker and receives its real result.
                async def must_not_start() -> ValidationResult:
                    raise AssertionError("retry started a second preflight instead of joining the running one")

                patient = asyncio.create_task(
                    service._preflight._coordinator.run(key, must_not_start, timeout=5.0),
                )
                await asyncio.sleep(0)
                release.set()
                assert await patient is expected
                mock_preflight.assert_called_once()
        finally:
            release.set()

    def test_runtime_preflight_settings_hash_is_non_secret(self) -> None:
        class FakeSettings:
            data_dir = Path("/tmp/elspeth-data")
            landscape_passphrase = "SECRET_CANARY_SHOULD_NOT_APPEAR"

        digest = runtime_preflight_settings_hash(FakeSettings())

        assert "SECRET_CANARY" not in digest
        assert len(digest) == 64


class TestComposerRuntimePreflightFinalGate:
    def test_literal_credential_preflight_repair_requires_secret_inventory_diagnosis(self) -> None:
        """elspeth-697b7377a7: literal credential failures must drive
        secret inventory diagnosis, not another copy of the validate complaint.
        """
        invalid_preflight = ValidationResultModel(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="summarize",
                    component_type="transform",
                    message="Credential field(s) api_key contain a literal value; expected a wired secret reference.",
                    suggestion="Wire each credential field through the Secrets panel.",
                    error_code="fabricated_secret",
                )
            ],
            readiness=_not_authoring_ready("fabricated_secret"),
        )

        repair = _compose_preflight_repair_message(invalid_preflight, next_turn=1)

        assert "list_secret_refs" in repair
        assert "validate_secret_ref" in repair
        assert "reason" in repair
        assert "fingerprint_resolver_not_configured" in repair
        assert "Do not answer by repeating the runtime preflight complaint" in repair
        assert "Do not inline a literal credential" in repair

    def test_preflight_recovery_preserves_authority_and_explains_read_only_evidence(self) -> None:
        """Inspection supplies evidence; authorized repairs still need mutation."""
        invalid_preflight = ValidationResultModel(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="mapper",
                    component_type="transform",
                    message="consumer requires ['colour'], producer guarantees (none)",
                    suggestion=None,
                    error_code=None,
                )
            ],
            readiness=_not_authoring_ready("graph_structure"),
        )

        repair = _compose_preflight_repair_message(invalid_preflight, next_turn=1)

        assert "Pre-finalisation runtime preflight" in repair
        assert "runtime review turn 1 of 2" in repair
        assert "get_pipeline_state" in repair
        assert "preview_pipeline" in repair
        assert "Read-only" in repair
        assert "do not repair a violation" in repair
        assert "If the user's active request authorizes changes" in repair
        assert "without changing the pipeline" in repair

    @pytest.mark.asyncio
    async def test_changed_state_completion_is_replaced_when_runtime_preflight_fails(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        changed_state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        ).with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "/data/outputs/out.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        changed_state = replace(changed_state, version=state.version + 1)

        llm_response = _make_llm_response(content="The pipeline is complete and valid.")
        failed_preflight = ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name="settings_load",
                    passed=False,
                    detail="Forbidden name: 'end_of_source'",
                    affected_nodes=(),
                    outcome_code=None,
                )
            ],
            errors=[
                ValidationError(
                    component_id="agg1",
                    component_type="aggregation",
                    message="Forbidden name: 'end_of_source'",
                    suggestion="Omit trigger for end-of-source-only aggregation.",
                    error_code=None,
                )
            ],
        )

        with patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            with patch.object(service._preflight, "runtime_preflight", return_value=failed_preflight) as mock_preflight:
                result = await service._completion._finalize_no_tool_response(
                    content="The pipeline is complete and valid.",
                    state=changed_state,
                    initial_version=state.version,
                    user_id="user-1",
                    session_id=None,
                    last_runtime_preflight=None,
                    runtime_preflight_cache=service._preflight.new_cache(),
                    session_scope="session:test",
                )

        assert result.message != "The pipeline is complete and valid."
        assert result.raw_assistant_content == "The pipeline is complete and valid."
        assert result.runtime_preflight is failed_preflight
        mock_preflight.assert_called_once_with(changed_state, "user-1", None)

    @pytest.mark.asyncio
    async def test_pending_interpretation_handoff_is_not_augmented_as_invalid_config(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state().with_source(
            SourceSpec(
                plugin="csv",
                on_success="rate_node",
                options={"path": "/data/blobs/input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        changed_state = replace(state, version=state.version + 1)
        pending_preflight = ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name="interpretation_review",
                    passed=False,
                    detail="Interpretation review is pending for rate_node:cool.",
                    affected_nodes=("rate_node",),
                    outcome_code=None,
                )
            ],
            errors=[
                ValidationError(
                    component_id="rate_node",
                    component_type="transform",
                    message="Interpretation review is pending for 'cool'.",
                    suggestion=(
                        "Resolve the pending interpretation review before running. "
                        "If no review card is shown, send a composer message so the pending reviews are surfaced."
                    ),
                    error_code=INTERPRETATION_REVIEW_PENDING_CODE,
                )
            ],
            readiness=_pending_interpretation_readiness(),
        )
        model_prose = "Review is pending for cool."

        with patch.object(service._preflight, "runtime_preflight", return_value=pending_preflight) as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content=model_prose,
                state=changed_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
                mutation_success_seen=True,
            )

        assert result.message == model_prose
        assert result.raw_assistant_content is None
        assert result.runtime_preflight is pending_preflight
        mock_preflight.assert_called_once_with(changed_state, "user-1", None)

    @pytest.mark.asyncio
    async def test_unchanged_text_without_preview_does_not_run_preflight(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()

        with patch.object(service._preflight, "runtime_preflight") as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content="I can help with that.",
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert result.message == "I can help with that."
        assert result.raw_assistant_content is None
        assert result.runtime_preflight is None
        mock_preflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_unchanged_state_reuses_preview_preflight_without_rerun(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        preview_preflight = ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name="settings_load",
                    passed=False,
                    detail="Forbidden name: 'end_of_source'",
                    affected_nodes=(),
                    outcome_code=None,
                )
            ],
            errors=[
                ValidationError(
                    component_id="agg1",
                    component_type="aggregation",
                    message="Forbidden name: 'end_of_source'",
                    suggestion=None,
                    error_code=None,
                )
            ],
        )

        with patch.object(service._preflight, "runtime_preflight") as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content="The pipeline is complete and valid.",
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=preview_preflight,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert result.message != "The pipeline is complete and valid."
        assert result.raw_assistant_content == "The pipeline is complete and valid."
        assert result.runtime_preflight is preview_preflight
        mock_preflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_unchanged_state_reuses_valid_preview_preflight_without_replacement(self) -> None:
        """Reuse path with is_valid=True must preserve the LLM message verbatim.

        Complement to test_unchanged_state_reuses_preview_preflight_without_rerun:
        that test exercises the invalid-cached branch (which replaces the
        message). This test pins the valid-cached branch (which must keep the
        LLM message intact and not populate raw_assistant_content).
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        passing_preview_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        with patch.object(service._preflight, "runtime_preflight") as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content="The pipeline is complete and valid.",
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=passing_preview_preflight,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert result.message == "The pipeline is complete and valid."
        assert result.raw_assistant_content is None
        assert result.runtime_preflight is passing_preview_preflight
        mock_preflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_passing_preflight_preserves_original_message_verbatim(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        changed_state = replace(state, version=state.version + 1)
        passed_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        with patch.object(service._preflight, "runtime_preflight", return_value=passed_preflight):
            result = await service._completion._finalize_no_tool_response(
                content="The pipeline is complete and valid.",
                state=changed_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert result.message == "The pipeline is complete and valid."
        assert result.raw_assistant_content is None
        assert result.runtime_preflight is passed_preflight

    @pytest.mark.asyncio
    async def test_unexpected_preflight_exception_preserves_partial_state(self) -> None:
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        changed_state = replace(state, version=state.version + 1)

        with (
            patch.object(service._preflight, "runtime_preflight", side_effect=RuntimeError("boom")),
            pytest.raises(ComposerRuntimePreflightError) as exc_info,
        ):
            await service._completion._finalize_no_tool_response(
                content="The pipeline is complete.",
                state=changed_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert exc_info.value.partial_state == changed_state
        assert exc_info.value.exc_class == "RuntimeError"

    def test_runtime_preflight_error_original_exception_is_frozen(self) -> None:
        original = RuntimeError("boom")
        error = ComposerRuntimePreflightError(original_exc=original, partial_state=None)

        with pytest.raises(AttributeError):
            error.original_exc = RuntimeError("replacement")


def test_pipeline_commit_intent_is_frozen_and_hash_bound() -> None:
    proposal_id = uuid4()
    intent = PipelineCommitIntent(proposal_id=proposal_id, draft_hash="a" * 64)

    assert intent.proposal_id == proposal_id
    assert intent.draft_hash == "a" * 64
    with pytest.raises(AttributeError):
        intent.draft_hash = "b" * 64  # type: ignore[misc]
    with pytest.raises(ValueError, match="SHA-256"):
        PipelineCommitIntent(proposal_id=proposal_id, draft_hash="not-a-hash")


class TestEmptyStateFinalizePassthrough:
    """Tier 1.5 §7.6 followup — empty-state finalize-time passthrough.

    Audit-DB inspection of three captured rag-text-llm REDs (2026-05-06
    cohort, sessions 2cf59016, 12f061d9, 29ef178e) showed the model spent
    20+ tool calls trying to converge on a valid set_pipeline build, gave
    up, and produced honest prose explaining what it tried and what's
    blocking. The synthesizer was discarding this prose and replacing it
    with raw Pydantic noise ("source: Field required, sinks: Field
    required") that looked like a system bug to a viewer.

    The structural fix: when state is structurally empty (no source, no
    nodes, no outputs) at finalize time AND the cached/computed runtime
    preflight is invalid, the model isn't lying about completion — it's
    reporting honest failure. Pass through its content with a
    system-attributed suffix telling the user what to do next.

    These tests pin the new behaviour. The original synthesizer path
    (non-empty state with invalid preflight) is left unchanged and tested
    by sister tests above.
    """

    # ── Standalone tests on the helpers ─────────────────────────────────

    def test_state_is_structurally_empty_returns_true_for_empty_state(self) -> None:
        from elspeth.web.composer.service import _state_is_structurally_empty

        state = _empty_state()
        assert _state_is_structurally_empty(state) is True

    def test_state_is_structurally_empty_false_with_source(self) -> None:
        from elspeth.web.composer.service import _state_is_structurally_empty

        state = _empty_state().with_source(
            SourceSpec(plugin="csv", on_success="t1", options={"path": "/tmp/x.csv"}, on_validation_failure="discard")
        )
        assert _state_is_structurally_empty(state) is False

    def test_state_is_structurally_empty_false_with_output(self) -> None:
        from elspeth.web.composer.service import _state_is_structurally_empty

        state = _empty_state().with_output(
            OutputSpec(
                name="main", plugin="csv", options={"path": "/tmp/y.csv", "schema": {"mode": "observed"}}, on_write_failure="discard"
            )
        )
        assert _state_is_structurally_empty(state) is False

    def test_state_is_structurally_empty_false_with_edge(self) -> None:
        from elspeth.web.composer.service import _state_is_structurally_empty

        state = _empty_state().with_edge(
            EdgeSpec(
                id="orphan-edge",
                from_node="source",
                to_node="out",
                edge_type="on_success",
                label=None,
            )
        )

        assert _state_is_structurally_empty(state) is False

    def test_compose_empty_state_message_appends_system_suffix(self) -> None:
        from elspeth.web.composer.no_tool_policy import compose_empty_state_message as _compose_empty_state_message

        content = "I tried to build the pipeline but couldn't converge."
        msg = _compose_empty_state_message(content)
        # Original content preserved (model's prose is the truthful part).
        assert content in msg
        # System-attributed marker present (UI / scorers can detect).
        assert "[ELSPETH-SYSTEM]" in msg
        # Concrete next-step guidance.
        assert "refine" in msg.lower() or "retry" in msg.lower()

    def test_compose_empty_state_message_handles_empty_content(self) -> None:
        """Edge case: model produced no content at all. Suffix becomes the
        whole message (better than silence)."""
        from elspeth.web.composer.no_tool_policy import compose_empty_state_message as _compose_empty_state_message

        msg = _compose_empty_state_message("")
        assert "[ELSPETH-SYSTEM]" in msg
        assert msg.startswith("[ELSPETH-SYSTEM]") or msg.lstrip().startswith("[ELSPETH-SYSTEM]")

    def test_compose_empty_state_message_with_blocker_includes_cause(self) -> None:
        """When a concrete blocker is supplied (no-mutation empty-state augmentation),
        the suffix surfaces it so the operator sees the precise cause without
        having to consult the audit DB. This is defense-in-depth: the model's
        prose usually mentions the blocker, but not always.
        """
        from elspeth.web.composer.no_tool_policy import compose_empty_state_message as _compose_empty_state_message

        content = "I tried to build but the source binding failed."
        blocker = "set_pipeline returned success=false: schema: Field required"
        msg = _compose_empty_state_message(content, blocker=blocker)
        # Model prose preserved verbatim at start.
        assert msg.startswith(content)
        # System suffix attribution + cause both present.
        assert "[ELSPETH-SYSTEM]" in msg
        assert "Cause:" in msg
        assert blocker in msg

    def test_compose_empty_state_message_without_blocker_uses_generic_suffix(self) -> None:
        """The preflight-invalid empty-state augmentation does not have a
        single concrete blocker — runtime_result already carries multi-error
        data — so it passes ``blocker=None`` and gets the generic suffix.
        The ``Cause:`` field is omitted to avoid implying a cause that
        isn't there.
        """
        from elspeth.web.composer.no_tool_policy import compose_empty_state_message as _compose_empty_state_message

        msg = _compose_empty_state_message("I tried.", blocker=None)
        assert "[ELSPETH-SYSTEM]" in msg
        assert "Cause:" not in msg

    def test_enforce_augmentation_prefix_invariant_accepts_prefixed_message(self) -> None:
        """The contract holds when the augmented message has content as a strict prefix.

        Empty content is also accepted because ``"".startswith("")`` is trivially True
        and the empty-state augmentation builder degenerates to suffix-only output
        for empty inputs.
        """
        from elspeth.web.composer.no_tool_policy import enforce_augmentation_prefix_invariant as _enforce_augmentation_prefix_invariant

        _enforce_augmentation_prefix_invariant(branch="test", content="model prose", augmented="model prose [ELSPETH-SYSTEM] suffix")
        _enforce_augmentation_prefix_invariant(branch="test", content="model prose", augmented="model prose")
        _enforce_augmentation_prefix_invariant(branch="test", content="", augmented="any suffix")

    def test_enforce_augmentation_prefix_invariant_raises_on_violation(self) -> None:
        """A producer that violates the prefix invariant raises AuditIntegrityError
        rather than committing a corrupt audit row that the consumer-side
        discriminator at routes._composer_history_content would silently misroute
        as replacement (LLM gets [INTERCEPTED] prefixed onto its own prose).
        """
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.web.composer.no_tool_policy import enforce_augmentation_prefix_invariant as _enforce_augmentation_prefix_invariant

        with pytest.raises(AuditIntegrityError) as exc_info:
            _enforce_augmentation_prefix_invariant(
                branch="no_mutation_empty_state_augmentation",
                content="model prose",
                augmented="[ELSPETH-SYSTEM] something else",
            )
        message = str(exc_info.value)
        assert "Tier 1" in message
        assert "no_mutation_empty_state_augmentation" in message
        assert "augmentation" in message
        assert "discriminator" in message

    # ── _last_mutation_was_pending_proposal helper ───────────────────────

    @staticmethod
    def _proposal_invocation(
        *,
        tool_name: str = "set_pipeline",
        version: int = 1,
    ) -> Any:
        """Build an invocation whose result_canonical mirrors the proposal-payload shape.

        Mirrors the proposal_result envelope written at composer/service.py
        line ~2697: ToolResult(success=True, data={status: APPROVAL_REQUIRED, ...}).
        The blocker classifier reads result_canonical, so the test pins the
        wire shape — if the proposal payload moves, this test will catch it.
        """
        from datetime import UTC, datetime

        from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
        from elspeth.core.canonical import stable_hash

        result_payload = {
            "success": True,
            "data": {
                "status": "APPROVAL_REQUIRED",
                "proposal_id": "00000000-0000-0000-0000-000000000000",
                "tool_name": tool_name,
                "summary": "Replace the pipeline with csv input, 3 transforms, and 1 output.",
                "message": "The requested pipeline change is pending human approval and has not been applied.",
            },
        }
        canon = canonical_json(result_payload)
        h = stable_hash(result_payload)
        t = datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC)
        return ComposerToolInvocation(
            tool_call_id="call_proposal",
            tool_name=tool_name,
            arguments_canonical=b"{}",
            arguments_hash="0" * 64,
            result_canonical=canon,
            result_hash=h,
            status=ComposerToolStatus.SUCCESS,
            error_class=None,
            error_message=None,
            version_before=version,
            version_after=version,
            started_at=t,
            finished_at=t,
            latency_ms=1,
            actor="test",
        )

    def test_last_mutation_was_pending_proposal_true_for_approval_required_payload(self) -> None:
        from elspeth.web.composer.no_tool_policy import last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal

        invocations = (self._proposal_invocation(),)
        assert _last_mutation_was_pending_proposal(invocations) is True

    def test_last_mutation_was_pending_proposal_false_for_empty_invocations(self) -> None:
        from elspeth.web.composer.no_tool_policy import last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal

        assert _last_mutation_was_pending_proposal(()) is False

    def test_last_mutation_was_pending_proposal_skips_discovery_tools(self) -> None:
        """Discovery tools (get_plugin_schema, list_*) are transparent — the
        model interleaves them between real mutations. A proposal followed by
        a discovery call still counts as a pending-proposal turn."""
        from datetime import UTC, datetime

        from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
        from elspeth.web.composer.no_tool_policy import last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal

        proposal = self._proposal_invocation()
        discovery = ComposerToolInvocation(
            tool_call_id="call_discovery",
            tool_name="get_plugin_schema",
            arguments_canonical=b"{}",
            arguments_hash="0" * 64,
            result_canonical=b'{"success": true}',
            result_hash="1" * 64,
            status=ComposerToolStatus.SUCCESS,
            error_class=None,
            error_message=None,
            version_before=1,
            version_after=1,
            started_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            finished_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            latency_ms=1,
            actor="test",
        )
        assert _last_mutation_was_pending_proposal((proposal, discovery)) is True

    def test_last_mutation_was_pending_proposal_false_when_most_recent_mutation_is_arg_error(self) -> None:
        """An ARG_ERROR after a successful proposal means the model retried
        and the retry failed. The augmentation should fire."""
        from datetime import UTC, datetime

        from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, ToolArgumentErrorCategory
        from elspeth.web.composer.no_tool_policy import last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal

        proposal = self._proposal_invocation()
        arg_error = ComposerToolInvocation(
            tool_call_id="call_failed",
            tool_name="set_pipeline",
            arguments_canonical=b"{}",
            arguments_hash="0" * 64,
            result_canonical=None,
            result_hash=None,
            status=ComposerToolStatus.ARG_ERROR,
            error_class="ToolArgumentError",
            error_message="bad shape",
            version_before=1,
            version_after=None,
            started_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            finished_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            latency_ms=1,
            actor="test",
            error_category=ToolArgumentErrorCategory.MODEL_VALIDATION,
        )
        assert _last_mutation_was_pending_proposal((proposal, arg_error)) is False

    def test_last_mutation_was_pending_proposal_false_for_create_blob_success(self) -> None:
        """Blob creation is not an approval proposal or a failed graph mutation."""
        from datetime import UTC, datetime

        from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
        from elspeth.web.composer.no_tool_policy import last_mutation_was_pending_proposal as _last_mutation_was_pending_proposal

        create_blob_inv = ComposerToolInvocation(
            tool_call_id="call_blob",
            tool_name="create_blob",
            arguments_canonical=b"{}",
            arguments_hash="0" * 64,
            result_canonical=b'{"success": true, "data": {"blob_id": "abc"}}',
            result_hash="2" * 64,
            status=ComposerToolStatus.SUCCESS,
            error_class=None,
            error_message=None,
            version_before=1,
            version_after=1,
            started_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            finished_at=datetime(2026, 5, 14, 21, 29, 5, tzinfo=UTC),
            latency_ms=1,
            actor="test",
        )
        assert _last_mutation_was_pending_proposal((create_blob_inv,)) is False

    # ── End-to-end: pending-proposal suppression of empty-state augmentation ─

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["", "CLARIFY: "])
    async def test_pending_proposal_does_not_trigger_empty_state_augmentation(self, marker: str) -> None:
        """Reproduces the convergence-failure path from staging session
        d121ba9c-8775-463d-afdf-75fd6b6f2456: under trust_mode=explicit_approve
        a successful set_pipeline produces a pending proposal with
        version_after == version_before. The empty-state augmentation gate
        previously misclassified this as no-mutation and appended
        '[ELSPETH-SYSTEM] The pipeline is still empty', derailing both the
        operator's framing and (via the synthesized suffix being re-read on
        subsequent turns) the model's own state model."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        model_prose = (
            "I picked 5 Australian Government agency pages and prepared the workflow, "
            "but the platform has put the pipeline change into human-approval pending "
            "state, so it has not been applied yet."
        )

        with patch.object(service._preflight, "runtime_preflight") as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content=marker + model_prose,
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
                user_message="please build me a pipeline",
                tool_invocations=(self._proposal_invocation(),),
            )

        # Model's truthful pending-approval prose preserved verbatim.
        assert result.message == model_prose
        # No "[ELSPETH-SYSTEM] pipeline is still empty" suffix — the build
        # succeeded as a pending proposal; that is not an empty-state failure.
        assert "[ELSPETH-SYSTEM]" not in result.message
        assert "pipeline is still empty" not in result.message
        # No re-run of runtime preflight (state.version unchanged).
        mock_preflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_state_invalid_preflight_passes_through_model_content(self) -> None:
        """The captured rag-text-llm RED scenario: empty state, invalid
        cached preflight, model gave up with honest prose. Synthesizer
        must NOT fire."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        invalid_preflight = ValidationResult(
            is_valid=False,
            checks=[
                ValidationCheck(
                    name="settings_load",
                    passed=False,
                    detail="Pipeline state is empty",
                    affected_nodes=(),
                    outcome_code=None,
                )
            ],
            errors=[
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message="2 validation errors for ElspethSettings — source: Field required, sinks: Field required",
                    suggestion=None,
                    error_code=None,
                )
            ],
        )
        model_prose = (
            "I did discover the needed plugin requirements: web_scrape needs explicit "
            "schema, url_field, content_field, fingerprint_field, and http. The setup "
            "needs one more valid build pass to supply the right options."
        )

        with patch.object(service._preflight, "runtime_preflight") as mock_preflight:
            result = await service._completion._finalize_no_tool_response(
                content=model_prose,
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=invalid_preflight,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        # Model's prose preserved as the load-bearing part of the message.
        assert model_prose in result.message
        # System suffix appended.
        assert "[ELSPETH-SYSTEM]" in result.message
        # Synthesized Pydantic-noise message did NOT replace the prose.
        assert "Field required" not in result.message
        assert "I cannot mark this pipeline complete yet" not in result.message
        # Audit fields preserved (matches synthesizer-path semantics).
        assert result.raw_assistant_content == model_prose
        assert result.runtime_preflight is invalid_preflight
        # No re-run of runtime preflight (state.version unchanged).
        mock_preflight.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_state_invalid_preflight_after_state_mutation_run_preflight(self) -> None:
        """Even when the preflight is computed fresh (state.version > initial),
        the empty-state branch fires if the resulting state is empty."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        # Simulate state.version bump without populating any state fields
        # (e.g., the model called a mutation that net-emptied the state).
        bumped_state = replace(state, version=state.version + 1)
        invalid_preflight = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message="Pipeline empty",
                    suggestion=None,
                    error_code=None,
                )
            ],
        )

        with patch.object(service._preflight, "runtime_preflight", return_value=invalid_preflight):
            result = await service._completion._finalize_no_tool_response(
                content="Model prose explaining the gap.",
                state=bumped_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert "Model prose explaining the gap." in result.message
        assert "[ELSPETH-SYSTEM]" in result.message
        assert "Pipeline empty" not in result.message  # synthesizer skipped

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["", "CLARIFY: "])
    async def test_non_empty_state_invalid_preflight_augments_with_validator_suffix(self, marker: str) -> None:
        """When state is populated AND runtime preflight is invalid, the
        finalizer augments the model's prose with a system-attributed
        suffix naming the validator's objection (issue elspeth-9cfbad6901).

        Panel-evals evidence (fork_coalesce__p4_adversarial_engineer,
        boolean_routing__p3_marketingops) showed the model's prose in
        this case is typically substantive disclosure rather than a
        false completion claim. The earlier replacement-shape policy
        discarded the prose; the augmentation shape preserves it
        verbatim while still surfacing the validator's reason to the
        operator. The model's next preview_pipeline call drives self-
        correction so the [INTERCEPTED] framing is no longer required.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="t1",
                    options={"path": "/data/inputs/in.csv"},
                    on_validation_failure="discard",
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": "/data/outputs/out.csv", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )
        bumped_state = replace(state, version=state.version + 1)
        invalid_preflight = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="t1",
                    component_type="transform",
                    message="Forbidden name: 'end_of_source'",
                    suggestion="Omit trigger for end-of-source-only aggregation.",
                    error_code=None,
                )
            ],
        )
        model_prose = "The pipeline is complete and valid."

        with patch.object(service._preflight, "runtime_preflight", return_value=invalid_preflight):
            result = await service._completion._finalize_no_tool_response(
                content=marker + model_prose,
                state=bumped_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        # Augmentation prefix invariant: model prose preserved verbatim
        # at the start of the message.
        assert result.message.startswith(model_prose)
        # System-attributed suffix carrying the validator's objection.
        assert "[ELSPETH-SYSTEM]" in result.message
        assert "Forbidden name" in result.message
        assert "Suggested fix" in result.message
        # Old replacement-shape framing must not appear.
        assert "I cannot mark this pipeline complete" not in result.message
        # raw_assistant_content carries the unaugmented prose for the
        # audit trail and LLM history replay.
        assert result.raw_assistant_content == model_prose
        assert result.runtime_preflight is invalid_preflight

    @pytest.mark.asyncio
    async def test_empty_content_non_empty_state_invalid_preflight_augments_with_suffix_only(self) -> None:
        """Edge case: state is non-empty, runtime preflight is invalid,
        and the model emits empty assistant text on the finalize turn.

        Under the post-elspeth-9cfbad6901 augmentation policy, empty
        content + non-empty content + invalid preflight degenerates to
        suffix-only output (the augmentation prefix invariant is
        trivially satisfied because every string startswith ""). This
        replaces the earlier replacement-shape guard that crashed with
        AuditIntegrityError on this shape: the [INTERCEPTED] framing it
        was protecting no longer exists, so the ambiguity it guarded
        against is no longer load-bearing. The model's next
        preview_pipeline call drives self-correction.
        """
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = (
            _empty_state()
            .with_source(
                SourceSpec(
                    plugin="csv",
                    on_success="t1",
                    options={"path": "/data/inputs/in.csv"},
                    on_validation_failure="discard",
                )
            )
            .with_output(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": "/data/outputs/out.csv", "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                )
            )
        )
        bumped_state = replace(state, version=state.version + 1)
        invalid_preflight = ValidationResult(
            is_valid=False,
            checks=[],
            errors=[
                ValidationError(
                    component_id="t1",
                    component_type="transform",
                    message="Forbidden name: 'end_of_source'",
                    suggestion="Omit trigger for end-of-source-only aggregation.",
                    error_code=None,
                )
            ],
        )

        with patch.object(service._preflight, "runtime_preflight", return_value=invalid_preflight):
            result = await service._completion._finalize_no_tool_response(
                content="",
                state=bumped_state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=None,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        # Suffix-only message — system-attributed, names the validator's
        # objection. The empty-content prefix invariant is trivially
        # satisfied (every string startswith "").
        assert result.message.startswith("[ELSPETH-SYSTEM]")
        assert "Forbidden name" in result.message
        # raw_assistant_content carries the (empty) original prose so
        # the audit row records what the model actually produced.
        assert result.raw_assistant_content == ""
        assert result.runtime_preflight is invalid_preflight

    @pytest.mark.asyncio
    async def test_empty_state_valid_preflight_preserves_message_verbatim(self) -> None:
        """Sanity: empty state but valid preflight is a degenerate but
        well-defined case (an empty pipeline that "passes" preflight is
        only possible if validation is bypassed). The model's content
        must NOT be touched in this case — neither synthesizer nor
        empty-state suffix."""
        catalog = _mock_catalog()
        settings = _make_settings()
        service = ComposerServiceImpl.for_trained_operator(catalog=catalog, settings=settings)
        state = _empty_state()
        valid_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        with patch.object(service._preflight, "runtime_preflight"):
            result = await service._completion._finalize_no_tool_response(
                content="All good.",
                state=state,
                initial_version=state.version,
                user_id="user-1",
                session_id=None,
                last_runtime_preflight=valid_preflight,
                runtime_preflight_cache=service._preflight.new_cache(),
                session_scope="session:test",
            )

        assert result.message == "All good."
        assert result.raw_assistant_content is None  # no replacement happened


# ---------------------------------------------------------------------------
# Forced-repair loop coverage. When the assistant emits no tool_calls but
# preview_pipeline's proof_diagnostics still reports blocking entries, the
# compose loop synthesises a repair message and continues. Hard cap of
# _MAX_REPAIR_TURNS=2 forced repair turns; blockers that survive the cap return
# a non-runnable result. The repair NEVER catches plugin exceptions — it only
# feeds the model proof diagnostics on configurations.
# ---------------------------------------------------------------------------


class TestAttemptProofRepair:
    """Direct exercise of CompositionCompletion._attempt_proof_repair."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from datetime import UTC, datetime

        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=tmp_path)
        engine, session_id = _session_engine_with_session()
        self.engine = engine
        self.session_id = session_id

        # Seed a CSV blob whose observed columns are {order_id, customer, price}
        self.blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_orders.csv"
        body = b"order_id,customer,price\nO-1,Alice,49.95\n"
        self.storage_path.write_bytes(body)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(self.storage_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        sessions = _test_sessions_service(engine, tmp_path)
        self.service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=sessions,
            session_engine=engine,
        )
        from elspeth.contracts.session_operation import SessionOperationKind
        from tests.helpers.session_fences import ensure_session_fence

        self.authority = sessions.session_operation_authority
        ensure_session_fence(engine, session_id, owner_instance_id=sessions.session_operation_owner_instance_id)
        self.operation_context = self.authority.acquire(
            session_id=UUID(session_id),
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions.session_operation_owner_instance_id,
            lease_seconds=120,
        )
        try:
            yield
        finally:
            self.authority.release(self.operation_context)

    def _replace_blob_content(self, body: bytes) -> None:
        from elspeth.web.blobs.service import content_hash as _content_hash

        self.storage_path.write_bytes(body)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.update().where(blobs_table.c.id == self.blob_id).values(size_bytes=len(body), content_hash=_content_hash(body))
            )

    def _state_with_blocking_csv(self, *, schema: dict[str, Any] | None = None):
        """Build a blob-backed CSV state; the default schema emits the fixed-schema blocker."""
        state = _empty_state()
        catalog = _mock_catalog()
        source_schema = schema if schema is not None else {"mode": "fixed", "fields": ["order_id: str"]}
        # Wire the source via set_source_from_blob (canonical path).
        result = _execute_tool(
            "set_source_from_blob",
            {
                "blob_id": self.blob_id,
                "on_success": "rows",
                "on_validation_failure": "discard",
                "options": {"schema": source_schema},
            },
            state,
            catalog,
            session_engine=self.engine,
            session_id=self.session_id,
            session_operation_context=self.operation_context,
            session_operation_authority=self.authority,
        )
        assert result.success, result.data
        state = result.updated_state

        result = _execute_tool(
            "set_output",
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {
                    "path": "outputs/out.json",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    def _state_without_blob(self):
        """A path-based source has nothing for proof_diagnostics to inspect."""
        state = _empty_state()
        catalog = _mock_catalog()
        result = _execute_tool(
            "set_pipeline",
            {
                "sources": {
                    "primary": {
                        "plugin": "csv",
                        "on_success": "rows",
                        "options": {"path": "/data/in.csv", "schema": {"mode": "observed"}},
                        "on_validation_failure": "discard",
                    }
                },
                "nodes": [],
                "edges": [],
                "outputs": [
                    {
                        "sink_name": "out",
                        "plugin": "csv",
                        "options": {"path": "/data/out.csv", "schema": {"mode": "observed"}},
                        "on_write_failure": "discard",
                    }
                ],
            },
            state,
            catalog,
        )
        assert result.success, result.data
        return result.updated_state

    def test_returns_clear_when_no_blocking_diagnostics(self) -> None:
        from elspeth.web.composer.tools.generation import ProofNotApplicable

        absent = self.service._completion._assess_source_proof(
            self._state_without_blob(),
            session_id=self.session_id,
            session_operation_context=self.operation_context,
        )
        assert type(absent) is ProofNotApplicable
        state = self._state_with_blocking_csv(schema={"mode": "observed"})
        messages: list[dict[str, Any]] = []
        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=0,
        )
        assert outcome.action == "clear"
        assert outcome.blocking_diagnostics == ()
        assert messages == []

    def _evaluated_proof(self, state: CompositionState):
        from elspeth.web.composer.tools.generation import ProofEvaluated

        proof = self.service._completion._assess_source_proof(
            state,
            session_id=self.session_id,
            session_operation_context=self.operation_context,
        )
        assert type(proof) is ProofEvaluated
        return proof

    def test_returns_blocked_when_budget_exhausted(self) -> None:

        state = self._state_with_blocking_csv()
        messages: list[dict[str, Any]] = []
        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=_MAX_REPAIR_TURNS,
        )
        assert outcome.action == "blocked"
        assert {diagnostic["code"] for diagnostic in outcome.blocking_diagnostics} == {"csv_fixed_schema_omits_observed_columns"}
        assert messages == []

    def test_appends_repair_message_when_blocking(self) -> None:
        state = self._state_with_blocking_csv()
        messages: list[dict[str, Any]] = []
        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=0,
        )
        assert outcome.action == "repair_injected"
        assert outcome.blocking_diagnostics
        assert len(messages) == 1
        msg = messages[0]
        assert msg["role"] == "user"
        assert "csv_fixed_schema_omits_observed_columns" in msg["content"]
        assert "Suggested repair" in msg["content"]
        assert "preview_pipeline" in msg["content"]
        # Budget note acknowledges the cap
        assert "proof review turn 1 of 2" in msg["content"]
        assert "If the user's active request authorizes changes" in msg["content"]
        assert "answer that request without changing the pipeline" in msg["content"]

    def test_duplicate_header_repair_payload_withholds_raw_header_values(self) -> None:
        sentinel = "ELSPETH_DUPLICATE_HEADER_SENTINEL_7F3A"
        self._replace_blob_content((f"order_id,{sentinel},{sentinel},price\nO-1,Alice,Smith,49.95\n").encode())
        state = self._state_with_blocking_csv(schema={"mode": "observed"})
        messages: list[dict[str, Any]] = []

        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=0,
        )

        assert outcome.action == "repair_injected"
        assert any(d["code"] == "csv_duplicate_headers" for d in outcome.blocking_diagnostics)
        provider_payload = messages[0]["content"]
        durable_diagnostics = repr(outcome.blocking_diagnostics)
        assert sentinel not in provider_payload
        assert sentinel not in durable_diagnostics
        assert "csv_duplicate_headers" in provider_payload
        assert "duplicate header" in provider_payload
        assert "field_mapping" not in provider_payload
        assert "on_validation_failure" not in provider_payload
        assert "quarantine" not in provider_payload
        assert "correct" in provider_payload
        assert "re-upload" in provider_payload
        assert "headerless" in provider_payload

    def test_duplicate_header_raw_warning_is_not_relayed_to_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import elspeth.web.composer.tools.generation as generation_module
        from elspeth.web.composer.source_inspection import SourceInspectionFacts

        raw_warning_sentinel = "RAW_GENERATION_SENTINEL_91D2"
        raw_facts = SourceInspectionFacts(
            source_kind="csv",
            redacted_identity={"filename": "orders.csv", "mime_type": "text/csv", "byte_size": "1"},
            byte_range_inspected=(0, 1),
            sample_row_count=1,
            observed_headers=("order_id", "repeated", "repeated", "price"),
            inferred_types=None,
            url_candidates=(),
            warnings=(f"csv_duplicate_headers: {raw_warning_sentinel}",),
        )
        monkeypatch.setattr(generation_module, "inspect_csv_source_content", lambda **_kwargs: raw_facts)
        state = self._state_with_blocking_csv(schema={"mode": "observed"})
        messages: list[dict[str, Any]] = []

        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=0,
        )

        duplicate = next(d for d in outcome.blocking_diagnostics if d["code"] == "csv_duplicate_headers")
        serialized_provider_content = json.dumps(
            {
                "blocking_diagnostic": deep_thaw(duplicate),
                "provider_message": messages[0],
            },
            sort_keys=True,
        )
        assert outcome.action == "repair_injected"
        assert raw_warning_sentinel not in serialized_provider_content
        assert duplicate["evidence_locator"]["duplicate_header_class_count"] == 1
        assert duplicate["evidence_locator"]["duplicate_header_column_count"] == 2
        assert tuple(duplicate["evidence_locator"]["duplicate_header_positions"]) == (2, 3)
        assert duplicate["evidence_locator"]["header_values_redacted"] is True
        assert "re-upload" in messages[0]["content"]
        assert "field_mapping" not in messages[0]["content"]

    def test_second_repair_message_increments_turn_counter_in_text(self) -> None:
        state = self._state_with_blocking_csv()
        messages: list[dict[str, Any]] = []
        # Simulate one already-used repair turn
        outcome = self.service._completion._attempt_proof_repair(
            proof=self._evaluated_proof(state),
            llm_messages=messages,
            repair_turns_used=1,
        )
        assert outcome.action == "repair_injected"
        assert "proof review turn 2 of 2" in messages[0]["content"]

    def test_repair_does_not_catch_plugin_exceptions(self) -> None:
        """Plugin exceptions must propagate — the repair gate only handles configs.

        Patch compute_proof_diagnostics to raise a synthetic 'plugin bug';
        The acquisition boundary must propagate it before repair admission.
        """
        from elspeth.web.composer import composition_completion as svc_module

        with (
            patch.object(svc_module, "compute_proof_diagnostics", side_effect=RuntimeError("simulated plugin crash")),
            pytest.raises(RuntimeError, match="simulated plugin crash"),
        ):
            self.service._completion._assess_source_proof(
                self._state_with_blocking_csv(),
                session_id=self.session_id,
                session_operation_context=self.operation_context,
            )

    def test_repair_message_caps_at_three_blockers(self) -> None:
        """When 4+ blocking diagnostics fire, the repair message renders only
        the first three to keep the LLM's context window manageable. The
        cap is documented in the synthesizer; this test pins it so future
        edits cannot relax the bound silently.

        The test supplies a deterministic evaluated proof with five blockers;
        the repair renderer consumes evidence rather than acquiring it again.
        """
        from elspeth.web.composer.tools.generation import ProofEvaluated

        fake_blockers = [
            {
                "severity": "blocking",
                "code": f"fake_blocker_{i}",
                "message": f"fake message {i}",
                "suggested_repair": f"fake repair {i}",
                "evidence_locator": {"path": f"$.blocker[{i}]"},
            }
            for i in range(5)
        ]
        messages: list[dict[str, Any]] = []
        outcome = self.service._completion._attempt_proof_repair(
            proof=ProofEvaluated(tuple(fake_blockers)),
            llm_messages=messages,
            repair_turns_used=0,
        )
        assert outcome.action == "repair_injected"
        assert len(messages) == 1
        body = messages[0]["content"]
        # First three blockers rendered.
        assert "fake_blocker_0" in body
        assert "fake_blocker_1" in body
        assert "fake_blocker_2" in body
        # Fourth and fifth blockers omitted from the synthesised message.
        assert "fake_blocker_3" not in body
        assert "fake_blocker_4" not in body


# ---------------------------------------------------------------------------
# End-to-end forced-repair loop coverage. Drives the real ``_compose_loop``
# ``while True`` through real repair turns with a scripted LLM at the
# ``_call_llm`` boundary. Bugs that would land silently otherwise:
# stale ``repair_turns_used``, missing ``continue`` after the repair message,
# ``replace(result, ...)`` clobbering, divergence behaviour when repair
# makes things worse, hard cap not respected.
# ---------------------------------------------------------------------------


class TestComposeLoopForcedRepair:
    """Exercise ``ComposerServiceImpl.compose`` through scripted repair turns.

    The scripted LLM is wired at ``_call_llm`` (network seam); ``execute_tool``
    runs for real, so ``set_source_from_blob`` / ``patch_source_options`` /
    ``set_output`` produce authentic state mutations and ``compute_proof_diagnostics``
    inspects authentic blob bytes. ``_runtime_preflight`` is patched to a
    permissive result so finalization is bounded by the proof gate alone.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path):
        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        catalog = _mock_catalog()
        settings = _make_settings(data_dir=tmp_path)
        engine, session_id = _session_engine_with_session()
        self.engine = engine
        self.session_id = session_id

        # CSV with three observed columns. Pipeline configured with
        # mode='fixed' + fields=['order_id: str'] + on_validation_failure='discard'
        # triggers the csv_fixed_schema_omits_observed_columns blocking
        # diagnostic in compute_proof_diagnostics.
        self.blob_id = str(uuid4())
        storage_dir = tmp_path / "blobs" / session_id
        storage_dir.mkdir(parents=True)
        self.storage_path = storage_dir / f"{self.blob_id}_orders.csv"
        body = b"order_id,customer,price\nO-1,Alice,49.95\n"
        self.storage_path.write_bytes(body)
        with engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=self.blob_id,
                    session_id=session_id,
                    filename="orders.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(self.storage_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        self.service = ComposerServiceImpl.for_trained_operator(
            catalog=catalog,
            settings=settings,
            sessions_service=_test_sessions_service(engine, tmp_path),
            session_engine=engine,
        )

    def _wire_blocking_pipeline_tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls that establish the blocking csv_fixed_schema_omits_observed_columns state."""
        return [
            {
                "id": "call_source",
                "name": "set_source_from_blob",
                "arguments": {
                    "blob_id": self.blob_id,
                    "on_success": "rows",
                    "on_validation_failure": "discard",
                    "options": {"schema": {"mode": "fixed", "fields": ["order_id: str"]}},
                },
            },
            {
                "id": "call_output",
                "name": "set_output",
                "arguments": {
                    "sink_name": "out",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                },
            },
        ]

    def _repair_tool_call(self) -> list[dict[str, Any]]:
        """Tool call that fixes the blocking diagnostic.

        Switch from ``mode='fixed'`` (which omitted observed columns) to
        ``mode='observed'`` (auto-infer types from data). ``mode='flexible'``
        without explicit ``fields`` is rejected by the csv source's
        config-model prevalidation, so observed is the cleanest repair.
        """
        return [
            {
                "id": "call_repair",
                "name": "patch_source_options",
                "arguments": {"patch": {"schema": {"mode": "observed"}}},
            },
        ]

    def _wire_uploaded_blob_to_output_tool_calls(self) -> list[dict[str, Any]]:
        """Tool calls for the uploaded-CSV happy path after an empty-state stall."""
        return [
            {
                "id": "call_source",
                "name": "set_source_from_blob",
                "arguments": {
                    "blob_id": self.blob_id,
                    "on_success": "out",
                    "on_validation_failure": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
            },
            {
                "id": "call_output",
                "name": "set_output",
                "arguments": {
                    "sink_name": "out",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                },
            },
        ]

    def _from_json_fallback_pipeline_tool_calls(self, blob_id: str) -> list[dict[str, Any]]:
        """Tool call for the p4_t2_edge fallback after unsupported from_json."""
        return [
            {
                "id": "call_set_pipeline",
                "name": "set_pipeline",
                "arguments": {
                    "source": {
                        "plugin": "csv",
                        "blob_id": blob_id,
                        "on_success": "rows",
                        "on_validation_failure": "discard",
                        "options": {"schema": {"mode": "observed"}},
                    },
                    "nodes": [
                        {
                            "id": "gate_signup",
                            "node_type": "gate",
                            "input": "rows",
                            "condition": "row['event_type'] == 'signup'",
                            "routes": {"true": "signups", "false": "non_signup"},
                        },
                        {
                            "id": "gate_upgrade",
                            "node_type": "gate",
                            "input": "non_signup",
                            "condition": "row['event_type'] == 'upgrade'",
                            "routes": {"true": "upgrades", "false": "other"},
                        },
                    ],
                    "edges": [],
                    "outputs": [
                        {
                            "sink_name": "signups",
                            "plugin": "json",
                            "options": {
                                "path": "outputs/signups.jsonl",
                                "format": "jsonl",
                                "schema": {"mode": "observed"},
                                "mode": "write",
                                "collision_policy": "auto_increment",
                            },
                            "on_write_failure": "discard",
                        },
                        {
                            "sink_name": "upgrades",
                            "plugin": "json",
                            "options": {
                                "path": "outputs/upgrades.jsonl",
                                "format": "jsonl",
                                "schema": {"mode": "observed"},
                                "mode": "write",
                                "collision_policy": "auto_increment",
                            },
                            "on_write_failure": "discard",
                        },
                        {
                            "sink_name": "other",
                            "plugin": "json",
                            "options": {
                                "path": "outputs/other.jsonl",
                                "format": "jsonl",
                                "schema": {"mode": "observed"},
                                "mode": "write",
                                "collision_policy": "auto_increment",
                            },
                            "on_write_failure": "discard",
                        },
                    ],
                    "metadata": {"name": "events-routing-fallback"},
                },
            }
        ]

    @pytest.mark.asyncio
    async def test_empty_state_uploaded_blob_stall_recovers_through_actual_end(self) -> None:
        """Actual END rejects an unsupported header-only claim before the provider builds."""
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        turn1_stall = _make_llm_response(
            content="The uploaded CSV appears to contain only the header row, so I cannot build yet.",
            tool_calls=None,
        )
        turn2_build = _make_llm_response(content=None, tool_calls=self._wire_uploaded_blob_to_output_tool_calls())
        turn3_done = _make_llm_response(content="Ready.", tool_calls=None)

        empty = _empty_state()
        request = 'Build from my uploaded CSV using this specification: "CSV source to JSON output; preserve every observed column".'
        user_message_id = _insert_user_message(self.engine, self.session_id, request)
        script = _SemanticRecoveryLLMScript(
            self.service,
            request,
            (turn1_stall, turn2_build, turn3_done),
            phases=("primary", "end", "primary", "early", "primary", "end"),
            first_findings="The header-only claim lacks current source inspection, and the authorized uploaded-source build remains unstarted.",
        )
        with (
            _real_semantic_recovery_sdk(script),
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            result = await self.service.compose(
                request,
                [],
                empty,
                session_id=self.session_id,
                user_id="test-user",
                user_message_id=user_message_id,
            )

        script.assert_finished()
        assert len(script.primary_requests) == 3
        assert result.repair_turns_used == 0
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].options["blob_ref"] == self.blob_id
        # Uploaded blob: no auto-declared guaranteed_fields (John's ruling
        # 2026-08-27, elspeth-da68332faf) — the schema stays bare observed.
        assert dict(result.state.sources["source"].options["schema"]) == {"mode": "observed"}
        assert result.state.outputs[0].name == "out"
        assert result.runtime_preflight is not None and result.runtime_preflight.is_valid is True

        initial_review = json.loads(script.end_inputs[0].payload_json)
        final_review = json.loads(script.end_inputs[1].payload_json)
        assert initial_review["saved_effects"]["saved_revision_count"] == 0
        assert initial_review["inspection"]["currently_available"] == []
        assert final_review["saved_effects"]["saved_revision_count"] > 0
        assert final_review["saved_effects"]["net_changed"] is True

    @pytest.mark.asyncio
    async def test_inspect_source_receipt_reaches_actual_end_after_primary_delivery(self) -> None:
        """Exercise real dispatch, consumer audit and physical END, without direct registration."""
        request = "Inspect my uploaded CSV and retain its source evidence for review."
        user_message_id = _insert_user_message(self.engine, self.session_id, request)
        stall = _make_llm_response(content="I have not inspected the source yet.")
        inspect_and_build = _make_llm_response(
            tool_calls=[
                {"id": "inspect-owned-source", "name": "inspect_source", "arguments": {"blob_id": self.blob_id, "encoding": "utf-8"}},
                *self._wire_uploaded_blob_to_output_tool_calls(),
            ]
        )
        done = _make_llm_response(content="The source has been inspected and the draft is ready for review.")
        script = _SemanticRecoveryLLMScript(
            self.service,
            request,
            (stall, inspect_and_build, done),
            phases=("primary", "end", "primary", "early", "primary", "end"),
            first_findings="Inspect the uploaded source before reporting its measured size.",
        )
        with (
            _real_semantic_recovery_sdk(script),
            patch.object(
                self.service._preflight,
                "runtime_preflight",
                return_value=ValidationResult(is_valid=True, checks=[], errors=[]),
            ),
        ):
            await self.service.compose(
                request,
                [],
                _empty_state(),
                session_id=self.session_id,
                user_id="test-user",
                user_message_id=user_message_id,
            )
        script.assert_finished()
        initial = json.loads(script.end_inputs[0].payload_json)["inspection"]
        assert "source_metadata_currently_available" not in initial
        inspection_messages = [
            message
            for message in script.primary_requests[-1]["messages"]
            if message["role"] == "tool" and message["tool_call_id"] == "inspect-owned-source"
        ]
        assert len(inspection_messages) == 1
        delivered = json.loads(inspection_messages[0]["content"])["data"]
        assert delivered["redacted_identity"]["blob_id"] == self.blob_id
        assert any(
            warning.startswith("csv_explicit_encoding: utf-8;")
            and "applies only to this bounded inspection" in warning
            and "does not configure the pipeline source" in warning
            for warning in delivered["warnings"]
        )
        inspection = json.loads(script.end_inputs[-1].provider_payload_json)["inspection"]
        assert inspection["currently_available"] == []
        assert inspection["historically_delivered"] == []
        metadata = inspection["source_metadata_currently_available"]
        assert metadata == inspection["source_metadata_historically_delivered"]
        assert len(metadata) == 1
        assert metadata[0]["blob_id"] == self.blob_id
        assert metadata[0]["total_size_bytes"] == int(delivered["redacted_identity"]["byte_size"]) == self.storage_path.stat().st_size
        assert metadata[0]["byte_range_inspected"] == delivered["byte_range_inspected"]
        assert metadata[0]["source_kind"] == delivered["source_kind"] == "csv"
        assert metadata[0]["receipt_envelope_hash"] == stable_hash(inspection_messages[0]["content"])
        final_end = script.requests[-1]
        assert script.end_inputs[-1].provider_payload_json in final_end["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_unsupported_from_json_recovery_retains_authorized_fallback_build(self, tmp_path: Path) -> None:
        """Unsupported requested syntax must not strand the session in empty state.

        Regression for elspeth-164d7078cb / hard-mode p4_t2_edge: the model
        correctly rejected ``from_json(payload)`` for ``value_transform`` but
        then kept returning prose instead of committing the supported fallback
        the user accepted: keep ``payload`` as a string, gate on
        ``event_type``, and emit the three JSONL sinks.
        """
        from elspeth.web.blobs.service import content_hash as _content_hash
        from elspeth.web.sessions.models import blobs_table

        event_blob_id = str(uuid4())
        body = (
            b"event_id,user_id,event_type,timestamp,payload\n"
            b'EV-1,U-101,signup,2026-09-01T10:00:00Z,{"plan":"free"}\n'
            b"EV-2,U-102,login,2026-09-01T10:05:00Z,{}\n"
            b'EV-3,U-101,upgrade,2026-09-02T11:00:00Z,{"to_plan":"pro"}\n'
        )
        storage_path = tmp_path / "blobs" / self.session_id / f"{event_blob_id}_events.csv"
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(body)
        with self.engine.begin() as conn:
            conn.execute(
                blobs_table.insert().values(
                    id=event_blob_id,
                    session_id=self.session_id,
                    filename="events.csv",
                    mime_type="text/csv",
                    size_bytes=len(body),
                    content_hash=_content_hash(body),
                    storage_path=str(storage_path),
                    created_at=datetime.now(UTC),
                    created_by="user",
                    source_description=None,
                    status="ready",
                )
            )

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        turn1_refusal = _make_llm_response(
            content=(
                "The value_transform expression from_json(payload) is unsupported here. "
                "I can keep payload as a string and route on event_type."
            ),
            tool_calls=None,
        )
        turn2_build = _make_llm_response(content=None, tool_calls=self._from_json_fallback_pipeline_tool_calls(event_blob_id))
        turn3_done = _make_llm_response(content="Built the supported fallback.", tool_calls=None)

        empty = _empty_state()
        request = (
            'Build the accepted specification from uploaded events.csv: "Parse payload with from_json(payload). '
            'If unsupported, keep payload as a string and route by event_type into signups, upgrades and other JSONL outputs".'
        )
        user_message_id = _insert_user_message(self.engine, self.session_id, request)
        script = _SemanticRecoveryLLMScript(
            self.service,
            request,
            (turn1_refusal, turn2_build, turn3_done),
            phases=("primary", "end", "primary", "early", "primary", "end"),
            first_findings="The accepted fallback remains unbuilt; unsupported parsing does not cancel the authorized routing and outputs.",
        )
        with (
            _real_semantic_recovery_sdk(script),
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            result = await self.service.compose(
                request,
                [],
                empty,
                session_id=self.session_id,
                user_id="test-user",
                user_message_id=user_message_id,
            )

        script.assert_finished()
        assert len(script.primary_requests) == 3
        assert result.repair_turns_used == 0
        assert result.state.sources["source"].options["blob_ref"] == event_blob_id
        assert {node.id for node in result.state.nodes} == {"gate_signup", "gate_upgrade"}
        assert {output.name for output in result.state.outputs} == {"signups", "upgrades", "other"}
        assert result.runtime_preflight is not None and result.runtime_preflight.is_valid is True

        initial_review = json.loads(script.end_inputs[0].payload_json)
        final_review = json.loads(script.end_inputs[1].payload_json)
        assert initial_review["saved_effects"]["saved_revision_count"] == 0
        assert final_review["saved_effects"]["saved_revision_count"] > 0
        assert final_review["saved_effects"]["net_changed"] is True

    def _futile_repair_tool_call(self, call_id: str, name_value: str) -> list[dict[str, Any]]:
        """Tool call that mutates state but does NOT clear the proof blocker.

        Calls ``set_metadata`` (state.version bumps) without touching the
        source's schema. The original ``csv_fixed_schema_omits_observed_columns``
        diagnostic survives. Used to verify the repair-budget cap holds
        when the model mutates without applying a useful repair.
        """
        return [
            {
                "id": call_id,
                "name": "set_metadata",
                "arguments": {"patch": {"name": name_value}},
            },
        ]

    @pytest.mark.asyncio
    async def test_happy_repair_path(self) -> None:
        """Turn 1 establishes blocker, turn 2 claims completion, turn 3 repairs.

        Loop sequence:
          - Turn 1 (LLM): tool_calls = [set_source_from_blob (blocking schema), set_output]
          - Turn 2 (LLM): no tool_calls, claims completion
            → _attempt_proof_repair fires, sees blocking diagnostic, injects
              repair message, loop continues
          - Turn 3 (LLM): tool_calls = [patch_source_options(mode=flexible)]
          - Turn 4 (LLM): no tool_calls, claims completion
            → no blocking diagnostic now, finalize cleanly

        Assertions:
          - result.repair_turns_used == 1
          - synthesised repair message reaches the LLM history
          - is_valid True (no blocking diagnostic remaining)
        """
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        turn1 = _make_llm_response(content=None, tool_calls=self._wire_blocking_pipeline_tool_calls())
        turn2_done = _make_llm_response(content="All set.", tool_calls=None)
        turn3 = _make_llm_response(content=None, tool_calls=self._repair_tool_call())
        turn4_done = _make_llm_response(content="Repaired and ready.", tool_calls=None)

        empty = _empty_state()
        with (
            patch.object(self.service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [turn1, turn2_done, turn3, turn4_done]
            result = await self.service.compose(
                "Build a pipeline",
                [],
                empty,
                session_id=self.session_id,
                user_id="test-user",
            )

        # Loop ran exactly four LLM turns: build, claim-complete, repair,
        # claim-complete-again.
        assert mock_llm.call_count == 4, f"expected 4 LLM calls, got {mock_llm.call_count}"
        # repair_turns_used == 1 — the model needed exactly one forced
        # repair turn to clear the diagnostic.
        assert result.repair_turns_used == 1
        # Final state has observed schema (the repair landed) and is_valid.
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].options["schema"] == {"mode": "observed"}
        assert result.runtime_preflight is not None and result.runtime_preflight.is_valid is True
        # The synthesised repair message reaches the LLM history before
        # turn 3 — inspect the messages of turn 3 (index 2).
        turn3_messages = mock_llm.call_args_list[2].args[0]
        repair_msgs = [
            m
            for m in turn3_messages
            if isinstance(m, dict)
            and m.get("role") == "user"
            and "[composer-system]" in str(m.get("content", ""))
            and "csv_fixed_schema_omits_observed_columns" in str(m.get("content", ""))
        ]
        assert len(repair_msgs) == 1, f"exactly one composer-system repair message must precede turn 3; got {len(repair_msgs)}"
        # Final assistant content reflects turn 4's claim, not turn 2's
        # (which was preempted by the repair gate).
        _assert_checkpoint_reply(result, "Repaired and ready.", count=3, changed=True)

    @pytest.mark.asyncio
    async def test_divergence_repair_does_not_clear_blocker(self) -> None:
        """Repair turns mutate state but never address the proof blocker.

        The model claims completion, sees the repair message, then on
        each repair turn issues a tool call that mutates *something else*
        (here: pipeline metadata.name) without addressing the source
        schema. ``state.version`` bumps each turn, the proof gate fires
        each ``no-tool-call`` turn while ``state.version > initial_version``,
        and the loop terminates when ``repair_turns_used`` reaches
        ``_MAX_REPAIR_TURNS``.

        Loop sequence (6 LLM turns):
          - Turn 1: tool_calls = [set_source_from_blob (blocker), set_output]
          - Turn 2: claim complete → repair fires, repair_turns_used=1
          - Turn 3: tool_calls = [set_metadata(name='attempt-1')] (no-op
            for the blocker)
          - Turn 4: claim complete → repair fires, repair_turns_used=2
          - Turn 5: tool_calls = [set_metadata(name='attempt-2')]
          - Turn 6: claim complete → repair_turns_used==_MAX_REPAIR_TURNS,
            repair gate returns an explicit non-runnable blocker.
        """

        # Sanity check on the constant — keeps the test honest if the
        # cap shifts.
        assert _MAX_REPAIR_TURNS == 2

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        turns = [
            _make_llm_response(content=None, tool_calls=self._wire_blocking_pipeline_tool_calls()),
            _make_llm_response(content="claim 1", tool_calls=None),
            _make_llm_response(content=None, tool_calls=self._futile_repair_tool_call("call_a", "attempt-1")),
            _make_llm_response(content="claim 2", tool_calls=None),
            _make_llm_response(content=None, tool_calls=self._futile_repair_tool_call("call_b", "attempt-2")),
            _make_llm_response(content="final", tool_calls=None),
        ]

        empty = _empty_state()
        with (
            patch.object(self.service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(
                self.service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                autospec=True,
                return_value=AdvisorCheckpointVerdict(ok=False, blocking=False, findings_text="", failure_class="unavailable"),
            ),
        ):
            mock_llm.side_effect = turns
            result = await self.service.compose(
                "Build something",
                [],
                empty,
                session_id=self.session_id,
                user_id="test-user",
            )

        # Six LLM turns total; the third claim-complete is blocked because
        # the repair budget is exhausted.
        assert mock_llm.call_count == 6, f"expected 6 LLM calls, got {mock_llm.call_count}"
        assert result.repair_turns_used == _MAX_REPAIR_TURNS
        # Source still has the original fixed-mode blocking schema; the
        # futile mutations never touched the source.
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].options["schema"]["mode"] == "fixed"
        # Metadata reflects the most recent (futile) repair attempt.
        assert result.state.metadata.name == "attempt-2"
        # The model's final claim is preserved for audit, but a backend-owned
        # blocker prevents ordinary successful finalisation.
        assert result.raw_assistant_content != "final"
        assert not result.message.startswith("final")
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is False
        assert result.runtime_preflight.readiness.authoring_valid is False
        assert result.runtime_preflight.readiness.completion_ready is False
        assert result.runtime_preflight.readiness.execution_ready is False
        assert {error.error_code for error in result.runtime_preflight.errors} == {"proof_repair_exhausted"}

    @pytest.mark.asyncio
    async def test_cap_respected_when_model_never_repairs(self) -> None:
        """Model claims completion but never repairs — the cap blocks finalization.

        The blocker remains every time the proof step fires. The repair
        budget is exhausted; finalization returns a non-runnable blocker.
        ``is_valid`` must reflect the blocking proof
        diagnostic (forced False by the proof gate even when authoring/
        runtime preflight pass).
        """

        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])
        # Turn 1 establishes the blocker. Turns 2..N all claim completion
        # without applying a fix. The repair gate fires after each
        # state-mutating turn — but turns 2..N have no tool calls, so
        # state.version doesn't bump on those turns. The proof gate fires
        # only when ``state.version > initial_version``, which holds
        # from turn 2 onwards (turn 1 mutated). Two repair-fires later
        # (turns 2 and 3 since the cap is 2), the third claim-complete
        # finalises.
        turns = [
            _make_llm_response(content=None, tool_calls=self._wire_blocking_pipeline_tool_calls()),
            _make_llm_response(content="claim 1", tool_calls=None),
            _make_llm_response(content="claim 2", tool_calls=None),
            _make_llm_response(content="final", tool_calls=None),
        ]

        empty = _empty_state()
        with (
            patch.object(self.service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
            patch.object(
                self.service._advisor_checkpoint,
                "_run_advisor_checkpoint",
                autospec=True,
                return_value=AdvisorCheckpointVerdict(ok=False, blocking=False, findings_text="", failure_class="unavailable"),
            ),
        ):
            mock_llm.side_effect = turns
            result = await self.service.compose(
                "Build something",
                [],
                empty,
                session_id=self.session_id,
                user_id="test-user",
            )

        # Four LLM turns: 1 mutating + 3 claim-complete (the first two
        # claim-completes fire repair injection, the third is final).
        assert mock_llm.call_count == 4
        assert result.repair_turns_used == _MAX_REPAIR_TURNS
        assert result.raw_assistant_content != "final"
        assert not result.message.startswith("final")
        # The model never applied a fix → schema still 'fixed' with the
        # original blocker config; runtime_preflight may still be valid
        # (it was patched to return is_valid=True), but the runtime gate
        # is not the proof gate. Verify the source state is unchanged.
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].options["schema"]["mode"] == "fixed"
        assert result.runtime_preflight is not None
        assert result.runtime_preflight.is_valid is False
        assert {blocker.code for blocker in result.runtime_preflight.readiness.blockers} == {"proof_repair_exhausted"}

    @pytest.mark.asyncio
    async def test_repair_gate_fires_on_first_turn_of_resumed_session(self) -> None:
        """Resumed session with a pre-bound blob-backed source + blocking
        diagnostic must trigger the repair gate on its first compose turn,
        even though the LLM has not mutated state yet.

        Regression for the ``state.version > initial_version`` guard issue:
        on a resumed session, the source was bound on a prior turn, so the
        first turn after resume has ``state.version == initial_version``.
        With the version guard, the gate skipped — exactly the cross-turn
        scenario it exists to catch (e.g.
        ``csv_fixed_schema_omits_observed_columns`` blockers persisting
        through session resume). The corrected predicate fires whenever
        the proof step is applicable (source is blob-backed and bound).
        """
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        # Resumed-session state: source is already bound to the blob with
        # the blocking schema config. No state mutation occurred this turn —
        # the LLM's first response on resume claims completion.
        # ``path`` mirrors the blob's canonical storage_path because the csv
        # source plugin requires it (set_source_from_blob populates it on
        # the binding turn; we reproduce that here for the resumed state).
        # ``on_success="out"`` matches the named output below so the graph
        # validates (preventing patch_source_options from rejecting the
        # repair due to a dangling connection unrelated to this regression).
        resumed_state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="out",
                options={
                    "blob_ref": self.blob_id,
                    "path": str(self.storage_path),
                    "schema": {"mode": "fixed", "fields": ["order_id: str"]},
                },
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="out",
                    plugin="json",
                    options={
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=7,  # Resumed mid-session — version is already non-trivial.
        )

        # Turn 1: claim completion immediately (no tool_calls). The repair
        # gate must fire here even though state.version was not advanced.
        # Turn 2: apply the repair. Turn 3: claim completion again, gate
        # finds no blocker, finalize cleanly.
        turn1_done = _make_llm_response(content="Already configured.", tool_calls=None)
        turn2_repair = _make_llm_response(content=None, tool_calls=self._repair_tool_call())
        turn3_done = _make_llm_response(content="Repaired and ready.", tool_calls=None)

        with (
            patch.object(self.service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [turn1_done, turn2_repair, turn3_done]
            result = await self.service.compose(
                "Continue building",
                [],
                resumed_state,
                session_id=self.session_id,
                user_id="test-user",
            )

        # The gate fired on turn 1 (no-mutation, version unchanged), forced
        # the model into a repair turn, and the loop converged.
        assert mock_llm.call_count == 3, f"expected 3 LLM calls, got {mock_llm.call_count}"
        assert result.repair_turns_used == 1
        # Turn 2 received the synthesised repair message before the model
        # acted on it — verify the diagnostic landed in the LLM history.
        turn2_messages = mock_llm.call_args_list[1].args[0]
        repair_msgs = [
            m
            for m in turn2_messages
            if isinstance(m, dict)
            and m.get("role") == "user"
            and "[composer-system]" in str(m.get("content", ""))
            and "csv_fixed_schema_omits_observed_columns" in str(m.get("content", ""))
        ]
        assert len(repair_msgs) == 1, f"expected one synthesised repair message in turn-2 history, found: {turn2_messages}"
        # Final state is repaired — the schema mode is now observed.
        assert result.state.sources["source"] is not None
        assert result.state.sources["source"].options["schema"] == {"mode": "observed"}

    @pytest.mark.asyncio
    async def test_repair_gate_skipped_when_source_is_not_blob_backed(self) -> None:
        """When the proof step is not applicable (no blob-backed source),
        the gate skips even if the LLM claims completion.

        This pins the converse of the resumed-session test: chat-only
        turns and path-based-source pipelines must not pay the proof
        step's cost when there is nothing to inspect.
        """
        passing_preflight = ValidationResult(is_valid=True, checks=[], errors=[])

        # Path-based source — no blob_ref, so compute_proof_diagnostics
        # would short-circuit and the gate has nothing to do.
        path_source_state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "/tmp/never-read.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(),
            outputs=(
                OutputSpec(
                    name="out",
                    plugin="json",
                    options={
                        "path": "outputs/out.json",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=4,
        )

        turn1_done = _make_llm_response(content="All set.", tool_calls=None)

        with (
            patch.object(self.service._provider_gateway, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch.object(self.service._preflight, "runtime_preflight", return_value=passing_preflight),
        ):
            mock_llm.side_effect = [turn1_done]
            result = await self.service.compose(
                "Anything else?",
                [],
                path_source_state,
                session_id=self.session_id,
                user_id="test-user",
            )

        # Gate skipped — only one LLM call, no repair turns.
        assert mock_llm.call_count == 1
        assert result.repair_turns_used == 0
