"""Actual V4 preview phase, original producer and atomic P4 cold replay."""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY
from elspeth.web import async_workers
from elspeth.web.composer import continuation_composition_initialization as composition_initialization
from elspeth.web.composer import continuation_initialization as initial_bootstrap
from elspeth.web.composer import continuation_preview_dispatch as preview
from elspeth.web.composer import continuation_runtime
from elspeth.web.composer import continuation_value_batch_preparation as value_preparation
from elspeth.web.composer.composer_preflight import ComposerPreflight
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_message_family_builders import capture_tool_result_recipe
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.execution.schemas import RUNTIME_CHECK_PLUGIN_INSTANTIATION
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.required_executor import RequiredInvocationWitness
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_composition_initialization as composition_initialization_sql
from elspeth.web.sessions import continuation_prepared_tail as prepared_tail
from elspeth.web.sessions import continuation_preview_result as preview_result
from elspeth.web.sessions import continuation_retained_capacity as retained_capacity
from elspeth.web.sessions import continuation_value_batch_publication as value_publication
from elspeth.web.sessions import continuation_value_batch_tail as value_tail
from elspeth.web.sessions.composer_drain import ComposerDrainSignal
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.continuation_next_iteration import is_applied_batch_cut
from elspeth.web.sessions.continuation_p4 import decode_p4_receipt
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as retained_chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as provider_holds
from elspeth.web.sessions.models import composer_segment_attempts_table as segment_attempts
from elspeth.web.sessions.models import composer_validation_terminal_holds_table as holds
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import quota_provider_attempts_table as provider_attempts
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_end_profile import EndPreparationProfile
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.composer.test_prompt_guard_admission import _checks as guard_checks
from tests.unit.web.sessions import test_continuation_composition_initialization as initialization_fixture
from tests.unit.web.sessions import test_continuation_live_phases as phases
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case
from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _PIPELINE_PLUGINS, _uploaded_source
from tests.unit.web.sessions.test_continuation_v4_pipeline_live import _arguments as pipeline_arguments

operation_store = phases.operation_store


