"""A review card the writer refuses and the surfacer skips leaves one warning line.

Before this line a refused ``create_pending_interpretation_event`` in repair
mode (or for an oversized prompt file, or for any non-prompt kind) was skipped
with no trace: the state kept ``interpretation_review_pending`` with no card
and nothing said why. The line carries identifiers and the exception class
only; the refusal message embeds the user term and is never logged.

The second half covers the more common silent path: runtime findings make the
no-tool finalize tail return before surfacing at all, which now leaves one
code-only line too.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import structlog
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_interpretation import InterpretationKind, InterpretationSurfaceOrigin
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.interpretation_surfacing import (
    _auto_surface_prompt_template_reviews_for_state,
    _surface_pending_interpretation_reviews_under_writer,
)
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.execution.schemas import ValidationError, ValidationReadiness, ValidationReadinessBlocker, ValidationResult
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.protocol import (
    InterpretationPlaceholderConsumedError,
    InterpretationResolveError,
    PromptFileTooLargeError,
)
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.composer.test_surface_pending_interpretation_reviews import _composer, _model_choice_node, _persist, _pt_node
from tests.unit.web.sessions.session_test_authority import FencedSessionServiceHarness

SKIP_EVENT = "composer_interpretation.surface_skipped"
PROMPT_TERM = "llm_prompt_template:rate_node"
MODEL_TERM = "llm_model_choice:rate_node"
# Credential-shaped text built at run time so the secret scanner sees no literal.
CREDENTIAL = "sk-" + "a" * 24


class _Sessions:
    """Stands in for the sessions writer: refuses the kinds it is told to, records the rest."""

    def __init__(self, refusals: dict[InterpretationKind, InterpretationResolveError] | None = None) -> None:
        self.refusals = dict(refusals or {})
        self.attempts: list[InterpretationKind] = []
        self.created: list[tuple[str, InterpretationKind]] = []

    async def create_pending_interpretation_event(self, **kwargs: object) -> None:
        kind = kwargs["kind"]
        assert type(kind) is InterpretationKind
        self.attempts.append(kind)
        if kind in self.refusals:
            raise self.refusals[kind]
        self.created.append((str(kwargs["affected_node_id"]), kind))


def _context(session_id: str) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id,
            operation_id=f"surface-skip-{session_id}",
            lease_token=f"surface-skip-token-{session_id}",
            operation_epoch=1,
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def _state(node_factory) -> CompositionState:
    return CompositionState(
        source=None,
        nodes=(node_factory(),),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _consumed(user_term: str) -> InterpretationPlaceholderConsumedError:
    # The real writer's wording: the message names the user term. A
    # credential-shaped tail stands for anything else the message may carry.
    return InterpretationPlaceholderConsumedError(
        f"create_pending_interpretation_event: does not contain exactly one pending requirement for {user_term!r}; found 0 ({CREDENTIAL})"
    )


async def _prompt_pass(sessions: _Sessions, *, session_id: str, state_id: str, repair_mode: bool) -> None:
    await _auto_surface_prompt_template_reviews_for_state(
        _state(_pt_node),
        sessions_service=sessions,
        session_id=session_id,
        current_state_id=state_id,
        surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
        model_identifier="test/model",
        model_version=None,
        provider="test",
        composer_skill_hash="0" * 64,
        session_operation_context=_context(session_id),
        repair_mode=repair_mode,
    )


async def _writer_pass(sessions: _Sessions, *, session_id: str, state_id: str, only_missing_evidence: bool) -> None:
    await _surface_pending_interpretation_reviews_under_writer(
        _state(_model_choice_node),
        sessions_service=sessions,
        session_id=session_id,
        current_state_id=state_id,
        surface_origin=InterpretationSurfaceOrigin.COMPOSER_LLM,
        model_identifier="test/model",
        model_version=None,
        provider="test",
        composer_skill_hash="0" * 64,
        already_surfaced=frozenset(),
        only_missing_evidence=only_missing_evidence,
        session_operation_context=_context(session_id),
    )


def _skips(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event["event"] == SKIP_EVENT]


@pytest.mark.asyncio
async def test_repair_mode_refusal_logs_one_warning_and_still_skips() -> None:
    session_id, state_id = str(uuid4()), str(uuid4())
    sessions = _Sessions({InterpretationKind.LLM_PROMPT_TEMPLATE: _consumed(PROMPT_TERM)})

    with structlog.testing.capture_logs() as events:
        await _prompt_pass(sessions, session_id=session_id, state_id=state_id, repair_mode=True)

    assert sessions.attempts == [InterpretationKind.LLM_PROMPT_TEMPLATE]
    assert sessions.created == []
    assert _skips(events) == [
        {
            "event": SKIP_EVENT,
            "log_level": "warning",
            "session_id": session_id,
            "composition_state_id": state_id,
            "affected_node_id": "rate_node",
            "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
            "repair_mode": True,
            "error_class": "InterpretationPlaceholderConsumedError",
        }
    ]


@pytest.mark.asyncio
async def test_successful_create_logs_nothing_and_only_the_refused_site_logs() -> None:
    session_id, state_id = str(uuid4()), str(uuid4())
    sessions = _Sessions({InterpretationKind.LLM_MODEL_CHOICE: _consumed(MODEL_TERM)})

    with structlog.testing.capture_logs() as events:
        await _writer_pass(sessions, session_id=session_id, state_id=state_id, only_missing_evidence=True)

    # The prompt card was created and says nothing; the refused model-choice
    # card is the one line, so the recorder is shown to see this module.
    assert sessions.created == [("rate_node", InterpretationKind.LLM_PROMPT_TEMPLATE)]
    skips = _skips(events)
    assert [(skip["kind"], skip["error_class"], skip["repair_mode"]) for skip in skips] == [
        (InterpretationKind.LLM_MODEL_CHOICE.value, "InterpretationPlaceholderConsumedError", True)
    ]

    clean = _Sessions()
    with structlog.testing.capture_logs() as clean_events:
        await _writer_pass(clean, session_id=session_id, state_id=state_id, only_missing_evidence=True)
    assert {kind for _node, kind in clean.created} == {
        InterpretationKind.LLM_PROMPT_TEMPLATE,
        InterpretationKind.LLM_MODEL_CHOICE,
    }
    assert _skips(clean_events) == []


@pytest.mark.asyncio
async def test_refusal_message_never_reaches_a_log_call() -> None:
    session_id, state_id = str(uuid4()), str(uuid4())
    sessions = _Sessions(
        {
            InterpretationKind.LLM_PROMPT_TEMPLATE: _consumed(PROMPT_TERM),
            InterpretationKind.LLM_MODEL_CHOICE: _consumed(MODEL_TERM),
        }
    )

    with structlog.testing.capture_logs() as events:
        await _writer_pass(sessions, session_id=session_id, state_id=state_id, only_missing_evidence=True)

    assert len(_skips(events)) == 2
    rendered = repr(events)
    assert CREDENTIAL not in rendered
    assert PROMPT_TERM not in rendered
    assert MODEL_TERM not in rendered


@pytest.mark.asyncio
async def test_non_repair_mode_still_raises_without_a_line_and_logs_the_cap_skip() -> None:
    session_id, state_id = str(uuid4()), str(uuid4())
    refusal = _consumed(PROMPT_TERM)
    sessions = _Sessions({InterpretationKind.LLM_PROMPT_TEMPLATE: refusal})

    with structlog.testing.capture_logs() as events, pytest.raises(InterpretationPlaceholderConsumedError) as raised:
        await _prompt_pass(sessions, session_id=session_id, state_id=state_id, repair_mode=False)
    assert raised.value is refusal
    assert _skips(events) == []

    # Settlement's one skip (an oversized prompt file) is logged with
    # repair_mode False; the recorder sees the module in this mode too.
    capped = _Sessions(
        {InterpretationKind.LLM_PROMPT_TEMPLATE: PromptFileTooLargeError(node_id="rate_node", limit_bytes=1024, aggregate=False)}
    )
    with structlog.testing.capture_logs() as cap_events:
        await _prompt_pass(capped, session_id=session_id, state_id=state_id, repair_mode=False)
    assert capped.created == []
    assert [(skip["error_class"], skip["repair_mode"]) for skip in _skips(cap_events)] == [("PromptFileTooLargeError", False)]


@pytest.mark.asyncio
async def test_non_prompt_kind_skip_logs_in_settlement_mode_too() -> None:
    session_id, state_id = str(uuid4()), str(uuid4())
    sessions = _Sessions({InterpretationKind.LLM_MODEL_CHOICE: _consumed(MODEL_TERM)})

    with structlog.testing.capture_logs() as events:
        await _writer_pass(sessions, session_id=session_id, state_id=state_id, only_missing_evidence=False)

    assert sessions.created == [("rate_node", InterpretationKind.LLM_PROMPT_TEMPLATE)]
    assert _skips(events) == [
        {
            "event": SKIP_EVENT,
            "log_level": "warning",
            "session_id": session_id,
            "composition_state_id": state_id,
            "affected_node_id": "rate_node",
            "kind": InterpretationKind.LLM_MODEL_CHOICE.value,
            "repair_mode": False,
            "error_class": "InterpretationPlaceholderConsumedError",
        }
    ]


# --- Runtime findings pre-empt surfacing (no-tool finalize tail) -------------
#
# ``_surface_pt_and_gate_orphans_or_none`` returns the "Runtime preflight
# failed" result before surfacing whenever structural findings stand (a sink
# path outside the allowed directories after a data directory moved, a
# missing secret, an unavailable profile). No card is written; the line below
# is the only trace that surfacing was skipped and why.

RUNTIME_SKIP_EVENT = "composer_interpretation.surface_skipped_runtime_findings"
TRAVERSAL_TEXT = f"Path traversal blocked: sink 'out' resolves outside allowed directories: /srv/moved/{CREDENTIAL}/out.csv"


def _sessions_service() -> FencedSessionServiceHarness:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    return FencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.sessions"))


def _path_allowlist_findings() -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[ValidationError(component_id="out", component_type="output", message=TRAVERSAL_TEXT, suggestion=None, error_code=None)],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="path_allowlist",
                    component_id="out",
                    component_type="output",
                    detail=TRAVERSAL_TEXT,
                    suggestion=None,
                    note=None,
                )
            ],
        ),
    )


async def _finalize_gate(tmp_path, monkeypatch, *, runtime_findings):
    """Run the real surface + orphan-gate pair over a persisted pending prompt review."""

    def _available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="anthropic")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)
    sessions = _sessions_service()
    composer = _composer(tmp_path, sessions)
    state = _state(_pt_node)
    session_id, state_id = await _persist(sessions, state)
    context = await sessions._run_sync(
        lambda: sessions.session_operation_authority.acquire(
            session_id=session_id,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=sessions.session_operation_owner_instance_id,
            lease_seconds=sessions.session_operation_lease_seconds,
        )
    )
    try:
        with structlog.testing.capture_logs() as events:
            result = await composer._completion._surface_pt_and_gate_orphans_or_none(
                state=state,
                session_id=str(session_id),
                current_state_id=str(state_id),
                session_operation_context=context,
                assistant_message=_AdmittedAssistantMessage(content="The pipeline is ready."),
                recorder=BufferingRecorder(),
                progress=None,
                runtime_findings=runtime_findings,
            )
    finally:
        await sessions._run_sync(sessions.session_operation_authority.release, context)
    cards = await sessions.list_interpretation_events(session_id, status="pending")
    return str(session_id), str(state_id), result, cards, events


def _runtime_skips(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [event for event in events if event["event"] == RUNTIME_SKIP_EVENT]


@pytest.mark.asyncio
async def test_runtime_findings_early_return_logs_codes_once_and_writes_no_card(tmp_path, monkeypatch) -> None:
    findings = _path_allowlist_findings()

    session_id, state_id, result, cards, events = await _finalize_gate(tmp_path, monkeypatch, runtime_findings=findings)

    # Control flow unchanged: the preflight-failed result, no card.
    assert result is not None
    assert result.runtime_preflight is findings
    assert cards == []
    assert _runtime_skips(events) == [
        {
            "event": RUNTIME_SKIP_EVENT,
            "log_level": "warning",
            "session_id": session_id,
            "composition_state_id": state_id,
            "path": "no_tool_finalize",
            "error_count": 1,
            "blocker_count": 1,
            "error_codes": [],
            "blocker_codes": ["path_allowlist"],
            "unsurfaced_site_count": 1,
        }
    ]
    # Codes and counts only: no finding text, path or credential reaches a log.
    rendered = repr(events)
    assert CREDENTIAL not in rendered
    assert "Path traversal" not in rendered
    assert "/srv/moved" not in rendered


@pytest.mark.asyncio
async def test_no_runtime_findings_surfaces_the_card_without_a_skip_line(tmp_path, monkeypatch) -> None:
    _session_id, _state_id, result, cards, events = await _finalize_gate(tmp_path, monkeypatch, runtime_findings=None)

    assert result is None
    assert [card.kind for card in cards] == [InterpretationKind.LLM_PROMPT_TEMPLATE]
    assert _runtime_skips(events) == []
    assert _skips(events) == []

    # Positive control on the same instrument: the findings arm does log.
    _, _, _, _, findings_events = await _finalize_gate(tmp_path / "findings", monkeypatch, runtime_findings=_path_allowlist_findings())
    assert len(_runtime_skips(findings_events)) == 1
