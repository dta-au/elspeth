"""Actual V4 preview phase, original producer and atomic P4 cold replay."""

import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY
from elspeth.web.composer import continuation_preview_dispatch as preview
from elspeth.web.composer import continuation_runtime
from elspeth.web.composer import continuation_value_batch_preparation as value_preparation
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_message_family_builders import capture_tool_result_recipe
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions import continuation_value_batch_publication as value_publication
from elspeth.web.sessions import continuation_value_batch_tail as value_tail
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_p4 import decode_p4_receipt
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_validation_terminal_holds_table as holds
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case

operation_store = phases.operation_store


@asynccontextmanager
async def actual_preview_case(store, tmp_path, monkeypatch):
    actual_work = initialization_fixture.inventory_work

    def timed_work(running):
        stored = store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(stored.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        sampled_at = time.monotonic()
        with store[0].connect() as conn:
            observed_at = database_now(conn)
            deadline = conn.execute(
                select(segments.c.deadline_at).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).scalar_one()
        assert deadline is not None
        remaining = (restore_utc(deadline) - observed_at).total_seconds()
        assert remaining > 0
        clock_owner = ContinuationSQLDeadlineOwner(
            running,
            policy=policy,
            drain_signal=ComposerDrainSignal(),
            drain_seconds=continuation_test_settings().composer_async_drain_seconds,
            hard_expires_at=sampled_at + remaining,
        )
        return actual_work(running, sql_deadline_owner=clock_owner)

    monkeypatch.setattr(initialization_fixture, "inventory_work", timed_work)
    async with _v4_tool_case(store, tmp_path, monkeypatch, tool="preview_pipeline", arguments={}) as case:
        current_store, owner, running, sdk = case
        clock_owner = owner.work.coordinator.sql_deadline_owner
        assert clock_owner is not None
        action = max(
            (
                clock_owner.policy.actions.read,
                clock_owner.policy.actions.session_sql,
                clock_owner.policy.actions.blob,
                clock_owner.policy.actions.external,
            ),
            key=lambda value: value.total,
        )
        clock = ContinuationActionClock(action)
        clock_owner.begin_action(action, started_at=clock.started_monotonic)
        owner.bind_action_clock(clock)
        yield current_store, owner, running, sdk


@pytest.mark.asyncio
async def test_actual_preview_phase_issues_once_retains_original_result_and_cold_never_reexecutes(operation_store, tmp_path, monkeypatch):
    async with actual_preview_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        actual = preview.execute_tool
        actual_bind = preview.bind_issued_preview_dispatch_on_connection
        actual_adopt = preview.adopt_preview_issued_message_source
        actual_register = CurrentContinuationMessageProducers.register_tool_result_message
        actual_verify = CurrentContinuationMessageProducers.register_issued_preview_result_message
        actual_lock = preview.locked_session_transaction
        actual_plan = value_preparation.prepare_issued_preview_batch
        actual_shape = value_publication.value_batch_publication_shape
        actual_preview_p4 = value_tail.prepare_issued_preview_p4_result_tail
        actual_graph_p4 = value_tail.prepare_p4_result_tail
        bound = []
        originals = []
        registrations = []
        verified = []
        active_preview_transactions = set()
        planned = []
        publication_sources = []
        nongraph_results = []
        original_admitted_results = []

        def observed_preview_p4(*, selected, receipt, publication, published_at, env_ref_names):
            assert publication.issued_preview is not None
            assert receipt.checkpoint_disposition in ("empty", "no_new_checkpoint")
            assert receipt.authored_state_hash is None and not receipt.inserted_state_ids
            arguments = {"selected": selected, "receipt": receipt, "published_at": published_at, "env_ref_names": env_ref_names}
            with pytest.raises(AuditIntegrityError, match="requires its ordinary successful publication"):
                actual_graph_p4(**arguments)
            with pytest.raises(AuditIntegrityError, match="requires its original issued preview publication"):
                actual_preview_p4(**arguments, publication=replace(publication, issued_preview=None))
            changed_message = replace(publication.messages[0], content=publication.messages[0].content + " changed")
            with pytest.raises(AuditIntegrityError, match="publication differs from its original source"):
                actual_preview_p4(**arguments, publication=replace(publication, messages=(changed_message, *publication.messages[1:])))
            result = actual_preview_p4(**arguments, publication=publication)
            assert result.result.historical_effect is None
            assert result.result.preview_publication is publication
            nongraph_results.append(result.result)
            return result

        def observed_shape(batch, *, locked_refusal, env_ref_names, issued_preview=None):
            assert issued_preview is not None
            with pytest.raises(AuditIntegrityError, match="one original Compose frame and ready batch"):
                actual_shape(batch, locked_refusal=locked_refusal, env_ref_names=env_ref_names)
            with pytest.raises(AuditIntegrityError, match="original complete preparation"):
                actual_shape(replace(batch), locked_refusal=locked_refusal, env_ref_names=env_ref_names, issued_preview=issued_preview)
            assert batch.selected.stack.frames[-1].phase == "effect_reconciling"
            publication_sources.append(batch.selected.checkpoint_id)
            return actual_shape(batch, locked_refusal=locked_refusal, env_ref_names=env_ref_names, issued_preview=issued_preview)

        @contextmanager
        def observed_lock(*args, **kwargs):
            with actual_lock(*args, **kwargs) as conn:
                thread = threading.get_ident()
                assert thread not in active_preview_transactions
                active_preview_transactions.add(thread)
                try:
                    yield conn
                finally:
                    active_preview_transactions.remove(thread)

        def observed_plan(*args, **kwargs):
            assert threading.get_ident() not in active_preview_transactions, "preview tail capture crossed its atomic result writer"
            planned.append(threading.get_ident())
            return actual_plan(*args, **kwargs)

        def observe_bind(conn, issuing, **kwargs):
            prepared = kwargs["prepared"]
            current = owner.runtime
            assert current is not None
            assert prepared.runtime.source.selected is prepared.runtime.materialization.selected
            assert prepared.context.message_producers is prepared.runtime.materialization
            assert prepared.runtime.source.selected != current.source.selected
            assert prepared.runtime.materialization is not current.materialization
            assert prepared.runtime.live is not current.live
            assert prepared.runtime.live.llm_messages is not current.live.llm_messages
            assert prepared.runtime.live.recorder is not current.live.recorder
            assert prepared.runtime.live.runtime_preflight_cache is not current.live.runtime_preflight_cache
            permit = actual_bind(conn, issuing, **kwargs)
            bound.append((issuing, kwargs))
            return permit

        def observed(*args, **kwargs):
            with store[0].connect() as conn:
                row = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
                effect = conn.execute(
                    select(effects).where(
                        effects.c.session_id == str(store[4]),
                        effects.c.operation_id == running.claim.operation_id,
                        effects.c.effect_key == row.effect_key,
                    )
                ).one()
            assert row.state == "active" and effect.disposition == "dispatched"
            assert effect.response_finished_at is None and effect.result_chunk_key is None and effect.provider_attempt_id is None
            assert len(bound) == 1
            issuing, original_kwargs = bound[0]
            with (
                locked_session_transaction(store[0], str(store[4])) as conn,
                pytest.raises(AuditIntegrityError, match="cannot bind its original issued source twice"),
            ):
                actual_bind(conn, issuing, **original_kwargs)
            result = actual(*args, **kwargs)
            originals.append(result)
            return result

        def observed_adopt(permit, *, prepared):
            producer = prepared.runtime.materialization
            assert producer.selected is prepared.runtime.source.selected
            actual_adopt(permit, prepared=prepared)
            issued = preview.original_preview_issued_message_source(permit, prepared=prepared)
            assert producer.selected is issued.selected
            with pytest.raises(AuditIntegrityError, match="once-only returned handoff"):
                actual_adopt(permit, prepared=prepared)
            copied = replace(issued.selected)
            producer.selected = copied
            try:
                with pytest.raises(AuditIntegrityError, match="changed its actual adopted issued selection"):
                    preview.original_preview_issued_message_source(permit, prepared=prepared)
            finally:
                producer.selected = issued.selected

        def observed_register(producer, **kwargs):
            prepared = bound[0][1]["prepared"]
            if producer is not prepared.runtime.materialization:
                return actual_register(producer, **kwargs)
            current = owner.runtime
            assert current is not None
            assert producer is prepared.runtime.materialization and producer is not current.materialization
            assert producer.selected is current.source.selected
            assert prepared.runtime.source.selected.stack.cursor + 1 == producer.selected.stack.cursor
            assert len(originals) == 1 and kwargs["result"].result is originals[0]
            assert kwargs["message"]["content"] is kwargs["serialized"]
            registrations.append(kwargs["message"])
            return actual_register(producer, **kwargs)

        def observed_verify(producer, *, prepared, permit):
            message = prepared.runtime.live.llm_messages[-1]
            assert message is registrations[0]
            serialized = message["content"]
            assert type(serialized) is str
            copied = serialized.encode("utf-8").decode("utf-8")
            assert copied == serialized and copied is not serialized
            admitted = preview.original_adopted_preview_result(permit, prepared=prepared)
            original_admitted_results.append(admitted.to_tool_result())
            arguments = {
                "response": prepared.runtime.source.selected.stack.frames[-1].response,
                "completion": prepared.call_model.completion,
                "tool_index": prepared.call.index,
                "call": prepared.call.tool_call,
                "result": admitted,
            }
            with pytest.raises(AuditIntegrityError, match="complete original wire or trust partition"):
                capture_tool_result_recipe(**arguments, serialized=copied, message=message)
            parts = message[PROMPT_GUARD_CONTENT_PARTS_KEY]
            assert parts
            changed = dict(message)
            changed[PROMPT_GUARD_CONTENT_PARTS_KEY] = (replace(parts[0], text=parts[0].text + " changed"), *parts[1:])
            with pytest.raises(AuditIntegrityError, match="complete original wire or trust partition"):
                capture_tool_result_recipe(**arguments, serialized=serialized, message=changed)
            actual_verify(producer, prepared=prepared, permit=permit)
            verified.append(message)

        monkeypatch.setattr(preview, "execute_tool", observed)
        monkeypatch.setattr(preview, "bind_issued_preview_dispatch_on_connection", observe_bind)
        monkeypatch.setattr(preview, "adopt_preview_issued_message_source", observed_adopt)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_tool_result_message", observed_register)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_issued_preview_result_message", observed_verify)
        monkeypatch.setattr(preview, "locked_session_transaction", observed_lock)
        monkeypatch.setattr(value_preparation, "prepare_issued_preview_batch", observed_plan)
        monkeypatch.setattr(value_publication, "value_batch_publication_shape", observed_shape)
        monkeypatch.setattr(value_tail, "prepare_issued_preview_p4_result_tail", observed_preview_p4)
        with (
            observed_lock(store[0], str(store[4])),
            pytest.raises(AssertionError, match="preview tail capture crossed its atomic result writer"),
        ):
            observed_plan()
        assert not planned and not active_preview_transactions
        await phases._advance(owner)
        assert len(originals) == len(registrations) == len(verified) == len(planned) == 1 and originals[0].success
        assert publication_sources and len(nongraph_results) == 1
        assert originals[0].runtime_preflight is not None
        assert originals[0].runtime_preflight.errors[0].error_code == "empty_pipeline"
        stored = owner.latest_snapshot
        assert stored is not None and owner.preview_terminal is None
        assert owner.runtime is owner.primary is owner.p4 is owner.finished_batch is None
        with store[0].connect() as conn:
            hold = conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).one()
            effect = conn.execute(select(effects).where(effects.c.effect_key == hold.effect_key)).one()
        assert hold.state == "consumed" and effect.disposition == "committed"
        assert effect.receipt_key is not None and effect.result_chunk_key is not None and effect.provider_attempt_id is None

        def forbidden(*args, **kwargs):
            raise AssertionError("cold preview reentered its producer")

        monkeypatch.setattr(preview, "execute_tool", forbidden)
        cold = ContinuationLivePhases(
            owner.service,
            owner.authority,
            owner.work,
            ProviderInvocationOwner(service=store[3], required_work=owner.work),
            owner.lease,
        )
        selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
        assert is_applied_batch_cut(selected)
        before = stored_rows(store[0])
        restored, _inventory = await cold.current(selected)
        assert restored.graph.batch is not None
        assert restored.graph.batch.batch.tool_outcomes[0].response == original_admitted_results[0]
        encoded_receipt = restored.graph.batch.batch.p4_receipt_json
        assert encoded_receipt is not None
        receipt, outcome = decode_p4_receipt(encoded_receipt, env_ref_names=restored.env_ref_names)
        assert receipt.checkpoint_disposition in ("empty", "no_new_checkpoint") and outcome.checkpoint is None
        assert receipt.authored_state_hash is None and not receipt.inserted_state_ids
        with store[0].connect() as conn:
            p4 = conn.execute(
                select(effects).where(
                    effects.c.session_id == str(store[4]),
                    effects.c.operation_id == running.claim.operation_id,
                    effects.c.effect_key == receipt.effect_key,
                )
            ).one()
        assert p4.disposition == "committed" and p4.receipt_key == str(receipt.assistant_id)
        assert p4.graph_sha256 is None and p4.committed_state_ids_json is None
        assert stored_rows(store[0]) == before and len(sdk) == 1 and len(originals) == 1


