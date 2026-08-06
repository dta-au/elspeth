# tests/unit/web/composer/test_no_tool_finalize_universal_qualification.py
"""The pending-handoff qualification must run behind EVERY no-tool announcement (elspeth-c5350d93fd).

``test_runtime_preflight_pending_review_verification`` pins the HELPERS —
``_append_interpretation_review_handoff_message`` and
``_pending_handoff_outstanding_findings`` — in isolation, and its docstring
claims the fix runs "behind EVERY handoff announcement". It did not. The
qualification lived on the staged-handoff branch of
``_classify_and_budget_turn`` alone, so the two OTHER callers of the shared
finalize tail published a pending-review result with nothing backend-authored
appended:

* the B-4D-3 budget-exhaustion last-chance finalize, and
* ``_try_terminate_no_tools`` — the CLEAN tail, and the single most common way
  a compose turn ends.

For a pending-handoff result with no grounding violations,
``finalize_no_tool_response`` returns the model's raw prose verbatim (its
plain-passthrough branch), so an operator whose model happened not to mention
the pending review got zero indication one existed. Nothing caught it because
no test drove those two call sites end-to-end.

These tests drive the REAL compose loop through ``_run_one_turn_for_test`` to
both call sites, plus the no-double-append regression the refactor could
introduce (a doubled suffix still satisfies
``_enforce_augmentation_prefix_invariant``, so it would be silent), plus the
red-verdict telemetry split (elspeth-ca0bd5d4ef).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import structlog
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer import service as service_module
from elspeth.web.composer.no_tool_policy import is_pending_interpretation_handoff
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.execution.schemas import (
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
)
from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_PENDING_CODE
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

from ._helpers import (
    _mock_catalog,
    _stub_advisor_end_gate_clean,  # noqa: F401  (autouse END-gate CLEAN stub)
)

_HANDOFF_SUFFIX = "Interpretation review cards are ready for this pipeline. Review the pending assumptions to continue."
_QUALIFIED_MARKER = "must be fixed before this pipeline can run"


# ---------------------------------------------------------------------------
# ValidationResult shapes (mirrors test_runtime_preflight_pending_review_verification)
# ---------------------------------------------------------------------------


def _handoff_result() -> ValidationResult:
    """The truncated-ledger shape ``review_interpretations`` produces at stage 10."""
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=True,
            blockers=[
                ValidationReadinessBlocker(
                    code=INTERPRETATION_REVIEW_PENDING_CODE,
                    component_id="map_node",
                    component_type="transform",
                    detail="vague_term review pending for transform 'map_node': cool",
                )
            ],
        ),
    )


def _structural_failure_result() -> ValidationResult:
    """A graph_structure-class failure the masked re-validation surfaces."""
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="map_node",
                component_type="transform",
                message="consumer requires ['llm_response'], producer guarantees (none - dynamic schema)",
                suggestion=None,
                error_code="graph_structure",
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="graph_structure",
                    component_id="map_node",
                    component_type="transform",
                    detail="consumer requires ['llm_response'], producer guarantees (none - dynamic schema)",
                )
            ],
        ),
    )


def _valid_result() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(
            authoring_valid=True,
            execution_ready=True,
            completion_ready=True,
            blockers=[],
        ),
    )


class _RecordingValidatePipeline:
    """Fake ``validate_pipeline`` returning strict/tolerant results per call mode.

    Two-armed on purpose. ``_pending_handoff_outstanding_findings`` runs the
    SAME entry point with ``allow_pending_interpretation_placeholders=True``
    and reports any non-valid tolerant result as outstanding findings, so a
    single-armed fake that always returned the handoff shape would make every
    qualification look red and hide the bare-suffix wording entirely.
    """

    def __init__(self, *, strict: ValidationResult, tolerant: ValidationResult) -> None:
        self._strict = strict
        self._tolerant = tolerant
        self.calls: list[bool] = []

    def __call__(self, *args: Any, **kwargs: Any) -> ValidationResult:
        tolerant_mode = bool(kwargs.get("allow_pending_interpretation_placeholders", False))
        self.calls.append(tolerant_mode)
        return self._tolerant if tolerant_mode else self._strict


# ---------------------------------------------------------------------------
# Compose-loop harness
# ---------------------------------------------------------------------------


def _fake_response_with_tool_call(*, tool_call_id: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    """Build a minimal LiteLLM-shaped response carrying one tool call."""

    class _Func:
        def __init__(self, name: str, args_json: str) -> None:
            self.name = name
            self.arguments = args_json

    class _ToolCall:
        def __init__(self, id_: str, function: _Func) -> None:
            self.id = id_
            self.function = function

    class _Message:
        def __init__(self) -> None:
            self.tool_calls = [_ToolCall(tool_call_id, _Func(tool_name, json.dumps(arguments)))]
            self.content = None

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]
            self.model = "anthropic/claude-opus-4-7-20260101"

    return _Response()


def _fake_text_response(text: str) -> Any:
    """Build a no-tool-calls assistant response (terminates the compose loop)."""

    class _Message:
        def __init__(self) -> None:
            self.content = text
            self.tool_calls = None

    class _Choice:
        def __init__(self) -> None:
            self.message = _Message()

    class _Response:
        def __init__(self) -> None:
            self.choices = [_Choice()]
            self.model = "anthropic/claude-opus-4-7-20260101"

    return _Response()


class _ScriptedLLM:
    """Returns a sequence of pre-built LiteLLM responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def __call__(self, _messages: list[dict[str, Any]], _tools: Any) -> Any:
        if not self._responses:
            return _fake_text_response("Done.")
        return self._responses.pop(0)


