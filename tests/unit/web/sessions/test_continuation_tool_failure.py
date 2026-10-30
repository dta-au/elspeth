"""Real failed dispatch custody, portable prefix and terminal append controls."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_batch_data import decode_coupled_batch_data, encode_coupled_batch_data
from tests.unit.web.sessions.test_continuation_checkpoint_outcome import coordinator as _coordinator
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_p4_product import _contains_identity

operation_store = _operation_store
coordinator = _coordinator


@asynccontextmanager
async def _issued_argument_feedback_case(store, tmp_path, monkeypatch, *, recover_owner=False):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.protocol import SchemaViolation, SchemaViolationCode, ToolArgumentError

    async with _phase_case(store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        original = ToolArgumentError(
            argument="arguments",
            expected="an object",
            actual_type="invalid value",
            schema_violations=(SchemaViolation((), SchemaViolationCode.UNEXPECTED),),
        )
        cause = RuntimeError("native argument cause stays private")
        dispatches = []

        def execute(*args, **kwargs):
            dispatches.append(args)
            raise original from cause

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        if recover_owner:
            async with _recovered_feedback_owner(store, owner) as current:
                yield current, current.work.running, primary, sdk, original, cause, dispatches
        else:
            yield owner, running, primary, sdk, original, cause, dispatches


@asynccontextmanager
async def _recovered_feedback_owner(store, owner):
    from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import _registered_read, restore_continuation_runtime
    from elspeth.web.composer.continuation_selection import load_selected_continuation
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation import StoredContinuationSnapshot
    from tests.helpers.continuation_inventory import inventory_work
    from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
    from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

    first = owner.work.running
    assert type(first) is ComposerOperationRunning
    assert first.claim.segment_sequence is not None and first.claim.ownership_generation is not None
    await owner.lease.close()
    cold_store = new_owners(store)
    _engine, repository, authority, _sessions, _sid = cold_store
    assert (
        repository.recover_composer_segment(
            session_id=first.claim.session_id,
            operation_id=first.claim.operation_id,
            expected_generation=first.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    running = ComposerOperationRunning(claim, context)
    work = inventory_work(running)
    adoption = ContinuationLeaseAdoption(repository, context, lease_seconds=30, required_work=work.coordinator)
    try:
        lease = await adoption.join()
        assert claim.operation_id == first.claim.operation_id and claim.segment_sequence == first.claim.segment_sequence
        assert claim.ownership_generation == first.claim.ownership_generation + 1 and claim.attempt == first.claim.attempt + 1
        assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
        service, _actors = current_service(cold_store)
        provider = ProviderInvocationOwner(service=cold_store[3], required_work=work)
        stored = await _registered_read(work, StoredContinuationSnapshot, lambda: authority.load_continuation_snapshot(running))
        selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=work
        )
        restored = await restore_continuation_runtime(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=work,
            provider_owner=provider,
        )
        current = ContinuationLivePhases(service, authority, work, provider, lease)
        current.bind_initial(restored)
        assert current.runtime is not owner.runtime and current.runtime.source.selected is selected
        assert current.runtime.bindings.session_operation_context is context
        yield current
    finally:
        if adoption.lease is None:
            repository.release(context)
        else:
            await adoption.lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True))
async def test_actual_issued_feedback_preserves_rich_ordered_wire_through_cold_read(operation_store, tmp_path, monkeypatch, stop):
    import json

    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
    from elspeth.web.composer.tool_error_payloads import arg_error_payload
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
    from tests.unit.web.sessions.test_continuation_live_phases import _finish_inventory, stored_rows

    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch) as values:
        owner, running, primary, sdk, original, cause, dispatches = values
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        assert type(recipe) is ToolFeedbackMessageRecipe and recipe.source == "arg_error"
        payload = arg_error_payload(original, "get_pipeline_state")
        assert "validation_errors" in payload and "repair_instruction" in payload
        assert message["content"] == json.dumps(payload) and message["content"] != canonical_json(payload)
        invocation = runtime.live.recorder.invocations[-1]
        assert invocation.status is ComposerToolStatus.ARG_ERROR and json.loads(invocation.result_canonical) == payload
        assert original.__cause__ is cause and owner.failure_custody.observation.feedback is original
        assert owner.failure_custody.original is None and not owner.failure_custody.completed
        selected, inventory, facet = runtime.source.selected, runtime.materialization.inventory, None
        if stop:
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            selected, inventory = await _finish_inventory(owner, running)
            facet = ContinuationFinishRead(running, selected, inventory)
        before = stored_rows(operation_store[0])
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=selected,
            inventory=inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
            finish_read=facet,
        )
        actual = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "first_read")
        assert actual is not message and actual["content"] == message["content"]
        assert restored.materialization.capture_message(actual) == (wire, recipe)
        assert restored.graph.batch.batch.tool_outcomes[0].call is restored.graph.batch.calls[0]
        assert runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        assert len(dispatches) == len(sdk) == 1 and stored_rows(operation_store[0]) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("copy", "body", "parts", "trust", "excess_body", "excess_partition"))
async def test_actual_feedback_provenance_precedes_ordinary_excess(operation_store, tmp_path, monkeypatch, change):
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY, PromptGuardContentPart, PromptGuardTrust
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES

    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch) as values:
        owner, _running, _primary, sdk, _original, _cause, dispatches = values
        materialization = owner.runtime.materialization
        message = next(item for item in owner.runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        original = materialization.capture_message(message)
        materialization.retention_message_view(message)
        content, parts = message["content"], message[PROMPT_GUARD_CONTENT_PARTS_KEY]
        target = message
        try:
            if change == "copy":
                target = dict(message)
            elif change == "body":
                message["content"] = content.encode().decode()
                assert message["content"] == content and message["content"] is not content
            elif change == "parts":
                message[PROMPT_GUARD_CONTENT_PARTS_KEY] = tuple(part for part in parts)
            elif change == "trust":
                object.__setattr__(parts[0], "trust", PromptGuardTrust.REFERENCE)
            else:
                message["content"] = '"' + "x" * JSON_MAX_TOTAL_UTF8_BYTES + '"'
                if change == "excess_body":
                    message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (PromptGuardContentPart(message["content"]),)
                else:
                    message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (PromptGuardContentPart(""), *parts)
            with pytest.raises(AuditIntegrityError):
                materialization.capture_message(target)
            with pytest.raises(AuditIntegrityError):
                materialization.retention_message_view(target)
        finally:
            object.__setattr__(parts[0], "trust", None)
            message["content"], message[PROMPT_GUARD_CONTENT_PARTS_KEY] = content, parts
        assert materialization.capture_message(message) == original and len(dispatches) == len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("kind", "call", "index", "wire", "status", "hash", "payload", "authoring"))
async def test_actual_feedback_current_reader_opposes_foreign_source_after_positive(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies

    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch) as values:
        owner, _running, _primary, sdk, _original, _cause, dispatches = values
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        families = CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        assert families.restore_messages(((recipe, wire),))
        if change in ("kind", "call", "index", "wire"):
            altered = recipe
            if change == "kind":
                altered = recipe.model_copy(update={"source": "advisor_success"})
            elif change == "call":
                altered = recipe.model_copy(update={"tool_call_id": "second_read"})
            elif change == "index":
                altered = recipe.model_copy(update={"tool_index": 1})
            else:
                wire = wire + " "
            with pytest.raises(AuditIntegrityError):
                families.restore_messages(((altered, wire),))
        else:
            from elspeth.web.composer.continuation_message_family_builders import require_feedback_invocation

            invocation = runtime.live.recorder.invocations[-1]
            authoring = runtime.graph.batch.batch.private_authorings_by_call["first_read"]
            updates = (
                {"status": ComposerToolStatus.SUCCESS, "error_category": None}
                if change == "status"
                else (
                    {"result_hash": "0" * 64}
                    if change == "hash"
                    else ({"result_canonical": "{}"} if change == "payload" else {"private_authoring": replace(authoring)})
                )
            )
            with pytest.raises(AuditIntegrityError):
                require_feedback_invocation(
                    call=runtime.graph.batch.calls[0],
                    invocation=replace(invocation, **updates),
                    authoring=authoring,
                    source="arg_error",
                    outcome=runtime.graph.batch.batch.tool_outcomes[0],
                )
        assert len(dispatches) == len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_argument_feedback_factory_divergence_retains_native_cause(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.protocol import ToolArgumentError

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        original, cause = ToolArgumentError(argument="arguments", expected="an object", actual_type="missing"), RuntimeError("actual cause")
        actual_factory, payloads, dispatches = tool_batch._arg_error_payload, [], []

        def payload(error, name):
            value = dict(actual_factory(error, name))
            if payloads:
                value["repair_instruction"] = "Different second factory result."
            payloads.append(value)
            return value

        def execute(*args, **kwargs):
            dispatches.append(args)
            raise original from cause

        monkeypatch.setattr(tool_batch, "_arg_error_payload", payload)
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and original.__cause__ is cause
        assert len(payloads) == 2 and payloads[0] != payloads[1] and len(dispatches) == len(sdk) == 1
        assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("success", "unicode", "timeout", "provider"))
async def test_actual_advisor_feedback_uses_successful_audit_projection_and_cold_source(operation_store, tmp_path, monkeypatch, branch):
    import json

    import litellm

    from elspeth.web.composer.advisor_request import advisor_feedback_payload
    from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
    from tests.unit.web.sessions.test_continuation_live_phases import stored_rows

    async with _advisor_case(operation_store, tmp_path, monkeypatch) as (owner, _running, sdk):
        original = (
            TimeoutError("actual Advisor feedback timeout")
            if branch == "timeout"
            else litellm.ServiceUnavailableError("private transient detail", model="advisor", llm_provider="openai")
        )
        if branch == "unicode":

            async def complete(**kwargs):
                sdk.append(kwargs)
                return litellm.ModelResponse(
                    model="test/model",
                    choices=[{"message": {"role": "assistant", "content": 'Actual café guidance: "inspect"\nThen continue.'}}],
                    usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                )

            monkeypatch.setattr(litellm, "acompletion", complete)
        elif branch != "success":

            async def complete(**kwargs):
                sdk.append(kwargs)
                raise original

            monkeypatch.setattr(litellm, "acompletion", complete)
        await _advance(owner)
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        wire, recipe = runtime.materialization.capture_message(message)
        assert type(recipe) is ToolFeedbackMessageRecipe and recipe.source == "advisor_success"
        invocation, outcome = runtime.live.recorder.invocations[-1], runtime.graph.batch.batch.tool_outcomes[-1]
        assert invocation.status is ComposerToolStatus.SUCCESS and outcome.error_class is None
        assert json.loads(message["content"]) == advisor_feedback_payload(outcome.response)
        assert "budget_used" in json.loads(invocation.result_canonical) and "budget_used" not in json.loads(message["content"])
        assert owner.failure_custody.original is None and not owner.failure_custody.completed
        if branch not in ("success", "unicode"):
            assert any(item is original for item in owner.failure_custody.handled)
        before = stored_rows(operation_store[0])
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=runtime.source.selected,
            inventory=runtime.materialization.inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
        )
        actual = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        assert restored.materialization.capture_message(actual) == (wire, recipe)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 2


@pytest.mark.asyncio
async def test_actual_advisor_issued_argument_refusal_uses_recorded_projected_wire(operation_store, tmp_path, monkeypatch):
    import json

    from elspeth.web.composer.advisor_request import advisor_feedback_payload
    from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime

    async with _advisor_case(operation_store, tmp_path, monkeypatch, advisor_prompt_tokens=1) as (owner, _running, sdk):
        await _advance(owner)
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        wire, recipe = runtime.materialization.capture_message(message)
        invocation = runtime.live.recorder.invocations[-1]
        assert type(recipe) is ToolFeedbackMessageRecipe and recipe.source == "arg_error"
        assert invocation.status is ComposerToolStatus.ARG_ERROR and invocation.error_category.value == "prompt_budget"
        assert json.loads(message["content"]) == advisor_feedback_payload(json.loads(invocation.result_canonical))
        assert runtime.graph.batch.batch.tool_outcomes[0].response is None and len(sdk) == 1
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=runtime.source.selected,
            inventory=runtime.materialization.inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
        )
        actual = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        assert restored.materialization.capture_message(actual) == (wire, recipe) and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("timeout", "provider"))
async def test_actual_advisor_feedback_then_primary_failure_keeps_distinct_origins(operation_store, tmp_path, monkeypatch, branch):
    import litellm

    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime

    async with _advisor_case(operation_store, tmp_path, monkeypatch, following_call=True) as (owner, running, sdk):
        feedback = (
            TimeoutError("actual earlier Advisor timeout")
            if branch == "timeout"
            else litellm.ServiceUnavailableError("private earlier provider outage", model="advisor", llm_provider="openai")
        )
        primary, calls = RuntimeError("actual following plugin origin"), []

        async def complete(**kwargs):
            sdk.append(kwargs)
            raise feedback

        def execute(*args, **kwargs):
            calls.append(args)
            raise primary

        monkeypatch.setattr(litellm, "acompletion", complete)
        await _advance(owner)
        first = owner.failure_custody
        assert first.original is None and first.handled == [feedback] and not first.completed
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        second = owner.failure_custody
        assert second is not first and second.original is primary and second.completed
        assert not second.handled and second.observation.feedback is None
        assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is not None
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=owner.runtime.source.selected,
            inventory=owner.runtime.materialization.inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
        )
        message = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        assert restored.materialization.capture_message(message)[1].source == "advisor_success"
        assert len(calls) == 1 and len(sdk) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("result", "arg_error"))
async def test_actual_session_inner_publication_preserves_owner_before_outer_completion(operation_store, tmp_path, monkeypatch, kind):
    from elspeth.web.composer import tools
    from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe, ToolResultMessageRecipe
    from elspeth.web.composer.continuation_prompt_producers import ContinuationToolMessagePublication
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
    from elspeth.web.composer.protocol import ToolArgumentError
    from elspeth.web.composer.tools._common import ToolResult

    arguments = {"affected_node_id": "rate_node", "kind": "vague_term", "user_term": "cool", "llm_draft": "Use concise wording."}
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="request_interpretation_review", tool_arguments=arguments, bound_base=True
    ) as (owner, _running, sdk):
        calls, publications = [], []
        original = ToolArgumentError(argument="llm_draft", expected="a string", actual_type="missing")
        actual_result, actual_feedback = (
            ContinuationToolMessagePublication.register_result,
            ContinuationToolMessagePublication.register_feedback,
        )

        async def handler(**kwargs):
            calls.append(kwargs)
            if kind == "arg_error":
                raise original
            return ToolResult(
                success=True,
                updated_state=kwargs["state"],
                validation=owner.service._policy_context.build("alice")[1].validate_composition_state(kwargs["state"]).validation,
                affected_nodes=(),
                data={"message": "actual handler result"},
            )

        def result(self, **kwargs):
            assert len(self.batch.tool_outcomes) == self.call.index
            value = actual_result(self, **kwargs)
            assert value is kwargs["message"]
            publications.append(value)
            return value

        def feedback(self, **kwargs):
            assert len(self.batch.tool_outcomes) == self.call.index
            value = actual_feedback(self, **kwargs)
            assert value is kwargs["message"]
            publications.append(value)
            return value

        monkeypatch.setitem(tools._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", handler)
        monkeypatch.setattr(ContinuationToolMessagePublication, "register_result", result)
        monkeypatch.setattr(ContinuationToolMessagePublication, "register_feedback", feedback)
        await _advance(owner)
        assert len(calls) == len(publications) == len(sdk) == 1
        runtime = owner.runtime
        assert any(item is publications[0] for item in runtime.live.llm_messages)
        wire, recipe = runtime.materialization.capture_message(publications[0])
        assert type(recipe) is (ToolResultMessageRecipe if kind == "result" else ToolFeedbackMessageRecipe)
        assert runtime.graph.batch.batch.tool_outcomes[0].call is runtime.graph.batch.calls[0]
        assert not owner.failure_custody.completed
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=runtime.source.selected,
            inventory=runtime.materialization.inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
        )
        message = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "actual_advisor")
        assert restored.materialization.capture_message(message) == (wire, recipe)
        assert len(calls) == len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("receipt", "creator", "event", "issue"))
async def test_actual_feedback_requires_committed_creator_event_receipt_and_issue(operation_store, tmp_path, monkeypatch, change):
    import json

    from sqlalchemy import select, update

    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import composer_continuation_events_table as events
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch, recover_owner=change == "creator") as values:
        owner, _running, _primary, sdk, _original, _cause, dispatches = values
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        families = CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        assert families.restore_messages(((recipe, wire),))
        key = f"tool:{recipe.response.digest}:{recipe.tool_index}"
        with operation_store[0].begin() as conn:
            row = conn.execute(select(effects).where(effects.c.effect_key == key)).one()
            if change == "receipt":
                conn.execute(update(effects).where(effects.c.effect_key == key).values(receipt_key=row.receipt_key + ":foreign"))
            elif change == "creator":
                attempt = conn.execute(
                    select(attempts).where(
                        attempts.c.session_id == str(_running.claim.session_id),
                        attempts.c.operation_id == _running.claim.operation_id,
                        attempts.c.sequence == _running.claim.segment_sequence,
                        attempts.c.generation == _running.claim.ownership_generation,
                    )
                ).one()
                assert attempt.session_operation_id == _running.session_operation_context.fence.operation_id
                assert attempt.session_operation_epoch == _running.session_operation_context.fence.operation_epoch
                assert attempt.generation == row.creator_generation + 1 and attempt.sequence == row.creator_sequence
                conn.execute(update(effects).where(effects.c.effect_key == key).values(creator_generation=attempt.generation))
            elif change == "issue":
                descriptor = json.loads(row.action_json)
                descriptor["tool_call_id"] = "foreign_call"
                conn.execute(update(effects).where(effects.c.effect_key == key).values(action_json=canonical_json(descriptor)))
            else:
                event = conn.execute(select(events).where(events.c.sequence == runtime.source.selected.stack.cursor)).one()
                conn.execute(
                    update(events)
                    .where(events.c.sequence == event.sequence)
                    .values(payload_chunk_key=f"value:{recipe.response.codec}:{recipe.response.digest}")
                )
        with pytest.raises(AuditIntegrityError):
            families.restore_messages(((recipe, wire),))
        assert len(dispatches) == len(sdk) == 1


@pytest.mark.asyncio
async def test_private_feedback_registration_does_not_admit_rolled_back_completion(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import continuation_phase_transitions, tool_batch
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
    from elspeth.web.composer.protocol import ToolArgumentError

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        actual_append, attempted = continuation_phase_transitions.append_complete_snapshot, []
        original, rollback = (
            ToolArgumentError(argument="arguments", expected="an object", actual_type="missing"),
            RuntimeError("actual rollback"),
        )

        def append(conn, **kwargs):
            value = actual_append(conn, **kwargs)
            if '"transition":"tool_completed"' in kwargs["event_batch"][0].payload_json:
                attempted.append(value)
                raise rollback
            return value

        def execute(*args, **kwargs):
            raise original

        monkeypatch.setattr(continuation_phase_transitions, "append_complete_snapshot", append)
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, rollback) and len(attempted) == 1
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        families = CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        with pytest.raises(AuditIntegrityError):
            families.restore_messages(((recipe, wire),))
        assert runtime.materialization.capture_message(message) == (wire, recipe) and len(sdk) == 1
        assert owner.failure_custody.observation.feedback is original and not owner.failure_custody.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("none", "body", "keys", "parts", "unicode"))
async def test_actual_lazy_feedback_registration_defers_size_to_real_capture_owner(operation_store, tmp_path, monkeypatch, corruption):
    from elspeth.web.composer import continuation_prompt_producers, tool_batch
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
    from elspeth.web.composer.protocol import ComposerContextUnsupported, ToolArgumentError

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        actual_payload, actual_register = tool_batch._arg_error_payload, CurrentContinuationMessageProducers.register_tool_feedback_message
        original, stop = (
            ToolArgumentError(argument="arguments", expected="an object", actual_type="missing"),
            RuntimeError("after real checks"),
        )
        registrations, encodings = [], []

        def payload(error, name):
            result = dict(actual_payload(error, name))
            result["repair_instruction"] = "\n" * (JSON_MAX_TOTAL_UTF8_BYTES // 4)
            return result

        def wire(message):
            encodings.append(message)
            raise AssertionError("oversize source must not allocate an enclosing wire")

        def register(self, **kwargs):
            with monkeypatch.context() as patch:
                patch.setattr(continuation_prompt_producers, "_wire", wire)
                actual = actual_register(self, **kwargs)
            assert actual is kwargs["message"] and not encodings
            registrations.append(actual)
            self.retention_message_view(actual)
            transcript = owner.runtime.live.turn_evidence.transcript
            with pytest.raises(ComposerContextUnsupported):
                transcript.snapshot_for_continuation(
                    (*owner.runtime.live.llm_messages, actual), materialization=self, mandatory_retention=True
                )
            if corruption == "body":
                actual["content"] = actual["content"].encode().decode()
            elif corruption == "keys":
                value = actual.pop("role")
                actual["role"] = value
            elif corruption == "parts":
                from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY

                actual[PROMPT_GUARD_CONTENT_PARTS_KEY] = tuple(part for part in actual[PROMPT_GUARD_CONTENT_PARTS_KEY])
            elif corruption == "unicode":
                actual["content"] += "\ud800"
            with monkeypatch.context() as patch:
                patch.setattr(continuation_prompt_producers, "_wire", wire)
                if corruption == "none":
                    with pytest.raises(ComposerContextUnsupported):
                        self.capture_message(actual)
                else:
                    with pytest.raises(AuditIntegrityError):
                        self.capture_message(actual)
                    with pytest.raises(AuditIntegrityError):
                        self.retention_message_view(actual)
            assert not encodings
            raise stop

        def execute(*args, **kwargs):
            raise original

        monkeypatch.setattr(tool_batch, "_arg_error_payload", payload)
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_tool_feedback_message", register)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and _contains_identity(caught.value, stop)
        assert len(registrations) == len(sdk) == 1 and not owner.failure_custody.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ("feedback_first", "generic_first"))
async def test_actual_feedback_cross_registry_duplicate_refuses_either_order(operation_store, tmp_path, monkeypatch, order):
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        from elspeth.web.composer import tool_batch
        from elspeth.web.composer.protocol import ToolArgumentError

        actual_register = CurrentContinuationMessageProducers.register_tool_feedback_message
        stopped, observed = RuntimeError("after actual duplicate refusal"), []

        def register(self, **kwargs):
            message = kwargs["message"]
            if order == "generic_first":
                # Actual generic owner registers the same object; the second
                # family must not acquire a new provenance identity for it.
                from elspeth.web.composer.continuation_prompt_recipes import ToolArgumentsRecipe

                self._register(message, ToolArgumentsRecipe())
                with pytest.raises(AuditIntegrityError, match=r"twice|duplicat"):
                    actual_register(self, **kwargs)
            else:
                actual_register(self, **kwargs)
                from elspeth.web.composer.continuation_prompt_recipes import ToolArgumentsRecipe

                with pytest.raises(AuditIntegrityError, match=r"twice|duplicat"):
                    self._register(message, ToolArgumentsRecipe())
                with pytest.raises(AuditIntegrityError, match=r"twice|duplicat"):
                    actual_register(self, **kwargs)
            observed.append(message)
            raise stopped

        def execute(*args, **kwargs):
            raise ToolArgumentError(argument="arguments", expected="an object", actual_type="missing")

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_tool_feedback_message", register)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, stopped) and len(observed) == len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_oversize_feedback_keeps_credential_refusal_before_size_classification(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import continuation_prompt_producers, tool_batch
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
    from elspeth.web.composer.protocol import ToolArgumentError
    from elspeth.web.credential_guard import CredentialMaterialRefused

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        original = ToolArgumentError(argument="arguments", expected="an object", actual_type="missing")
        actual_payload, actual_register = tool_batch._arg_error_payload, CurrentContinuationMessageProducers.register_tool_feedback_message
        refused, encodings = [], []

        def payload(error, name):
            result = dict(actual_payload(error, name))
            result["repair_instruction"] = "\n" * (JSON_MAX_TOTAL_UTF8_BYTES // 4) + "Bearer " + "A" * 64
            return result

        def wire(message):
            encodings.append(message)
            raise AssertionError("credential rejection must precede feedback encoding")

        def register(self, **kwargs):
            with monkeypatch.context() as patch:
                patch.setattr(continuation_prompt_producers, "_wire", wire)
                with pytest.raises(CredentialMaterialRefused) as caught:
                    actual_register(self, **kwargs)
            refused.append(caught.value)
            raise caught.value

        def execute(*args, **kwargs):
            raise original

        monkeypatch.setattr(tool_batch, "_arg_error_payload", payload)
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_tool_feedback_message", register)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert len(refused) == 1 and _contains_identity(caught.value, refused[0]) and _contains_identity(caught.value, original)
        assert not encodings and len(sdk) == 1 and not owner.failure_custody.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "copy", "foreign_call", "double", "missing"))
async def test_actual_session_feedback_binds_only_its_original_outer_outcome_once(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import tools
    from elspeth.web.composer.continuation_prompt_producers import ContinuationToolMessagePublication
    from elspeth.web.composer.protocol import ToolArgumentError

    arguments = {"affected_node_id": "rate_node", "kind": "vague_term", "user_term": "cool", "llm_draft": "Use concise wording."}
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="request_interpretation_review", tool_arguments=arguments, bound_base=True
    ) as (owner, _running, sdk):
        original, cause = (
            ToolArgumentError(argument="llm_draft", expected="a string", actual_type="missing"),
            RuntimeError("actual argument cause"),
        )
        calls, bindings = [], []
        actual_bind = ContinuationToolMessagePublication.bind_completed_outcome

        async def handler(**kwargs):
            calls.append(kwargs)
            raise original from cause

        def bind(self, outcome):
            assert self.batch.tool_outcomes[self.call.index] is outcome and outcome.call is self.call.tool_call
            bindings.append(outcome)
            if fault == "missing":
                return
            if fault == "copy":
                copied = replace(outcome)
                assert copied == outcome and copied is not outcome
                return actual_bind(self, copied)
            if fault == "foreign_call":
                return actual_bind(self, replace(outcome, call=replace(outcome.call, id="foreign_call")))
            actual_bind(self, outcome)
            if fault == "double":
                actual_bind(self, outcome)

        monkeypatch.setitem(tools._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", handler)
        monkeypatch.setattr(ContinuationToolMessagePublication, "bind_completed_outcome", bind)
        if fault == "none":
            await _advance(owner)
        else:
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            if fault != "missing":
                assert _contains_identity(caught.value, original) and original.__cause__ is cause
        assert len(calls) == len(bindings) == len(sdk) == 1 and not owner.failure_custody.completed
        assert owner.failure_custody.observation.feedback is original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("source_copy", "message_copy", "request_copy", "foreign_cohort"))
async def test_actual_cold_feedback_refuses_copied_or_cross_cohort_sources(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
    from tests.unit.web.sessions.test_continuation_live_phases import stored_rows

    async with _issued_argument_feedback_case(operation_store, tmp_path, monkeypatch) as values:
        owner, _running, _primary, sdk, _original, _cause, dispatches = values
        runtime = owner.runtime

        async def restore():
            return await restore_continuation_runtime(
                service=owner.service,
                authority=owner.authority,
                selected=runtime.source.selected,
                inventory=runtime.materialization.inventory,
                required_work=owner.work,
                provider_owner=owner.provider_owner,
            )

        positive = await restore()
        assert any(item.get("tool_call_id") == "first_read" for item in positive.live.llm_messages)
        actual, reached = CurrentContinuationMessageFamilies._restore_cohort, []

        def cohort(self, requests):
            returned = actual(self, requests)
            if not returned.feedback:
                return returned
            reached.append(returned)
            request, source = returned.feedback[0]
            if change == "source_copy":
                return replace(returned, feedback=((request, replace(source)), *returned.feedback[1:]))
            if change == "message_copy":
                return replace(returned, messages=tuple(dict(item) if item is source.message else item for item in returned.messages))
            if change == "request_copy":
                return replace(returned, feedback=((request.model_copy(), source), *returned.feedback[1:]))
            foreign = actual(self, requests)
            assert foreign is not returned and foreign.feedback[0][1] is not source
            return replace(returned, feedback=foreign.feedback)

        before = stored_rows(operation_store[0])
        monkeypatch.setattr(CurrentContinuationMessageFamilies, "_restore_cohort", cohort)
        with pytest.raises(AuditIntegrityError, match="cohort/source"):
            await restore()
        assert len(reached) == 1 and stored_rows(operation_store[0]) == before and len(sdk) == len(dispatches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "construct", "append"))
async def test_real_issued_progress_cancellation_keeps_primary_and_cause(operation_store, tmp_path, monkeypatch, fault):
    import asyncio

    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.audit import BufferingRecorder

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        entered = asyncio.Event()
        cause, secondary = RuntimeError("original cancellation cause"), RuntimeError("actual cancellation audit failure")
        observed, records, dispatches = [], [], []
        actual_finish = tool_batch.finish_cancelled
        actual_record = BufferingRecorder.record_with_failure_observation

        async def progress(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                observed.append(error)
                raise error from cause

        def finish(audit, *, exc):
            assert exc is observed[0]
            if fault == "construct":
                raise secondary
            return actual_finish(audit, exc=exc)

        def record(self, value, observation):
            records.append(value)
            if fault == "append":
                raise secondary
            return actual_record(self, value, observation)

        def dispatch(*args, **kwargs):
            dispatches.append(args)
            raise AssertionError("cancelled issued call must not dispatch")

        monkeypatch.setattr(tool_batch, "emit_progress", progress)
        monkeypatch.setattr(tool_batch, "finish_cancelled", finish)
        monkeypatch.setattr(BufferingRecorder, "record_with_failure_observation", record)
        monkeypatch.setattr(tool_batch, "execute_tool", dispatch)
        task = asyncio.create_task(_advance(owner))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel("actual issued progress cancellation")
            with pytest.raises(BaseException) as caught:
                await task
            assert len(observed) == 1 and observed[0].__cause__ is cause
            assert _contains_identity(caught.value, observed[0]) and _contains_identity(caught.value, cause)
            if fault != "none":
                assert _contains_identity(caught.value, secondary)
            assert owner.failure_custody.original is observed[0]
            assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
            assert len(records) == (0 if fault == "construct" else 1)
            assert not dispatches and len(sdk) == 1
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("narrow", "integrity", "cancel"))
async def test_real_dispatch_singleton_preserves_explicit_cause(operation_store, tmp_path, monkeypatch, kind):
    import asyncio

    from elspeth.web.composer import tool_batch

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        cause = RuntimeError("original explicit dispatch cause")
        original = {
            "narrow": AssertionError("actual narrow failure"),
            "integrity": AuditIntegrityError("actual integrity failure"),
            "cancel": asyncio.CancelledError("actual dispatch cancellation"),
        }[kind]

        def execute(*args, **kwargs):
            raise original from cause

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and _contains_identity(caught.value, cause)
        assert original.__cause__ is cause
        assert owner.failure_custody.original is original and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ("none", "limit", "used", "remaining", "copy_outcome", "copy_invocation", "negative_limit", "bool_limit")
)
async def test_actual_context_refusal_has_portable_allowance_proof(operation_store, tmp_path, monkeypatch, change):
    from copy import copy

    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.protocol import ComposerContextUnsupported

    async with _advisor_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original = ComposerContextUnsupported("graph_coverage_unavailable")

        def refuse(self, value):
            raise original

        monkeypatch.setattr(AdvisorCheckpointOwner, "_require_complete_graph_summary", refuse)
        await _advance(owner)
        data = owner.runtime.graph.batch
        failure = data.batch.retained_failure
        source = failure.refusal_source
        assert source.allowance_limit is not None
        assert source.allowance_limit == data.batch.manual_advisor_allowance.limit
        cold = decode_coupled_batch_data(
            encode_coupled_batch_data(
                batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=owner.runtime.env_ref_names
            ),
            env_ref_names=owner.runtime.env_ref_names,
        )
        assert cold.batch.manual_advisor_allowance is None
        assert cold.batch.retained_failure.refusal_source.allowance_limit == source.allowance_limit
        actual = owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names)
        assert actual is not None and not actual.applied
        if change == "none":
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
            assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names).applied
        elif change in ("negative_limit", "bool_limit"):
            with pytest.raises(AuditIntegrityError):
                replace(source, allowance_limit=-1 if change == "negative_limit" else True)
        else:
            if change == "limit":
                source = replace(source, allowance_limit=source.allowance_limit + 1)
            elif change in ("used", "remaining"):
                response = dict(source.outcome.response)
                field = "budget_used" if change == "used" else "budget_remaining"
                response[field] += 1
                source = replace(source, outcome=replace(source.outcome, response=response))
            elif change == "copy_outcome":
                source = replace(source, outcome=copy(source.outcome))
            else:
                source = replace(source, invocation=replace(source.invocation))
            data.batch.retained_failure = replace(failure, refusal_source=source)
            with pytest.raises(AuditIntegrityError):
                if change == "copy_invocation":
                    from elspeth.web.composer.continuation_evidence_application_data import capture_joint_evidence_graph

                    capture_joint_evidence_graph(
                        owner=owner.runtime.live.turn_evidence,
                        recorder=owner.runtime.live.recorder,
                        drained=owner.runtime.drained,
                        data=data,
                        env_ref_names=owner.runtime.env_ref_names,
                    )
                else:
                    encode_coupled_batch_data(
                        batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=owner.runtime.env_ref_names
                    )
        assert len(sdk) == 1


class _FeedbackReadTiming:
    """Bounded test-only callable observations, containing no business DATA."""

    from typing import ClassVar

    capacity = 512
    roles: ClassVar[dict[str, int]] = {
        "current_inputs": 1,
        "public_cold": 2,
        "runtime_source": 3,
        "capture_initial": 4,
        "capture_final": 5,
        "original_evidence": 6,
        "selected_evidence": 7,
        "original_rows": 8,
        "renewal": 9,
    }
    types: ClassVar[dict[tuple[str, str], int]] = {
        ("builtins", "tuple"): 1,
        ("builtins", "dict"): 2,
        ("elspeth.web.composer.continuation_prompt_producers", "CurrentMessageProduction"): 3,
        ("elspeth.web.sessions.continuation_runtime", "ContinuationRuntimeSource"): 4,
        ("elspeth.web.composer.continuation_evidence", "AdmittedTurnEvidence"): 5,
        ("elspeth.web.composer.continuation_runtime", "RestoredContinuationRuntime"): 6,
        ("elspeth.contracts.session_operation", "SessionOperationContext"): 7,
        ("elspeth.contracts.errors", "AuditIntegrityError"): 100,
        ("elspeth.web.sessions.composer_operations", "ComposerOperationFenceLost"): 101,
        ("elspeth.web.coordination.contracts", "SessionOperationFenceLost"): 102,
        ("asyncio.exceptions", "CancelledError"): 103,
        ("builtins", "AssertionError"): 104,
        ("builtins", "RuntimeError"): 105,
        ("sqlalchemy.exc", "OperationalError"): 106,
        ("builtins", "ExceptionGroup"): 107,
        ("builtins", "BaseExceptionGroup"): 108,
    }

    def __init__(self):
        from collections import deque
        from threading import Lock

        self._lock = Lock()
        self._records = deque(maxlen=self.capacity)
        self._calls = 0
        self._events = 0

    def _event(self, call, phase, role, value=None):
        from threading import get_ident
        from time import monotonic_ns

        actual_type = type(value)
        code = 0 if phase == 0 else self.types.get((actual_type.__module__, actual_type.__qualname__), 0)
        self._events += 1
        self._records.append((call, phase, role, monotonic_ns(), get_ident(), code))

    def wrap(self, role, operation):
        if role not in self.roles:
            raise ValueError("unknown feedback timing role")
        code = self.roles[role]

        def invoke(*args, **kwargs):
            with self._lock:
                self._calls += 1
                call = self._calls
                self._event(call, 0, code)
            try:
                result = operation(*args, **kwargs)
            except BaseException as error:
                with self._lock:
                    self._event(call, 2, code, error)
                raise
            with self._lock:
                self._event(call, 1, code, result)
            return result

        return invoke

    def snapshot(self):
        with self._lock:
            return {
                "schema": 1,
                "capacity": self.capacity,
                "calls": self._calls,
                "events": self._events,
                "dropped": self._events - len(self._records),
                "records": tuple(self._records),
            }


def _calibrate_feedback_read_timing():
    """Control the observer without invoking any product callable or SQL."""
    from asyncio import CancelledError
    from threading import get_ident

    def require_once(attempts, actual, expected):
        assert attempts == [1] and actual is expected

    def require_pair(records, *, phase, result_code):
        assert len(records) == 2
        first, last = records
        assert first[0] == last[0] and first[1] == 0 and last[1] == phase
        assert first[2] == last[2] == _FeedbackReadTiming.roles["current_inputs"]
        assert first[3] <= last[3]
        assert first[4] == last[4] == get_ident()
        assert first[5] == 0 and last[5] == result_code

    observed, calls, value = _FeedbackReadTiming(), [], (object(),)

    def returned():
        calls.append(1)
        return value

    actual = observed.wrap("current_inputs", returned)()
    assert actual is value and calls == [1]
    require_once(calls, actual, value)
    for missing_or_double in ([], [1, 1]):
        with pytest.raises(AssertionError):
            require_once(missing_or_double, value, value)
    equal_copy = tuple(item for item in value)
    assert equal_copy == value and equal_copy is not value
    with pytest.raises(AssertionError):
        require_once([1], equal_copy, value)
    records = observed.snapshot()["records"]
    require_pair(records, phase=1, result_code=1)
    for invalid in (records[:1], (*records, records[-1])):
        with pytest.raises(AssertionError):
            require_pair(invalid, phase=1, result_code=1)
    with pytest.raises(ValueError, match="unknown feedback timing role"):
        observed.wrap("unknown", returned)
    assert calls == [1]
    cause = ValueError("private calibration cause")
    for original, code in ((RuntimeError("private calibration error"), 105), (CancelledError("private cancellation"), 103)):
        original.__cause__ = cause
        attempts = []

        def raised(original=original, attempts=attempts):
            attempts.append(1)
            raise original

        with pytest.raises(type(original)) as caught:
            observed.wrap("current_inputs", raised)()
        assert caught.value is original and original.__cause__ is cause and attempts == [1]
        require_pair(observed.snapshot()["records"][-2:], phase=2, result_code=code)
    unknown = object()
    assert observed.wrap("current_inputs", lambda: unknown)() is unknown
    require_pair(observed.snapshot()["records"][-2:], phase=1, result_code=0)
    bounded = _FeedbackReadTiming()
    repeat = bounded.wrap("current_inputs", lambda: value)
    for _ in range(bounded.capacity // 2 + 1):
        assert repeat() is value
    snapshot = bounded.snapshot()
    assert len(snapshot["records"]) == bounded.capacity and snapshot["events"] == bounded.capacity + 2 and snapshot["dropped"] == 2


@asynccontextmanager
async def _feedback_timing_case(store, monkeypatch, record_property):
    import json

    from elspeth.web.composer import continuation_prompt_producers
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead

    _calibrate_feedback_read_timing()
    timing = _FeedbackReadTiming()
    with monkeypatch.context() as observed:
        observed.setattr(
            continuation_prompt_producers,
            "_produce_current_messages",
            timing.wrap("current_inputs", continuation_prompt_producers._produce_current_messages),
        )
        observed.setattr(store[2], "read_continuation_runtime", timing.wrap("runtime_source", store[2].read_continuation_runtime))
        observed.setattr(ContinuationFinishRead, "read_runtime", timing.wrap("runtime_source", ContinuationFinishRead.read_runtime))
        observed.setattr(store[1], "renew", timing.wrap("renewal", store[1].renew))
        try:
            yield timing
        finally:
            # This outer context exits after the real case and lease owner have
            # unwound. No artifact publication occurs inside a SQL worker/lock.
            record_property("p4_feedback_read_timing", json.dumps(timing.snapshot(), separators=(",", ":")))


def _feedback_original_rows(conn, running):
    """Fingerprint original rows in memory; never export their private bytes."""
    from hashlib import sha256

    from sqlalchemy import select

    from elspeth.web.sessions.models import (
        composer_continuation_checkpoints_table,
        composer_continuation_chunks_table,
        composer_continuation_effects_table,
        composer_continuation_events_table,
    )

    def digest(values):
        return sha256(repr(tuple(values)).encode("utf-8")).hexdigest()

    assert digest(("row", b"original")) == digest(("row", b"original"))
    assert digest(("row", b"original")) != digest(("row", b"altered!"))
    result = {}
    for name, table in (
        ("checkpoints", composer_continuation_checkpoints_table),
        ("chunks", composer_continuation_chunks_table),
        ("effects", composer_continuation_effects_table),
        ("events", composer_continuation_events_table),
    ):
        rows = conn.execute(
            select(table).where(table.c.session_id == str(running.claim.session_id), table.c.operation_id == running.claim.operation_id)
        ).all()
        keys = tuple(column.name for column in table.primary_key.columns)
        result[name] = {tuple(row._mapping[key] for key in keys): digest(row) for row in rows}
        assert result[name], f"actual original {name} proof was empty"
    return result


def _assert_feedback_original_rows(conn, running, original):
    current = _feedback_original_rows(conn, running)
    for name, rows in original.items():
        assert {key: current[name][key] for key in rows} == rows, f"original completed {name} changed"
    # A later checkpoint may add snapshot/event chunks, never another issue or
    # result. Preserve all original effect/receipt rows, including their set.
    assert current["effects"] == original["effects"]
    return current


def _append_feedback_checkpoint(conn, running, selected, *, stack=None, chunks=()):
    """Use the real append/CAS owner for a disposable later fixture cut."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    from elspeth.web.composer.continuation_selection import SelectedContinuationState
    from elspeth.web.sessions.continuation import (
        ContinuationEvent,
        _require_current_continuation_running,
        append_complete_snapshot,
        put_chunk,
    )
    from elspeth.web.sessions.continuation_selection import load_private_chunks

    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    for chunk in chunks:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"value:{chunk.reference.codec}:{chunk.reference.digest}",
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    stored = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
        snapshot_json=encode_continuation_snapshot(selected.stack if stack is None else stack),
        event_batch=(ContinuationEvent("checkpoint", canonical_json({"owner": "later_feedback_source_fixture"})),),
        handoff=False,
    )
    root = decode_continuation_snapshot(stored.payload_json)
    return SelectedContinuationState(
        stored.checkpoint_id, root, load_private_chunks(conn, session_id=sid, operation_id=operation, stack=root)
    )


