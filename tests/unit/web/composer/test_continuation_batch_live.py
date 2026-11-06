"""Real legacy dispatch through the extracted live owners; no cold authority."""

import pytest

from elspeth.contracts.composer_mutation import ComposerAutoMutationRefused, ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.web.composer import tool_batch
from elspeth.web.composer.audit import ComposerToolFailure
from elspeth.web.composer.protocol import ComposerMutationAuthorityRevoked, ComposerPluginCrashError
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer.test_dependent_batch_refusal import _run_batch
from tests.unit.web.composer.test_prompt_patch_reviews import _amended_approved_state

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean


@pytest.mark.asyncio
async def test_real_batch_initializes_once_and_copies_each_mutation_to_the_next_call(monkeypatch):
    initialize = tool_batch._initialize_continuation_batch_live
    prepare = tool_batch._prepare_continuation_call_live
    execute = tool_batch._execute_continuation_call_live
    finish = tool_batch._finish_continuation_batch_live
    initialized = []
    prepared = []
    executed = []
    finished = []

    async def observe_initialize(**kwargs):
        batch, messages = kwargs["batch"], kwargs["llm_messages"]
        before = len(messages)
        await initialize(**kwargs)
        assert batch.assistant_message_index == before
        assert len(messages) == before + 1
        assert messages[before]["role"] == "assistant"
        assert batch.entry_state is kwargs["acc"].state
        initialized.append((batch, messages, kwargs["acc"]))

    async def observe_prepare(**kwargs):
        batch, call = kwargs["batch"], kwargs["call"]
        assert call.pre_state is batch.state
        before = batch.state
        outcome = await prepare(**kwargs)
        assert outcome == "prepared"
        assert call.audit is not None and call.audit.version_before == before.version
        assert call.pre_version == before.version
        prepared.append((call.tool_call.id, before, call.wire))
        return outcome

    async def observe_execute(**kwargs):
        batch, call = kwargs["batch"], kwargs["call"]
        assert kwargs["acc"] is initialized[0][2]
        result = await execute(**kwargs)
        assert result == "applied" and call.phase == "applied"
        assert batch.tool_outcomes[-1].call is call.tool_call
        assert batch.tool_outcomes[-1].pre_version == call.pre_version
        assert batch.tool_outcomes[-1].strict_sent == call.wire.strict_sent
        assert batch.tool_outcomes[-1].wire_conformant == call.wire.wire_conformant
        if call.tool_call.id in ("first", "second"):
            assert batch.state is batch.tool_outcomes[-1].response.updated_state
        executed.append((call.tool_call.id, batch.state))
        return result

    def observe_finish(**kwargs):
        result = finish(**kwargs)
        batch = kwargs["batch"]
        assert result[0].state is batch.state
        assert result[0].authority_revocation is kwargs["acc"].authority_revocation
        assert result[0].tool_outcomes == tuple(batch.tool_outcomes)
        finished.append((batch.next_tool_index, batch.current_call, batch.phase))
        return result

    monkeypatch.setattr(tool_batch, "_initialize_continuation_batch_live", observe_initialize)
    monkeypatch.setattr(tool_batch, "_prepare_continuation_call_live", observe_prepare)
    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    monkeypatch.setattr(tool_batch, "_finish_continuation_batch_live", observe_finish)
    state = _amended_approved_state()
    result, handlers = await _run_batch(
        state,
        [
            ("first", "set_metadata", {"patch": {"description": "first retained description"}}),
            ("second", "set_metadata", {"patch": {"description": "second retained description"}}),
            ("read", "get_pipeline_state", {}),
        ],
    )
    assert len(initialized) == 1
    assert [item[0] for item in prepared] == ["first", "second", "read"]
    assert [item[0] for item in executed] == ["first", "second", "read"]
    assert prepared[0][1] is state
    assert prepared[1][1] is executed[0][1]
    assert prepared[2][1] is executed[1][1]
    assert executed[0][1].metadata.description == "first retained description"
    assert executed[1][1].metadata.description == "second retained description"
    assert executed[1][1].version > executed[0][1].version > state.version
    assert executed[2][1] is executed[1][1]
    assert handlers == ["set_metadata", "set_metadata", "get_pipeline_state"]
    assert finished == [(3, None, "dispatch_ready")]
    assert [item.call.id for item in result.tool_outcomes_for_assertion] == ["first", "second", "read"]


