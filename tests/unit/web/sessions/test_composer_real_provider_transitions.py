"""Per-transition provider custody through real Composer, quota and durable worker."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import local
from time import monotonic
from traceback import extract_tb
from typing import Any
from uuid import UUID, uuid4

import litellm
import pytest
import structlog
from httpx import ASGITransport, AsyncClient, MockTransport, Response
from jsonschema import Draft202012Validator
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_primary
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion
from elspeth.web.composer.advisor_audit import ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_limits import CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.composer.provider_gateway import (
    ProviderGateway,
    _admit_composer_llm_completion,
)
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.continuation_driver import _SegmentDriver
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    composer_async_operations_table,
    composer_continuation_effects_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.composer_operations import (
    message_body,
    recompose_body,
    submit_and_settle,
)
from tests.unit.web.sessions.test_routes import _make_app
from tests.unit.web.sessions.test_run_composer_turn import _seed_output_only_draft


@dataclass(frozen=True, slots=True)
class _Dispatch:
    operation_id: str
    attempt_id: str
    fence_id: str
    epoch: int
    model: str


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ("initial", "followup", "source_missing", "recompose", "authoring", "authoring_direct"))
async def test_each_real_composer_transition_has_its_own_settled_provider_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    app, fixture_service = _make_app(tmp_path, quota_enabled=True)
    engine = app.state.session_engine
    # The route fixture uses a subclass for old direct-turn shortcuts. The live
    # Continuation pre-title owner requires the exact production service type.
    service = SessionServiceImpl(
        engine,
        telemetry=app.state.sessions_telemetry,
        log=structlog.get_logger("test"),
        session_operation_authority=fixture_service.session_operation_authority,
        owner_instance_id=fixture_service.session_operation_owner_instance_id,
        chargeable_admission_policy=fixture_service._chargeable_admission_policy,
    )
    app.state.session_service = service
    from elspeth.contracts.bounded_serialization import bounded_canonical_json_size
    from elspeth.web.composer import continuation_evidence, continuation_live_phases, continuation_prompt_producers
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    from elspeth.web.composer.continuation_retention import retained_model_json_view
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

    evidence_pairs: list[tuple[int | None, int]] = []
    rejected_evidence_pairs: list[tuple[int | None, int]] = []
    rejected_graph_diagnostics: list[dict[str, int | str]] = []
    rejected_tool_phases: list[tuple[str, str, str | None]] = []
    timed_out_phases: list[tuple[str, str, str | None]] = []
    original_encode_evidence = continuation_evidence._encode_turn_evidence_source
    original_advance = ContinuationLivePhases.advance
    original_required_sql = continuation_primary.run_required_sql_finish_once
    original_clock_init = ContinuationActionClock.__init__
    original_finish_preparation = ContinuationActionClock.finish_preparation
    timing_origin = monotonic()
    primary_owner_timings: list[dict[str, object]] = []
    nested_primary_timings: list[dict[str, object]] = []
    post_primary_timings: list[dict[str, object]] = []
    nested_owner_context = local()
    excluded_primary_owner_names: list[str] = []
    primary_clock_timings: list[tuple[ContinuationActionClock, dict[str, object]]] = []

    def observed_clock_init(self: ContinuationActionClock, policy: ContinuationActionPolicy) -> None:
        original_clock_init(self, policy)
        primary_clock_timings.append(
            (
                self,
                {
                    "started_s": round(self.started - timing_origin, 6),
                    "preparation_deadline_s": round(self.started + self.policy.preparation - timing_origin, 6),
                    "total_deadline_s": round(self.deadline - timing_origin, 6),
                    "preparation_allowance_s": self.policy.preparation,
                },
            )
        )

    def observed_finish_preparation(self: ContinuationActionClock) -> None:
        record = next((value for owner, value in primary_clock_timings if owner is self), None)
        if record is not None:
            record["finish_attempt_s"] = round(monotonic() - timing_origin, 6)
        try:
            return original_finish_preparation(self)
        except BaseException as error:
            if record is not None:
                record["finish_error"] = type(error).__name__
            raise

    monkeypatch.setattr(ContinuationActionClock, "__init__", observed_clock_init)
    monkeypatch.setattr(ContinuationActionClock, "finish_preparation", observed_finish_preparation)

    async def observe_primary_required_sql(ticket, operation, *args, **kwargs):
        name = operation.__name__
        if name not in {"_freeze_input", "prepare_provider_action", "begin_continuation_primary"}:
            excluded_primary_owner_names.append(name)
            return await original_required_sql(ticket, operation, *args, **kwargs)
        record: dict[str, object] = {"owner": name, "submitted_s": round(monotonic() - timing_origin, 6)}
        if name == "_freeze_input":
            selected = kwargs["selected"]
            record.update(
                checkpoint_id=selected.checkpoint_id,
                cursor=selected.stack.cursor,
                frame_phase=selected.stack.frames[-1].phase,
                selected_chunk_count=len(selected.chunks),
                selected_chunk_bytes=sum(chunk.reference.byte_length for chunk in selected.chunks),
            )
            record.update(message_count=len(kwargs["messages"]), tool_count=len(kwargs["tools"]))
        elif name == "prepare_provider_action":
            record.update(
                checkpoint_id=kwargs["expected_checkpoint_id"],
                cursor=kwargs["expected_cursor"],
                snapshot_bytes=len(kwargs["snapshot_json"].encode("utf-8")),
                provider_input_bytes=len(kwargs["primary_input_json"].encode("utf-8")),
            )
        else:
            prepared = args[0]
            record.update(
                checkpoint_id=prepared.snapshot.checkpoint_id,
                cursor=prepared.snapshot.cursor,
                snapshot_bytes=len(prepared.snapshot.payload_json.encode("utf-8")),
            )
        primary_owner_timings.append(record)

        def delegate(*inner_args, **inner_kwargs):
            record["started_s"] = round(monotonic() - timing_origin, 6)
            if transition == "authoring_direct":
                nested_owner_context.owner = name
                nested_owner_context.parent_index = len(primary_owner_timings) - 1
            try:
                return operation(*inner_args, **inner_kwargs)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)
                if transition == "authoring_direct":
                    nested_owner_context.__dict__.pop("owner", None)
                    nested_owner_context.__dict__.pop("parent_index", None)

        try:
            outcome = await original_required_sql(ticket, delegate, *args, **kwargs)
        except BaseException as error:
            record["joined_s"] = round(monotonic() - timing_origin, 6)
            record["outcome"] = type(error).__name__
            raise
        record["joined_s"] = round(monotonic() - timing_origin, 6)
        record["outcome"] = type(outcome).__name__
        return outcome

    monkeypatch.setattr(continuation_primary, "run_required_sql_finish_once", observe_primary_required_sql)

    if transition == "authoring_direct":
        original_runtime_source = ComposerAsyncOperationAuthority.read_continuation_runtime

        def timed_runtime_source(self, *args, **kwargs):
            selected = kwargs["selected"]
            top = selected.stack.frames[-1]
            if type(top) is not ComposeFrame or top.phase != "response_retained":
                return original_runtime_source(self, *args, **kwargs)
            record: dict[str, object] = {
                "step": "runtime_source",
                "checkpoint_id": selected.checkpoint_id,
                "cursor": selected.stack.cursor,
                "started_s": round(monotonic() - timing_origin, 6),
            }
            post_primary_timings.append(record)
            try:
                return original_runtime_source(self, *args, **kwargs)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(ComposerAsyncOperationAuthority, "read_continuation_runtime", timed_runtime_source)

        original_advance_current = CurrentContinuationMessageProducers.advance_current

        def timed_advance_current(self, selected, inventory):
            top = selected.stack.frames[-1]
            if type(top) is not ComposeFrame or top.phase != "response_retained":
                return original_advance_current(self, selected, inventory)
            record: dict[str, object] = {
                "step": "producer_advance",
                "checkpoint_id": selected.checkpoint_id,
                "cursor": selected.stack.cursor,
                "started_s": round(monotonic() - timing_origin, 6),
            }
            post_primary_timings.append(record)
            try:
                return original_advance_current(self, selected, inventory)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(CurrentContinuationMessageProducers, "advance_current", timed_advance_current)

        original_initialize_batch = ContinuationLivePhases._initialize_batch

        async def timed_initialize_batch(self, selected):
            record: dict[str, object] = {
                "step": "initialize_batch",
                "checkpoint_id": selected.checkpoint_id,
                "cursor": selected.stack.cursor,
                "started_s": round(monotonic() - timing_origin, 6),
            }
            post_primary_timings.append(record)
            try:
                return await original_initialize_batch(self, selected)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(ContinuationLivePhases, "_initialize_batch", timed_initialize_batch)

        original_phase_read = ContinuationLivePhases._current_phase_read

        async def timed_phase_read(self, selected):
            top = selected.stack.frames[-1]
            if type(top) is not ComposeFrame or top.phase != "response_retained":
                return await original_phase_read(self, selected)
            record: dict[str, object] = {
                "step": "phase_read",
                "checkpoint_id": selected.checkpoint_id,
                "cursor": selected.stack.cursor,
                "started_s": round(monotonic() - timing_origin, 6),
            }
            post_primary_timings.append(record)
            try:
                return await original_phase_read(self, selected)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(ContinuationLivePhases, "_current_phase_read", timed_phase_read)

        original_context = continuation_live_phases.build_runtime_tool_batch_context

        async def timed_context(*args, **kwargs):
            record: dict[str, object] = {"step": "tool_context", "started_s": round(monotonic() - timing_origin, 6)}
            post_primary_timings.append(record)
            try:
                return await original_context(*args, **kwargs)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(continuation_live_phases, "build_runtime_tool_batch_context", timed_context)

        original_preferences = SessionServiceImpl.get_composer_preferences

        async def timed_preferences(self, session_id):
            record: dict[str, object] = {"step": "preferences", "started_s": round(monotonic() - timing_origin, 6)}
            post_primary_timings.append(record)
            try:
                return await original_preferences(self, session_id)
            finally:
                record["finished_s"] = round(monotonic() - timing_origin, 6)

        monkeypatch.setattr(SessionServiceImpl, "get_composer_preferences", timed_preferences)

        def timed_nested(label: str, original):
            def delegated(*args, **kwargs):
                owner = nested_owner_context.__dict__.get("owner")
                if owner != "_freeze_input":
                    return original(*args, **kwargs)
                record: dict[str, object] = {
                    "parent": owner,
                    "parent_index": nested_owner_context.parent_index,
                    "step": label,
                    "started_s": round(monotonic() - timing_origin, 6),
                }
                nested_primary_timings.append(record)
                try:
                    return original(*args, **kwargs)
                finally:
                    record["finished_s"] = round(monotonic() - timing_origin, 6)

            return delegated

        nested_targets = (
            (continuation_primary, "read_continuation_cost_snapshot_on_connection", "freeze.cost_snapshot"),
            (continuation_prompt_producers, "_read_current_inputs", "producer.current_inputs"),
            (continuation_prompt_producers, "read_turn_evidence_on_connection", "producer.turn_evidence"),
            (continuation_prompt_producers, "_inputs_from_current_evidence_on_connection", "producer.project_inputs"),
            (continuation_primary, "decode_recorder_snapshot", "freeze.decode_recorder"),
            (CurrentContinuationMessageProducers, "_require_current_on_connection", "freeze.require_current_on_connection"),
            (CurrentContinuationMessageProducers, "capture_message", "freeze.capture_message"),
            (BufferingRecorder, "snapshot_for_continuation", "freeze.snapshot_recorder"),
            (ProviderGateway, "prepare_continuation_primary", "freeze.prepare_gateway"),
        )
        for target, name, label in nested_targets:
            monkeypatch.setattr(target, name, timed_nested(label, target.__dict__[name]))

    def graph_diagnostic(snapshot: Any, recorder_chunk: Any) -> dict[str, int | str]:
        graph_json = snapshot.application_entry.graph_json if snapshot.application_entry is not None else snapshot.joint_graph_json
        if graph_json is None:
            return {"graph": "absent", "recorder_chunk_bytes": recorder_chunk.reference.byte_length}
        document = json.loads(graph_json)
        nodes = document["nodes"]

        def reachable(root: object) -> set[int]:
            found: set[int] = set()
            pending = [root]
            while pending:
                value = pending.pop()
                if type(value) is dict:
                    if set(value) == {"ref"}:
                        index = value["ref"]
                        if index not in found:
                            found.add(index)
                            pending.append(nodes[index]["value"])
                    else:
                        pending.extend(value.values())
                elif type(value) is list:
                    pending.extend(value)
            return found

        roots = document["roots"]
        recorder_nodes = reachable(roots[4])
        other_nodes = set().union(*(reachable(root) for index, root in enumerate(roots) if index != 4))
        recorder_root_index = roots[4]["ref"]
        recorder_node_json = canonical_json(nodes[recorder_root_index])
        graph_bytes = graph_json.encode("utf-8")
        recorder_bytes = recorder_chunk.canonical.encode("utf-8")
        return {
            "graph_bytes": len(graph_bytes),
            "graph_sha256": sha256(graph_bytes).hexdigest(),
            "graph_schema": document["schema_version"],
            "graph_node_count": len(nodes),
            "graph_recorder_ref_digest": document["recorder_source"]["digest"],
            "graph_recorder_ref_bytes": document["recorder_source"]["byte_length"],
            "recorder_node_kind": nodes[recorder_root_index]["kind"],
            "recorder_node_bytes": len(recorder_node_json.encode("utf-8")),
            "recorder_node_sha256": sha256(recorder_node_json.encode("utf-8")).hexdigest(),
            "recorder_closure_nodes": len(recorder_nodes),
            "recorder_exclusive_nodes": len(recorder_nodes - other_nodes),
            "recorder_closure_json_bytes": len(canonical_json([nodes[index] for index in sorted(recorder_nodes)]).encode("utf-8")),
            "recorder_exclusive_json_bytes": len(
                canonical_json([nodes[index] for index in sorted(recorder_nodes - other_nodes)]).encode("utf-8")
            ),
            "recorder_chunk_bytes": len(recorder_bytes),
            "recorder_chunk_sha256": sha256(recorder_bytes).hexdigest(),
        }

    def measure_evidence_pair(*args, **kwargs):
        try:
            result = original_encode_evidence(*args, **kwargs)
        except (AuditIntegrityError, ComposerContextUnsupported) as error:
            if str(error) == "evidence and original recorder exceed their complete pair bound" or (
                type(error) is ComposerContextUnsupported and error.reason == "retained_chunk_bytes"
            ):
                measured = bounded_canonical_json_size(
                    retained_model_json_view(args[0]), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
                )
                evidence_bytes = measured.admitted_bytes
                recorder_bytes = kwargs["recorder_chunk"].reference.byte_length
                if evidence_bytes is not None and evidence_bytes + recorder_bytes > CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES:
                    rejected_evidence_pairs.append((evidence_bytes, recorder_bytes))
                    rejected_graph_diagnostics.append(graph_diagnostic(args[0], kwargs["recorder_chunk"]))
            raise
        evidence_bytes = bounded_canonical_json_size(
            retained_model_json_view(args[0]), limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
        ).admitted_bytes
        evidence_pairs.append((evidence_bytes, kwargs["recorder_chunk"].reference.byte_length))
        return result

    monkeypatch.setattr(continuation_evidence, "_encode_turn_evidence_source", measure_evidence_pair)

    async def observe_tool_phase(self, selected):
        try:
            return await original_advance(self, selected)
        except TimeoutError:
            top = selected.stack.frames[-1]
            graph = self.runtime.graph if self.runtime is not None else None
            batch = graph.batch if graph is not None else None
            timed_out_phases.append(
                (
                    type(top).__name__,
                    top.phase if type(top) in (ComposeFrame, ToolBatchFrame) else "no-phase",
                    batch.batch.phase if batch is not None else None,
                )
            )
            raise
        except AuditIntegrityError as error:
            if str(error) == "selected phase has no matching live tool action":
                top = selected.stack.frames[-1]
                graph = self.runtime.graph if self.runtime is not None else None
                batch = graph.batch if graph is not None else None
                rejected_tool_phases.append(
                    (
                        type(top).__name__,
                        top.phase if type(top) in (ComposeFrame, ToolBatchFrame) else "no-phase",
                        batch.batch.phase if batch is not None else None,
                    )
                )
            raise

    monkeypatch.setattr(ContinuationLivePhases, "advance", observe_tool_phase)
    worker_failures: list[BaseException] = []
    settled_failures: list[BaseException] = []
    original_finished = ComposerAsyncWorker._job_finished
    original_settle = _SegmentDriver.settle

    def capture_worker_failure(self, key, task):
        if self is app.state.composer_async_worker and not task.cancelled():
            failure = task.exception()
            if failure is not None:
                worker_failures.append(failure)
        original_finished(self, key, task)

    monkeypatch.setattr(ComposerAsyncWorker, "_job_finished", capture_worker_failure)

    async def capture_settlement_failure(self, record, original=None):
        if original is not None:
            settled_failures.append(original)
        return await original_settle(self, record, original)

    monkeypatch.setattr(_SegmentDriver, "settle", capture_settlement_failure)

    def safe_failure(error: BaseException) -> str:
        if isinstance(error, BaseExceptionGroup):
            return "group(" + ", ".join(safe_failure(child) for child in error.exceptions) + ")"
        if type(error) in (AuditIntegrityError, CommonProviderCostRefused):
            return f"{type(error).__name__}: {str(error).splitlines()[0]}"
        if type(error) is TimeoutError:
            frames = [(Path(frame.filename).name, frame.lineno, frame.name) for frame in extract_tb(error.__traceback__)[-12:]]
            context = type(error.__context__).__name__ if error.__context__ is not None else None
            return f"TimeoutError: {str(error).splitlines()[0] if str(error) else '<empty>'}; frames={frames}; context={context}"
        if type(error) is AttributeError:
            line = str(error).splitlines()[0]
            if re.fullmatch(r"'[A-Za-z_][A-Za-z_0-9]*' object has no attribute '[A-Za-z_][A-Za-z_0-9]*'", line):
                return f"AttributeError: {line}"
        return type(error).__name__

    def local_available(**kwargs: object) -> ComposerAvailability:
        model = kwargs["model"]
        assert type(model) is str
        return ComposerAvailability(available=True, model=model, provider="test")

    # This local SDK double needs no real provider credentials. Availability
    # is an explicit test dependency, rather than a fabricated environment key.
    monkeypatch.setattr("elspeth.web.composer.service.compute_availability", local_available)
    app.state.settings = app.state.settings.model_copy(
        update={
            "composer_prompt_guard_mode": "required",
            "composer_prompt_guard": PromptGuardSettings(
                endpoint="https://primary-shield.example.test", subscription_key="offline-primary-shield"
            ),
            "composer_advisor_prompt_guard": PromptGuardSettings(
                endpoint="https://advisor-shield.example.test", subscription_key="offline-advisor-shield"
            ),
        }
    )
    shield_requests = []

    def shield_response(request):
        assert request.url.host in {"primary-shield.example.test", "advisor-shield.example.test"}
        payload = json.loads(request.content)
        shield_requests.append((request.url.host, payload))
        return Response(
            200,
            json={
                "userPromptAnalysis": {"attackDetected": False},
                "documentsAnalysis": [{"attackDetected": False} for _document in payload["documents"]],
            },
        )

    def mount(settings):
        return PromptGuardMount(AzurePromptGuardClient(settings, http_transport=MockTransport(shield_response)))

    monkeypatch.setattr("elspeth.web.composer.provider_gateway.build_prompt_guard_mount", mount)
    monkeypatch.setattr("elspeth.web.composer.service.build_prompt_guard_mount", mount)
    app.state.composer_service = ComposerServiceImpl(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=service,
        session_engine=engine,
        plugin_snapshot_factory=app.state.plugin_snapshot_factory.for_user_id,
        operator_profile_registry=app.state.operator_profile_registry,
    )
    # This is the actual surfacing dependency constructed by the real Composer.
    app.state.interpretation_surfacing = app.state.composer_service._interpretation_surfacing
    dispatches: list[_Dispatch] = []
    rootless_reviews = []
    rootless_outcomes = []
    rootless_prompts = []
    rootless_request = "Build a pipeline from customer complaints."
    rootless_correction = "No pipeline has been configured. Please supply the customer complaints source and desired report."
    rootless_findings = "The reply claims a ready pipeline, but the saved output draft has no customer complaints source."
    actual_checkpoint = app.state.composer_service._advisor_checkpoint._run_advisor_checkpoint
    assert actual_checkpoint.__func__ is AdvisorCheckpointOwner._run_advisor_checkpoint
    assert actual_checkpoint.__module__ == "elspeth.web.composer.advisor_checkpoint"

    async def observe_checkpoint(*args, **kwargs):
        if transition == "source_missing" and kwargs["phase"] == "end":
            rootless_reviews.append(kwargs["publication_review"])
        verdict = await actual_checkpoint(*args, **kwargs)
        if transition == "source_missing" and kwargs["phase"] == "end":
            assert verdict.publication_binding == rootless_reviews[-1].binding
            rootless_outcomes.append(verdict)
        return verdict

    monkeypatch.setattr(app.state.composer_service._advisor_checkpoint, "_run_advisor_checkpoint", observe_checkpoint)
    primary_replies = ["Hello. I can explain pipelines."]
    if transition == "source_missing":
        primary_replies = ["The pipeline is ready.", rootless_correction]
    if transition == "followup":
        primary_replies.append("Hello again. I can explain pipelines.")
    if transition == "authoring":
        primary_replies = ["", "", "", "Pipeline configured."]
    if transition == "authoring_direct":
        primary_replies = ["", "", "Pipeline configured."]

    async def local_completion(**kwargs: object) -> litellm.ModelResponse:
        model = kwargs["model"]
        assert type(model) is str
        assert model in (
            app.state.settings.composer_model,
            app.state.settings.composer_advisor_model,
        )
        with engine.connect() as conn:
            pending = (
                conn.execute(select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.settled_at.is_(None)))
                .mappings()
                .one()
            )
            job = (
                conn.execute(select(composer_async_operations_table).where(composer_async_operations_table.c.status == "running"))
                .mappings()
                .one()
            )
        assert pending["identity_id"] == job["actor_user_id"] == "alice"
        assert pending["session_id"] == job["session_id"]
        assert pending["operation_id"] == job["session_operation_id"]
        assert pending["operation_epoch"] == job["session_operation_epoch"]
        assert pending["lease_token"] == job["session_operation_lease_token"]
        dispatches.append(
            _Dispatch(
                job["operation_id"],
                pending["attempt_id"],
                pending["operation_id"],
                pending["operation_epoch"],
                model,
            )
        )
        tool_calls = None
        if model == app.state.settings.composer_model:
            assert primary_replies, "Unexpected extra primary provider dispatch"
            content = primary_replies.pop(0)
            primary_ordinal = sum(call.model == model for call in dispatches)
            sent_names = {tool["function"]["name"] for tool in kwargs["tools"]}
            # H2 retired the empty-state planner route: every transition, initial
            # and recompose included, runs the ordinary compose loop and its palette.
            assert "set_pipeline" in sent_names and "emit_pipeline_proposal" not in sent_names
            if transition == "source_missing":
                rootless_prompts.append(deepcopy(kwargs["messages"]))
                if primary_ordinal == 2:
                    assert len(rootless_reviews) == len(rootless_outcomes) == 1, "PRIMARY repair bypassed actual END"
                    assert rootless_outcomes[0].publication_verdict == "WITHHOLD"
                    assert rootless_findings in json.dumps(kwargs["messages"])
                    assert any(item["role"] == "assistant" and item["content"] == "The pipeline is ready." for item in kwargs["messages"])
            if transition in {"authoring", "authoring_direct"} and primary_ordinal < (4 if transition == "authoring" else 3):
                arguments = {}
                tool_name = "set_pipeline" if primary_ordinal == (2 if transition == "authoring" else 1) else "preview_pipeline"
                if tool_name == "set_pipeline":
                    tool_name = "set_pipeline"
                    arguments = {
                        "source": {
                            "plugin": "text",
                            "on_success": "main",
                            "options": {
                                "column": "text",
                                "schema": {"mode": "observed"},
                            },
                            "inline_blob": {
                                "filename": "input.txt",
                                "mime_type": "text/plain",
                                "content": "hello",
                            },
                            "on_validation_failure": "discard",
                        },
                        "nodes": [],
                        "edges": [],
                        "outputs": [
                            {
                                "sink_name": "main",
                                "plugin": "csv",
                                "options": {
                                    "path": str(tmp_path / "outputs" / job["session_id"] / "authoring.csv"),
                                    "schema": {"mode": "observed"},
                                    "mode": "write",
                                    "collision_policy": "auto_increment",
                                },
                                "on_write_failure": "discard",
                            }
                        ],
                        "metadata": {"name": "Local provider-authored pipeline"},
                    }
                semantic_calls = [(tool_name, arguments)]
                if transition == "authoring" and primary_ordinal == 1:
                    semantic_calls = [
                        ("get_plugin_schema", {"plugin_type": "source", "name": "text"}),
                        ("get_plugin_schema", {"plugin_type": "sink", "name": "csv"}),
                    ]
                sent_tools = kwargs["tools"]
                assert type(sent_tools) is list
                tool_calls = []
                for index, (tool_name, arguments) in enumerate(semantic_calls):
                    sent = next(tool["function"] for tool in sent_tools if tool["function"]["name"] == tool_name)
                    dialect = ToolContractDialect.OPENAI_STRICT if "strict" in sent else ToolContractDialect.NONE
                    wire_arguments = encode_semantic_arguments(tool_name, dialect, arguments)
                    Draft202012Validator(sent["parameters"]).validate(wire_arguments)
                    tool_calls.append(
                        {
                            "id": f"local-tool-{primary_ordinal}-{index}",
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(wire_arguments),
                            },
                        }
                    )
        else:
            schema_name = kwargs["response_format"]["json_schema"]["name"]
            assert schema_name in {"advisor_checkpoint_response", "advisor_end_response"}
            payload = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "CLEAN", "note": None}
            if schema_name == "advisor_end_response":
                payload["publication_verdict"] = "ADMIT"
                if transition == "source_missing":
                    assert len(rootless_reviews) <= 2
                    review = rootless_reviews[-1]
                    assert any(
                        review.provider_review_context_json()
                        in (item["content"] if isinstance(item["content"], str) else "".join(part["text"] for part in item["content"]))
                        for item in kwargs["messages"]
                    )
                    packet = json.loads(review.payload_json)
                    assert packet["current_request"] == rootless_request
                    assert packet["candidate_reply"] == ("The pipeline is ready." if len(rootless_reviews) == 1 else rootless_correction)
                    assert packet["saved_effects"]["saved_revision_count"] == 0
                    payload["publication_verdict"] = "WITHHOLD" if len(rootless_reviews) == 1 else "ADMIT"
                    payload["findings"] = rootless_findings if len(rootless_reviews) == 1 else ""
            elif transition == "source_missing":
                raise AssertionError("An unchanged source-less draft turn must not use EARLY")
            content = json.dumps(payload)
        return litellm.ModelResponse(
            id=f"local-{len(dispatches)}",
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None if tool_calls else content,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.01},
        )

    # Leave ProviderGateway, physical admission, audited advisor and settlement real.
    # Replace the sole outbound SDK seam before running any transition.
    monkeypatch.setattr(litellm, "acompletion", local_completion)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/sessions", json={"title": "Per transition evidence"})
            assert created.status_code == 201, created.text
            session_id = created.json()["id"]
            baseline = None
            if transition in {"followup", "source_missing", "authoring", "authoring_direct"}:
                # These scenarios start from a real previous draft. Initial and
                # recompose start from an empty session, which since H2 also runs
                # the ordinary compose loop (PRIMARY -> END), not a planner route.
                baseline = await _seed_output_only_draft(app, service, UUID(session_id))
            operation_ids: list[str] = []
            rounds = 2 if transition == "followup" else 1
            for _ in range(rounds):
                operation_id = str(uuid4())
                operation_ids.append(operation_id)
                if transition == "recompose":
                    from tests.helpers.session_fences import fenced_operation_context

                    with fenced_operation_context(engine, session_id) as context:
                        user = await service.add_message(
                            UUID(session_id),
                            "user",
                            "Hello",
                            writer_principal="route_user_message",
                            session_operation_context=context,
                        )
                    path = f"/api/sessions/{session_id}/recompose"
                    body = recompose_body(user.id, operation_id=operation_id)
                else:
                    path = f"/api/sessions/{session_id}/messages"
                    content = rootless_request if transition == "source_missing" else "Hello"
                    if transition in {"authoring", "authoring_direct"}:
                        content = "Create a pipeline reading hello from an inline text source and writing the text to CSV."
                    current = await service.get_current_state(UUID(session_id))
                    body = message_body(
                        content,
                        state_id=None if current is None else current.id,
                        operation_id=operation_id,
                    )
                before = len(dispatches)
                try:
                    settled = await submit_and_settle(client, app, path=path, body=body)
                except AssertionError as error:
                    if worker_failures:
                        raise AssertionError(f"{error}; original worker failure: {worker_failures[-1]!r}") from error
                    raise
                failure_detail = (
                    settled.final.text
                    + "; original settlement failure: "
                    + safe_failure(settled_failures[-1])
                    + f"; evidence/recorder byte pairs: {evidence_pairs[-6:]}"
                    + f"; rejected pair: {rejected_evidence_pairs[-1:]}"
                    + f"; rejected graph: {rejected_graph_diagnostics[-1:]}"
                    + f"; rejected tool phases: {rejected_tool_phases[-3:]}"
                    + f"; timed out phases: {timed_out_phases[-3:]}"
                    + f"; primary owner timings: {primary_owner_timings[-12:]}"
                    + f"; nested PRIMARY timings: {nested_primary_timings[-100:]}"
                    + f"; post-PRIMARY source timings: {post_primary_timings[-40:]}"
                    + f"; excluded PRIMARY SQL owners: {excluded_primary_owner_names[-8:]}"
                    + f"; primary clocks: {[record for _clock, record in primary_clock_timings[-6:]]}"
                    + f"; provider dispatch count: {len(dispatches)}"
                    if settled_failures
                    else settled.final.text
                )
                final = settled.final.json()
                if transition == "authoring_direct":
                    observed_owners = {record["owner"] for record in primary_owner_timings}
                    assert {
                        "_freeze_input",
                        "prepare_provider_action",
                        "begin_continuation_primary",
                    }.issubset(observed_owners), failure_detail
                    assert "publish_continuation_primary" in excluded_primary_owner_names, failure_detail
                    observed_steps = {record["step"] for record in nested_primary_timings}
                    assert {
                        "freeze.cost_snapshot",
                        "producer.current_inputs",
                        "freeze.require_current_on_connection",
                        "freeze.prepare_gateway",
                    }.issubset(observed_steps), failure_detail
                    assert all(record["parent"] == "_freeze_input" for record in nested_primary_timings), failure_detail
                if transition == "authoring":
                    assert final["status"] == "failed", failure_detail
                    assert final["error"]["http_status"] == 422, failure_detail
                    assert final["error"]["failure_code"] == "http_error", failure_detail
                    assert final["error"]["error_type"] == "composer_context_unsupported", failure_detail
                    assert final["error"]["body"]["detail"]["failure_code"] == "context_unsupported", failure_detail
                    assert final["error"]["body"]["detail"]["reason"] == "retained_chunk_bytes", failure_detail
                    assert len(rejected_evidence_pairs) == 1, failure_detail
                    evidence_bytes, recorder_bytes = rejected_evidence_pairs[0]
                    assert evidence_bytes is not None
                    assert evidence_bytes + recorder_bytes > CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
                else:
                    assert final["status"] == "completed", failure_detail
                this_transition = dispatches[before:]
                assert this_transition, "transition bypassed the primary provider"
                assert {call.operation_id for call in this_transition} == {operation_id}
                assert sum(call.model == app.state.settings.composer_model for call in this_transition) == (
                    1
                    if transition == "authoring"
                    else 3
                    if transition == "authoring_direct"
                    else 2
                    if transition == "source_missing"
                    else 1
                ), "transition bypassed the primary provider"
                if transition in {"initial", "recompose"}:
                    assert [call.model for call in this_transition] == [
                        app.state.settings.composer_model,
                        app.state.settings.composer_advisor_model,
                    ]
                if transition == "source_missing":
                    assert [call.model for call in this_transition] == [
                        app.state.settings.composer_model,
                        app.state.settings.composer_advisor_model,
                        app.state.settings.composer_model,
                        app.state.settings.composer_advisor_model,
                    ]
                replay = await client.post(path, json=body)
                assert replay.status_code == 202, replay.text
                await app.state.composer_async_worker.run_until_idle()
                assert len(dispatches) == before + len(this_transition)
            if transition == "authoring":
                assert primary_replies == ["", "", "Pipeline configured."]
            else:
                assert not primary_replies
            with engine.connect() as conn:
                attempts = conn.execute(select(quota_provider_attempts_table)).mappings().all()
                usage = conn.execute(select(token_usage_ledger_table)).mappings().all()
                jobs = conn.execute(select(composer_async_operations_table)).mappings().all()
                effects = (
                    conn.execute(
                        select(composer_continuation_effects_table).where(
                            composer_continuation_effects_table.c.session_id == session_id,
                            composer_continuation_effects_table.c.operation_id == operation_ids[0],
                        )
                    )
                    .mappings()
                    .all()
                    if transition == "authoring"
                    else []
                )
            assert {job["operation_id"] for job in jobs} == set(operation_ids)
            assert all(job["status"] == ("failed" if transition == "authoring" else "completed") for job in jobs)
            assert {row["attempt_id"] for row in attempts} == {call.attempt_id for call in dispatches}
            assert all(row["settled_at"] is not None for row in attempts)
            assert {row["ledger_entry_id"] for row in attempts} == {row["entry_id"] for row in usage}
            assert all(row["prompt_tokens"] == row["completion_tokens"] == 1 for row in usage)
            if transition == "authoring":
                provider_effects = [row for row in effects if row["owner"] == "provider"]
                tool_effects = [row for row in effects if row["owner"] == "tool_dispatch"]
                p4_effects = [row for row in effects if row["owner"] == "session_sql" and row["effect_key"].startswith("batch:")]
                assert len(provider_effects) == 1
                assert len(tool_effects) == 2
                assert all(
                    row["disposition"] == "committed" and row["receipt_key"] is not None for row in (*provider_effects, *tool_effects)
                )
                assert len(p4_effects) == 1
                assert p4_effects[0]["disposition"] == "prepared"
                assert p4_effects[0]["receipt_key"] is None and p4_effects[0]["result_chunk_key"] is None
            messages = await service.get_messages(UUID(session_id), limit=None)
            audit_ids = {
                entry["call"]["call_id"]
                for message in messages
                if message.role == "audit" and message.tool_calls is not None
                for entry in message.tool_calls
                if entry["_kind"] == "llm_call_audit"
            }
            assert audit_ids == {call.attempt_id for call in dispatches}
            guards = [
                entry["check"]
                for message in messages
                if message.role == "audit" and message.tool_calls is not None
                for entry in message.tool_calls
                if entry["_kind"] == "prompt_guard_audit"
            ]
            assert [(check["role"], check["disposition"]) for check in guards] == [
                ("composer" if call.model == app.state.settings.composer_model else "advisor", "allow") for call in dispatches
            ]
            assert shield_requests
            if transition == "source_missing":
                assert [outcome.publication_verdict for outcome in rootless_outcomes] == ["WITHHOLD", "ADMIT"]
                assert rootless_reviews[0].binding != rootless_reviews[1].binding
                passes = [
                    entry["pass"]
                    for message in messages
                    if message.role == "audit" and message.tool_calls is not None
                    for entry in message.tool_calls
                    if entry["_kind"] == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
                ]
                assert [(item["publication_verdict"], item["publication_binding"]) for item in passes] == [
                    (outcome.publication_verdict, review.binding)
                    for review, outcome in zip(rootless_reviews, rootless_outcomes, strict=True)
                ]
                assert not any(
                    item["role"] == "user"
                    and isinstance(item["content"], str)
                    and item["content"].startswith(
                        (
                            "[composer-system] No tool has run this turn",
                            "[composer-system] No composition-state mutation completed successfully",
                        )
                    )
                    for prompt in rootless_prompts
                    for item in prompt
                )
                current = await service.get_current_state(UUID(session_id))
                assert current is not None and baseline is not None
                assert current.id == baseline.id
                assert not current.sources and current.outputs == baseline.outputs
                assert any(message.role == "assistant" and message.content.startswith(rootless_correction) for message in messages)
                assert not any(message.role == "assistant" and "The pipeline is ready." in message.content for message in messages)
                assert any(
                    message.role == "audit"
                    and message.content == "The pipeline is ready."
                    and any(
                        entry["_kind"] == COMPOSER_WITHHELD_REPLY_KIND and entry["origin"] == "repair_gate_superseded"
                        for entry in (message.tool_calls or ())
                    )
                    for message in messages
                )
            if transition == "authoring":
                current = await service.get_current_state(UUID(session_id))
                assert current is not None and baseline is not None
                assert current.id == baseline.id
                assert await service.list_composition_proposals(UUID(session_id)) == []
                assert not any(
                    message.role == "tool" and message.tool_call_id in {"local-tool-1-0", "local-tool-1-1"} for message in messages
                )
                assert not any(
                    message.role == "assistant"
                    and any(call["id"] in {"local-tool-1-0", "local-tool-1-1"} for call in (message.tool_calls or ()))
                    for message in messages
                )
            if transition == "authoring_direct":
                current = await service.get_current_state(UUID(session_id))
                assert current is not None
                assert current.sources is not None
                assert current.sources["source"]["plugin"] == "text"
                assert current.metadata_ is not None
                assert current.metadata_["name"] == "Local provider-authored pipeline"
                calls = [
                    entry
                    for message in messages
                    if message.role == "assistant" and message.tool_calls is not None
                    for entry in message.tool_calls
                ]
                assert {(entry["id"], entry["function"]["name"]) for entry in calls} == {
                    ("local-tool-1-0", "set_pipeline"),
                    ("local-tool-2-0", "preview_pipeline"),
                }
                assert len(calls) == 2
                assert all(entry["wire_conformant"] is True for entry in calls)
                tool_rows = [message for message in messages if message.role == "tool"]
                assert len(tool_rows) == 2
                assert {message.tool_call_id for message in tool_rows} == {entry["id"] for entry in calls}
                assert all(json.loads(message.content)["success"] is True for message in tool_rows)
                mutation = next(message for message in tool_rows if message.tool_call_id == "local-tool-1-0")
                assert mutation.composition_state_id is not None
                authored = await service.get_state_in_session(state_id=mutation.composition_state_id, session_id=UUID(session_id))
                assert authored.session_id == current.session_id
                assert composition_content_hash(state_from_record(authored)) == composition_content_hash(state_from_record(current))
                # The default auto-commit compose-loop path persists the
                # authored state and dispatch audit directly. Proposal rows
                # belong to the separate approval/planner paths.
                proposals = await service.list_composition_proposals(UUID(session_id))
                assert proposals == []
    finally:
        await app.state.composer_async_worker.stop()
        engine.dispose()


@pytest.mark.asyncio
async def test_transition_instrument_rejects_primary_provider_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    physical_call = ProviderGateway._call_llm
    calls = 0
    counterfeited = False

    # The stand-in forwards every keyword it does not inspect. An explicit
    # keyword list went stale when production began passing prepared_input:
    # the first call then raised TypeError, the operation settled failed,
    # and this control failed before the census could judge the bypass.
    async def bypass(
        self: ProviderGateway,
        messages: object,
        tools: object,
        *,
        provider_custody: ProviderCallCustody | None = None,
        **forwarded: Any,
    ) -> _AdmittedLLMCompletion:
        nonlocal calls, counterfeited
        calls += 1
        if calls == 1:
            return await physical_call(self, messages, tools, provider_custody=provider_custody, **forwarded)
        # First transition dispatches normally. Counterfeit the second path
        # while leaving accounting real: a positive per-walk total misses it.
        assert type(provider_custody) is ProviderCallCustody
        await provider_custody.admit(model=self._model)
        # This intentional mutation counterfeits the dispatch marker and
        # response while skipping the sole SDK seam. The transition census
        # must still reject it after ordinary accounting settles.
        provider_custody.mark_sdk_entered()
        response = litellm.ModelResponse(
            id="local-bypass-control",
            model=self._model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hello",
                        "tool_calls": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.01},
        )
        counterfeited = True
        return _admit_composer_llm_completion(response)

    monkeypatch.setattr(ProviderGateway, "_call_llm", bypass)
    with pytest.raises(AssertionError) as caught:
        await test_each_real_composer_transition_has_its_own_settled_provider_evidence(tmp_path, monkeypatch, "followup")
    # The rejection proves nothing unless the counterfeit actually ran: check
    # that first, so a census failure for another reason is named as such.
    assert counterfeited, f"the counterfeit dispatch never ran; the census failed for another reason: {caught.value}"
    assert calls == 2
    assert "transition bypassed the primary provider" in str(caught.value)