def _changed_feedback_checkpoint(conn, running, selected, inventory, change):
    import json

    from elspeth.contracts.hashing import stable_hash
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_application_graph import encode_application_graph
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.composer.continuation_evidence import admit_turn_evidence, encode_turn_evidence
    from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
    from elspeth.web.composer.continuation_message_family_recipes import ToolFeedbackMessageRecipe
    from elspeth.web.composer.continuation_message_shelf import encode_message_shelf, encode_transcript_object_json

    admitted = read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
    snapshot, graph = admitted.snapshot, admitted.graph
    assert graph is not None and snapshot.application_entry is None
    matches = tuple(
        (index, recipe) for index, recipe in enumerate(snapshot.transcript.producer_recipes) if type(recipe) is ToolFeedbackMessageRecipe
    )
    assert len(matches) == 1
    index, recipe = matches[0]
    call = graph.selected.calls[recipe.tool_index]
    outcome = graph.selected.batch.tool_outcomes[recipe.tool_index]
    authoring = graph.selected.batch.private_authorings_by_call[call.id]
    (invocation,) = tuple(item for item in graph.recorder.invocations if item.private_authoring is authoring)
    assert recipe.source == "arg_error" and outcome.response is None and outcome.call is call
    payload = json.loads(invocation.result_canonical)
    assert "validation_errors" in payload and "repair_instruction" in payload
    message = json.loads(snapshot.transcript.messages_json[index])
    assert json.loads(message["content"]) == payload and message["content"] != invocation.result_canonical
    recorder = graph.recorder
    if change == "rich_payload":
        altered = {**payload, "repair_instruction": "A different repair instruction in the later selected source."}
        changed_invocation = replace(invocation, result_canonical=canonical_json(altered), result_hash=stable_hash(altered))
        assert changed_invocation.result_canonical != invocation.result_canonical
        assert changed_invocation.result_hash != invocation.result_hash
        recorder = replace(
            recorder,
            invocations=tuple(changed_invocation if item is invocation else item for item in recorder.invocations),
        )
        message["content"] = json.dumps(altered)
        changed_graph = replace(graph, recorder=recorder)
        graph_json = encode_application_graph(changed_graph, env_ref_names=inventory.names)
    else:
        assert change in ("body_order", "body_whitespace")
        message["content"] = (
            json.dumps(dict(reversed(tuple(payload.items()))))
            if change == "body_order"
            else json.dumps(payload, ensure_ascii=False, indent=2)
        )
        assert message["content"] != json.loads(snapshot.transcript.messages_json[index])["content"]
        assert canonical_json(json.loads(message["content"])) == invocation.result_canonical
        # These opponents change only the ordered shelf body. The actual
        # invocation, canonical result/hash and complete joint graph stay exact.
        graph_json = snapshot.joint_graph_json
    texts = list(snapshot.transcript.messages_json)
    texts[index] = encode_transcript_object_json(message)
    # Current messages and explicitly observed events are interned separately.
    # Actual issued feedback can be shelved before transcript observation;
    # alter only a corresponding event that genuinely already exists.
    observed = []
    for entry in snapshot.transcript.observed:
        if entry.message_index == index:
            event = json.loads(entry.event_json)
            assert event["role"] == "tool" and event["tool_call_id"] == call.id
            assert event["content"] == json.loads(snapshot.transcript.messages_json[index])["content"]
            event["content"] = message["content"]
            entry = entry.model_copy(update={"event_json": encode_transcript_object_json(event)})
        observed.append(entry)
    assert tuple((entry.message_index, entry.pending_adoption) for entry in observed) == tuple(
        (entry.message_index, entry.pending_adoption) for entry in snapshot.transcript.observed
    )
    assert all(
        actual is original
        for original, actual in zip(snapshot.transcript.observed, observed, strict=True)
        if original.message_index != index
    )
    shelf = snapshot.transcript.model_copy(update={"messages_json": tuple(texts), "observed": tuple(observed)})
    audit = encode_recorder_snapshot(recorder)
    updated = snapshot.model_copy(update={"transcript": shelf, "recorder": audit.reference, "joint_graph_json": graph_json})
    evidence = encode_turn_evidence(
        admit_turn_evidence(updated, env_ref_names=inventory.names, source_chunks=(audit, *admitted.sources.chunks)),
        env_ref_names=inventory.names,
    )
    transcript = encode_message_shelf(shelf)
    messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes}),
        env_ref_names=inventory.names,
    )
    (frame,) = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    changed_frame = frame.model_copy(
        update={
            "evidence": evidence.reference,
            "audit_buffer": audit.reference,
            "transcript": transcript.reference,
            "messages": messages.reference,
        }
    )
    stack = selected.stack.model_copy(update={"frames": tuple(changed_frame if item is frame else item for item in selected.stack.frames)})
    return _append_feedback_checkpoint(conn, running, selected, stack=stack, chunks=(evidence, audit, transcript, messages))