@pytest.mark.asyncio
async def test_real_preparation_refusal_continues_without_executing_any_call(monkeypatch):
    prepare = tool_batch._prepare_continuation_call_live
    completed = []

    async def observe_prepare(**kwargs):
        outcome = await prepare(**kwargs)
        call, batch = kwargs["call"], kwargs["batch"]
        completed.append((call.tool_call.id, outcome, call.phase, len(batch.tool_outcomes)))
        return outcome

    async def must_not_execute(**kwargs):
        raise AssertionError("mixed abandonment preparation must finish before tool execution")

    monkeypatch.setattr(tool_batch, "_prepare_continuation_call_live", observe_prepare)
    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", must_not_execute)
    result, handlers = await _run_batch(
        _amended_approved_state(),
        [
            (
                "abandon",
                "request_advisor_hint",
                {
                    "trigger": "cannot_complete",
                    "problem_summary": "No authorized next action",
                    "recent_errors": [],
                    "attempted_actions": [],
                },
            ),
            ("following", "get_pipeline_state", {}),
        ],
    )
    assert completed == [("abandon", "applied", "applied", 1), ("following", "applied", "applied", 2)]
    assert handlers == []
    assert [outcome.error_class for outcome in result.tool_outcomes_for_assertion] == ["ToolArgumentError", "ToolArgumentError"]


@pytest.mark.asyncio
async def test_real_plugin_break_retains_original_exception_and_executed_prefix(monkeypatch):
    execute_tool = tool_batch.execute_tool
    execute_call = tool_batch._execute_continuation_call_live
    failure = RuntimeError("controlled handler failure")
    seen = []
    retained = []

    def fail_second(tool_name, arguments, state, catalog, **kwargs):
        seen.append(arguments["patch"]["description"])
        if seen[-1] == "fail here":
            raise failure
        return execute_tool(tool_name, arguments, state, catalog, **kwargs)

    async def observe_execute(**kwargs):
        result = await execute_call(**kwargs)
        if kwargs["batch"].plugin_crash is not None:
            assert kwargs["batch"].stop_issued_batch is True
            retained.append(kwargs["batch"])
        return result

    monkeypatch.setattr(tool_batch, "execute_tool", fail_second)
    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    with pytest.raises(ComposerPluginCrashError) as raised:
        await _run_batch(
            _amended_approved_state(),
            [
                ("successful", "set_metadata", {"patch": {"description": "preserved prefix"}}),
                ("crashing", "set_metadata", {"patch": {"description": "fail here"}}),
                ("unissued", "set_metadata", {"patch": {"description": "must not run"}}),
            ],
        )
    assert raised.value.original_exc is failure
    assert seen == ["preserved prefix", "fail here"]
    assert len(retained) == 1
    batch = retained[0]
    assert batch.plugin_crash_cause is failure
    assert batch.plugin_crash.original_exc is failure
    assert batch.stop_issued_batch is True
    assert batch.state.metadata.description == "preserved prefix"
    assert [outcome.call.id for outcome in batch.tool_outcomes] == ["successful", "crashing"]
    assert batch.tool_outcomes[1].pre_version == batch.tool_outcomes[0].post_version


