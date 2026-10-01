"""Admission binds nominal HTTP authority to its sink and execution mode."""

import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from typing import cast

import httpx
import pytest
import respx
from sqlalchemy import update

from elspeth.contracts.enums import CallType, RunMode
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.sink_effect_http import (
    HTTPSinkEffectCapability,
    SinkEffectHTTPBindContext,
    SinkEffectHTTPEnvironment,
    SinkEffectHTTPPost,
    SinkEffectHTTPPostFactory,
    SinkEffectHTTPPostRequest,
    SinkEffectHTTPPostResponse,
)
from elspeth.contracts.sink_effects import (
    MemberSinkEffectCapability,
    ResolvedSinkEffectMode,
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
    SinkEffectRuntimeBinding,
)
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import sink_effects_table
from elspeth.core.security.web import SSRFSafeRequest
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator
from elspeth.engine.orchestrator.preflight import (
    SinkEffectCapabilityError,
    require_sink_effect_admission,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members
from tests.unit.engine.test_sink_effect_executor import _CumulativeObservableSink, _CumulativeTarget, _execution_request
from tests.unit.engine.test_sink_effect_preflight import EffectCapableSink


class _HTTPTestSink(EffectCapableSink, HTTPSinkEffectCapability):
    pass


class _Factory(SinkEffectHTTPPostFactory):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    @property
    def safe_config_fingerprint(self):
        return self.fingerprint

    def bind(self, context):
        raise AssertionError("preflight cannot bind transport")


def _binding(sink, factory):
    return SinkEffectRuntimeBinding(
        sink_name="output",
        sink=sink,
        sink_type=type(sink),
        config_fingerprint=stable_hash(sink.config),
        purpose=SinkEffectExecutionPurpose.FRESH,
        effect_mode=ResolvedSinkEffectMode("write"),
        http_post_factory=factory,
    )


def test_http_sink_requires_factory_before_lifecycle():
    sink = _HTTPTestSink()
    with pytest.raises(SinkEffectCapabilityError, match=r"HTTP.*factory"):
        validate_pipeline_sink_effect_capabilities(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        )
    assert sink.on_start_calls == sink.write_calls == 0


def test_admission_refuses_factory_swap_and_safe_config_mutation():
    sink = _HTTPTestSink()
    factory = _Factory(stable_hash(sink.config))
    binding = _binding(sink, factory)
    bindings = {"output": binding}
    admission = validate_pipeline_sink_effect_capabilities(
        {"output": sink},
        configured_modes={"output": "write"},
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        runtime_bindings=bindings,
    )
    assert (
        require_sink_effect_admission(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            admission=admission,
            runtime_bindings=bindings,
        )
        is admission
    )
    with pytest.raises(SinkEffectCapabilityError):
        require_sink_effect_admission(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            admission=admission,
            runtime_bindings={"output": replace(binding, http_post_factory=_Factory(stable_hash(sink.config)))},
        )
    sink.config["mode"] = "changed"
    with pytest.raises(SinkEffectCapabilityError):
        require_sink_effect_admission(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            admission=admission,
            runtime_bindings=bindings,
        )


@pytest.mark.parametrize("mode", [RunMode.REPLAY, RunMode.VERIFY])
def test_nonlive_requires_absent_transport_and_mode_bound_admission(mode):
    sink = _HTTPTestSink()
    binding = _binding(sink, None)
    bindings = {"output": binding}
    admission = validate_pipeline_sink_effect_capabilities(
        {"output": sink},
        configured_modes={"output": "write"},
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        runtime_bindings=bindings,
        run_mode=mode,
    )
    with pytest.raises(SinkEffectCapabilityError):
        require_sink_effect_admission(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            admission=admission,
            runtime_bindings=bindings,
            run_mode=RunMode.LIVE,
        )
    with pytest.raises(SinkEffectCapabilityError, match="nonlive"):
        validate_pipeline_sink_effect_capabilities(
            {"output": sink},
            configured_modes={"output": "write"},
            required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            runtime_bindings={"output": replace(binding, http_post_factory=_Factory(stable_hash(sink.config)))},
            run_mode=mode,
        )


class _GuardedPost(SinkEffectHTTPPost):
    """Controlled endpoint; the callback must authorize every observable send."""

    def __init__(self, context: SinkEffectHTTPBindContext, before_dispatch: Callable[[], None]) -> None:
        self.context = context
        self.before_dispatch = before_dispatch
        self.sends = 0

    def post_json(self, request: SinkEffectHTTPPostRequest) -> SinkEffectHTTPPostResponse:
        assert isinstance(request, SinkEffectHTTPPostRequest)
        self.before_dispatch()
        self.context.before_send()
        self.sends += 1
        return SinkEffectHTTPPostResponse(
            status_code=200,
            content_type="application/json",
            body=b"{}",
            call_id="controlled-call",
            request_ref="a" * 64,
            response_ref="b" * 64,
        )


class _AttemptFactory(SinkEffectHTTPPostFactory):
    def __init__(self, recorder: RecorderFactory, before_dispatch: Callable[[], None] = lambda: None) -> None:
        self.recorder = recorder
        self.before_dispatch = before_dispatch
        self.posts: list[_GuardedPost] = []

    @property
    def safe_config_fingerprint(self) -> str:
        return "a" * 64

    def bind(self, context: SinkEffectHTTPBindContext) -> SinkEffectHTTPPost:
        attempts = self.recorder.execution.sink_effects.get_attempts_for_run(context.run_id)
        # Authority is composed only after a durable member intent exists.
        assert attempts[-1].member_ordinal is not None
        assert attempts[-1].state.value == "intent"
        assert context.recorder is self.recorder.execution
        post = _GuardedPost(context, self.before_dispatch)
        self.posts.append(post)
        return post


class _HTTPMemberSink(_CumulativeObservableSink, MemberSinkEffectCapability, HTTPSinkEffectCapability):
    effect_call_type = CallType.HTTP

    def __init__(self, fail_action: str | None = None) -> None:
        super().__init__(_CumulativeTarget())
        self.fail_action = fail_action
        self.posts: list[SinkEffectHTTPPost] = []
        self.revoked_during_next_callback = 0

    def prepare_effect(self, request: SinkEffectPrepareRequest, ctx: RestrictedSinkEffectContext) -> SinkEffectPlan:
        assert ctx.http_post is None
        return super().prepare_effect(request, ctx)

    def _send(self, ctx: RestrictedSinkEffectContext, action: str) -> None:
        assert ctx.http_post is not None
        if self.posts:
            # The same effect lease remains valid while a new member callback
            # runs; the earlier callback's authority must nevertheless be dead.
            with pytest.raises(FrameworkBugError, match="authority was revoked"):
                self.posts[-1].post_json(SinkEffectHTTPPostRequest({"retained": True}))
            self.revoked_during_next_callback += 1
        self.posts.append(ctx.http_post)
        ctx.http_post.post_json(SinkEffectHTTPPostRequest({"action": action}))
        if self.fail_action == action:
            raise RuntimeError("controlled callback failure")

    def reconcile_member_effect(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectReconcileResult:
        self._send(ctx, "status")
        return SinkEffectReconcileResult.not_applied(evidence={"status": "not_applied"})

    def commit_member_effect(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectCommitResult:
        self._send(ctx, "commit")
        assert plan.expected_descriptor is not None
        return SinkEffectCommitResult(
            descriptor=plan.expected_descriptor, evidence={}, accepted_ordinals=(member.ordinal,), diverted_ordinals=()
        )


def test_each_durable_member_attempt_gets_fresh_transport_and_revokes_previous_authority():
    db = make_landscape_db()
    try:
        recorder = make_factory(db)
        run_id, sink_id, members = _pipeline_members(recorder, 2)
        http_factory = _AttemptFactory(recorder)
        sink = _HTTPMemberSink()
        result = SinkEffectCoordinator(
            factory=recorder,
            worker_id="worker-a",
            coordination_token=leader_token_for(db, run_id),
            http_post_factory=http_factory,
            http_environment=SinkEffectHTTPEnvironment(lambda event: None),
        ).execute(_execution_request(run_id, sink_id, members), sink)

        assert result.effect.state.value == "finalized"
        assert sink.prepare_calls == 1
        assert len(http_factory.posts) == len({id(post) for post in http_factory.posts}) == 4
        assert [post.sends for post in http_factory.posts] == [1, 1, 1, 1]
        assert sink.revoked_during_next_callback == 3
        attempts = recorder.execution.sink_effects.get_attempts_for_run(run_id)
        assert [(attempt.action.value, attempt.member_ordinal, attempt.state.value) for attempt in attempts] == [
            ("inspect", None, "returned"),
            ("reconcile", 0, "returned"),
            ("commit", 0, "returned"),
            ("reconcile", 1, "returned"),
            ("commit", 1, "returned"),
        ]
        for post in http_factory.posts:
            with pytest.raises(FrameworkBugError, match="authority was revoked"):
                post.post_json(SinkEffectHTTPPostRequest({"retained": True}))
        assert [post.sends for post in http_factory.posts] == [1, 1, 1, 1]
    finally:
        db.close()


@pytest.mark.parametrize("action", ["status", "commit"])
def test_callback_failure_revokes_capability_and_marks_durable_attempt_response_lost(action):
    db = make_landscape_db()
    try:
        recorder = make_factory(db)
        run_id, sink_id, members = _pipeline_members(recorder, 1)
        http_factory = _AttemptFactory(recorder)
        with pytest.raises(RuntimeError, match="controlled callback failure"):
            SinkEffectCoordinator(
                factory=recorder,
                worker_id="worker-a",
                coordination_token=leader_token_for(db, run_id),
                http_post_factory=http_factory,
                http_environment=SinkEffectHTTPEnvironment(lambda event: None),
            ).execute(_execution_request(run_id, sink_id, members), _HTTPMemberSink(action))
        assert recorder.execution.sink_effects.get_attempts_for_run(run_id)[-1].state.value == "response_lost"
        post = http_factory.posts[-1]
        assert post.sends == 1
        with pytest.raises(FrameworkBugError, match="authority was revoked"):
            post.post_json(SinkEffectHTTPPostRequest({"retained": True}))
        assert post.sends == 1
    finally:
        db.close()


@pytest.mark.parametrize("interrupt", ["shutdown", "lease_takeover", "coordination_latch"])
def test_changed_authority_after_binding_suppresses_wire_and_preserves_uncertain_attempt(interrupt):
    db = make_landscape_db()
    try:
        recorder = make_factory(db)
        run_id, sink_id, members = _pipeline_members(recorder, 1)
        shutdown = threading.Event()
        latch_lost = False

        def before_dispatch():
            nonlocal latch_lost
            if interrupt == "shutdown":
                shutdown.set()
            elif interrupt == "coordination_latch":
                latch_lost = True
            else:
                (effect,) = recorder.execution.sink_effects.get_effects_for_run(run_id)
                with db.engine.begin() as connection:
                    connection.execute(
                        update(sink_effects_table)
                        .where(sink_effects_table.c.effect_id == effect.effect_id)
                        .values(
                            generation=effect.generation + 1,
                            lease_owner="replacement-worker",
                        )
                    )

        def check_latch():
            if latch_lost:
                raise FrameworkBugError("controlled coordination loss")

        http_factory = _AttemptFactory(recorder, before_dispatch)
        expected_error = (
            InterruptedError
            if interrupt == "shutdown"
            else FrameworkBugError
            if interrupt == "coordination_latch"
            else LandscapeRecordError
        )
        expected_message = (
            "shutdown requested"
            if interrupt == "shutdown"
            else "controlled coordination loss"
            if interrupt == "coordination_latch"
            else "stale generation"
        )
        with pytest.raises(expected_error, match=expected_message):
            SinkEffectCoordinator(
                factory=recorder,
                worker_id="worker-a",
                coordination_token=leader_token_for(db, run_id),
                lease_ttl=timedelta(minutes=5),
                shutdown_event=shutdown,
                check_coordination_latch=check_latch,
                http_post_factory=http_factory,
                http_environment=SinkEffectHTTPEnvironment(lambda event: None),
            ).execute(_execution_request(run_id, sink_id, members), _HTTPMemberSink())
        assert len(http_factory.posts) == 1
        assert http_factory.posts[0].sends == 0
        # A deposed effect owner also loses classification authority. Its
        # intent stays unsettled for the next generation's recovery instead
        # of the stale callback writing an authoritative terminal result.
        expected_state = "intent" if interrupt == "lease_takeover" else "response_lost"
        assert recorder.execution.sink_effects.get_attempts_for_run(run_id)[-1].state.value == expected_state
    finally:
        db.close()


class _AuditedPost(SinkEffectHTTPPost):
    def __init__(self, context: SinkEffectHTTPBindContext) -> None:
        self.context = context

    def post_json(self, request: SinkEffectHTTPPostRequest) -> SinkEffectHTTPPostResponse:
        self.context.before_send()
        target = SSRFSafeRequest(
            original_url="https://flow.example.test/invoke",
            resolved_ip="93.184.216.34",
            host_header="flow.example.test",
            port=443,
            path="/invoke",
            scheme="https",
            bare_hostname="flow.example.test",
        )
        with closing(
            AuditedHTTPClient(
                execution=self.context.recorder,
                state_id=None,
                run_id=self.context.run_id,
                operation_id=self.context.operation_id,
                coordination_token=self.context.coordination_token,
                telemetry_emit=self.context.telemetry_emit,
                max_response_body_bytes=1024,
            )
        ) as client:
            response, _, call = client.request_ssrf_safe(
                "POST",
                target,
                json=deep_thaw(request.json_body),
                before_send=self.context.before_send,
            )
        assert call.request_ref is not None
        assert call.response_ref is not None
        self.context.before_send()
        return SinkEffectHTTPPostResponse(
            status_code=response.status_code,
            content_type=response.headers["content-type"],
            body=response.content,
            call_id=call.call_id,
            request_ref=call.request_ref,
            response_ref=call.response_ref,
        )


class _AuditedFactory(SinkEffectHTTPPostFactory):
    @property
    def safe_config_fingerprint(self) -> str:
        return "a" * 64

    def bind(self, context: SinkEffectHTTPBindContext) -> SinkEffectHTTPPost:
        return _AuditedPost(context)


class _FailingBindFactory(SinkEffectHTTPPostFactory):
    def __init__(self, return_impostor: bool = False) -> None:
        self.retained_context: SinkEffectHTTPBindContext | None = None
        self.return_impostor = return_impostor

    @property
    def safe_config_fingerprint(self) -> str:
        return "a" * 64

    def bind(self, context: SinkEffectHTTPBindContext) -> SinkEffectHTTPPost:
        self.retained_context = context
        if self.return_impostor:
            # Exercise runtime admission even when an annotated implementation
            # dishonestly claims its unrelated object implements the ABC.
            return cast(SinkEffectHTTPPost, object())
        raise RuntimeError("controlled binding failure")


@pytest.mark.parametrize("return_impostor", [False, True])
def test_failed_factory_binding_revokes_retained_attempt_guard(return_impostor):
    db = make_landscape_db()
    try:
        recorder = make_factory(db)
        run_id, sink_id, members = _pipeline_members(recorder, 1)
        http_factory = _FailingBindFactory(return_impostor)
        expected_error = FrameworkBugError if return_impostor else RuntimeError
        expected_message = "non-nominal attempt capability" if return_impostor else "controlled binding failure"
        with pytest.raises(expected_error, match=expected_message):
            SinkEffectCoordinator(
                factory=recorder,
                worker_id="worker-a",
                coordination_token=leader_token_for(db, run_id),
                http_post_factory=http_factory,
                http_environment=SinkEffectHTTPEnvironment(lambda event: None),
            ).execute(_execution_request(run_id, sink_id, members), _HTTPMemberSink())
        assert http_factory.retained_context is not None
        assert recorder.execution.sink_effects.get_attempts_for_run(run_id)[-1].state.value == "response_lost"
        with pytest.raises(FrameworkBugError, match="authority was revoked"):
            http_factory.retained_context.before_send()
    finally:
        db.close()


@respx.mock
def test_actual_http_and_synthetic_member_results_have_distinct_operation_indices_and_payload_refs():
    db = make_landscape_db()
    try:
        recorder = make_factory(db)
        run_id, sink_id, members = _pipeline_members(recorder, 1)
        route = respx.post("https://93.184.216.34/invoke").mock(
            return_value=httpx.Response(200, json={"result": "controlled"}),
        )
        SinkEffectCoordinator(
            factory=recorder,
            worker_id="worker-a",
            coordination_token=leader_token_for(db, run_id),
            http_post_factory=_AuditedFactory(),
            http_environment=SinkEffectHTTPEnvironment(lambda event: None),
        ).execute(_execution_request(run_id, sink_id, members), _HTTPMemberSink())
        assert route.call_count == 2
        (operation,) = recorder.execution.get_operations_for_run(run_id)
        calls = recorder.execution.get_operation_calls(operation.operation_id)
        assert [call.call_index for call in calls] == [0, 1, 2, 3, 4]
        assert len({call.call_id for call in calls}) == 5
        assert all(call.operation_id == operation.operation_id and call.state_id is None for call in calls)
        actual_http_calls = [calls[1], calls[3]]
        synthetic_calls = [calls[0], calls[2], calls[4]]
        assert all(call.request_ref is not None and call.response_ref is not None for call in actual_http_calls)
        assert all(call.request_ref is None and call.response_ref is None for call in synthetic_calls)
        attempts = recorder.execution.sink_effects.get_attempts_for_run(run_id)
        assert [attempt.action.value for attempt in attempts] == ["inspect", "reconcile", "commit"]
        assert [attempt.request_hash for attempt in attempts] == [call.request_hash for call in synthetic_calls]
    finally:
        db.close()