class _InitializationPublicationProbe:
    """Observe one actual second-request publication; never replace its work."""

    def __init__(self):
        self.profile = EndPreparationProfile(enabled=True)
        self.operation_id = None
        self.task = None
        self.worker_thread = None
        self.submitted_callable = None
        self.active_initializer = False
        self.facts = {"target_bound": False, "registered_calls": 0}

    def matches(self, operation_id, source, task):
        return (
            self.operation_id is not None
            and operation_id == self.operation_id
            and source is RequiredWorkSource.CONTINUATION_STATE_SQL
            and task is self.task
            and task is not None
        )

    def sync(self, label, original):
        @wraps(original)
        def observed(*args, **kwargs):
            if threading.get_native_id() != self.worker_thread:
                return original(*args, **kwargs)
            with self.profile.span(label, original_callable_identity=id(original)) as row:
                if label == "initialization.sql.put_chunk":
                    row.update(codec=kwargs["codec"], payload_bytes=len(kwargs["payload"]))
                result = original(*args, **kwargs)
                if label.endswith("read_composition_initialization_source_on_connection"):
                    row["initialized"] = result.initialized
                return result

        return observed

    @asynccontextmanager
    async def observe(self, monkeypatch, engine, record_property, directory):
        actual_work = initialization_fixture.inventory_work
        actual_initialize = composition_initialization.initialize_continuation_composition
        actual_stage = ContinuationActionClock.run
        actual_registered = composition_initialization._registered
        actual_finish = initial_bootstrap.run_required_sql_finish_once
        actual_submit = async_workers._submit_shared

        def identify(running, **kwargs):
            current = asyncio.current_task()
            if self.operation_id is None:
                self.operation_id, self.task = running.claim.operation_id, current
                self.facts.update(target_bound=True, operation_id=self.operation_id, task_identity=id(current))
            elif self.operation_id != running.claim.operation_id or self.task is not current:
                self.profile.errors.append("initialization probe changed its second request/task")
            return actual_work(running, **kwargs)

        @wraps(actual_initialize)
        async def initialize(**kwargs):
            work = kwargs["required_work"]
            if not self.matches(work.running.claim.operation_id, RequiredWorkSource.CONTINUATION_STATE_SQL, asyncio.current_task()):
                return await actual_initialize(**kwargs)
            self.active_initializer = True
            try:
                return await actual_initialize(**kwargs)
            finally:
                self.active_initializer = False

        @wraps(actual_stage)
        async def stage(clock, name, operation):
            if not self.active_initializer or asyncio.current_task() is not self.task:
                return await actual_stage(clock, name, operation)
            with self.profile.span(
                "initialization.stage." + name,
                clock_identity=id(clock),
                clock_started=clock.started_monotonic,
                original_deadline=clock.deadline,
                remaining_at_entry=clock.deadline - asyncio.get_running_loop().time(),
                stage_allowance=clock.policy.physical if name == "physical" else clock.policy.retention,
            ):
                return await actual_stage(clock, name, operation)

        @wraps(actual_submit)
        async def submit(callable_, ticket=None, *args, **kwargs):
            if (
                not self.active_initializer
                or ticket is None
                or not self.matches(ticket.key.authority.durable_operation_id, ticket.key.source, asyncio.current_task())
            ):
                return await actual_submit(callable_, ticket, *args, **kwargs)
            self.submitted_callable = callable_
            with self.profile.span("initialization.shared_submit", original_callable_identity=id(callable_), ticket_identity=id(ticket)):
                result = await actual_submit(callable_, ticket, *args, **kwargs)
                self.facts["shared_future_identity"] = id(result)
                return result

        @wraps(actual_finish)
        async def finish(ticket, function, *args, **kwargs):
            if not self.active_initializer or not self.matches(
                ticket.key.authority.durable_operation_id, ticket.key.source, asyncio.current_task()
            ):
                return await actual_finish(ticket, function, *args, **kwargs)
            result = await actual_finish(ticket, function, *args, **kwargs)
            self.facts.update(
                original_finish_callable_identity=id(function),
                outcome_identity=id(result),
                outcome_type=type(result).__name__,
                deferred_cancellation_types=[type(error).__name__ for error in result.deferred_cancellations],
            )
            if type(result) is RequiredSQLReturned:
                self.facts.update(returned_checkpoint_id=result.value.checkpoint_id, returned_cursor=result.value.cursor)
            elif type(result) is RequiredSQLRaised:
                self.facts["physical_error_type"] = type(result.error).__name__
            return result

        @wraps(actual_registered)
        async def registered(*, work, source, operation, expected):
            if not self.active_initializer or not self.matches(work.running.claim.operation_id, source, asyncio.current_task()):
                return await actual_registered(work=work, source=source, operation=operation, expected=expected)
            self.facts["registered_calls"] += 1
            self.facts["controls"] = {
                "positive": self.matches(work.running.claim.operation_id, source, asyncio.current_task()),
                "wrong_task_refused": not self.matches(work.running.claim.operation_id, source, None),
                "wrong_operation_refused": not self.matches(str(uuid4()), source, asyncio.current_task()),
                "wrong_source_refused": not self.matches(
                    work.running.claim.operation_id, RequiredWorkSource.CONTINUATION_READ_SQL, self.task
                ),
            }
            original_invoke = RequiredInvocationWitness.invoke
            with (
                monkeypatch.context() as physical_patch,
                self.profile.sql(engine),
                self.profile.workers(physical_patch),
                monkeypatch.context() as witness_patch,
            ):
                profiled_invoke = RequiredInvocationWitness.invoke

                @wraps(profiled_invoke)
                def invoke(witness, callable_):
                    if callable_ is not self.submitted_callable:
                        return original_invoke(witness, callable_)
                    self.worker_thread = threading.get_native_id()
                    self.facts.update(
                        witness_identity=witness.identity,
                        generation_identity=witness.generation_identity,
                        physical_callable_identity=id(callable_),
                        physical_thread=self.worker_thread,
                        exact_original_callable=True,
                    )
                    try:
                        return profiled_invoke(witness, callable_)
                    finally:
                        snapshot = witness.snapshot()
                        self.facts["physical_witness"] = {
                            "gate": snapshot.gate.value,
                            "entered": snapshot.entered,
                            "callable_started": snapshot.callable_started,
                            "callable_finished": snapshot.callable_finished,
                            "exited": snapshot.exited,
                        }
                        self.worker_thread = None

                witness_patch.setattr(RequiredInvocationWitness, "invoke", invoke)
                for name, original in (
                    (
                        "read_composition_initialization_source_on_connection",
                        composition_initialization_sql.read_composition_initialization_source_on_connection,
                    ),
                    ("capture_initial_composition", composition_initialization_sql.capture_initial_composition),
                    ("require_private_chunk_closure", composition_initialization_sql.require_private_chunk_closure),
                    ("put_chunk", composition_initialization_sql.put_chunk),
                    ("append_complete_snapshot", composition_initialization_sql.append_complete_snapshot),
                    (
                        "load_selected_private_chunks_on_connection",
                        composition_initialization_sql.load_selected_private_chunks_on_connection,
                    ),
                    ("_require_first_values", composition_initialization_sql._require_first_values),
                ):
                    physical_patch.setattr(composition_initialization_sql, name, self.sync("initialization.sql." + name, original))
                with self.profile.span(
                    "initialization.registered", original_callable_identity=id(operation), expected_type=expected.__name__
                ):
                    return await actual_registered(work=work, source=source, operation=operation, expected=expected)

        with monkeypatch.context() as owned:
            owned.setattr(initialization_fixture, "inventory_work", identify)
            owned.setattr(composition_initialization, "initialize_continuation_composition", initialize)
            owned.setattr(composition_initialization, "_registered", registered)
            owned.setattr(initial_bootstrap, "run_required_sql_finish_once", finish)
            owned.setattr(ContinuationActionClock, "run", stage)
            owned.setattr(async_workers, "_submit_shared", submit)
            original_error = None
            try:
                yield self
            except BaseException as original:
                original_error = original
                self.profile.case_outcome = {"status": "raised", "error": type(original).__name__}
                raise
            else:
                self.profile.case_outcome = {"status": "returned"}
            finally:
                try:
                    report = {"target": self.facts, "timing": self.profile.snapshot()}
                    encoded = json.dumps(report, sort_keys=True)
                    record_property("preview_initialization_publication", encoded)
                    (directory / "preview-initialization-publication.json").write_text(encoded + "\n", encoding="utf-8")
                except Exception as diagnostic_error:
                    if original_error is None:
                        raise
                    original_error.add_note("preview initialization observation failed: " + type(diagnostic_error).__name__)