async def _read_feedback_path(owner, selected, inventory, facet, recipe, text, reader, *, timing):
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
    from elspeth.web.composer.continuation_message_family_builders import family_message_wire
    from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime, _registered_read, restore_continuation_runtime

    if reader == "current":
        production = await load_current_message_producers(
            service=owner.service,
            authority=owner.authority,
            selected=selected,
            inventory=inventory,
            required_work=owner.work,
            finish_read=facet,
        )
        inputs = production.producers.inputs
        assert (recipe, text) in tuple(
            zip(inputs.evidence.transcript.producer_recipes, inputs.evidence.transcript.messages_json, strict=True)
        )
    elif reader == "public_cold":
        family = CurrentContinuationMessageFamilies(owner.service, owner.authority, selected, inventory, facet)
        (message,) = await _registered_read(
            owner.work, tuple, timing.wrap("public_cold", lambda: family.restore_messages(((recipe, text),)))
        )
        assert family_message_wire(message) == text
    else:
        assert reader == "runtime_cold"
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=selected,
            inventory=inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
            finish_read=facet,
        )
        (message,) = tuple(item for item in restored.live.llm_messages if item.get("tool_call_id") == recipe.tool_call_id)

        def capture():
            assert family_message_wire(message) == text and restored.materialization.capture_message(message) == (text, recipe)
            return restored

        await _registered_read(owner.work, RestoredContinuationRuntime, timing.wrap("capture_final", capture))


