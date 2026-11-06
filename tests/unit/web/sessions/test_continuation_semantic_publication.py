"""A real reviewed result and paid whole Stop bind the terminal SQL composite."""

import json
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from functools import partial
from uuid import UUID, uuid4

import httpx
import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import prompt_guard as original_guard
from elspeth.web.composer.continuation_checkpoint_end_terminal import EndCheckpointCompleteReview
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor import prepare_semantic_advisor_request
from elspeth.web.composer.continuation_semantic_invocation import make_semantic_advisor_invocation
from elspeth.web.composer.continuation_semantic_source import capture_semantic_advisor_source
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.sessions.composer_finalization import _message_with_state_response
from elspeth.web.sessions.composer_operations import ComposerOperationAssistantWrite, ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.continuation_semantic_actions import (
    install_semantic_action_on_connection,
    require_semantic_publication_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import (
    consume_semantic_advisor_whole_stop_on_connection,
    read_semantic_advisor_retained_inputs_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.routes._helpers import composer_turn_end_assistant_row
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.sessions import test_continuation_end_shared_execution as shared_fixture
from tests.unit.web.sessions import test_continuation_semantic_advisor as semantic_fixture

operation_store = shared_fixture.operation_store


@asynccontextmanager
async def actual_semantic_publication_case(store, tmp_path, monkeypatch):
    async with shared_fixture.actual_shared_end_case(store, tmp_path, monkeypatch) as case:
        running, execution = case["running"], case["execution"]
        assert type(execution) is EndCheckpointCompleteReview
        review = execution.review
        service, authority = case["phases"].service, case["phases"].authority
        sid, operation = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(store[0], sid) as conn:
            reviewed = load_snapshot(conn, session_id=sid, operation_id=operation)
            assert reviewed == review.completed
            retained_inputs = read_semantic_advisor_retained_inputs_on_connection(conn, running, selected=reviewed)
            assert retained_inputs.review is not None
            assert retained_inputs.review.owner_effect_key == review.owner_effect_key
            assert retained_inputs.review.completed_checkpoint_id == reviewed.checkpoint_id
        selected = await load_selected_continuation(authority=authority, running=running, selected=reviewed, required_work=case["work"])
        inventory = await materialize_continuation_env_inventory(
            advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=case["work"]
        )
        runtime = await restore_continuation_runtime(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=case["work"],
            provider_owner=case["provider"],
            progress=case["phases"].progress,
        )
        invocation = make_semantic_advisor_invocation(capture_semantic_advisor_source(runtime))
        request = prepare_semantic_advisor_request(
            owner=service._advisor_checkpoint,
            provider_owner=case["provider"],
            source=invocation.invocation,
            frontier=invocation.frontier,
            state=runtime.live.state,
            content_scope=ComposerContentScope(running.claim.session_id, composition_content_hash(runtime.live.state)),
            user_message=runtime.bindings.message,
            runtime=runtime,
            env_ref_names=runtime.env_ref_names,
            retained_inputs=retained_inputs,
        )
        original_request = next(item for item in request.frontier.obligations if item.obligation_id == "request")
        answer = semantic_fixture._answer(request)
        answer.update(
            decision="STOP_CONFIRMED",
            reason="The real reviewed result completes the original request.",
            remaining_actionable_ids=[],
            prescribed_action=None,
            obligations=[{**asdict(original_request), "status": "satisfied"}],
        )
        stop_sdk = []
        guard_inputs = []
        guard_part_lengths = []
        azure_requests = []
        original_request_builder = original_guard._request
        guard_mount = request.prepared.mount
        assert guard_mount is not None
        guard_client = guard_mount.client
        assert type(guard_client) is AzurePromptGuardClient
        transport = guard_client._http_transport
        assert type(transport) is httpx.MockTransport
        original_http_request = transport.handle_async_request

        async def count_actual_azure_request(actual_request):
            azure_requests.append(1)
            return await original_http_request(actual_request)

        def record_actual_guard_request(**kwargs):
            guarded, supported = original_request_builder(**kwargs)
            guard_inputs.append(
                (guarded.role, supported, len(guarded.user_prompt), len(guarded.documents), max(map(len, guarded.documents), default=0))
            )
            partitions = kwargs["content_parts"]
            message_two = partitions[2] if partitions is not None and len(partitions) > 2 else None
            parts = () if message_two is None else message_two
            lengths = sorted(
                (
                    (
                        index,
                        len(part.text if part.inspection_text is None else part.inspection_text),
                        len(f"Message 2, content part {index}:\n")
                        + len(part.text if part.inspection_text is None else part.inspection_text),
                        part.trust is None,
                    )
                    for index, part in enumerate(parts)
                ),
                key=lambda item: item[2],
                reverse=True,
            )
            guard_part_lengths.append((tuple(sorted(kwargs["trusted_message_indexes"])), len(parts), lengths[:5]))
            return guarded, supported

        async def complete_stop(**kwargs):
            stop_sdk.append(kwargs)
            return semantic_fixture._reply(json.dumps(answer))

        monkeypatch.setattr(litellm, "acompletion", complete_stop)
        monkeypatch.setattr(original_guard, "_request", record_actual_guard_request)
        monkeypatch.setattr(transport, "handle_async_request", count_actual_azure_request)
        stop_case = {
            **case,
            "lease": case["phases"].lease,
            "runtime": runtime,
            "recorder": runtime.live.recorder,
            "invocation": invocation,
            "request": request,
            "clock": ContinuationActionClock(runtime.source.policy.actions.advisor),
        }
        try:
            prepared, paid = await semantic_fixture._run_paid_semantic(stop_case, monkeypatch)
        except Exception as error:
            # A real pre-SDK failure is diagnostic evidence, never an original
            # paid outcome. Preserve it instead of masking it with the paid
            # retention owner's provenance refusal below.
            error.add_note(f"actual Stop SDK entries before original failure: {len(stop_sdk)}")
            if type(error) is PromptGuardRefused:
                try:
                    checks = [check for check in _checks(store[0]) if check["audit_id"] == error.audit_id]
                except Exception as probe_error:
                    checks = []
                    error.add_note(f"actual Stop guard audit lookup failed: {type(probe_error).__name__}")
                error.add_note(
                    f"actual Stop guard refusal: disposition={error.disposition}; "
                    f"http_status={error.http_status}; audit_id={error.audit_id}; "
                    f"matching_audit_checks={len(checks)}"
                )
                error.add_note(
                    "actual Stop guard audit (reason, role, mode, adapter, request_hash, disposition): "
                    f"{[(check['reason'], check['role'], check['mode'], check['adapter'], check['request_hash'], check['disposition']) for check in checks]}"
                )
                error.add_note(
                    "actual guard inputs (role, supported, user characters, documents, largest document characters): "
                    f"{guard_inputs}; message 2 (trusted indexes, part count, top part index/inspection characters/prefixed unit characters/untrusted): "
                    f"{guard_part_lengths}; Azure mock requests: {len(azure_requests)}"
                )
            raise
        with locked_session_transaction(store[0], sid) as conn:
            installed = install_semantic_action_on_connection(conn, running, selected=paid.snapshot, effect_key=prepared.binding.effect_key)
            consumed = consume_semantic_advisor_whole_stop_on_connection(
                conn, running, selected=installed, effect_key=prepared.binding.effect_key
            )
            proof = require_semantic_publication_on_connection(conn, running, selected=consumed)
            assert proof is not None
        assert len(stop_sdk) == 1
        original = proof.review_result.result
        result = replace(original, llm_calls=(*original.llm_calls, proof.admission.paid.audit_call))
        yield {**case, "runtime": runtime, "publication": consumed, "publication_proof": proof, "publication_result": result}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "result", "assistant", "missing_result", "public_stop"))
