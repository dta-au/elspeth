"""Real recipe histories share SQL proof only within one lexical read."""

import asyncio
import json

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_message_families as families
from elspeth.web.composer import continuation_phase_transitions as transitions
from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, encode_restored_prepared_batch_data
from elspeth.web.composer.continuation_message_family_builders import family_message_wire
from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe, ToolResultMessageRecipe
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case, stored_rows
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_count", (1, 3))
@pytest.mark.parametrize("corruption", ("unselected_issue", "selected_completion"))
async def test_actual_result_and_feedback_share_one_fresh_lazy_cohort(operation_store, tmp_path, monkeypatch, requested_count, corruption):
    async with _phase_case(operation_store, tmp_path, monkeypatch, following_tool_call=("feedback_read", "get_pipeline_state", {})) as (
        owner,
        running,
        primary,
        sdk,
    ):
        actual_cohort = transitions.read_tool_effect_sources_on_connection
        cohorts = []

        def cohort(conn, *args, **values):
            assert conn.in_transaction()
            result = actual_cohort(conn, *args, **values)
            cohorts.append((conn, result))
            return result

        # A genuine current SQL read with no result/feedback recipes must not
        # eagerly acquire the new cohort. Later positives calibrate this spy.
        with monkeypatch.context() as patch:
            patch.setattr(transitions, "read_tool_effect_sources_on_connection", cohort)
            await asyncio.to_thread(
                owner.authority.read_continuation_runtime,
                running,
                selected=owner.runtime.source.selected,
                inventory=owner.runtime.materialization.inventory,
            )
        assert cohorts == []
        await _advance(owner)
        await _advance(owner)
        original_error = ToolArgumentError(argument="arguments", expected="an object", actual_type="invalid value")

        def refuse_arguments(*args, **kwargs):
            raise original_error

        with monkeypatch.context() as patch:
            patch.setattr(tool_batch, "execute_tool", refuse_arguments)
            await _advance(owner)
        runtime = owner.runtime
        requests = []
        for call_id in ("first_read", "second_read", "feedback_read"):
            message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == call_id)
            wire, recipe = runtime.materialization.capture_message(message)
            requests.append((recipe, wire))
        assert [type(recipe) for recipe, _wire in requests] == [ToolResultMessageRecipe, ToolResultMessageRecipe, ToolFeedbackMessageRecipe]
        current = families.CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        selected_requests = tuple(requests[:requested_count])
        before = stored_rows(operation_store[0])
        actual_completed = families._require_completed_tool_source_on_connection
        indexes = []

        def completed(*args, **values):
            actual_lookup = values["read_issues"]

            def lookup():
                index = actual_lookup()
                indexes.append(index)
                return index

            return actual_completed(*args, **{**values, "read_issues": lookup})

        with monkeypatch.context() as patch:
            patch.setattr(transitions, "read_tool_effect_sources_on_connection", cohort)
            patch.setattr(families, "_require_completed_tool_source_on_connection", completed)
            restored = await asyncio.to_thread(current.restore_messages, selected_requests)
            assert tuple(map(family_message_wire, restored)) == tuple(wire for _recipe, wire in selected_requests)
            assert len(cohorts) == 1 and len(cohorts[0][1]) == 3 and cohorts[0][0].closed
            assert len(indexes) == 3 and all(index is indexes[0] for index in indexes)
            assert indexes[0] == {(issue.response.digest, issue.tool_index): (issue,) for issue in cohorts[0][1]}
            restored_again = await asyncio.to_thread(current.restore_messages, selected_requests)
            assert tuple(map(family_message_wire, restored_again)) == tuple(wire for _recipe, wire in selected_requests)
            assert len(cohorts) == 2 and cohorts[1][1] == cohorts[0][1]
            assert all(second is not first for first, second in zip(cohorts[0][1], cohorts[1][1], strict=True))
            assert len(indexes) == 6 and all(index is indexes[3] for index in indexes[3:])
            assert indexes[3] is not indexes[0]
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls

        first_issue = next(issue for issue in cohorts[0][1] if issue.tool_call_id == "first_read")
        last_issue = next(issue for issue in cohorts[0][1] if issue.tool_call_id == "feedback_read")
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        if corruption == "unselected_issue":
            key = f"tool:{last_issue.response.digest}:{last_issue.tool_index}"
            where = (effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == key)
            with operation_store[0].begin() as conn:
                original = conn.execute(select(effects.c.action_json).where(*where)).scalar_one()
                changed = json.loads(original)
                changed["tool_call_id"] = "different_original_call"
                conn.execute(update(effects).where(*where).values(action_json=canonical_json(changed)))
            restore = update(effects).where(*where).values(action_json=original)
            error = "tool issue lost its actual immutable event/checkpoint"
        else:
            where = (events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == first_issue.cursor + 1)
            with operation_store[0].begin() as conn:
                original = conn.execute(select(events.c.tag).where(*where)).scalar_one()
                conn.execute(update(events).where(*where).values(tag="may_dispatch"))
            restore = update(events).where(*where).values(tag=original)
            error = "tool result has no exact committed result/event/checkpoint"
        try:
            # Even requesting only the first result re-proves the unrequested
            # historical issue; a previous successful read grants no reuse.
            with pytest.raises(AuditIntegrityError, match=error):
                await asyncio.to_thread(current.restore_messages, (requests[0],))
        finally:
            with operation_store[0].begin() as conn:
                conn.execute(restore)
        final = await asyncio.to_thread(current.restore_messages, selected_requests)
        assert tuple(map(family_message_wire, final)) == tuple(wire for _recipe, wire in selected_requests)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ("none", "selected_call_id", "selected_reference", "unselected"))