@pytest.mark.asyncio
async def test_actual_request_revocation_owner_survives_extraction_and_stops_following_call(monkeypatch):
    finish = tool_batch._finish_continuation_batch_live
    revocations = []
    observed = []

    def refuse_current_mutation(tool_name, arguments, state, catalog, **kwargs):
        ceiling = kwargs["mutation_ceiling"]
        assert type(ceiling) is ComposerMutationCeiling
        assert ceiling.initial_trust_mode == "auto_commit"
        refusal = ComposerMutationRevocation(ceiling, "explicit_approve")
        revocations.append(refusal)
        raise ComposerAutoMutationRefused(refusal)

    def observe_finish(**kwargs):
        result = finish(**kwargs)
        observed.append((kwargs["acc"].authority_revocation, result[0].authority_revocation, kwargs["batch"]))
        return result

    monkeypatch.setattr(tool_batch, "execute_tool", refuse_current_mutation)
    monkeypatch.setattr(tool_batch, "_finish_continuation_batch_live", observe_finish)
    with pytest.raises(ComposerMutationAuthorityRevoked) as raised:
        await _run_batch(
            _amended_approved_state(),
            [
                ("revoked", "set_metadata", {"patch": {"description": "refused"}}),
                ("unissued", "set_metadata", {"patch": {"description": "must not run"}}),
            ],
        )
    assert len(revocations) == len(observed) == 1
    assert raised.value.revocation is revocations[0]
    acc_revocation, dispatch_revocation, batch = observed[0]
    assert acc_revocation is dispatch_revocation is revocations[0]
    assert batch.stop_issued_batch is True
    assert [outcome.call.id for outcome in batch.tool_outcomes] == ["revoked"]
    assert batch.tool_outcomes[0].response.validation.errors[0].error_code == "auto_mutation_revoked"


@pytest.mark.asyncio
async def test_real_cache_continue_keeps_following_mutation_and_invocation_order(monkeypatch):
    execute = tool_batch._execute_continuation_call_live
    observations = []

    async def observe_execute(**kwargs):
        result = await execute(**kwargs)
        call, batch, recorder = kwargs["call"], kwargs["batch"], kwargs["ctx"].recorder
        invocation = next(item for item in recorder.invocations if item.tool_call_id == call.tool_call.id)
        observations.append((call.tool_call.id, invocation.cache_hit, batch.stop_issued_batch))
        return result

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    result, handlers = await _run_batch(
        _amended_approved_state(),
        [
            ("miss", "list_sources", {}),
            ("hit", "list_sources", {}),
            ("following", "set_metadata", {"patch": {"description": "after cached continue"}}),
        ],
    )
    assert observations == [("miss", False, False), ("hit", True, False), ("following", False, False)]
    assert handlers == ["list_sources", "set_metadata"]
    assert result.tool_outcomes_for_assertion[-1].response.updated_state.metadata.description == "after cached continue"


@pytest.mark.asyncio
async def test_real_narrow_exception_unwinds_with_copyback_and_original_identity(monkeypatch):
    execute_tool = tool_batch.execute_tool
    execute_call = tool_batch._execute_continuation_call_live
    failure = AssertionError("controlled invariant failure")
    seen = []
    unwound = []

    def fail_second(tool_name, arguments, state, catalog, **kwargs):
        seen.append(arguments["patch"]["description"])
        if seen[-1] == "raise invariant":
            raise failure
        return execute_tool(tool_name, arguments, state, catalog, **kwargs)

    async def observe_execute(**kwargs):
        try:
            return await execute_call(**kwargs)
        except AssertionError as inner_failure:
            assert inner_failure is failure
            unwound.append((kwargs["batch"], kwargs["ctx"].recorder.invocations))
            raise

    monkeypatch.setattr(tool_batch, "execute_tool", fail_second)
    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    with pytest.raises(ComposerToolFailure) as raised:
        await _run_batch(
            _amended_approved_state(),
            [
                ("successful", "set_metadata", {"patch": {"description": "preserved before unwind"}}),
                ("invariant", "set_metadata", {"patch": {"description": "raise invariant"}}),
                ("unissued", "set_metadata", {"patch": {"description": "must not run"}}),
            ],
        )
    assert type(raised.value) is ComposerToolFailure
    assert raised.value.original_exception is failure
    assert raised.value.__cause__ is failure
    assert seen == ["preserved before unwind", "raise invariant"]
    assert len(unwound) == 1
    batch, invocations = unwound[0]
    assert raised.value.tool_invocations == invocations
    assert batch.state.metadata.description == "preserved before unwind"
    assert batch.plugin_crash is None and batch.plugin_crash_cause is None
    assert [outcome.call.id for outcome in batch.tool_outcomes] == ["successful"]
    assert [(item.tool_call_id, item.error_class) for item in invocations] == [("successful", None), ("invariant", "AssertionError")]