async def test_actual_whole_stop_binds_terminal_result_and_rolls_back_substitution(operation_store, tmp_path, monkeypatch, change):
    async with actual_semantic_publication_case(operation_store, tmp_path, monkeypatch) as case:
        running, phases = case["running"], case["phases"]
        service = phases.service._require_sessions_service()
        original = case["publication_result"]
        actual = replace(original, message="A different result was substituted.") if change == "result" else original
        draft = composer_turn_end_assistant_row(actual)
        if change == "public_stop":
            stopped = phases.authority.request_owner_cancel(
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                actor_user_id=case["runtime"].bindings.user_id,
                auth_provider_type="local",
            )
            assert stopped is not None and stopped.cancel_requested_at is not None
        with operation_store[0].connect() as conn:
            before = semantic_fixture._rows_on_connection(conn)
            assistants_before = tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant")))

        async def finish():
            return await service.complete_composer_async_operation(
                running,
                assistant=ComposerOperationAssistantWrite(
                    message_id=uuid4(),
                    content="Foreign assistant words." if change == "assistant" else draft.content,
                    raw_content=draft.raw_content,
                    composition_state_id=None if original.final_persisted_state_id is None else UUID(original.final_persisted_state_id),
                ),
                assistant_record=None,
                audit_cohort=(),
                audit_composition_state_id=None,
                required_work=case["work"].coordinator,
                provider_audit_calls=original.llm_calls,
                publication_result=None if change == "missing_result" else actual,
                build_response=partial(_message_with_state_response, state_response=None, live_validation=None),
            )

        if change == "none":
            terminal = await finish()
            assert terminal.status == "completed" and terminal.result.message.content == draft.content
        else:
            with pytest.raises((AuditIntegrityError, ComposerOperationCancelledDuringTurn)):
                await finish()
            with operation_store[0].connect() as conn:
                assert semantic_fixture._rows_on_connection(conn) == before
                assert tuple(conn.execute(select(messages.c.id).where(messages.c.role == "assistant"))) == assistants_before