async def test_actual_cohort_index_retains_selected_multiplicity_without_global_duplicate_refusal(
    operation_store, tmp_path, monkeypatch, duplicate
):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, primary, sdk):
        await _advance(owner)
        await _advance(owner)
        runtime = owner.runtime
        requests = []
        for call_id in ("first_read", "second_read"):
            message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == call_id)
            wire, recipe = runtime.materialization.capture_message(message)
            assert type(recipe) is ToolResultMessageRecipe
            requests.append((recipe, wire))
        current = families.CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        actual_cohort = transitions.read_tool_effect_sources_on_connection
        observed = []
        selected_keys = {
            (recipe.response.digest, recipe.tool_index)
            for recipe in runtime.source.evidence.transcript.producer_recipes or ()
            if type(recipe) in (ToolResultMessageRecipe, ToolFeedbackMessageRecipe)
        }
        assert selected_keys == {(recipe.response.digest, recipe.tool_index) for recipe, _wire in requests}

        def cohort(conn, *args, **values):
            issues = actual_cohort(conn, *args, **values)
            assert len(issues) == 2
            first = next(issue for issue in issues if issue.tool_call_id == "first_read")
            # An accidental reader-output opponent, after the genuine complete
            # SQL proof; these copies do not purport to be persisted history.
            if duplicate == "selected_call_id":
                supplied = (*issues, first.model_copy(update={"tool_call_id": "another_call"}))
            elif duplicate == "selected_reference":
                response = first.response.model_copy(update={"byte_length": first.response.byte_length + 1})
                supplied = (*issues, first.model_copy(update={"response": response}))
            elif duplicate == "unselected":
                index = max(issue.tool_index for issue in issues) + 1
                assert (first.response.digest, index) not in selected_keys
                unrelated = first.model_copy(update={"tool_index": index})
                supplied = (*issues, unrelated, unrelated.model_copy(update={"tool_call_id": "another_unselected_call"}))
            else:
                supplied = issues
            observed.append((issues, supplied))
            return supplied

        before = stored_rows(operation_store[0])
        with monkeypatch.context() as patch:
            patch.setattr(transitions, "read_tool_effect_sources_on_connection", cohort)
            if duplicate.startswith("selected_"):
                with pytest.raises(AuditIntegrityError, match="tool result source lost its actual immutable issue"):
                    await asyncio.to_thread(current.restore_messages, tuple(requests))
            else:
                restored = await asyncio.to_thread(current.restore_messages, tuple(requests))
                assert tuple(map(family_message_wire, restored)) == tuple(wire for _recipe, wire in requests)
        assert len(observed) == 1
        restored = await asyncio.to_thread(current.restore_messages, tuple(requests))
        assert tuple(map(family_message_wire, restored)) == tuple(wire for _recipe, wire in requests)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("reread", ("same", "valid_version", "valid_other_prepared", "malformed"))