@asynccontextmanager
async def _later_feedback_source_case(store, tmp_path, monkeypatch, *, timing, change=None, stop=False):
    import json

    from elspeth.contracts.hashing import stable_hash
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
    from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
    from elspeth.web.composer.continuation_evidence_restore import read_turn_evidence_on_connection
    from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
    from elspeth.web.composer.continuation_message_family_builders import (
        family_message_wire,
        rebuild_tool_feedback_message,
        retained_family_wire,
    )
    from elspeth.web.composer.continuation_runtime import _registered_read
    from elspeth.web.composer.prompt_guard_rendering import json_content_parts
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
    from elspeth.web.sessions.locking import locked_session_transaction

    async with _issued_argument_feedback_case(store, tmp_path, monkeypatch) as values:
        owner, running, _primary, sdk, original, cause, dispatches = values
        runtime, completed = owner.runtime, owner.runtime.source.selected
        (warm,) = tuple(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        original_text, recipe = await _registered_read(
            owner.work, tuple, timing.wrap("capture_initial", lambda: runtime.materialization.capture_message(warm))
        )
        original_body = warm["content"]
        engine, sid = owner.authority._engine, str(running.claim.session_id)

        async def inventory_for(selected):
            return await materialize_continuation_env_inventory(
                advisor=owner.service._advisor_checkpoint, authority=owner.authority, selected=selected, required_work=owner.work
            )

        inventory = await inventory_for(completed)
        # First prove the actual committed issued result, including its original
        # event, receipt, recorder, rich payload and original ordered shelf.
        await _read_feedback_path(owner, completed, inventory, None, recipe, original_text, "current", timing=timing)

        def original_evidence():
            with locked_session_transaction(engine, sid) as conn:
                original_admitted = read_turn_evidence_on_connection(conn, running, selected=completed, inventory=inventory)
                original_rows = _feedback_original_rows(conn, running)
                assert (sid, running.claim.operation_id, completed.checkpoint_id) in original_rows["checkpoints"]
                assert (sid, running.claim.operation_id, f"tool:{recipe.response.digest}:{recipe.tool_index}") in original_rows["effects"]
                assert (sid, running.claim.operation_id, completed.stack.cursor) in original_rows["events"]
                (original_frame,) = tuple(frame for frame in completed.stack.frames if type(frame) is ComposeFrame)
                for ref in (original_frame.evidence, original_frame.audit_buffer, original_frame.transcript, original_frame.messages):
                    assert (sid, running.claim.operation_id, f"value:{ref.codec}:{ref.digest}") in original_rows["chunks"]
                assert (sid, running.claim.operation_id, f"snapshot:{completed.checkpoint_id}") in original_rows["chunks"]
                return original_admitted, original_rows

        original_admitted, original_rows = await _registered_read(owner.work, tuple, timing.wrap("original_evidence", original_evidence))
        # This is still the actual fixture WRITE under its original canonical
        # owner, never submitted or classified as CONTINUATION_READ_SQL.
        with locked_session_transaction(engine, sid) as conn:
            selected = _append_feedback_checkpoint(conn, running, completed)
        assert selected.checkpoint_id != completed.checkpoint_id and selected.stack.cursor == completed.stack.cursor + 1
        assert selected.stack.model_copy(update={"cursor": completed.stack.cursor}) == completed.stack
        inventory = await inventory_for(selected)
        # This valid later checkpoint is a positive BEFORE either opponent. It
        # exercises the same completed-history join and all public cold paths.
        for reader in ("current", "public_cold", "runtime_cold"):
            await _read_feedback_path(owner, selected, inventory, None, recipe, original_text, reader, timing=timing)
        if change is not None:
            with locked_session_transaction(engine, sid) as conn:
                selected = _changed_feedback_checkpoint(conn, running, selected, inventory, change)
            inventory = await inventory_for(selected)
            assert selected.stack.model_copy(update={"cursor": completed.stack.cursor, "frames": completed.stack.frames}) == completed.stack
        facet = None
        if stop:
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            # Inventory is actual SQL/name DATA from immediately before Stop,
            # with the same head. The nominal facet now re-proves the current
            # physical owner, durable Stop, CAS and actual completed prefix.
            facet = ContinuationFinishRead(running, selected, inventory)
        try:

            def selected_evidence():
                with locked_session_transaction(engine, sid) as conn:
                    admitted = (
                        read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
                        if facet is None
                        else facet.read_evidence(conn)
                    )
                    _assert_feedback_original_rows(conn, running, original_rows)
                    return admitted

            admitted = await _registered_read(owner.work, AdmittedTurnEvidence, timing.wrap("selected_evidence", selected_evidence))
            old_graph, graph = original_admitted.graph, admitted.graph
            assert old_graph is not None and graph is not None
            transcript = admitted.snapshot.transcript
            (text,) = tuple(
                text for saved, text in zip(transcript.producer_recipes, transcript.messages_json, strict=True) if saved == recipe
            )
            call = graph.selected.calls[recipe.tool_index]
            outcome = graph.selected.batch.tool_outcomes[recipe.tool_index]
            authoring = graph.selected.batch.private_authorings_by_call[call.id]
            (invocation,) = tuple(item for item in graph.recorder.invocations if item.private_authoring is authoring)
            old_call = old_graph.selected.calls[recipe.tool_index]
            old_authoring = old_graph.selected.batch.private_authorings_by_call[old_call.id]
            (old_invocation,) = tuple(item for item in old_graph.recorder.invocations if item.private_authoring is old_authoring)
            assert graph.selected.calls == old_graph.selected.calls
            assert graph.selected.batch.tool_outcomes == old_graph.selected.batch.tool_outcomes and outcome.response is None
            assert graph.selected.batch.private_authorings_by_call == old_graph.selected.batch.private_authorings_by_call
            assert outcome.call is call and invocation.private_authoring is authoring
            payload = json.loads(invocation.result_canonical)
            assert canonical_json(payload) == invocation.result_canonical and stable_hash(payload) == invocation.result_hash
            for entry in transcript.observed:
                if entry.message_index == transcript.messages_json.index(text):
                    assert json.loads(entry.event_json)["content"] == json.loads(text)["content"]
            if change == "body_whitespace":
                # This is the counterfactual's local partition proof, not a
                # producer registration or current-source admission. Match the
                # exact serializer used by the deliberately altered shelf body.
                rebuilt = retained_family_wire(text)
                rebuilt[PROMPT_GUARD_CONTENT_PARTS_KEY] = json_content_parts(rebuilt["content"], ensure_ascii=False, indent=2)
                assert "".join(part.text for part in rebuilt[PROMPT_GUARD_CONTENT_PARTS_KEY]) == rebuilt["content"]
            else:
                rebuilt = rebuild_tool_feedback_message(
                    call=call, invocation=invocation, authoring=authoring, outcome=outcome, recipe=recipe, selected_message_json=text
                )
            assert family_message_wire(rebuilt) == text
            # Prove selected DATA/hash/alias coherence. Matching local parts do
            # not make the indented counterfactual a supported producer format;
            # that branch expects the actual reader's earlier format refusal.
            if change == "rich_payload":
                assert invocation == replace(
                    old_invocation, result_canonical=invocation.result_canonical, result_hash=invocation.result_hash
                )
                assert payload["repair_instruction"] != json.loads(old_invocation.result_canonical)["repair_instruction"]
                assert (
                    invocation.result_canonical != old_invocation.result_canonical and invocation.result_hash != old_invocation.result_hash
                )
                assert admitted.recorder_chunk.canonical != original_admitted.recorder_chunk.canonical
            else:
                assert invocation == old_invocation and admitted.recorder_chunk.canonical == original_admitted.recorder_chunk.canonical
                assert admitted.snapshot.joint_graph_json == original_admitted.snapshot.joint_graph_json
                assert canonical_json(json.loads(rebuilt["content"])) == old_invocation.result_canonical
            if change is None:
                assert text == original_text
            else:
                assert text != original_text
            yield owner, running, selected, inventory, facet, recipe, text, completed.checkpoint_id
        finally:

            def original_rows_unchanged():
                with locked_session_transaction(engine, sid) as conn:
                    return _assert_feedback_original_rows(conn, running, original_rows)

            await _registered_read(owner.work, dict, timing.wrap("original_rows", original_rows_unchanged))
            assert runtime is owner.runtime and warm["content"] is original_body
            assert len(dispatches) == len(sdk) == 1 and original.__cause__ is cause
            assert owner.failure_custody.original is None and not owner.failure_custody.completed


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True))
async def test_actual_feedback_unchanged_later_checkpoint_admits_current_and_public_cold_source(
    operation_store, tmp_path, monkeypatch, stop, record_property
):
    async with (
        _feedback_timing_case(operation_store, monkeypatch, record_property) as timing,
        _later_feedback_source_case(operation_store, tmp_path, monkeypatch, timing=timing, stop=stop) as values,
    ):
        owner, _running, selected, inventory, facet, recipe, text, _completed_id = values
        for reader in ("current", "public_cold", "runtime_cold"):
            await _read_feedback_path(owner, selected, inventory, facet, recipe, text, reader, timing=timing)


async def _assert_original_feedback_rejection(
    owner, selected, inventory, facet, recipe, text, completed_id, reader, monkeypatch, *, reason, timing, expected_partition_error=None
):
    from elspeth.web.composer import continuation_evidence_restore

    actual = continuation_evidence_restore.read_selected_turn_evidence_graph_on_connection
    observed = []

    def read(conn, running, *, selected, names):
        result = actual(conn, running, selected=selected, names=names)
        observed.append(selected.checkpoint_id)
        return result

    # Observe the actual already-called reader, preserving its returned object
    # and all SQL/integrity errors. No producer/admission is mocked or blessed.
    # These reach checks establish evidence admission only, not passage through
    # the later original-completion comparison or the producer-format guard.
    monkeypatch.setattr(continuation_evidence_restore, "read_selected_turn_evidence_graph_on_connection", read)
    try:
        expected = f"feedback {reason} differs from its original completion"
        if expected_partition_error is not None:
            assert reason == "producer wire format"
            assert expected_partition_error == "Prompt producer JSON partition changed its exact wire serialization"
            expected = r"\APrompt producer JSON partition changed its exact wire serialization\Z"
        with pytest.raises(AuditIntegrityError, match=expected):
            await _read_feedback_path(owner, selected, inventory, facet, recipe, text, reader, timing=timing)
    finally:
        assert completed_id in observed, "opposition did not reach actual original completion evidence admission"
        assert selected.checkpoint_id in observed, "opposition did not admit its actual later selected evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True))
