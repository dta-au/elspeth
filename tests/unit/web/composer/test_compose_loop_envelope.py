"""Production prompt-byte envelope for the composer's provider requests.

This file is the regression gate for the elspeth-4e79436719 cluster:
unbounded prompt growth across LLM provider turns (a 76 KB skill prompt +
20 KB tools spec re-sent on every turn burned 600 K+ prompt tokens for a
trivial pipeline-creation request on 2026-05-06 staging).

The deleted V1 ``compose()`` loop is gone. The bytes are now measured on the
shared owners that build every Continuation primary request:
``continuation_message_builders.build_service_initial_messages`` (system
prompt, skill and context) plus ``composer_loop_tool_definitions`` (the
tool palette), and ``tool_batch.run_tool_batch`` for the assistant/tool
messages a batch appends before the next request. Only the provider is
absent: no request is sent.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_message_builders import build_service_initial_messages
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion, composer_loop_tool_definitions
from elspeth.web.composer.service import ComposerAvailability
from elspeth.web.composer.tool_batch import BatchAccumulator, ToolBatchContext, ToolBatchCustody, ToolBatchProvenance, run_tool_batch
from tests.helpers.session_fences import fenced_operation_context
from tests.unit.web.composer._helpers import (
    _composer_service_with_session,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)


@pytest.fixture(autouse=True)
def _composer_available_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the OPENAI_API_KEY availability check (deterministic offline)."""

    def _available(*, model: str, **_kwargs: object) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=model, provider="test")

    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", _available)


# ---------------------------------------------------------------------------
# Named envelope constants — bumping these requires a paired plan-update PR.
# ---------------------------------------------------------------------------

# Cumulative byte-size envelope on the production-emitted ``messages`` +
# ``tools`` JSON blobs across one compose() call. This IS a real production
# gate: the bytes asserted are what ``_build_messages`` and
# ``composer_loop_tool_definitions`` actually serialize and what would land on the
# provider wire.
#
# Recalibrated baseline (2026-05-18, 1-turn happy script): ~210 KB
# (~177 KB skill prompt + ~31 KB tools spec for 39 tools + small overhead).
# The original baseline (2026-05-06: ~100 KB / 200 KB envelope) was overtaken
# by ~20 incremental skill-prompt commits — recipe-first fork-coalesce
# guidance, mandatory advisor escalation for Recipe #10, fabrication and
# silent-downgrade loophole closures, abuse_contact/scraping_reason
# requirements, audit-backend skill+tool additions — and by five new tool
# definitions added to the LLM-visible toolset. Each commit was a deliberate
# bug-fix; the growth is structural, not regressional. Setting the envelope
# at 300 KB gives ~45 % headroom over the recalibrated baseline so a
# subsequent uncontrolled doubling still bites loudly, without rejecting
# the committed prompt improvements that landed since the original gate
# was set.
#
# Note: Phase 3 (provider prompt caching) will NOT change this number —
# caching reduces the provider's billable token accounting, not the bytes
# we send. This envelope is a pure regression gate, independent of
# whether prompt caching is wired.
ENVELOPE_MAX_PRODUCTION_BYTES_TRIVIAL_PROMPT = 300_000

# Keep the existing two-payload allowance for catalog discovery followed by a
# concrete clarification. The scenario has two legitimate provider transitions;
# no lexical retry or increase to the turn, token, or per-call bound is needed.
ENVELOPE_MAX_PRODUCTION_BYTES_ROOTLESS_RECOVERY = 2 * ENVELOPE_MAX_PRODUCTION_BYTES_TRIVIAL_PROMPT


def _serialize_call(messages: Any, tools: Any) -> int:
    """Return the JSON byte size of the messages+tools sent to a fake _call_llm."""
    import json

    payload = {"messages": messages, "tools": tools}
    return len(json.dumps(payload, default=str).encode("utf-8"))