async def test_actual_effects_keep_prepared_reread_and_changed_text_decoder(operation_store, tmp_path, monkeypatch, reread):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        await _advance(owner)
        selected, names = owner.runtime.source.selected, owner.runtime.materialization.inventory.names
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        key = f"tool:{primary.response.reference.digest}:0"
        with locked_session_transaction(operation_store[0], sid) as conn:
            issue = transitions.read_tool_issue_data_on_connection(
                conn, session_id=running.claim.session_id, operation_id=operation, effect_key=key, env_ref_names=names
            )
            assert type(issue) is transitions.ToolIssueSource  # public issue-only compatibility
            prepared_key = f"value:{issue.prepared.codec}:{issue.prepared.digest}"
            encoded = read_chunk(conn, session_id=sid, operation_id=operation, key=prepared_key).decode("utf-8", errors="strict")
            other = transitions.read_tool_issue_data_on_connection(
                conn,
                session_id=running.claim.session_id,
                operation_id=operation,
                effect_key=f"tool:{primary.response.reference.digest}:1",
                env_ref_names=names,
            )
            other_encoded = read_chunk(
                conn, session_id=sid, operation_id=operation, key=f"value:{other.prepared.codec}:{other.prepared.digest}"
            ).decode("utf-8", errors="strict")
        candidate = encoded
        if reread == "valid_version":
            detached = decode_prepared_batch_data(encoded, env_ref_names=names)
            detached.batch.provider_model_version += "-another-safe-version"
            candidate = encode_restored_prepared_batch_data(detached, env_ref_names=names)
            assert candidate != encoded
            admitted_candidate = decode_prepared_batch_data(candidate, env_ref_names=names)
            assert admitted_candidate.calls == detached.calls
        elif reread == "valid_other_prepared":
            candidate = other_encoded
            assert candidate != encoded
            admitted_candidate = decode_prepared_batch_data(candidate, env_ref_names=names)
            assert admitted_candidate.batch.current_call is not None
            assert admitted_candidate.batch.current_call.index == 1
        elif reread == "malformed":
            candidate = "{}"
        actual_issue = transitions._read_tool_issue_with_prepared_data_on_connection
        actual_read = transitions.read_chunk
        actual_decode = transitions.decode_prepared_batch_data
        issued = []
        target_reads, decoded = [], []

        def read_issue(conn, **values):
            result = actual_issue(conn, **values)
            if values["effect_key"] == key:
                issued.append(result)
            return result

        def read(conn, **values):
            raw = actual_read(conn, **values)
            if values["key"] == prepared_key:
                target_reads.append(raw)
                if issued:
                    return candidate.encode("utf-8")
            return raw

        def decode(text, **values):
            decoded.append(text)
            return actual_decode(text, **values)

        before = stored_rows(operation_store[0])
        with monkeypatch.context() as patch:
            patch.setattr(transitions, "_read_tool_issue_with_prepared_data_on_connection", read_issue)
            patch.setattr(transitions, "read_chunk", read)
            patch.setattr(transitions, "decode_prepared_batch_data", decode)
            with locked_session_transaction(operation_store[0], sid) as conn:
                if reread in ("valid_other_prepared", "malformed"):
                    match = "tool result changed its exact issued call/completed prefix" if reread == "valid_other_prepared" else None
                    with pytest.raises(AuditIntegrityError, match=match):
                        transitions.read_tool_effect_sources_on_connection(conn, running, selected=selected, env_ref_names=names)
                else:
                    sources = transitions.read_tool_effect_sources_on_connection(conn, running, selected=selected, env_ref_names=names)
                    assert len(sources) == 2 and issue in sources
        assert len(issued) == 1 and issued[0][1] == encoded
        assert target_reads == [encoded.encode("utf-8"), encoded.encode("utf-8")]
        assert decoded.count(encoded) == 1
        if reread != "same":
            assert candidate in decoded and decoded.index(candidate) > decoded.index(encoded)
        assert owner.runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        owner.work.coordinator.assert_completed()