@pytest.mark.parametrize("reader", ("current", "public_cold", "runtime_cold"))
async def test_actual_feedback_rejects_later_coherent_rich_payload_and_hash(
    operation_store, tmp_path, monkeypatch, stop, reader, record_property
):
    async with (
        _feedback_timing_case(operation_store, monkeypatch, record_property) as timing,
        _later_feedback_source_case(operation_store, tmp_path, monkeypatch, timing=timing, change="rich_payload", stop=stop) as values,
    ):
        owner, _running, selected, inventory, facet, recipe, text, completed_id = values
        await _assert_original_feedback_rejection(
            owner, selected, inventory, facet, recipe, text, completed_id, reader, monkeypatch, reason="invocation", timing=timing
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True))
@pytest.mark.parametrize("reader", ("current", "public_cold", "runtime_cold"))
@pytest.mark.parametrize("change", ("body_order", "body_whitespace"))
async def test_actual_feedback_rejects_later_semantically_equal_but_different_ordered_body(
    operation_store, tmp_path, monkeypatch, stop, reader, change, record_property
):
    async with (
        _feedback_timing_case(operation_store, monkeypatch, record_property) as timing,
        _later_feedback_source_case(operation_store, tmp_path, monkeypatch, timing=timing, change=change, stop=stop) as values,
    ):
        owner, _running, selected, inventory, facet, recipe, text, completed_id = values
        if change == "body_whitespace":
            # Unsupported producer formatting is rejected before F1's ordered
            # original-wire comparison; both SQL evidence admissions still run.
            await _assert_original_feedback_rejection(
                owner,
                selected,
                inventory,
                facet,
                recipe,
                text,
                completed_id,
                reader,
                monkeypatch,
                reason="producer wire format",
                timing=timing,
                expected_partition_error="Prompt producer JSON partition changed its exact wire serialization",
            )
        else:
            await _assert_original_feedback_rejection(
                owner, selected, inventory, facet, recipe, text, completed_id, reader, monkeypatch, reason="ordered wire", timing=timing
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("returned", "raised", "cancelled", "no_submit"))
async def test_terminal_failure_validator_requires_exact_actual_handoff(coordinator, monkeypatch, mode):
    import asyncio
    import threading

    from elspeth.web import async_workers
    from elspeth.web.async_workers import run_required_sql_finish_once
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
    from elspeth.web.required_work import RequiredWorkSource, RequiredWorkTicket

    first = coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL)
    original, value = RuntimeError("actual terminal physical error"), object()
    entered, release = threading.Event(), threading.Event()

    def physical():
        entered.set()
        if mode == "cancelled" and not release.wait(5):
            raise TimeoutError("terminal handoff control not released")
        if mode in ("raised", "no_submit"):
            raise original
        return value

    if mode == "no_submit":
        monkeypatch.setattr(async_workers, "_get_shared_executor", physical)
    task = asyncio.create_task(run_required_sql_finish_once(first, physical))
    try:
        if mode == "cancelled":
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel("actual terminal caller cancellation")
            await asyncio.sleep(0)
            release.set()
        actual = await task
        assert coordinator.validate_terminal_failure_sql_outcome(ticket=first, actual_outcome=actual) is (mode == "no_submit")
        if mode in ("raised", "no_submit"):
            assert type(actual) is RequiredSQLRaised and actual.error is original
            counterfeit = RequiredSQLRaised(original, actual.deferred_cancellations)
        else:
            assert type(actual) is RequiredSQLReturned and actual.value is value
            counterfeit = RequiredSQLReturned(value, actual.deferred_cancellations)
        if mode == "cancelled":
            assert actual.deferred_cancellations[0].args == ("actual terminal caller cancellation",)
        with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
            coordinator.validate_terminal_failure_sql_outcome(ticket=first, actual_outcome=counterfeit)
        copy = RequiredWorkTicket(first.key, coordinator=coordinator)
        with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
            coordinator.validate_terminal_failure_sql_outcome(ticket=copy, actual_outcome=actual)
        second = coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=1)
        next_actual = await run_required_sql_finish_once(second, physical)
        coordinator.validate_terminal_failure_sql_outcome(ticket=second, actual_outcome=next_actual)
        with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
            coordinator.validate_terminal_failure_sql_outcome(ticket=second, actual_outcome=actual)
        coordinator.assert_completed()
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_default_v1_dispatch_preserves_original_error_and_single_record():
    from elspeth.web.composer.audit import BufferingRecorder, begin_dispatch, dispatch_with_audit

    recorder = BufferingRecorder()
    audit = begin_dispatch("v1_origin", "get_pipeline_state", {}, version_before=0, actor="composer-web:user-alice")
    original = RuntimeError("actual default dispatch origin")
    dispatched = []

    async def physical():
        dispatched.append(audit)
        raise original

    with pytest.raises(RuntimeError) as caught:
        await dispatch_with_audit(
            recorder=recorder,
            audit=audit,
            do_dispatch=physical,
            version_after_provider=lambda result: 0,
            arg_error_payload_factory=lambda error: {},
        )
    assert caught.value is original and dispatched == [audit]
    assert len(recorder.invocations) == 1 and recorder.invocations[0].status is ComposerToolStatus.PLUGIN_CRASH
    assert recorder.invocations[0].error_class == "RuntimeError" and recorder.invocations[0].private_failure is not None


def test_terminal_failure_validator_refuses_unjoined_unknown(coordinator):
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised
    from elspeth.web.required_work import RequiredWorkIncomplete, RequiredWorkSource

    ticket = coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL)
    error = RuntimeError("actual unknown-submission marker control")
    ticket.observe_submission_unknown(error)
    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_terminal_failure_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLRaised(error, ()))
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.assert_completed()


@asynccontextmanager
async def _advisor_case(
    store,
    tmp_path,
    monkeypatch,
    *,
    block=False,
    tool_name="request_advisor_hint",
    tool_arguments=None,
    bound_base=False,
    required_controls=False,
    advisor_prompt_tokens=None,
    following_call=False,
):
    """Same complete initialization and real SDK seam, with an Advisor call."""
    import json

    import httpx
    import litellm
    from litellm import ModelResponse

    from elspeth.web.composer import prompt_guard_clients
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_primary import run_continuation_primary
    from elspeth.web.composer.continuation_selection import load_selected_continuation
    from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
    from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
    from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
    from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
    from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _checks, _required_guard_settings
    from tests.unit.web.sessions import test_continuation_composition_initialization, test_continuation_prompt_producers
    from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

    settings, _guard_requests = _required_guard_settings(tmp_path, monkeypatch)
    if advisor_prompt_tokens is not None:
        settings = settings.model_copy(update={"composer_advisor_max_prompt_tokens": advisor_prompt_tokens})
    advisor_ready = False

    def client(configuration):
        def serve(request):
            guard_requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "userPromptAnalysis": {"attackDetected": block and advisor_ready},
                    "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
                },
            )

        return AzurePromptGuardClient(configuration, http_transport=httpx.MockTransport(serve))

    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", client)
    monkeypatch.setattr(test_continuation_prompt_producers, "WebSettings", lambda **kwargs: settings.model_copy(update=kwargs))
    if required_controls:
        from elspeth.web.composer.application_policy import PluginPolicyContextFactory
        from elspeth.web.plugin_policy.availability import build_plugin_snapshot
        from tests.unit.web.composer.test_planner_authoring_aids import _guardrail_profile_view
        from tests.unit.web.composer.test_required_control_autowire import _INLINE_CONTENT
        from tests.unit.web.sessions import test_continuation_initialization

        actual_request = test_continuation_initialization.SendMessageRequest

        def request(**kwargs):
            return actual_request(**dict(kwargs, content=kwargs["content"] + "\n\n" + _INLINE_CONTENT))

        monkeypatch.setattr(test_continuation_initialization, "SendMessageRequest", request)

        view, _prior_snapshot = _guardrail_profile_view(tmp_path)
        profiles = view._profiles
        assert profiles is not None

        class Inventory:
            def has_server_ref(self, name):
                return name == "OPENROUTER_API_KEY"

            def has_user_ref(self, principal, name):
                return False

            def has_ref(self, principal, name):
                return name == "OPENROUTER_API_KEY"

            def server_generation(self, name):
                return "gen-1" if name == "OPENROUTER_API_KEY" else None

            def user_generation(self, principal, name):
                return None

        def snapshot(actor):
            assert actor == "alice"
            return build_plugin_snapshot(
                policy=profiles._policy,
                catalog=view._full,
                profiles=profiles,
                principal_scope="local:" + actor,
                secret_inventory=Inventory(),
                generation_key=b"authoring-aids-key",
            )

        actual_current = test_continuation_prompt_producers.current_service

        def current(*args, **kwargs):
            service, actors = actual_current(*args, **kwargs)
            service._policy_context = PluginPolicyContextFactory(view._full, snapshot, profiles, trained_operator_mode=False)
            return service, actors

        monkeypatch.setattr(test_continuation_prompt_producers, "current_service", current)
    if bound_base:
        actual_ready = test_continuation_composition_initialization.ready_case

        async def ready(*args, **kwargs):
            return await actual_ready(*args, **kwargs, bound_base=True)

        monkeypatch.setattr(test_continuation_composition_initialization, "ready_case", ready)
    runtime, work, guard_requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    monkeypatch.setattr(prompt_guard_clients, "build_prompt_guard_client", client)
    running = work.running
    assert running is not None
    service, provider = runtime.materialization.service, runtime.bindings.provider_owner
    lease = await ContinuationLeaseAdoption(
        store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()
    owner = ContinuationLivePhases(service, store[2], work, provider, lease)
    owner.bind_initial(runtime)
    sdk = []

    async def completion(**kwargs):
        if sdk:
            checks = _checks(store[0])
            assert checks[-1]["role"] == "advisor" and checks[-1]["disposition"] == "allow"
            assert checks[-1]["operation_id"] == running.session_operation_context.fence.operation_id
            assert checks[-1]["operation_epoch"] == running.session_operation_context.fence.operation_epoch
        sdk.append(kwargs)
        if len(sdk) > 1:
            return ModelResponse(
                model="test/model",
                choices=[{"message": {"role": "assistant", "content": "Actual Advisor guidance."}}],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )
        _assert_required_guard_admitted(
            store[0], running.session_operation_context, guard_requests, role="composer", user_prompt=runtime.bindings.message
        )
        arguments = {
            "trigger": "proactive_security_safety",
            "problem_summary": "Inspect the actual current request.",
            "recent_errors": [],
            "attempted_actions": [],
        }
        if tool_arguments is not None:
            arguments = tool_arguments
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Requesting an ordinary Advisor review.",
                        "tool_calls": [
                            {
                                "id": "actual_advisor",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": canonical_json(encode_semantic_arguments(tool_name, service._planner_dialect, arguments)),
                                },
                            }
                        ]
                        + (
                            [
                                {
                                    "id": "after_advisor",
                                    "type": "function",
                                    "function": {
                                        "name": "get_pipeline_state",
                                        "arguments": canonical_json(
                                            encode_semantic_arguments("get_pipeline_state", service._planner_dialect, {})
                                        ),
                                    },
                                }
                            ]
                            if following_call
                            else []
                        ),
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    try:
        _messages, _tools, delivery, _input = service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            primary = await run_continuation_primary(
                service=service,
                authority=store[2],
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=provider,
                timeout=5,
            )
            if delivery is not None:
                runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
            owner.accept_primary(primary)
            advisor_ready = True
            selected = await load_selected_continuation(authority=store[2], running=running, selected=primary.snapshot, required_work=work)
            await owner.advance(selected)
            yield owner, running, sdk
    finally:
        await lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ("context", "cost", "guard", "crash", "timeout", "grouped"))
async def test_actual_advisor_failure_keeps_successful_refusal_separate_from_crash(operation_store, tmp_path, monkeypatch, branch):
    import litellm

    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.protocol import ComposerContextUnsupported
    from elspeth.web.composer.provider_cost import CommonProviderCostRefused
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

    async with _advisor_case(operation_store, tmp_path, monkeypatch, block=branch == "guard") as (owner, running, sdk):
        original = (
            ComposerContextUnsupported("graph_coverage_unavailable") if branch == "context" else RuntimeError("actual Advisor SDK origin")
        )
        cost = []
        actual_cost = ProviderCallCustody.require_cost_output

        def summary(self, value):
            raise original

        async def fail_sdk(**kwargs):
            sdk.append(kwargs)
            if branch == "timeout":
                raise TimeoutError("actual Advisor timeout")
            raise original

        def policy(self):
            actual_cost(self)
            if owner.failure_custody is not None and self is owner.failure_custody.observation.provider_custody:
                calls = owner.runtime.live.recorder.llm_calls
                assert calls and calls[-1].call_id is not None
                correlation = calls[-1].call_id
                error = CommonProviderCostRefused("COST_UNAVAILABLE", call_id=correlation)
                cost.append(error)
                raise error

        if branch == "context":
            monkeypatch.setattr(AdvisorCheckpointOwner, "_require_complete_graph_summary", summary)
        elif branch in ("cost", "grouped"):
            monkeypatch.setattr(ProviderCallCustody, "require_cost_output", policy)
        if branch in ("crash", "timeout", "grouped"):
            monkeypatch.setattr(litellm, "acompletion", fail_sdk)
        if branch == "grouped":
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original) and any(_contains_identity(caught.value, error) for error in cost)
            assert owner.runtime.graph.batch.batch.retained_failure is None
            assert owner.failure_custody.original is original and not owner.failure_custody.completed
            return
        await _advance(owner)
        data, native = owner.runtime.graph.batch, owner.failure_custody
        assert data.batch.plugin_crash is data.batch.plugin_crash_cause is data.batch.advisor_failure is None
        if branch == "timeout":
            assert data.batch.retained_failure is None and not native.completed
            return
        assert data.batch.retained_failure.kind == "advisor_failure" and native.completed
        invocation = native.invocation
        if branch == "crash":
            assert invocation.status is ComposerToolStatus.PLUGIN_CRASH and invocation.private_failure is not None
            assert data.batch.retained_failure.refusal_source is None and native.original is original
        else:
            refusal = data.batch.retained_failure.refusal_source
            assert refusal is not None and refusal.outcome is data.batch.tool_outcomes[-1] and refusal.invocation is invocation
            assert invocation.status is ComposerToolStatus.SUCCESS and invocation.error_class is invocation.private_failure is None
            assert invocation.result_canonical == canonical_json(refusal.outcome.response)
        encoded = encode_coupled_batch_data(
            batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=owner.runtime.env_ref_names
        )
        cold = decode_coupled_batch_data(encoded, env_ref_names=owner.runtime.env_ref_names)
        if branch != "crash":
            assert cold.batch.retained_failure.refusal_source.outcome is cold.batch.tool_outcomes[-1]
        before = stored_rows(operation_store[0])
        actual = owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names)
        assert actual is not None and not actual.applied and actual.invocation == invocation
        assert stored_rows(operation_store[0]) == before
        assert len(sdk) == (1 if branch in ("context", "guard") else 2)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, native.original)
        following = owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names)
        assert following is not None and following.applied and following.invocation == invocation


def _install_original_deadline(store, monkeypatch):
    """Create the real deadline owner when the fixture first binds its work."""
    import time

    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkCoordinator
    from elspeth.web.sessions.composer_drain import ComposerDrainSignal
    from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
    from tests.unit.web.sessions import test_continuation_composition_initialization

    original = test_continuation_composition_initialization.inventory_work

    def bind(running):
        actual = original(running)
        assert not actual.coordinator.tickets
        started = time.monotonic()
        observed = store[2].read_continuation_timing(running)
        root = decode_continuation_snapshot(store[2].load_continuation_snapshot(running).payload_json)
        timing = ContinuationSQLDeadlineOwner(
            running,
            policy=decode_continuation_policy(root.admission_policy_json, root.policy_hash),
            drain_signal=ComposerDrainSignal(),
            drain_seconds=10,
            hard_expires_at=started + observed.hard_remaining_seconds,
        )
        return RequiredWorkBinding(
            RequiredWorkCoordinator(actual.coordinator.authority, sql_deadline_owner=timing),
            actual.transition_ordinal,
            actual.semantic_ordinal,
            actual.role,
            running,
        )

    monkeypatch.setattr(test_continuation_composition_initialization, "inventory_work", bind)


