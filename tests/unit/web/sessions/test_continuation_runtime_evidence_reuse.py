"""Runtime reads retain their one real SQL evidence result and late live guard."""

import asyncio
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_prompt_producers as producers
from tests.unit.web.coordination.test_continuation_p4_recovery_start import recovery_rows
from tests.unit.web.sessions import test_continuation_runtime as runtime_cases

operation_store = runtime_cases.operation_store


@pytest.mark.asyncio
async def test_runtime_source_retains_its_actual_validated_evidence_and_rejects_another_running_owner(operation_store, monkeypatch):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    original = producers.read_turn_evidence_on_connection
    observed = []

    def capture(*args, **kwargs):
        evidence = original(*args, **kwargs)
        observed.append(evidence)
        return evidence

    monkeypatch.setattr(producers, "read_turn_evidence_on_connection", capture)
    try:
        before = recovery_rows(operation_store[0])
        result = await asyncio.to_thread(
            arguments["authority"].read_continuation_runtime,
            running,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
        )
        assert len(observed) == 1 and result.evidence is observed[0].snapshot
        assert result.selected is arguments["selected"]
        assert recovery_rows(operation_store[0]) == before
        another = replace(running)
        assert another == running and another is not running
        with pytest.raises(AuditIntegrityError, match="current inventory owner"):
            await asyncio.to_thread(
                arguments["authority"].read_continuation_runtime,
                another,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
            )
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
