"""Carriers (frozen dataclasses) used to thread state between the phases of
``ComposerServiceImpl._compose_loop``.

The compose loop has been decomposed into five phases:

  * **P1 ``_call_model_turn``** — one LLM call, builds ``_CallModelOutcome``.
  * **P2 ``_try_terminate_no_tools``** — if the assistant emitted no tool
    calls, either inject a repair prompt and continue, or finalize and
    return; output is ``_TerminateOutcome``.
  * **P3 ``_dispatch_tool_batch``** — execute every tool call in the
    assistant message, accumulate ``_ToolOutcome`` records, mutate the
    in-process ``state`` as successful tool calls advance it; output is
    ``_DispatchOutcome``.
  * **P4 ``_persist_turn_audit``** — redact the dispatch result and write
    the per-turn audit row via ``persist_compose_turn_async``; output is
    ``_PersistOutcome``.
  * **P5 ``_classify_and_budget_turn``** — anti-anchor hint, cache-hit
    short-circuit, dual-counter budget bookkeeping, B-4D-3 last-chance
    LLM call; output is ``_ClassifyOutcome``.

The driver (``_compose_loop``) owns the multi-iteration accumulators
(``composition_turns_used``, ``discovery_turns_used``,
``mutation_success_seen``, ``advisor_calls_used``, ``repair_turns_used``,
``persisted_assistant_message_id``, ``persisted_tool_call_turn``,
``failed_turn``, ``current_state_id``) and the long-lived per-call
service objects (``recorder``, ``anti_anchor``, ``discovery_cache``,
``runtime_preflight_cache``, ``llm_messages``, ``tools``). Phase helpers
receive these as parameters and return updates as deltas or replacement
values.

Layer: L3 (application). Imports from L0 (``contracts/freeze``,
``contracts/errors``), L1 (``core/composer/state``,
``core/composer/runtime_preflight``), and L3 (``web/composer/protocol``,
``web/composer/state``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.token_usage import TokenUsage
from elspeth.web.composer.protocol import ComposerPluginCrashError, ComposerResult
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult

_ToolOutcomeResponse = ToolResult | Mapping[str, Any] | None


class _ToolBatchCancellationRequested(Exception):
    """Internal sentinel: cancellation landed before any tool dispatched."""


@dataclass(frozen=True, slots=True)
class _AdmittedAssistantMessage:
    """Validated provider prose copied into an ELSPETH-owned message."""

    content: str | None


@dataclass(frozen=True, slots=True)
class _AdmittedToolFunction:
    """Immutable copy of execution-relevant provider function metadata."""

    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class _AdmittedToolCall:
    """Immutable provider call admitted for this dispatch batch."""

    id: str
    function: _AdmittedToolFunction


@dataclass(frozen=True, slots=True)
class _AdmittedToolBatch:
    """Immutable batch used exclusively after provider-boundary admission."""

    calls: tuple[_AdmittedToolCall, ...]
    call_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _AdmittedLLMProviderMetadata:
    """Minimal provider projection retained for audit and provenance."""

    model_returned: str | None
    finish_reason: str | None
    usage: TokenUsage
    provider_cost: float | None
    provider_cost_source: ComposerLLMProviderCostSource
    provider_request_id: str | None
    reasoning_content: str | None
    reasoning_details: Any | None
    thinking_blocks: Any | None

    def __post_init__(self) -> None:
        freeze_fields(self, "reasoning_details", "thinking_blocks")


@dataclass(frozen=True, slots=True)
class _AdmittedLLMCompletion:
    """Read-once, owned snapshot of one successful provider completion."""

    message: _AdmittedAssistantMessage
    tool_batch: _AdmittedToolBatch
    provider_metadata: _AdmittedLLMProviderMetadata


@dataclass(frozen=True, slots=True)
class _AdvisorReviewState:
    """Bounded END-checkpoint context carried across compose-loop iterations."""

    completed_passes: int = 0
    previous_findings: tuple[str, ...] = ()
    previous_evidence_hash: str | None = None
    successful_mutating_actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "previous_findings", "successful_mutating_actions")


@dataclass(frozen=True, slots=True)
class _ToolOutcome:
    """Result of one tool call within a compose turn.

    The response contract is a closed sum type produced by P3
    ``tool_batch.run_tool_batch`` and consumed by P4 ``turn_audit``:

    * ``ToolResult`` for normal execute-tool, cache-hit, proposal, and
      session-aware success paths.
    * ``Mapping[str, Any]`` for the first-party ``request_advisor_hint``
      interception envelopes. That tool is intentionally outside
      ``execute_tool`` because it makes an async advisor LLM call, so its
      successful/disabled/budget/error/timeout payloads are already
      serialized mapping envelopes rather than ``ToolResult`` instances.
    * ``None`` for argument-error and plugin-crash paths, where
      ``error_class`` / ``error_message`` carry the outcome.

    ``call`` is the ELSPETH-owned tool-call projection admitted before the raw
    provider response is discarded.
    """

    call: _AdmittedToolCall
    response: _ToolOutcomeResponse
    error_class: str | None
    error_message: str | None
    pre_version: int
    post_version: int

    def __post_init__(self) -> None:
        freeze_fields(self, "call", "response")


@dataclass(frozen=True, slots=True)
class _CallModelOutcome:
    """Result of one LLM call in the compose loop (Phase P1).

    The sole field is the frozen completion admitted before the provider
    object leaves the Tier-3 boundary. P2 reads its owned message, P3 consumes
    its already-admitted tool batch and provider metadata, and P5's B-4D-3
    last-chance path consumes the same carrier directly.
    """

    completion: _AdmittedLLMCompletion


@dataclass(frozen=True, slots=True)
class _TerminateOutcome:
    """Decision returned by P2 (``_try_terminate_no_tools``).

    * ``action == "continue"`` — the helper appended a repair prompt; the
      driver should bump ``repair_turns_used`` by ``repair_turns_delta``
      and re-enter P1 next iteration.
    * ``action == "return"`` — the helper called
      ``_finalize_no_tool_response`` and produced ``result``; the driver
      should return ``result`` (already threaded through
      ``persisted_assistant_message_id`` / ``persisted_tool_call_turn``
      / ``repair_turns_used``).
    """

    action: Literal["continue", "return"]
    result: ComposerResult | None = None
    repair_turns_delta: int = 0  # 0 or 1; nonzero only for continue path
    # END-gate advisor budget — SEPARATE from repair_turns_delta (D-8). A
    # flagged advisor repair-continue (or a fail-closed end-gate return)
    # increments this, never the repair counter; the driver folds it into
    # ``advisor_checkpoint_passes_used``.
    advisor_passes_delta: int = 0  # bounded non-negative delta; retries may consume more than one
    # Set only when this "continue" was produced by a FLAGGED END advisor
    # pass: the index of the synthetic advisor sign-off message just
    # appended to ``llm_messages``. The driver elides it once a genuine
    # repair tool call lands (Task 6 Step 3, elspeth-bff8fe6864) so the
    # eventual CLEAN finalize turn cannot anchor on advisor text the real
    # user never saw.
    advisor_injection_index: int | None = None
    advisor_review_state: _AdvisorReviewState | None = None


@dataclass(frozen=True, slots=True)
class _DispatchOutcome:
    """Result of dispatching one tool batch (Phase P3 → P4 → P5).

    Replaces the per-iteration scratch locals (``turn_has_mutation``,
    ``turn_has_discovery``, ``all_cache_hits``, ``tool_outcomes``,
    ``plugin_crash``, ``plugin_crash_cause``, ``pre_state_id``,
    ``decoded_args_by_call_id``) with a single visible boundary.

    State mutation happens *inside* P3 (every successful tool call
    rebinds ``state = result.updated_state``). The ``state`` field here
    is the final value after the for-loop exits. P4 does not mutate
    state; P5 reads it for convergence-raise envelopes.

    ``plugin_crash`` is set when a tool handler raised an exception
    other than ``ToolArgumentError``. The carrier carries it forward;
    the driver propagates after P4 has had a chance to persist the
    pre-crash mutations (CLAUDE.md "partial_state" discipline).
    """

    # State at end of dispatch
    state: CompositionState
    last_validation: ValidationSummary | None
    last_runtime_preflight: Any  # ValidationResult | None — avoid extra L1 import

    # Tool outcomes (already deep-frozen by _ToolOutcome.__post_init__)
    tool_outcomes: tuple[_ToolOutcome, ...]

    # Per-tool decoded arguments, by tool_call.id (used by P4 redaction)
    decoded_args_by_call_id: Mapping[str, Mapping[str, Any]]

    # Turn classification (set during dispatch, read by P5)
    turn_has_mutation: bool
    turn_has_discovery: bool
    all_cache_hits: bool

    # Plugin-crash carrier — P4 must propagate this AFTER persist
    plugin_crash: ComposerPluginCrashError | None
    plugin_crash_cause: BaseException | None

    # Advisor-tool compose timeout — the tool envelope is already closed in
    # P3, but P4 must persist/redact the current assistant+tool turn before the
    # driver raises the ordinary convergence-timeout recovery carrier.
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None

    # Owned LLM completion fields threaded through for P4 persistence.
    assistant_message: _AdmittedAssistantMessage
    raw_assistant_content: str | None
    assistant_tool_calls: tuple[_AdmittedToolCall, ...]

    # mutation_success_seen rebinds inside dispatch's success path; carry
    # the delta so the driver can fold it into its multi-iteration
    # accumulator.
    mutation_success_observed: bool

    # pre_state_id captured at the start of the dispatch turn — written to
    # the ``self._phase3_last_expected_current_state_id`` test-hook by P3.
    pre_state_id: str | None

    def __post_init__(self) -> None:
        # decoded_args_by_call_id is dict[str, dict[str, Any]] at
        # construction time; deep-freeze into MappingProxyType[str, MappingProxyType].
        # tool_outcomes is a tuple of _ToolOutcome — already frozen by its
        # own __post_init__; freeze_fields is identity-preserving.
        freeze_fields(self, "decoded_args_by_call_id", "tool_outcomes")


@dataclass(frozen=True, slots=True)
class _PersistOutcome:
    """Result of redact-and-persist (Phase P4 → P5).

    When the dispatch carried a plugin crash, the carrier distinguishes a
    committed unwind audit from a rolled-back unwind attempt through
    ``persisted_tool_call_turn``. The driver then propagates the crash with
    either no invocation rows (committed) or exactly the current unpersisted
    suffix (rolled back).
    """

    current_state_id: str | None
    persisted_assistant_message_id: str | None
    persisted_tool_call_turn: bool
    unwind_audit_failed: bool
    failed_turn: FailedTurnMetadata | None


@dataclass(frozen=True, slots=True)
class _ClassifyOutcome:
    """Decision returned by P5 (``_classify_and_budget_turn``).

    * ``action == "continue"`` — driver re-enters P1 next iteration with
      the supplied counter deltas (``composition_turns_delta`` and
      ``discovery_turns_delta`` are 0 or 1 each).
    * ``action == "return"`` — driver returns ``result`` (the B-4D-3
      last-chance branch terminated the loop).

    Convergence-on-exhausted-budget raises ``ComposerConvergenceError``
    directly from the phase helper; that path does not reach this
    carrier.
    """

    action: Literal["continue", "return"]
    result: ComposerResult | None = None
    composition_turns_delta: int = 0  # 0 or 1
    discovery_turns_delta: int = 0  # 0 or 1
    # END-gate advisor budget (the P5 last-chance gate). Folded into the
    # driver's ``advisor_checkpoint_passes_used`` after P5, mirroring the
    # P2 ``_TerminateOutcome.advisor_passes_delta`` field.
    advisor_passes_delta: int = 0  # bounded non-negative delta; retries may consume more than one