@pytest.mark.asyncio
@pytest.mark.parametrize("applied", (False, True))
async def test_actual_failed_prefix_cold_read_reproves_new_generation_without_replay(operation_store, tmp_path, monkeypatch, applied):
    from elspeth.web.composer import tool_batch
    from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost, ComposerOperationRunning
    from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
    from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, first, _primary, sdk):
        original = RuntimeError("actual cold failed prefix")
        executed = []

        def execute(*args, **kwargs):
            executed.append(args)
            raise original

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        if applied:
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
        source = owner.authority.read_continuation_tool_failure(first, env_ref_names=owner.runtime.env_ref_names)
        assert source is not None and source.applied is applied
        assert source.invocation == owner.failure_custody.invocation
        assert source.invocation is not owner.failure_custody.invocation
        await owner.lease.close()
        _engine, repository, authority, _sessions, _sid = new_owners(operation_store)
        assert (
            repository.recover_composer_segment(
                session_id=first.claim.session_id,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is True
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        second = ComposerOperationRunning(claim, context)
        try:
            assert claim.ownership_generation == first.claim.ownership_generation + 1
            assert context.fence.operation_epoch > first.session_operation_context.fence.operation_epoch
            before = stored_rows(operation_store[0])
            cold = authority.read_continuation_tool_failure(second, env_ref_names=owner.runtime.env_ref_names)
            assert cold is not None and cold.applied is applied and cold.issue == source.issue
            assert cold.invocation == source.invocation and cold.invocation is not source.invocation
            assert cold.data.batch.plugin_crash is cold.data.batch.plugin_crash_cause is cold.data.batch.advisor_failure is None
            with pytest.raises(ComposerOperationFenceLost):
                owner.authority.read_continuation_tool_failure(first, env_ref_names=owner.runtime.env_ref_names)
            assert stored_rows(operation_store[0]) == before and len(executed) == len(sdk) == 1
        finally:
            repository.release(context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "rollback", "after_commit", "stop_after_rollback"))
async def test_actual_failed_prefix_terminal_uses_two_slots_and_exact_bundle(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import tool_batch
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions import continuation_tool_failure
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker, _StartedSettlement
    from elspeth.web.sessions.composer_operation_errors import request_cancelled_error

    _install_original_deadline(operation_store, monkeypatch)
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        original = RuntimeError("actual terminal plugin origin")
        injected = RuntimeError("actual terminal transaction boundary")
        executed, observations = [], []

        def execute(*args, **kwargs):
            executed.append(args)
            raise original

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original)
        assert owner.p4.committed_outcome is not None and owner.p4.physical is not None
        service = owner.service._require_sessions_service()
        actual_settle = continuation_tool_failure.read_failure_bundle_on_connection
        actual_begin = type(service)._session_process_locked_begin
        exits = []

        @contextmanager
        def begin(self, sid):
            from sqlalchemy import select

            from elspeth.web.sessions.models import chat_messages_table as messages

            terminal = False
            try:
                with actual_begin(self, sid) as conn:
                    yield conn
                    from elspeth.web.sessions.models import composer_async_operations_table as jobs

                    terminal = (
                        conn.execute(select(jobs.c.status).where(jobs.c.operation_id == running.claim.operation_id)).scalar_one()
                        == "failed"
                    )
                    if terminal:
                        exits.append(
                            conn.execute(
                                select(messages.c.id).where(messages.c.content == "Private credential-filtered operation failure facts")
                            ).all()
                        )
                        if fault in ("rollback", "stop_after_rollback") and len(exits) == 1:
                            raise injected
            except BaseException as error:
                if fault == "stop_after_rollback" and error is injected:
                    owner.authority.request_cancel(
                        session_id=running.claim.session_id,
                        operation_id=running.claim.operation_id,
                        cancelled_failure=request_cancelled_error,
                    )
                raise
            if terminal and fault == "after_commit" and len(exits) == 1:
                raise injected

        def read(conn, observation):
            observations.append(observation)
            return actual_settle(conn, observation)

        monkeypatch.setattr(type(service), "_session_process_locked_begin", begin)
        monkeypatch.setattr(continuation_tool_failure, "read_failure_bundle_on_connection", read)
        timing = owner.work.coordinator.sql_deadline_owner
        clocks = timing.hard_expires_at, timing._action_expires_at, timing._settlement_expires_at
        projection = owner.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
        committed = await service.fail_continuation_tool_operation(
            running,
            required_work=owner.work.coordinator,
            failure_projection_work=projection,
            env_ref_names=owner.runtime.env_ref_names,
            original=caught.value,
            custody=owner.failure_custody,
        )
        assert committed.observation.committed and committed.terminal.status == "failed"
        if fault == "none":
            assert not observations
        elif fault == "stop_after_rollback":
            assert observations[-1] is committed.observation.prior
        else:
            assert observations[-1] is committed.observation
        sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.TERMINAL_FAILURE_SQL)
        assert len(sql) == (2 if fault in ("rollback", "stop_after_rollback") else 1)
        assert all(ticket.complete and ticket._finish_once_handoff is not None for ticket in sql)
        if fault == "rollback":
            assert observations[0].prepared is committed.observation.prepared
            assert len({str(row[0]) for rows in exits for row in rows}) == 1
        if fault == "stop_after_rollback":
            assert committed.terminal.failure_code == "request_cancelled"
            assert committed.observation.prior is not None
            assert committed.observation.prior.prepared.append.checkpoint_id != committed.observation.prepared.append.checkpoint_id
            assert committed.observation.prior.prepared.outcome.failure_code != "request_cancelled"
        assert (timing.hard_expires_at, timing._action_expires_at, timing._settlement_expires_at) == clocks
        import threading

        from starlette.applications import Starlette

        from elspeth.web.process_recovery import ProcessRecovery
        from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

        draining = threading.Event()
        worker = ComposerAsyncWorker(
            app=Starlette(),
            authority=owner.authority,
            concurrency=1,
            scan_interval_seconds=0.01,
            claim_lease_seconds=30,
            drain_seconds=10,
            owner_instance_id=service.session_operation_owner_instance_id,
            process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining),
            instance_draining=draining,
        )
        await worker._verify_terminal_before_release(
            running, owner.lease, committed.terminal, _StartedSettlement(terminal_receipt=committed.terminal)
        )
        assert projection.complete
        assert len(executed) == len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    (
        "readback",
        "private",
        "event",
        "creator",
        "predecessor",
        "segment",
        "claim",
        "snapshot_key",
        "snapshot_codec",
        "event_key",
        "event_codec",
    ),
)
async def test_actual_terminal_uncertainty_keeps_pending_projection_and_native_custody(operation_store, tmp_path, monkeypatch, fault):
    from sqlalchemy import delete, select, update

    from elspeth.web.composer import tool_batch
    from elspeth.web.required_work import RequiredWorkIncomplete, RequiredWorkSource
    from elspeth.web.sessions import continuation_tool_failure
    from elspeth.web.sessions.models import chat_messages_table as messages
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    from elspeth.web.sessions.models import composer_continuation_events_table as events
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    from elspeth.web.sessions.service import ComposerToolFailureSQLCompletionUnknown

    _install_original_deadline(operation_store, monkeypatch)
    # The production lease must refuse release of the unresolved projection.
    # Observe that actual refusal; never close it with a favorable test receipt.
    with pytest.raises(RequiredWorkIncomplete):
        async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
            original = RuntimeError("actual uncertain terminal tool origin")
            commit_error = RuntimeError("actual post-commit return failure")
            read_error = RuntimeError("actual readback unavailable")

            def tool(*args, **kwargs):
                raise original

            monkeypatch.setattr(tool_batch, "execute_tool", tool)
            await _advance(owner)
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
            service = owner.service._require_sessions_service()
            actual_begin = type(service)._session_process_locked_begin
            actual_read = continuation_tool_failure.read_failure_bundle_on_connection
            failed_exit = []

            @contextmanager
            def begin(self, sid):
                is_terminal = False
                with actual_begin(self, sid) as conn:
                    yield conn
                    is_terminal = (
                        conn.execute(select(jobs.c.status).where(jobs.c.operation_id == running.claim.operation_id)).scalar_one()
                        == "failed"
                    )
                if is_terminal and not failed_exit:
                    failed_exit.append(True)
                    raise commit_error

            def read(conn, observation):
                if fault == "readback":
                    raise read_error
                positive = actual_read(conn, observation)
                assert type(positive) is continuation_tool_failure.FailureBundleCommitted
                with conn.begin_nested() as savepoint:
                    if fault == "private":
                        conn.execute(delete(messages).where(messages.c.id == observation.message_id))
                    elif fault == "event":
                        conn.execute(
                            update(events)
                            .where(
                                events.c.session_id == str(running.claim.session_id),
                                events.c.operation_id == running.claim.operation_id,
                                events.c.sequence == observation.prepared.append.expected_cursor + 1,
                            )
                            .values(tag="received")
                        )
                    elif fault == "creator":
                        conn.execute(
                            update(attempts)
                            .where(
                                attempts.c.session_id == str(running.claim.session_id),
                                attempts.c.operation_id == running.claim.operation_id,
                                attempts.c.generation == running.claim.ownership_generation,
                            )
                            .values(session_operation_epoch=running.session_operation_context.fence.operation_epoch + 1)
                        )
                    elif fault == "segment":
                        conn.execute(update(segments).where(segments.c.operation_id == running.claim.operation_id).values(state="running"))
                    elif fault == "claim":
                        driving = observation.prepared.driving
                        assert (
                            conn.execute(
                                update(segments)
                                .where(segments.c.operation_id == running.claim.operation_id)
                                .values(
                                    claim_token=driving.claim_token,
                                    claim_owner_instance_id=driving.claim_owner_instance_id,
                                    claim_expires_at=driving.claim_expires_at,
                                )
                            ).rowcount
                            == 1
                        )
                    elif fault in ("snapshot_key", "event_key"):
                        from elspeth.web.sessions.continuation import put_chunk, read_chunk

                        if fault == "snapshot_key":
                            old_key = f"snapshot:{observation.prepared.append.checkpoint_id}"
                            codec = "snapshot.v2"
                        else:
                            old_key = f"event:{observation.prepared.append.expected_cursor + 1}"
                            codec = "event.v2"
                        alias = old_key + ":same-bytes"
                        put_chunk(
                            conn,
                            session_id=str(running.claim.session_id),
                            operation_id=running.claim.operation_id,
                            key=alias,
                            payload=read_chunk(
                                conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=old_key
                            ),
                            codec=codec,
                        )
                        if fault == "snapshot_key":
                            conn.execute(
                                update(checkpoints)
                                .where(checkpoints.c.checkpoint_id == observation.prepared.append.checkpoint_id)
                                .values(snapshot_chunk_key=alias)
                            )
                        else:
                            conn.execute(
                                update(events)
                                .where(
                                    events.c.operation_id == running.claim.operation_id,
                                    events.c.sequence == observation.prepared.append.expected_cursor + 1,
                                )
                                .values(payload_chunk_key=alias)
                            )
                    elif fault in ("snapshot_codec", "event_codec"):
                        key = (
                            f"snapshot:{observation.prepared.append.checkpoint_id}"
                            if fault == "snapshot_codec"
                            else f"event:{observation.prepared.append.expected_cursor + 1}"
                        )
                        conn.execute(
                            update(chunks)
                            .where(chunks.c.operation_id == running.claim.operation_id, chunks.c.chunk_key == key)
                            .values(codec="private.v1")
                        )
                    else:
                        assert fault == "predecessor"
                        observation.prepared = replace(
                            observation.prepared,
                            append=replace(observation.prepared.append, expected_checkpoint_id="00000000-0000-0000-0000-000000000000"),
                        )
                    try:
                        return actual_read(conn, observation)
                    finally:
                        savepoint.rollback()

            monkeypatch.setattr(type(service), "_session_process_locked_begin", begin)
            monkeypatch.setattr(continuation_tool_failure, "read_failure_bundle_on_connection", read)
            projection = owner.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
            with pytest.raises(ComposerToolFailureSQLCompletionUnknown) as unknown:
                await service.fail_continuation_tool_operation(
                    running,
                    required_work=owner.work.coordinator,
                    failure_projection_work=projection,
                    env_ref_names=owner.runtime.env_ref_names,
                    original=caught.value,
                    custody=owner.failure_custody,
                )
            assert _contains_identity(unknown.value, original) and _contains_identity(unknown.value, commit_error)
            if fault == "readback":
                assert _contains_identity(unknown.value, read_error)
            assert unknown.value.custody is owner.failure_custody and unknown.value.observation is not None
            assert unknown.value.prepared_candidates and unknown.value.ticket._finish_once_handoff is not None
            assert not projection.complete and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "facts", "record", "record_after", "outcome", "wrapper"))
async def test_real_failed_dispatch_retains_origin_before_fallible_audit(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import private_diagnostic_facts, tool_batch
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.protocol import ComposerPluginCrashError

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, primary, sdk):
        original = RuntimeError("actual failed tool")
        cause = ValueError("actual explicit cause")
        original.__cause__ = cause
        secondary = RuntimeError("actual audit construction failure")
        executed = []
        append = BufferingRecorder._append_invocation_locked

        def execute(*args, **kwargs):
            executed.append(args)
            raise original

        def facts(*args, **kwargs):
            raise secondary

        def record(self, invocation, **kwargs):
            raise secondary

        def record_after(self, invocation, **kwargs):
            append(self, invocation, **kwargs)
            raise secondary

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        if fault == "facts":
            monkeypatch.setattr(private_diagnostic_facts, "capture_failure_facts", facts)
        elif fault == "record":
            monkeypatch.setattr(BufferingRecorder, "_append_invocation_locked", record)
        elif fault == "record_after":
            monkeypatch.setattr(BufferingRecorder, "_append_invocation_locked", record_after)
        elif fault == "outcome":
            monkeypatch.setattr(tool_batch, "_append_continuation_tool_outcome", facts)
        elif fault == "wrapper":
            monkeypatch.setattr(ComposerPluginCrashError, "capture", facts)
        if fault == "none":
            await _advance(owner)
        else:
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
            assert _contains_identity(caught.value, secondary)
        custody = owner.failure_custody
        assert custody is not None and custody.original is original and custody.original.__cause__ is cause
        assert custody.response is primary.response and len(executed) == len(sdk) == 1
        data = owner.runtime.graph.batch
        assert data.batch.plugin_crash is data.batch.plugin_crash_cause is data.batch.advisor_failure is None
        if fault != "none":
            assert data.batch.retained_failure is None and custody.completed is False
            assert secondary in custody.errors
            if fault in ("record_after", "outcome", "wrapper"):
                assert custody.observation.stored is owner.runtime.live.recorder.invocations[-1]
                assert custody.observation.stored.status is ComposerToolStatus.PLUGIN_CRASH
            return
        assert custody.completed and custody.invocation is owner.runtime.live.recorder.invocations[-1]
        assert custody.invocation.status is ComposerToolStatus.PLUGIN_CRASH
        assert custody.invocation.private_failure is not None
        assert custody.wrapper.original_exc is original and data.batch.stop_issued_batch
        assert data.batch.next_tool_index == 1 and data.batch.retained_failure.kind == "plugin_crash"
        encoded = encode_coupled_batch_data(
            batch=data.batch, accumulator=data.accumulator, calls=data.calls, env_ref_names=owner.runtime.env_ref_names
        )
        cold = decode_coupled_batch_data(encoded, env_ref_names=owner.runtime.env_ref_names)
        assert cold.batch.retained_failure == data.batch.retained_failure
        assert cold.batch.plugin_crash is cold.batch.plugin_crash_cause is cold.batch.advisor_failure is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "missing", "class", "prefix", "authoring"))
async def test_actual_failed_prefix_refuses_counterfeit_descriptor_and_alias(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        original = RuntimeError("actual opposed failed prefix")

        def execute(*args, **kwargs):
            raise original

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        data = owner.runtime.graph.batch
        invocations = owner.runtime.live.recorder.invocations
        require_retained_failure_links(data, invocations)
        assert data.batch.retained_failure is not None and owner.failure_custody.original is original
        if change == "none":
            assert len(sdk) == 1
            return
        if change == "missing":
            data.batch.retained_failure = None
        elif change == "class":
            data.batch.retained_failure = replace(data.batch.retained_failure, error_class="ValueError")
        elif change == "prefix":
            data.batch.next_tool_index += 1
        else:
            assert change == "authoring"
            copied = replace(invocations[-1], private_authoring=replace(invocations[-1].private_authoring))
            invocations = (*invocations[:-1], copied)
        with pytest.raises(AuditIntegrityError):
            require_retained_failure_links(data, invocations)
        assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "construct", "append"))