def _set_pipeline_without_interpretation_sites() -> dict[str, Any]:
    """A ``set_pipeline`` payload whose state enumerates ZERO interpretation sites.

    Deliberately NOT an ``llm`` node: ``_missing_prompt_template_review_sites``
    and ``_missing_model_choice_review_sites`` both short-circuit on
    ``plugin != "llm"``, and the source carries no authoring metadata, so
    ``interpretation_sites(state)`` is empty. That keeps the fail-closed orphan
    gate and the PT auto-surfacer structurally silent, so the ONLY thing under
    test is what the finalize tail does with the pending-handoff preflight the
    fake validator supplies. The state is still structurally non-empty and
    bumps ``state.version``, which is what makes ``finalize_no_tool_response``
    recompute the preflight rather than pass ``None`` through.
    """
    return {
        "source": {"plugin": "null", "on_success": "rows", "options": {}},
        "nodes": [
            {
                "id": "map_node",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "rows",
                "on_success": "mapped_rows",
                "on_error": "discard",
                "options": {"mapping": {"text": "text"}, "schema": {"mode": "observed"}},
            }
        ],
        "edges": [],
        "outputs": [],
    }


def _non_empty_state() -> CompositionState:
    """A structurally non-empty state for the direct finalize-tail tests.

    ``finalize_no_tool_response`` dispatches on state structure BEFORE it looks
    at the preflight verdict: over a structurally empty state it takes the
    empty-state branch and synthesises its own red result, which would mask the
    verdict the test is actually supplying. ``on_error`` is set rather than left
    None because the mutation boundary (``set_pipeline``) defaults it and the
    YAML generator refuses to fabricate it.
    """
    return CompositionState(
        source=SourceSpec(plugin="null", on_success="rows", options={}, on_validation_failure="error"),
        nodes=(
            NodeSpec(
                id="map_node",
                node_type="transform",
                plugin="field_mapper",
                input="rows",
                on_success="mapped_rows",
                on_error="discard",
                options={"mapping": {"text": "text"}, "schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.fixture
def engine():
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def sessions_service(engine) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.sessions"),
    )


def _build_composer(
    tmp_path: Path,
    sessions_service: SessionServiceImpl,
    *,
    max_composition_turns: int = 15,
) -> ComposerServiceImpl:
    # Built on _mock_catalog() rather than a bare MagicMock(spec=CatalogService):
    # the dispatch wrapper's schema augmentation calls ``get_schema``, and an
    # unstubbed spec'd mock returns a MagicMock that the tool-audit RFC-8785
    # canonicalizer rejects, surfacing as ComposerPluginCrashError long before
    # the finalize tail under test.
    catalog = _mock_catalog()
    catalog.list_sources.return_value = [
        PluginSummary(name="null", description="Null source", plugin_type="source", config_fields=[]),
    ]
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=max_composition_turns,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        composer_model="anthropic/claude-opus-4-7",
        shareable_link_signing_key=b"\x00" * 32,
    )
    return ComposerServiceImpl.for_trained_operator(
        catalog=catalog,
        settings=settings,
        sessions_service=sessions_service,
        session_engine=sessions_service._engine,
    )


async def _seed_session(sessions_service: SessionServiceImpl) -> str:
    session_id = uuid4()
    with sessions_service._engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=str(session_id),
                user_id="alice",
                auth_provider_type="local",
                title="No-tool finalize qualification session",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return str(session_id)


async def _run_no_tool_turn(
    tmp_path: Path,
    sessions_service: SessionServiceImpl,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake: _RecordingValidatePipeline,
    max_composition_turns: int,
    prose: str = "All set — I mapped the text field across.",
) -> Any:
    """Drive one mutation turn followed by a no-tool terminal turn."""
    composer = _build_composer(tmp_path, sessions_service, max_composition_turns=max_composition_turns)
    session_id = await _seed_session(sessions_service)
    monkeypatch.setattr(service_module, "validate_pipeline", fake)
    llm = _ScriptedLLM(
        [
            _fake_response_with_tool_call(
                tool_call_id="call_set_pipeline",
                tool_name="set_pipeline",
                arguments=_set_pipeline_without_interpretation_sites(),
            ),
            _fake_text_response(prose),
        ]
    )
    _out = await composer._run_one_turn_for_test(
        llm=llm,
        session_id=session_id,
        current_state_id=None,
        message="map the text field across",
    )
    # Guard, not decoration: a rejected set_pipeline leaves the state empty and
    # version-unchanged, so finalize_no_tool_response passes the prose straight
    # through with runtime_preflight=None and every assertion below would be
    # vacuously satisfied by a test that never reached the code under test.
    assert [outcome for outcome in _out.tool_outcomes if not outcome.response.success] == [], (
        f"the set_pipeline mutation must land: {_out.tool_outcomes}"
    )
    return _out


# ---------------------------------------------------------------------------
# The two regressed call sites, end to end
# ---------------------------------------------------------------------------


class TestCleanNoToolTailQualifiesTheHandoff:
    """``_try_terminate_no_tools`` — the DEFAULT way a compose turn ends."""

    @pytest.mark.asyncio
    async def test_bare_suffix_is_appended_when_the_masked_pass_is_green(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prose = "All set — I mapped the text field across."
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=15, prose=prose)

        assert result.runtime_preflight is not None
        assert is_pending_interpretation_handoff(result.runtime_preflight)
        # The whole defect: pre-fix this path returned the model's prose verbatim.
        assert _HANDOFF_SUFFIX in result.assistant_message
        assert result.assistant_message.startswith(prose)
        assert _QUALIFIED_MARKER not in result.assistant_message
        # Strict pass, then the tolerant qualification pass this fix introduces.
        assert fake.calls == [False, True]

    @pytest.mark.asyncio
    async def test_suffix_is_qualified_when_the_masked_pass_is_red(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=15)

        assert _QUALIFIED_MARKER in result.assistant_message
        assert "(none - dynamic schema)" in result.assistant_message

    @pytest.mark.asyncio
    async def test_green_preflight_gets_no_handoff_suffix(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: the tail must not announce a review that does not exist."""
        fake = _RecordingValidatePipeline(strict=_valid_result(), tolerant=_valid_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=15)

        assert _HANDOFF_SUFFIX not in result.assistant_message
        # No tolerant pass is run when there is no handoff to qualify.
        assert fake.calls == [False]


class TestBudgetExhaustionFinalizeQualifiesTheHandoff:
    """The B-4D-3 last-chance finalize in ``_classify_and_budget_turn``."""

    @pytest.mark.asyncio
    async def test_bare_suffix_is_appended_when_the_masked_pass_is_green(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prose = "All set — I mapped the text field across."
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        # max_composition_turns=1: the single set_pipeline mutation exhausts the
        # composition budget, so the bonus call's no-tool response finalizes
        # through the B-4D-3 branch rather than _try_terminate_no_tools.
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=1, prose=prose)

        assert result.runtime_preflight is not None
        assert is_pending_interpretation_handoff(result.runtime_preflight)
        assert _HANDOFF_SUFFIX in result.assistant_message
        assert result.assistant_message.startswith(prose)
        assert fake.calls == [False, True]

    @pytest.mark.asyncio
    async def test_suffix_is_qualified_when_the_masked_pass_is_red(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_structural_failure_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=1)

        assert _QUALIFIED_MARKER in result.assistant_message
        assert "(none - dynamic schema)" in result.assistant_message


class TestSuffixIsAppendedExactlyOnce:
    """The regression the refactor itself could introduce.

    The staged-handoff branch appends on ``preflight is None or is_valid`` and
    the shared tail appends on the pending-handoff shape. If those conditions
    ever overlap the suffix lands TWICE, and
    ``_enforce_augmentation_prefix_invariant`` still passes — a doubled suffix
    keeps the model's prose as a strict prefix — so nothing else would catch it.
    """

    @pytest.mark.asyncio
    async def test_clean_tail_appends_one_suffix(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=15)

        assert result.assistant_message.count(_HANDOFF_SUFFIX) == 1

    @pytest.mark.asyncio
    async def test_budget_exhaustion_finalize_appends_one_suffix(
        self, tmp_path: Path, sessions_service: SessionServiceImpl, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _RecordingValidatePipeline(strict=_handoff_result(), tolerant=_valid_result())
        result = await _run_no_tool_turn(tmp_path, sessions_service, monkeypatch, fake=fake, max_composition_turns=1)

        assert result.assistant_message.count(_HANDOFF_SUFFIX) == 1
