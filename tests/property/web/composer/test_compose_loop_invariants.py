"""Generated audit invariants through the current Continuation app and worker.

The removed V1 one-turn test driver and its result type are not a valid
Continuation authority. This module drives the public route and checks actual
SQL/audit owners. P4 SQL/lock fault and mutation cases remain in the release
UNCOVERED ledger; this module does not relabel them as ordinary success.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from litellm import ModelResponse
from sqlalchemy import select
from tests.helpers.composer_operations import message_body
from tests.helpers.continuation_app import (
    PRIMARY_MODEL,
    ScriptedSDK,
    assistant_contents,
    continuation_app,
    effects,
    end_admit,
    install_sdk,
    settle,
    submit_and_settle,
    tool_call,
)

from elspeth.web.sessions.models import (
    chat_messages_table,
    composer_continuation_effects_table,
    composition_states_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)


@dataclass(frozen=True, slots=True)
class _AuditOutcome:
    operation_id: str
    provider_calls: int
    provider_effects: int
    tool_effects: int
    attempted_ids: frozenset[str]
    ledger_ids: frozenset[str]
    linked_ledger_ids: frozenset[str | None]
    public_audit_ids: frozenset[str]
    public_audit_count: int
    private_request_ids: frozenset[str]
    private_request_count: int
    private_origins: frozenset[tuple[str, str, int]]
    physical_origins: frozenset[tuple[str, str, int]]
    sequence_numbers: tuple[int, ...]
    orphan_tool_rows: tuple[str, ...]
    orphan_tool_states: tuple[str, ...]


class _CostedSDK(ScriptedSDK):
    async def __call__(self, **kwargs):
        response = await super().__call__(**kwargs)
        assert isinstance(response, ModelResponse) and response.usage is not None
        response.usage.cost = 0.01
        return response


def _session_rows(app, session_id):
    sid = str(session_id)
    tables = {
        "messages": chat_messages_table,
        "states": composition_states_table,
        "effects": composer_continuation_effects_table,
        "attempts": quota_provider_attempts_table,
        "ledger": token_usage_ledger_table,
    }
    with app.state.session_engine.connect() as conn:
        return {name: tuple(conn.execute(select(table).where(table.c.session_id == sid)).all()) for name, table in tables.items()}


def _audit_outcome(app, session_id, operation_id, sdk: ScriptedSDK) -> _AuditOutcome:
    rows = _session_rows(app, session_id)
    messages = sorted(rows["messages"], key=lambda row: row.sequence_no)
    by_id = {row.id: row for row in messages}
    states = {row.id: row for row in rows["states"]}
    effects_for_operation = tuple(row for row in rows["effects"] if row.operation_id == operation_id)
    # The charged SDK attempt retains its original physical operation/epoch.
    # The current worker can settle it under a different public operation, so
    # the public operation ID is not a valid attempt filter.
    attempts = rows["attempts"]
    ledger = rows["ledger"]
    public_ids: set[str] = set()
    private_ids: set[str] = set()
    public_count = 0
    private_count = 0
    private_origins: set[tuple[str, str, int]] = set()
    for row in messages:
        if row.role != "audit":
            continue
        for envelope in row.tool_calls or ():
            if envelope.get("_kind") == "llm_call_audit":
                public_count += 1
                public_ids.add(envelope["call"]["call_id"])
            if envelope.get("_kind") == "composer_diagnostic_evidence":
                evidence = envelope["evidence"]
                if evidence["status"] == "provider_request":
                    private_count += 1
                    private_ids.add(evidence["tool_call_id"])
                    private_origins.add((evidence["tool_call_id"], evidence["operation_id"], evidence["operation_epoch"]))
    orphan_tools = tuple(
        row.id
        for row in messages
        if row.role == "tool"
        and (
            row.parent_assistant_id not in by_id
            or by_id[row.parent_assistant_id].role != "assistant"
            or (row.composition_state_id is not None and row.composition_state_id not in states)
        )
    )
    linked_state_ids = {row.composition_state_id for row in messages if row.role == "tool"}
    orphan_states = tuple(row.id for row in rows["states"] if row.provenance == "tool_call" and row.id not in linked_state_ids)
    return _AuditOutcome(
        operation_id=operation_id,
        provider_calls=len(sdk.requests),
        provider_effects=sum(row.owner == "provider" for row in effects_for_operation),
        tool_effects=sum(row.owner == "tool_dispatch" for row in effects_for_operation),
        attempted_ids=frozenset(row.attempt_id for row in attempts),
        ledger_ids=frozenset(row.entry_id for row in ledger),
        linked_ledger_ids=frozenset(row.ledger_entry_id for row in attempts),
        public_audit_ids=frozenset(public_ids),
        public_audit_count=public_count,
        private_request_ids=frozenset(private_ids),
        private_request_count=private_count,
        private_origins=frozenset(private_origins),
        physical_origins=frozenset((row.attempt_id, row.operation_id, row.operation_epoch) for row in attempts),
        sequence_numbers=tuple(row.sequence_no for row in messages),
        orphan_tool_rows=orphan_tools,
        orphan_tool_states=orphan_states,
    )


async def _completed_read_trace(path: Path, monkeypatch: pytest.MonkeyPatch, call_id: str) -> _AuditOutcome:
    app_ref = []
    sdk = _CostedSDK(
        [
            (
                "primary",
                lambda app: (
                    "Reading the current pipeline state.",
                    [tool_call(app, call_id, "get_pipeline_state", {})],
                ),
            ),
            ("primary", ("The current pipeline state has been inspected.", None)),
            ("end", end_admit()),
        ],
        app_ref=app_ref,
    )
    install_sdk(monkeypatch, sdk)
    operation_id = str(uuid4())
    async with continuation_app(path, monkeypatch) as harness:
        app_ref.append(harness.app)
        terminal = await submit_and_settle(
            harness,
            message_body("Inspect the current pipeline.", operation_id=operation_id),
        )
        assert terminal.status == "completed", terminal.error
        assert sdk.roles == ["primary", "primary", "end"] and not sdk.steps
        assert [content.split("\n\n---\n\n")[0] for content in assistant_contents(harness.app, harness.session_id)] == [
            "The current pipeline state has been inspected."
        ]
        second_messages = sdk.requests[1]["messages"]
        assert any(message.get("role") == "tool" and message.get("tool_call_id") == call_id for message in second_messages)
        current_effects = effects(harness.app, operation_id)
        assert all(row.disposition == "committed" for row in current_effects)
        assert {row.owner for row in current_effects} >= {"provider", "tool_dispatch", "composer_end"}
        assert any(row.owner == "tool_dispatch" and row.receipt_key and row.result_chunk_key for row in current_effects)
        return _audit_outcome(harness.app, harness.session_id, operation_id, sdk)


def test_continuation_audit_state_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Generated read-tool calls retain bidirectional audit and owner custody."""

    class ContinuationAuditMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.outcomes: list[_AuditOutcome] = []

        @rule(call_id=st.from_regex(r"call_[a-z0-9_]{1,12}", fullmatch=True))
        def read_tool(self, call_id: str) -> None:
            case = tmp_path / f"continuation-property-{uuid4().hex}"
            case.mkdir()
            self.outcomes.append(asyncio.run(_completed_read_trace(case, monkeypatch, call_id)))

        @invariant()
        def no_orphan_or_unaccounted_audit(self) -> None:
            for outcome in self.outcomes:
                assert outcome.provider_calls == 3
                assert outcome.provider_effects == 2 and outcome.tool_effects == 1
                assert len(outcome.attempted_ids) == outcome.provider_calls
                assert outcome.public_audit_ids == outcome.private_request_ids == outcome.attempted_ids
                assert outcome.public_audit_count == outcome.private_request_count == outcome.provider_calls
                assert outcome.private_origins == outcome.physical_origins
                assert None not in outcome.linked_ledger_ids
                assert outcome.ledger_ids == outcome.linked_ledger_ids
                assert len(outcome.ledger_ids) == outcome.provider_calls
                assert outcome.sequence_numbers == tuple(sorted(set(outcome.sequence_numbers)))
                assert not outcome.orphan_tool_rows and not outcome.orphan_tool_states

    run_state_machine_as_test(ContinuationAuditMachine, settings=settings(max_examples=25, stateful_step_count=2, deadline=None))