async def test_actual_session_await_cancellation_observes_before_audit(operation_store, tmp_path, monkeypatch, fault):
    import asyncio

    from elspeth.web.composer import tool_batch, tools
    from elspeth.web.composer.audit import BufferingRecorder

    arguments = {"affected_node_id": "rate_node", "kind": "vague_term", "user_term": "cool", "llm_draft": "Use concise wording."}
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="request_interpretation_review", tool_arguments=arguments, bound_base=True
    ) as (owner, _running, sdk):
        original, cause = asyncio.CancelledError("actual session-aware cancelled work"), RuntimeError("actual session cancellation cause")
        secondary = RuntimeError("actual session cancellation audit failure")
        entered, records = [], []
        actual_finish, actual_record = tool_batch.finish_cancelled, BufferingRecorder.record_with_failure_observation

        async def handler(**kwargs):
            entered.append(kwargs)
            raise original from cause

        def finish(audit, *, exc):
            assert exc is original
            if fault == "construct":
                raise secondary
            return actual_finish(audit, exc=exc)

        def record(self, value, observation):
            records.append(value)
            if fault == "append":
                raise secondary
            return actual_record(self, value, observation)

        monkeypatch.setitem(tools._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", handler)
        monkeypatch.setattr(tool_batch, "finish_cancelled", finish)
        monkeypatch.setattr(BufferingRecorder, "record_with_failure_observation", record)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and original.__cause__ is cause
        if fault != "none":
            assert _contains_identity(caught.value, secondary)
        assert owner.failure_custody.original is original and not owner.failure_custody.completed
        assert owner.runtime.graph.batch.batch.retained_failure is None
        assert len(entered) == len(sdk) == 1 and len(records) == (0 if fault == "construct" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("construct", "append"))
async def test_actual_advisor_origin_survives_inner_audit_failure(operation_store, tmp_path, monkeypatch, fault):
    import litellm

    from elspeth.web.composer import advisor_checkpoint
    from elspeth.web.composer.audit import BufferingRecorder

    async with _advisor_case(operation_store, tmp_path, monkeypatch) as (owner, _running, sdk):
        original, cause = RuntimeError("actual Advisor earlier SDK origin"), RuntimeError("actual Advisor explicit cause")
        secondary = RuntimeError("actual Advisor inner audit failure")
        records = []

        async def fail(**kwargs):
            sdk.append(kwargs)
            raise original from cause

        def build(*args, **kwargs):
            raise secondary

        def record(self, call, **kwargs):
            records.append(call)
            raise secondary

        monkeypatch.setattr(litellm, "acompletion", fail)
        if fault == "construct":
            monkeypatch.setattr(advisor_checkpoint, "build_llm_call_record", build)
        else:
            monkeypatch.setattr(BufferingRecorder, "record_llm_call", record)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and _contains_identity(caught.value, secondary)
        assert original.__cause__ is cause and owner.failure_custody.original is original
        assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
        assert len(sdk) == 2
        if fault == "construct":
            assert not records
        else:
            assert records


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("cost", "guard"))
@pytest.mark.parametrize("change", ("none", "omit", "copy_invocation", "authoring", "correlation"))
async def test_actual_successful_refusal_requires_original_aliases_and_correlation(operation_store, tmp_path, monkeypatch, kind, change):
    from uuid import uuid4

    from elspeth.web.composer.continuation_evidence_application_data import capture_joint_evidence_graph
    from elspeth.web.composer.provider_cost import CommonProviderCostRefused
    from elspeth.web.composer.provider_quota import ProviderCallCustody

    async with _advisor_case(operation_store, tmp_path, monkeypatch, block=kind == "guard") as (owner, running, sdk):
        actual_cost = ProviderCallCustody.require_cost_output

        def cost(self):
            actual_cost(self)
            if owner.failure_custody is not None and self is owner.failure_custody.observation.provider_custody:
                calls = owner.runtime.live.recorder.llm_calls
                assert calls and calls[-1].call_id is not None
                raise CommonProviderCostRefused("COST_UNAVAILABLE", call_id=calls[-1].call_id)

        if kind == "cost":
            monkeypatch.setattr(ProviderCallCustody, "require_cost_output", cost)
        await _advance(owner)
        data = owner.runtime.graph.batch
        original = data.batch.retained_failure
        source = original.refusal_source
        assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is not None
        capture_joint_evidence_graph(
            owner=owner.runtime.live.turn_evidence,
            recorder=owner.runtime.live.recorder,
            drained=owner.runtime.drained,
            data=data,
            env_ref_names=owner.runtime.env_ref_names,
        )
        if change == "none":
            assert source.invocation is owner.failure_custody.invocation
        elif change == "correlation" and kind == "guard":
            from sqlalchemy import select, update

            from elspeth.web.sessions.continuation_tool_failure import read_tool_failure_source_on_connection
            from elspeth.web.sessions.locking import locked_session_transaction
            from elspeth.web.sessions.models import chat_messages_table as messages

            sid = str(running.claim.session_id)
            with locked_session_transaction(operation_store[0], sid) as conn:
                assert read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names) is not None
                row = conn.execute(
                    select(messages).where(
                        messages.c.session_id == sid,
                        messages.c.tool_calls[0]["check"]["audit_id"].as_string() == source.outcome.response["audit_id"],
                    )
                ).one()
                with conn.begin_nested() as savepoint:
                    envelope = dict(row.tool_calls[0])
                    envelope["check"] = dict(envelope["check"], audit_id=str(uuid4()))
                    assert (
                        conn.execute(
                            update(messages).where(messages.c.id == row.id).values(tool_calls=[envelope], content=canonical_json(envelope))
                        ).rowcount
                        == 1
                    )
                    with pytest.raises(AuditIntegrityError, match="guard"):
                        read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names)
                    savepoint.rollback()
                assert read_tool_failure_source_on_connection(conn, running, env_ref_names=owner.runtime.env_ref_names) is not None
        else:
            if change == "omit":
                data.batch.retained_failure = replace(original, refusal_source=None)
            elif change == "copy_invocation":
                data.batch.retained_failure = replace(original, refusal_source=replace(source, invocation=replace(source.invocation)))
            elif change == "authoring":
                data.batch.private_authorings_by_call[source.outcome.call.id] = replace(source.invocation.private_authoring)
            else:
                assert change == "correlation" and kind == "cost"
                data.batch.retained_failure = replace(original, refusal_source=replace(source, provider_call_id=str(uuid4())))
            with pytest.raises(AuditIntegrityError):
                capture_joint_evidence_graph(
                    owner=owner.runtime.live.turn_evidence,
                    recorder=owner.runtime.live.recorder,
                    drained=owner.runtime.drained,
                    data=data,
                    env_ref_names=owner.runtime.env_ref_names,
                )
            assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is not None
        assert len(sdk) == (2 if kind == "cost" else 1)


@pytest.mark.asyncio
async def test_actual_argument_feedback_then_primary_crash_keeps_distinct_custody(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.protocol import ToolArgumentError

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        feedback = ToolArgumentError(argument="fixture_input", expected="an admitted value", actual_type="missing")
        original = RuntimeError("actual later primary")
        calls = []

        def execute(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise feedback
            raise original

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        await _advance(owner)
        first = owner.failure_custody
        assert first.original is None and first.observation.feedback is feedback and not first.completed
        assert owner.runtime.graph.batch.batch.retained_failure is None
        await _advance(owner)
        second = owner.failure_custody
        assert second is not first and second.original is original and second.completed
        assert second.observation.feedback is None
        assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is not None
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original) and not _contains_identity(caught.value, feedback)
        assert len(calls) == 2 and len(sdk) == 1


@pytest.mark.asyncio
async def test_actual_advisor_timeout_is_retained_feedback_without_primary_terminal(operation_store, tmp_path, monkeypatch):
    import litellm

    async with _advisor_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = TimeoutError("actual nonprimary Advisor timeout"), RuntimeError("original timeout cause")

        async def fail(**kwargs):
            sdk.append(kwargs)
            raise original from cause

        monkeypatch.setattr(litellm, "acompletion", fail)
        await _advance(owner)
        custody = owner.failure_custody
        assert custody.original is None and custody.handled == [original] and original.__cause__ is cause
        assert not custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
        assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is None
        assert len(sdk) == 2


def _actual_failure_driver(store, owner, running):
    import threading

    from starlette.applications import Starlette

    from elspeth.web.composer.progress import ComposerProgressRegistry
    from elspeth.web.process_recovery import ProcessRecovery
    from elspeth.web.sessions.composer_app_services import ComposerAppServices
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
    from elspeth.web.sessions.continuation_driver import _SegmentDriver
    from elspeth.web.sessions.routes._helpers import _SessionComposeLockRegistry
    from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog

    service = owner.service
    policy = service._policy_context
    services = ComposerAppServices(
        session_service=store[3],
        composer_service=service,
        settings=service._settings,
        catalog_service=policy._catalog,
        operator_profile_registry=policy._profile_registry,
        scoped_secret_resolver=service._secret_service,
        session_engine=store[0],
        interpretation_surfacing=service._interpretation_surfacing,
        plugin_snapshot_for_user_id=policy._snapshot_factory,
        progress_registry=ComposerProgressRegistry(),
        compose_locks=_SessionComposeLockRegistry(),
        composer_operation_authority=store[2],
    )
    draining = threading.Event()
    worker = ComposerAsyncWorker(
        app=Starlette(),
        authority=store[2],
        concurrency=1,
        scan_interval_seconds=0.01,
        claim_lease_seconds=30,
        drain_seconds=10,
        owner_instance_id=store[3].session_operation_owner_instance_id,
        process_recovery=ProcessRecovery(watchdog=OwnedTestProcessWatchdog(draining), instance_draining=draining),
        instance_draining=draining,
    )
    driver = _SegmentDriver(worker, services, running, owner.lease)
    driver.phases = owner
    return driver


@asynccontextmanager
async def _phase_case_with_release_opposition(store, tmp_path, monkeypatch, *, expected=False):
    from elspeth.web.required_work import RequiredWorkIncomplete

    body_complete = False
    try:
        async with _phase_case(store, tmp_path, monkeypatch) as case:
            yield case
            body_complete = True
    except RequiredWorkIncomplete:
        if not expected or not body_complete:
            raise
    else:
        assert not expected, "actual pending verification projection must refuse lease release"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ("record", "env", "dispose"))
@pytest.mark.parametrize("close_fails", (False, True))
async def test_actual_driver_entry_read_failure_keeps_native_with_successful_single_close(
    operation_store, tmp_path, monkeypatch, entry, close_fails
):
    from elspeth.web.composer import tool_batch
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions import continuation_driver

    _install_original_deadline(operation_store, monkeypatch)
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        original, cause = RuntimeError("actual retained entry origin"), RuntimeError("actual original explicit cause")
        later, cleanup = RuntimeError("actual later current read failure"), RuntimeError("actual later close failure")
        original.__cause__ = cause
        dispatches = []

        def execute(*args, **kwargs):
            dispatches.append(args)
            raise original

        if entry != "dispose":
            monkeypatch.setattr(tool_batch, "execute_tool", execute)
            await _advance(owner)
            assert owner.failure_custody.completed and owner.p4 is None
        driver = _actual_failure_driver(operation_store, owner, running)
        actual_get, actual_names, actual_close = (
            owner.authority.get_for_turn,
            continuation_driver._read_current_env_names,
            type(owner.lease).close,
        )
        reads, closes = [], []

        def get(**kwargs):
            record = actual_get(**kwargs)
            reads.append(record)
            raise later

        async def names(*args, **kwargs):
            value = await actual_names(*args, **kwargs)
            reads.append(value)
            raise later

        async def close(self, **kwargs):
            assert self is owner.lease
            await actual_close(self, **kwargs)
            closes.append(self._close_task)
            assert self._close_task.done() and self._close_task.exception() is None
            if close_fails:
                raise cleanup

        def construct(*args, **kwargs):
            assert args[2] is running and args[3] is owner.lease
            return driver

        if entry == "env":
            monkeypatch.setattr(continuation_driver, "_read_current_env_names", names)
        else:
            monkeypatch.setattr(owner.authority, "get_for_turn", get)
        monkeypatch.setattr(type(owner.lease), "close", close)
        monkeypatch.setattr(continuation_driver, "_SegmentDriver", construct)
        try:
            with pytest.raises(BaseException) as caught:
                await continuation_driver.run_continuation_segment(
                    driver.worker, driver.services, running, owner.lease, adoption_failure=original if entry == "dispose" else None
                )
            assert _contains_identity(caught.value, original) and _contains_identity(caught.value, cause)
            assert _contains_identity(caught.value, later) and original.__cause__ is cause
            if close_fails:
                assert _contains_identity(caught.value, cleanup)
            assert len(reads) == len(closes) == 1 and len(sdk) == 1
            assert len(dispatches) == (0 if entry == "dispose" else 1)
            assert driver._committed_failure is None and owner.p4 is None
            assert not any(
                item.key.source in (RequiredWorkSource.TERMINAL_FAILURE_SQL, RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
                for item in driver.work.coordinator.tickets
            )
        finally:
            monkeypatch.setattr(type(owner.lease), "close", actual_close)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("normalize", "construct", "append"))
async def test_actual_session_success_then_finalizer_failure_retains_returned_object(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import session_tool, tools
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.tools._common import ToolResult

    arguments = {"affected_node_id": "rate_node", "kind": "vague_term", "user_term": "cool", "llm_draft": "Use concise wording."}
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="request_interpretation_review", tool_arguments=arguments, bound_base=True
    ) as (owner, _running, sdk):
        original, cause = RuntimeError("actual session finalizer failure"), RuntimeError("actual session finalizer cause")
        results, constructions, records = [], [], []
        actual_finish = session_tool.finish_success

        async def handler(**kwargs):
            state = kwargs["state"]
            result = ToolResult(
                success=True,
                updated_state=state,
                affected_nodes=(),
                validation=owner.service._policy_context.build("alice")[1].validate_composition_state(state).validation,
            )
            results.append(result)
            return result

        def normalize(value, catalog):
            assert value is results[0]
            raise original from cause

        def finish(*args, **kwargs):
            constructions.append(args)
            if fault == "construct":
                raise original from cause
            return actual_finish(*args, **kwargs)

        def record(self, value, observation):
            records.append(value)
            raise original from cause

        monkeypatch.setitem(tools._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", handler)
        monkeypatch.setattr(session_tool, "finish_success", finish)
        if fault == "normalize":
            monkeypatch.setattr(session_tool, "normalize_tool_result_validation", normalize)
        elif fault == "append":
            monkeypatch.setattr(BufferingRecorder, "record_with_failure_observation", record)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        observation = owner.failure_custody.observation
        assert observation.returned and observation.returned_output is results[0] and not observation.execution_failed
        assert _contains_identity(caught.value, original) and original.__cause__ is cause
        assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
        assert len(results) == len(sdk) == 1 and len(constructions) == (0 if fault == "normalize" else 1)
        assert len(records) == (1 if fault == "append" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "verify", "close", "close_cancel", "terminal_unknown"))