@asynccontextmanager
async def actual_preview_case(
    store,
    tmp_path,
    monkeypatch,
    *,
    base_state_data: CompositionStateData | None = None,
    available_plugins: frozenset[PluginId] | None = None,
    prior_operation_id: str | None = None,
):
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
    prepared_case = (
        _v4_tool_case(store, tmp_path, monkeypatch, tool="preview_pipeline", arguments={})
        if base_state_data is None and available_plugins is None and prior_operation_id is None
        else _v4_tool_case(
            store,
            tmp_path,
            monkeypatch,
            tool="preview_pipeline",
            arguments={},
            base_state_data=base_state_data,
            available_plugins=available_plugins,
            prior_operation_id=prior_operation_id,
        )
    )
    async with prepared_case as case:
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


@pytest.mark.asyncio
async def test_actual_nonempty_preview_preserves_provider_authored_base_and_cold_result(
    operation_store, tmp_path, monkeypatch, record_property
):
    blob = await _uploaded_source(operation_store)
    output_path = Path(operation_store[3]._data_dir) / "outputs" / str(operation_store[4]) / "original-model-output.json"
    arguments = pipeline_arguments(blob, output_path)
    with monkeypatch.context() as initial_patch:
        async with _v4_tool_case(
            operation_store, tmp_path, initial_patch, tool="set_pipeline", arguments=arguments, available_plugins=_PIPELINE_PLUGINS
        ) as (store, author, author_running, author_sdk):
            await phases._advance(author)
            assert author.latest_snapshot is not None and author.runtime is None and len(author_sdk) == 1
            with store[0].connect() as conn:
                row = conn.execute(
                    select(states).where(states.c.session_id == str(store[4])).order_by(states.c.version.desc()).limit(1)
                ).one()
            original_state = store[3]._row_to_state_record(row)
            assert original_state.sources is not None and original_state.outputs
            assert original_state.sources["source"]["options"]["blob_ref"] == str(blob.id)
            base = CompositionStateData(
                sources=original_state.sources,
                nodes=original_state.nodes,
                edges=original_state.edges,
                outputs=original_state.outputs,
                metadata_=original_state.metadata_,
                is_valid=original_state.is_valid,
                validation_errors=original_state.validation_errors,
                composer_meta=original_state.composer_meta,
            )
            before_unsealed = stored_rows(store[0])
            with pytest.raises(ValueError, match="actual same-session sealed Stop"):
                await initialization_fixture.bootstrap_case(
                    store, base_state_data=base, prior_operation_id=author_running.claim.operation_id
                )
            assert stored_rows(store[0]) == before_unsealed
            cancelled = author.authority.request_cancel(session_id=store[4], operation_id=author_running.claim.operation_id)
            assert cancelled is not None and cancelled.cancel_requested_at is not None
            terminal = author.authority.settle_continuation_owner(
                author_running, outcome=request_cancelled_error(request_id=cancelled.request_id)
            )
            assert terminal.status == "failed" and terminal.failure_code == "request_cancelled"
            first_context = author_running.session_operation_context
            first_guards = guard_checks(store[0])
            assert len(first_guards) == 1
            assert (first_guards[0]["operation_id"], first_guards[0]["operation_epoch"]) == (
                first_context.fence.operation_id,
                first_context.fence.operation_epoch,
            )
            with store[0].connect() as conn:
                first_segment = conn.execute(
                    select(segment_attempts).where(
                        segment_attempts.c.session_id == str(store[4]),
                        segment_attempts.c.operation_id == terminal.operation_id,
                        segment_attempts.c.sequence == author_running.claim.segment_sequence,
                        segment_attempts.c.generation == author_running.claim.ownership_generation,
                    )
                ).one()
            assert (first_segment.session_operation_id, first_segment.session_operation_epoch) == (
                first_context.fence.operation_id,
                first_context.fence.operation_epoch,
            )
        assert author.lease.closed

    foreign = operation_store[1].create_session_with_initial_fence(
        user_id="alice", title="Actual foreign fixture", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    foreign_store = (*operation_store[:4], foreign.id)
    before_refusal = stored_rows(operation_store[0])
    with pytest.raises(ValueError, match="actual same-session sealed Stop"):
        await initialization_fixture.bootstrap_case(operation_store, base_state_data=base, prior_operation_id=str(uuid4()))
    with pytest.raises(ValueError, match="actual same-session sealed Stop"):
        await initialization_fixture.bootstrap_case(foreign_store, base_state_data=base, prior_operation_id=terminal.operation_id)
    with pytest.raises(ValueError, match="base DTO differs from its actual saved state"):
        await initialization_fixture.bootstrap_case(
            operation_store,
            base_state_data=replace(base, metadata_={**base.metadata_, "description": "changed caller base"}),
            prior_operation_id=terminal.operation_id,
        )
    assert stored_rows(operation_store[0]) == before_refusal

    probe = _InitializationPublicationProbe()
    async with (
        probe.observe(monkeypatch, operation_store[0], record_property, tmp_path),
        actual_preview_case(
            operation_store,
            tmp_path,
            monkeypatch,
            base_state_data=base,
            available_plugins=_PIPELINE_PLUGINS,
            prior_operation_id=terminal.operation_id,
        ) as (store, owner, running, sdk),
    ):
        all_guards = guard_checks(store[0])
        assert len(all_guards) == 2 and all_guards[0] == first_guards[0]
        current_context = running.session_operation_context
        assert current_context.fence.operation_id != first_context.fence.operation_id
        assert (all_guards[1]["operation_id"], all_guards[1]["operation_epoch"]) == (
            current_context.fence.operation_id,
            current_context.fence.operation_epoch,
        )
        with store[0].connect() as conn:
            physical = conn.execute(
                select(provider_attempts).where(provider_attempts.c.session_id == str(store[4]), provider_attempts.c.source == "composer")
            ).all()
        assert len(physical) == 2
        assert {(attempt.operation_id, attempt.operation_epoch) for attempt in physical} == {
            (first_context.fence.operation_id, first_context.fence.operation_epoch),
            (current_context.fence.operation_id, current_context.fence.operation_epoch),
        }
        assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in physical)
        runtime = owner.runtime
        assert runtime is not None and runtime.live.state.sources and runtime.live.state.outputs
        assert runtime.live.state.sources["source"].options["blob_ref"] == str(blob.id)
        assert runtime.live.state.outputs[0].options["path"] == str(output_path)
        before_state = runtime.live.state.to_dict()
        with store[0].connect() as conn:
            before_rows = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
        actual_preflight = ComposerPreflight.cached_runtime_preflight
        actual_tool = preview.execute_tool
        actual_tail = value_preparation.prepare_issued_preview_batch
        preflights = []
        originals = []
        adopted_preflights = []
        capacity_records = []

        async def observe_preflight(preflight, state, **kwargs):
            result = await actual_preflight(preflight, state, **kwargs)
            preflights.append(result)
            return result

        def observe_tool(*args, **kwargs):
            assert args[0] == "preview_pipeline" and args[2].sources and args[2].outputs
            with store[0].connect() as conn:
                hold = conn.execute(
                    select(holds).where(holds.c.session_id == str(store[4]), holds.c.operation_id == running.claim.operation_id)
                ).one()
            assert hold.state == "active"
            result = actual_tool(*args, **kwargs)
            assert result.updated_state is args[2]
            originals.append(result)
            return result

        def observe_tail(*args, **kwargs):
            prepared = kwargs["prepared"]
            assert len(originals) == 1
            verdict = originals[0].runtime_preflight
            assert verdict is not None and prepared.runtime.live.last_runtime_preflight is verdict
            assert prepared.context.runtime_preflight_cache is prepared.runtime.live.runtime_preflight_cache
            adopted_preflights.append(verdict)
            return actual_tail(*args, **kwargs)

        def measure_capacity(original, kind):
            @wraps(original)
            def measured(conn, *, session_id, operation_id, prospective, retained_payload_limit_bytes, **kwargs):
                if (session_id, operation_id) == (str(store[4]), running.claim.operation_id):
                    assert conn.dialect.name == "sqlite"
                    stored = conn.execute(
                        select(retained_chunks.c.chunk_key, func.length(retained_chunks.c.payload)).where(
                            retained_chunks.c.session_id == session_id, retained_chunks.c.operation_id == operation_id
                        )
                    ).all()
                    existing = {row[0] for row in stored}
                    candidates = {}
                    for candidate in prospective:
                        assert candidate.key not in candidates or candidates[candidate.key] == candidate
                        candidates[candidate.key] = candidate
                    held = 0
                    for table in (holds, provider_holds):
                        held += conn.execute(
                            select(func.coalesce(func.sum(table.c.reserved_bytes), 0)).where(
                                table.c.session_id == session_id, table.c.operation_id == operation_id, table.c.state == "active"
                            )
                        ).scalar_one()
                    used = sum(row[1] for row in stored)
                    added = sum(len(candidate.payload) for key, candidate in candidates.items() if key not in existing)
                    credit = preview_result.preview_returned_credit_on_connection(conn, session_id=session_id, operation_id=operation_id)
                    new_hold = kwargs.get("_new_hold_bytes", 0)
                    capacity_records.append(
                        {
                            "kind": kind,
                            "used": used,
                            "live_holds": held,
                            "source_credit": credit,
                            "new_hold": new_hold,
                            "added": added,
                            "observed_bytes": used + held - credit + new_hold + added,
                            "limit_bytes": retained_payload_limit_bytes,
                        }
                    )
                return original(
                    conn,
                    session_id=session_id,
                    operation_id=operation_id,
                    prospective=prospective,
                    retained_payload_limit_bytes=retained_payload_limit_bytes,
                    **kwargs,
                )

            return measured

        monkeypatch.setattr(ComposerPreflight, "cached_runtime_preflight", observe_preflight)
        monkeypatch.setattr(preview, "execute_tool", observe_tool)
        monkeypatch.setattr(value_preparation, "prepare_issued_preview_batch", observe_tail)
        with monkeypatch.context() as capacity_patch:
            capacity_patch.setattr(
                retained_capacity,
                "admit_retained_payload_chunks",
                measure_capacity(retained_capacity.admit_retained_payload_chunks, "preview_issue_or_write"),
            )
            capacity_patch.setattr(
                prepared_tail,
                "admit_retained_payload_chunks",
                measure_capacity(prepared_tail.admit_retained_payload_chunks, "complete_tail_prefix"),
            )
            try:
                await phases._advance(owner)
            finally:
                record_property("preview_actual_tail_capacity", json.dumps(capacity_records, sort_keys=True))
        assert any(row["new_hold"] > 0 for row in capacity_records)
        assert any(row["kind"] == "complete_tail_prefix" for row in capacity_records)
        assert len(originals) == 1 and originals[0].success
        verdict = originals[0].runtime_preflight
        assert verdict is not None and any(verdict is result for result in preflights)
        assert len(adopted_preflights) == 1 and adopted_preflights[0] is verdict
        assert all(error.error_code != "empty_pipeline" for error in verdict.errors)
        assert any(check.name == RUNTIME_CHECK_PLUGIN_INSTANTIATION for check in verdict.checks)
        assert originals[0].updated_state.to_dict() == before_state
        assert owner.preview_terminal is None and owner.latest_snapshot is not None
        with store[0].connect() as conn:
            after_rows = conn.execute(select(states).where(states.c.session_id == str(store[4]))).all()
            hold = conn.execute(
                select(holds).where(holds.c.session_id == str(store[4]), holds.c.operation_id == running.claim.operation_id)
            ).one()
        assert after_rows == before_rows and hold.state == "consumed"
        original_admitted = admit_discovery_result("preview_pipeline", originals[0]).to_tool_result()

        def forbidden(*args, **kwargs):
            raise AssertionError("cold nonempty preview reentered an original producer")

        monkeypatch.setattr(preview, "execute_tool", forbidden)
        monkeypatch.setattr(ComposerPreflight, "cached_runtime_preflight", forbidden)
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        assert is_applied_batch_cut(selected)
        cold = ContinuationLivePhases(
            owner.service, owner.authority, owner.work, ProviderInvocationOwner(service=store[3], required_work=owner.work), owner.lease
        )
        before_cold = stored_rows(store[0])
        restored, _inventory = await cold.current(selected)
        assert restored.live.state.to_dict() == before_state and restored.graph.batch is not None
        result = restored.graph.batch.batch.tool_outcomes[0].response
        assert result == original_admitted and result.runtime_preflight == verdict
        # Cold caches retain original DATA, never a current validation grant.
        history = restored.live.continuation_cache_history
        assert history is not None and history.last_preflight_json == canonical_json(verdict.model_dump(mode="json"))
        assert history.last_preflight_identity is not None and history.needs_current_preflight is True
        assert (history.capture_creators[-1].operation_id, history.capture_creators[-1].operation_epoch) == (
            current_context.fence.operation_id,
            current_context.fence.operation_epoch,
        )
        assert restored.live.last_runtime_preflight is None and restored.live.runtime_preflight_cache == {}
        assert restored.live.discovery_cache == {}
        assert stored_rows(store[0]) == before_cold and len(originals) == 1 and len(sdk) == len(author_sdk) == 1
        assert guard_checks(store[0]) == all_guards
        assert not output_path.exists()
    assert probe.facts["registered_calls"] == 1
    assert all(probe.facts["controls"].values()) and not probe.profile.errors