@pytest.mark.asyncio
async def test_public_stop_during_parked_primary_keeps_paid_audit_and_no_assistant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A public Stop controls the worker; cancelling the HTTP observer does not."""
    entered, release = asyncio.Event(), asyncio.Event()
    script = _CostedSDK([("primary", ("This reply must be withheld.", None)), ("end", end_admit())])

    async def parked(**kwargs):
        if kwargs["model"] == PRIMARY_MODEL:
            entered.set()
            await release.wait()
        return await script(**kwargs)

    install_sdk(monkeypatch, parked)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        async with harness.client() as client:
            accepted = await client.post(
                f"/api/sessions/{harness.session_id}/messages",
                json=message_body("Wait for the model.", operation_id=operation_id),
            )
            assert accepted.status_code == 202, accepted.text
            try:
                await asyncio.wait_for(entered.wait(), timeout=45.0)
                cancelled = await client.post(f"/api/sessions/{harness.session_id}/operations/{operation_id}/cancel")
                assert cancelled.status_code in (200, 202), cancelled.text
            finally:
                release.set()
            terminal = await settle(harness, operation_id=operation_id, client=client)
        assert terminal.status == "failed" and terminal.cancel_requested
        assert assistant_contents(harness.app, harness.session_id) == []
        outcome = _audit_outcome(harness.app, harness.session_id, operation_id, script)
        assert outcome.provider_calls == 1
        assert outcome.public_audit_ids == outcome.private_request_ids == outcome.attempted_ids
        assert outcome.public_audit_count == outcome.private_request_count == outcome.provider_calls
        assert outcome.private_origins == outcome.physical_origins
        assert None not in outcome.linked_ledger_ids
        assert outcome.ledger_ids == outcome.linked_ledger_ids and len(outcome.ledger_ids) == 1
        assert not outcome.orphan_tool_rows and not outcome.orphan_tool_states


@pytest.mark.asyncio
async def test_tool_call_cap_refuses_before_any_tool_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The current phase owner rejects an over-cap model batch after paid PRIMARY."""
    app_ref = []
    script = _CostedSDK(
        [
            (
                "primary",
                lambda app: (
                    "Over-cap tool batch.",
                    [tool_call(app, f"cap_{index}", "get_pipeline_state", {}) for index in range(17)],
                ),
            )
        ],
        app_ref=app_ref,
    )
    install_sdk(monkeypatch, script)
    operation_id = str(uuid4())
    async with continuation_app(tmp_path, monkeypatch) as harness:
        app_ref.append(harness.app)
        terminal = await submit_and_settle(
            harness,
            message_body("Inspect the current pipeline.", operation_id=operation_id),
        )
        assert terminal.status == "failed" and terminal.error is not None
        assert script.roles == ["primary"]
        current_effects = effects(harness.app, operation_id)
        assert any(row.owner == "provider" and row.disposition == "committed" for row in current_effects)
        assert not any(row.owner == "tool_dispatch" for row in current_effects)
        outcome = _audit_outcome(harness.app, harness.session_id, operation_id, script)
        assert outcome.public_audit_ids == outcome.private_request_ids == outcome.attempted_ids
        assert outcome.public_audit_count == outcome.private_request_count == outcome.provider_calls
        assert outcome.private_origins == outcome.physical_origins
        assert None not in outcome.linked_ledger_ids
        assert outcome.ledger_ids == outcome.linked_ledger_ids and len(outcome.ledger_ids) == 1