@pytest.mark.asyncio
async def test_actual_preview_refuses_borrowed_live_message_producer_before_issue(operation_store, tmp_path, monkeypatch):
    async with actual_preview_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        selected, inventory = await phases._finish_inventory(owner, running)
        runtime, inventory = await owner.current(selected)
        restore = continuation_runtime.restore_metadata_preparation_runtime
        returned = []

        async def borrowed_producer(**kwargs):
            detached = await restore(**kwargs)
            assert detached.materialization is not runtime.materialization
            changed = replace(detached, materialization=runtime.materialization)
            returned.append(changed)
            return changed

        def forbidden(*args, **kwargs):
            raise AssertionError("borrowed live message producer reached preview execution")

        monkeypatch.setattr(continuation_runtime, "restore_metadata_preparation_runtime", borrowed_producer)
        monkeypatch.setattr(preview, "execute_tool", forbidden)
        before = stored_rows(store[0])
        with pytest.raises(AuditIntegrityError, match="reused or changed an original mutable owner"):
            await preview.try_admit_preview_tool(owner, runtime=runtime, selected=selected, inventory=inventory)
        assert len(returned) == 1 and returned[0].materialization is runtime.materialization
        assert stored_rows(store[0]) == before and len(sdk) == 1
        with store[0].connect() as conn:
            assert not conn.execute(select(holds).where(holds.c.session_id == str(store[4]))).all()