async def _rootless_trivial_request_sizes(user_message: str = "Build a CSV pipeline.") -> tuple[int, ...]:
    """Byte sizes of the two provider requests of the rootless trivial script.

    Request 1 is the initial primary input. Request 2 is request 1 plus the
    assistant/tool messages the ``list_sources`` discovery batch appends.
    """
    service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
    sessions = service._require_sessions_service()
    state = _empty_state()
    snapshot, policy_catalog = service._policy_context.build("test-user")
    tools = composer_loop_tool_definitions(service._planner_dialect)
    preferences = await sessions.get_composer_preferences(UUID(session_id))
    ceiling = ComposerMutationCeiling(session_id, "envelope-request", preferences.trust_mode)
    with fenced_operation_context(sessions._engine, session_id) as context:
        messages = build_service_initial_messages(
            service,
            chat_history=[],
            state=state,
            user_message=user_message,
            session_id=session_id,
            user_id="test-user",
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
            session_operation_context=context,
        )
        sizes = [_serialize_call(messages, tools)]
        ctx = ToolBatchContext(
            session_tools=service._session_tools,
            provenance=ToolBatchProvenance(
                model_identifier=service._model, provider=service._availability.provider, skill_hash=service._composer_skill_hash
            ),
            custody=ToolBatchCustody(
                data_dir=service._data_dir,
                session_engine=service._session_engine,
                secret_service=service._secret_service,
                secret_wiring_policy=service._secret_wiring_policy,
                max_blob_storage_per_session_bytes=service._settings.max_blob_storage_per_session_bytes,
            ),
            redaction_telemetry=service._redaction_telemetry,
            preflight=service._preflight,
            schema_disclosure=service._schema_disclosure,
            advisor_checkpoint=service._advisor_checkpoint,
            advisor_max_calls_per_compose=service._settings.composer_advisor_max_calls_per_compose,
            advisor_timeout_seconds=service._settings.composer_advisor_timeout_seconds,
            recorder=BufferingRecorder(),
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache=service._preflight.new_cache(),
            session_id=session_id,
            session_operation_context=context,
            user_id="test-user",
            user_message_id=None,
            user_message_content=user_message,
            current_state_id=None,
            actor="composer-web:user:test-user",
            initial_version=state.version,
            deadline=asyncio.get_running_loop().time() + 60,
            progress=None,
            session_scope=f"session:{session_id}",
            turn_sessions_service=sessions,
            turn_session_uuid=UUID(session_id),
            turn_preferences=preferences,
            mutation_ceiling=ceiling,
            review_required=not ceiling.permits_auto(preferences.trust_mode),
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=policy_catalog,
            tool_contract_dialect=service._planner_dialect,
            session_operation_authority=sessions.session_operation_authority,
        )
        completion = _admit_composer_llm_completion(
            _make_llm_response(tool_calls=[{"id": "trivial_sources", "name": "list_sources", "arguments": {}}])
        )
        acc = BatchAccumulator(state=state, last_validation=None, last_runtime_preflight=None, advisor_calls_used=0)
        before = len(messages)
        outcome, _advisor_calls = await run_tool_batch(
            call_model=_CallModelOutcome(completion=completion), ctx=ctx, acc=acc, llm_messages=messages
        )
    assert outcome.plugin_crash is None and len(outcome.tool_outcomes) == 1
    assert [message["role"] for message in messages[before:]] == ["assistant", "tool"]
    sizes.append(_serialize_call(messages, tools))
    return tuple(sizes)


@pytest.mark.asyncio
async def test_rootless_trivial_prompt_under_production_byte_envelope() -> None:
    """Real production prompt-size assertion.

    The bytes are what the shared request builders emit for each of the two
    provider requests of the rootless trivial script. A regression that
    re-introduces unbounded transcript growth or ships a dramatically larger
    system prompt or tool palette fails here.
    """
    sizes = await _rootless_trivial_request_sizes()
    total_bytes = sum(sizes)
    assert all(size <= ENVELOPE_MAX_PRODUCTION_BYTES_TRIVIAL_PROMPT for size in sizes), sizes
    assert total_bytes <= ENVELOPE_MAX_PRODUCTION_BYTES_ROOTLESS_RECOVERY, (
        f"Trivial first-turn creation emitted {total_bytes} bytes of "
        f"messages+tools across {len(sizes)} requests ({sizes}); exceeded named envelope "
        f"({ENVELOPE_MAX_PRODUCTION_BYTES_ROOTLESS_RECOVERY}). A request is re-emitting bytes "
        "it should not, or the static prefix grew without compensating compaction."
    )


@pytest.mark.asyncio
async def test_envelope_measure_grows_with_the_request() -> None:
    """Self-test: the instrument sees request bytes, so a larger request measures larger.

    A harness that cannot fail when something is wrong is a placebo.
    """
    small = await _rootless_trivial_request_sizes("Build a CSV pipeline.")
    large = await _rootless_trivial_request_sizes("Build a CSV pipeline. " + "x" * 50_000)
    assert all(after >= before + 50_000 for before, after in zip(small, large, strict=True)), (small, large)