async def test_actual_driver_retains_consumed_failure_through_verify_and_single_close(operation_store, tmp_path, monkeypatch, fault):
    import asyncio

    from elspeth.web.composer import tool_batch
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions import continuation_driver

    _install_original_deadline(operation_store, monkeypatch)
    async with _phase_case_with_release_opposition(
        operation_store, tmp_path, monkeypatch, expected=fault in ("verify", "terminal_unknown")
    ) as (
        owner,
        running,
        _primary,
        sdk,
    ):
        original, cause = RuntimeError("actual driver tool origin"), RuntimeError("actual driver explicit cause")
        secondary = (
            asyncio.CancelledError("actual close cancellation") if fault == "close_cancel" else RuntimeError("actual later join failure")
        )

        def tool(*args, **kwargs):
            raise original from cause

        monkeypatch.setattr(tool_batch, "execute_tool", tool)
        await _advance(owner)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        assert _contains_identity(caught.value, original)
        driver = _actual_failure_driver(operation_store, owner, running)
        actual_get, actual_close = owner.authority.get, type(owner.lease).close
        closes, constructors = [], []

        def get(**kwargs):
            terminal = actual_get(**kwargs)
            assert terminal is not None and terminal.status == "failed"
            if fault == "verify":
                raise secondary
            return terminal

        async def close(self, **kwargs):
            assert self is owner.lease
            closes.append(self._close_task)
            await actual_close(self, **kwargs)
            if fault in ("close", "close_cancel"):
                raise secondary

        def construct(*args, **kwargs):
            assert all(
                actual is expected for actual, expected in zip(args, (driver.worker, driver.services, running, owner.lease), strict=True)
            )
            constructors.append(args)
            return driver

        monkeypatch.setattr(owner.authority, "get", get)
        monkeypatch.setattr(type(owner.lease), "close", close)
        monkeypatch.setattr(continuation_driver, "_SegmentDriver", construct)
        committed_exits, unknown_observations = [], []
        commit_error = RuntimeError("actual unknown post-commit origin")
        if fault == "terminal_unknown":
            from sqlalchemy import select

            from elspeth.web.sessions import continuation_tool_failure
            from elspeth.web.sessions.models import composer_async_operations_table as jobs

            service = operation_store[3]
            actual_begin = type(service)._session_process_locked_begin

            @contextmanager
            def begin(self, sid):
                terminal = False
                with actual_begin(self, sid) as conn:
                    yield conn
                    terminal = (
                        conn.execute(select(jobs.c.status).where(jobs.c.operation_id == running.claim.operation_id)).scalar_one()
                        == "failed"
                    )
                if terminal and not committed_exits:
                    committed_exits.append(True)
                    raise commit_error

            def unknown_read(conn, observation):
                unknown_observations.append(observation)
                raise secondary

            monkeypatch.setattr(type(service), "_session_process_locked_begin", begin)
            monkeypatch.setattr(continuation_tool_failure, "read_failure_bundle_on_connection", unknown_read)
        try:
            if fault == "none":
                result = await continuation_driver.run_continuation_segment(driver.worker, driver.services, running, owner.lease)
                assert result.terminal.status == "failed"
            else:
                with pytest.raises(BaseException) as escaping:
                    await continuation_driver.run_continuation_segment(driver.worker, driver.services, running, owner.lease)
                assert _contains_identity(escaping.value, original) and _contains_identity(escaping.value, secondary)
                assert _contains_identity(escaping.value, cause) and original.__cause__ is cause
            if fault == "terminal_unknown":
                assert driver._committed_failure is None and len(committed_exits) == len(unknown_observations) == 1
                assert _contains_identity(escaping.value, commit_error)
            else:
                assert driver._committed_failure is not None
                assert driver._committed_failure.observation.committed
            assert len(closes) == len(constructors) == len(sdk) == 1
            projection = tuple(t for t in owner.work.coordinator.tickets if t.key.source is RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)
            assert len(projection) == 1 and projection[0].complete is (fault not in ("verify", "terminal_unknown"))
        finally:
            monkeypatch.setattr(type(owner.lease), "close", actual_close)
            monkeypatch.setattr(owner.authority, "get", actual_get)
        if fault in ("verify", "terminal_unknown"):
            # Preserve the actual unresolved release failure without falsely completing projection work.
            from elspeth.web.required_work import RequiredWorkIncomplete

            with pytest.raises(RequiredWorkIncomplete):
                await actual_close(owner.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("plugin", "advisor"))
async def test_actual_cold_failed_prefix_finishes_p4_and_terminal_under_new_owner(operation_store, tmp_path, monkeypatch, family):
    import time

    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    from elspeth.web.composer.protocol import ComposerContextUnsupported
    from elspeth.web.composer.provider_quota import ProviderInvocationOwner
    from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
    from elspeth.web.required_work import (
        RequiredAuthorityKind,
        RequiredWorkAuthority,
        RequiredWorkBinding,
        RequiredWorkCoordinator,
        RequiredWorkRole,
    )
    from elspeth.web.sessions.composer_drain import ComposerDrainSignal
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
    from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
    from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

    _install_original_deadline(operation_store, monkeypatch)
    arguments = {} if family == "plugin" else None
    async with _advisor_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_name="get_pipeline_state" if family == "plugin" else "request_advisor_hint",
        tool_arguments=arguments,
    ) as (owner, first, sdk):
        original = (
            RuntimeError("actual cold plugin origin") if family == "plugin" else ComposerContextUnsupported("graph_coverage_unavailable")
        )
        issued = []

        def execute(*args, **kwargs):
            issued.append(args)
            raise original

        def context(self, value):
            raise original

        if family == "plugin":
            monkeypatch.setattr(tool_batch, "execute_tool", execute)
        else:
            monkeypatch.setattr(AdvisorCheckpointOwner, "_require_complete_graph_summary", context)
        await _advance(owner)
        assert owner.failure_custody.completed and owner.p4 is None
        await owner.lease.close()
        cold_store = new_owners(operation_store)
        _engine, repository, authority, _sessions, _sid = cold_store
        assert (
            repository.recover_composer_segment(
                session_id=first.claim.session_id,
                operation_id=first.claim.operation_id,
                expected_generation=first.claim.ownership_generation,
                actor_user_id="alice",
                auth_provider_type="local",
            )
            is True
        )
        (claim,) = authority.claim_segment(limit=1)
        physical = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
        )
        running = ComposerOperationRunning(claim, physical)
        assert running.claim.ownership_generation > first.claim.ownership_generation
        started = time.monotonic()
        observed = authority.read_continuation_timing(running)
        root = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
        timing = ContinuationSQLDeadlineOwner(
            running,
            policy=decode_continuation_policy(root.admission_policy_json, root.policy_hash),
            drain_signal=ComposerDrainSignal(),
            drain_seconds=10,
            hard_expires_at=started + observed.hard_remaining_seconds,
        )
        coordinator = RequiredWorkCoordinator(
            RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, physical, claim.operation_id, claim.attempt),
            sql_deadline_owner=timing,
        )
        work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
        lease = await ContinuationLeaseAdoption(repository, physical, lease_seconds=30, required_work=coordinator).join()
        try:
            service, _actors = current_service(cold_store)
            cold = ContinuationLivePhases(
                service, authority, work, ProviderInvocationOwner(service=cold_store[3], required_work=work), lease
            )
            driver = _actual_failure_driver(cold_store, cold, running)
            assert driver.phases.runtime is None and driver.phases.failure_custody is None
            result = await driver.run(None)
            assert result.terminal.status == "failed" and driver._committed_failure is not None
            assert driver._committed_failure.observation.source.applied
            assert driver.phases.failure_custody is None
            assert driver.phases.runtime.graph.batch.batch.plugin_crash is None
            assert len(sdk) == 1 and len(issued) == (1 if family == "plugin" else 0)
            coordinator.assert_completed()
        finally:
            await lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "snapshot", "checkpoint", "event", "time", "predecessor"))
async def test_real_prepared_terminal_append_reproves_exact_candidate(operation_store, tmp_path, monkeypatch, change):
    from sqlalchemy import select

    from elspeth.web.sessions.composer_operation_errors import worker_lost_error
    from elspeth.web.sessions.continuation import prepare_segment_settlement_on_connection, settle_segment_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (_owner, running, _primary, sdk):
        sid, oid = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(operation_store[0], sid) as conn:
            parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == oid)).one()
            segment = conn.execute(select(segments).where(segments.c.session_id == sid, segments.c.operation_id == oid)).one()
            history = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == oid)).one()
            prepared = prepare_segment_settlement_on_connection(
                conn,
                session_id=sid,
                operation_id=oid,
                parent=parent,
                segment=segment,
                history=history,
                outcome=worker_lost_error(request_id=None),
                settled_by="owner_terminal",
                attempt_outcome="settled",
            )
            actual = prepared
            if change == "snapshot":
                actual = replace(prepared, append=replace(prepared.append, snapshot_json=prepared.append.snapshot_json + " "))
            elif change == "checkpoint":
                actual = replace(prepared, append=replace(prepared.append, checkpoint_id="00000000-0000-0000-0000-000000000000"))
            elif change == "event":
                actual = replace(prepared, append=replace(prepared.append, events=()))
            elif change == "time":
                actual = replace(prepared, append=replace(prepared.append, published_at=prepared.published_at.replace(year=2001)))
            elif change == "predecessor":
                actual = replace(prepared, append=replace(prepared.append, expected_cursor=prepared.append.expected_cursor + 1))
            if change != "none":
                with pytest.raises(AuditIntegrityError):
                    settle_segment_on_connection(
                        conn,
                        session_id=sid,
                        operation_id=oid,
                        parent=parent,
                        segment=segment,
                        history=history,
                        outcome=prepared.outcome,
                        settled_by="owner_terminal",
                        attempt_outcome="settled",
                        now=prepared.published_at,
                        prepared_settlement=actual,
                    )
            else:
                settle_segment_on_connection(
                    conn,
                    session_id=sid,
                    operation_id=oid,
                    parent=parent,
                    segment=segment,
                    history=history,
                    outcome=prepared.outcome,
                    settled_by="owner_terminal",
                    attempt_outcome="settled",
                    now=prepared.published_at,
                    prepared_settlement=actual,
                )
                assert (
                    conn.execute(select(jobs.c.checkpoint_id).where(jobs.c.session_id == sid, jobs.c.operation_id == oid)).scalar_one()
                    == prepared.append.checkpoint_id
                )
        assert len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "facts", "record"))
async def test_actual_session_handler_crash_uses_native_custody_and_owed_p4(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import audit, tools
    from elspeth.web.composer.audit import BufferingRecorder

    arguments = {"affected_node_id": "rate_node", "kind": "vague_term", "user_term": "cool", "llm_draft": "Use concise wording."}
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="request_interpretation_review", tool_arguments=arguments, bound_base=True
    ) as (owner, running, sdk):
        original, secondary = RuntimeError("actual interpretation handler origin"), RuntimeError("actual interpretation audit failure")
        calls, records = [], []
        actual_record = BufferingRecorder.record_with_failure_observation

        async def handler(**kwargs):
            calls.append(kwargs)
            raise original

        def facts(*args, **kwargs):
            raise secondary

        def record(self, value, observation):
            records.append(value)
            if fault == "record":
                raise secondary
            return actual_record(self, value, observation)

        monkeypatch.setitem(tools._SESSION_AWARE_TOOL_HANDLERS, "request_interpretation_review", handler)
        if fault == "facts":
            monkeypatch.setattr(audit, "capture_failure_facts", facts)
        monkeypatch.setattr(BufferingRecorder, "record_with_failure_observation", record)
        if fault == "none":
            await _advance(owner)
            assert owner.failure_custody.original is original and owner.failure_custody.completed
            assert owner.failure_custody.wrapper.original_exc is original
            invocation = owner.failure_custody.invocation
            assert invocation.status is ComposerToolStatus.PLUGIN_CRASH and invocation.private_failure is not None
            assert owner.authority.read_continuation_tool_failure(running, env_ref_names=owner.runtime.env_ref_names) is not None
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original)
            assert owner.p4.committed_outcome is not None
        else:
            with pytest.raises(BaseException) as caught:
                await _advance(owner)
            assert _contains_identity(caught.value, original) and _contains_identity(caught.value, secondary)
            assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
        assert len(calls) == len(sdk) == 1
        assert len(records) == (0 if fault == "facts" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("payload", "record"))
async def test_actual_success_return_then_audit_failure_is_not_a_thrown_handler(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import audit, tool_batch
    from elspeth.web.composer.audit import BufferingRecorder

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        actual_execute = tool_batch.execute_tool
        error, cause = RuntimeError("actual success finalization failure"), RuntimeError("actual finalization cause")
        returned, attempts = [], []

        def execute(*args, **kwargs):
            result = actual_execute(*args, **kwargs)
            returned.append(result)
            return result

        def payload(result):
            assert returned and result is returned[0]
            raise error from cause

        def record(self, value, observation):
            attempts.append(value)
            raise error from cause

        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        if fault == "payload":
            monkeypatch.setattr(audit, "_result_to_audit_payload", payload)
        else:
            monkeypatch.setattr(BufferingRecorder, "record_with_failure_observation", record)
        with pytest.raises(BaseException) as caught:
            await _advance(owner)
        observation = owner.failure_custody.observation
        assert len(returned) == len(sdk) == 1 and observation.returned_output is returned[0]
        assert observation.returned and not observation.execution_failed
        assert _contains_identity(caught.value, error) and error.__cause__ is cause
        assert not owner.failure_custody.completed and owner.runtime.graph.batch.batch.retained_failure is None
        assert len(attempts) == (0 if fault == "payload" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "foreign", "equal_copy", "late"))
async def test_actual_set_pipeline_required_control_rebind_has_exact_owner(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.audit import begin_dispatch
    from tests.unit.web.composer.test_required_control_autowire import _bare_llm_candidate

    (tmp_path / "outputs").mkdir(exist_ok=True)
    arguments = _bare_llm_candidate()
    async with _advisor_case(
        operation_store, tmp_path, monkeypatch, tool_name="set_pipeline", tool_arguments=arguments, required_controls=True
    ) as (owner, _running, sdk):
        actual_rebind, actual_execute = tool_batch.rebind_dispatch_arguments, tool_batch.execute_tool
        rebound, dispatches = [], []

        def rebind(old, values, *, failure_observation=None):
            assert failure_observation is owner.failure_custody.observation
            if change == "foreign":
                old = begin_dispatch("foreign", old.tool_name, values, version_before=old.version_before, actor=old.actor)
            elif change == "equal_copy":
                old = replace(old)
            result = actual_rebind(old, values, failure_observation=failure_observation)
            rebound.append((old, result))
            return result

        def execute(*args, **kwargs):
            dispatches.append(args)
            return actual_execute(*args, **kwargs)

        monkeypatch.setattr(tool_batch, "rebind_dispatch_arguments", rebind)
        monkeypatch.setattr(tool_batch, "execute_tool", execute)
        if change in ("foreign", "equal_copy"):
            with pytest.raises(AuditIntegrityError) as caught:
                await _advance(owner)
            assert "actual" in str(caught.value)
            assert not dispatches and not rebound
        else:
            await _advance(owner)
            assert rebound and len(dispatches) == 1
            old, current = rebound[-1]
            observation = owner.failure_custody.observation
            assert current is observation.audit and old is not current
            assert (old.started_at, old.started_ns, old.actor, old.tool_call_id) == (
                current.started_at,
                current.started_ns,
                current.actor,
                current.tool_call_id,
            )
            assert current.arguments_hash != old.arguments_hash
            assert observation.stored is not None and observation.stored.arguments_hash == current.arguments_hash
            assert not owner.failure_custody.completed
            if change == "late":
                with pytest.raises(AuditIntegrityError, match="open owner"):
                    actual_rebind(current, arguments, failure_observation=observation)
        assert len(sdk) == 1
