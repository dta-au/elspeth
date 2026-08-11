"""Caller-level contract for durable sink-effect execution."""

from __future__ import annotations

import ast
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from elspeth.contracts import CallType, NodeStateStatus, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    MemberSinkEffectCapability,
    RestagingSinkEffectCapability,
    RestrictedSinkEffectContext,
    SinkEffectAttemptAction,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectMemberCandidate,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
)
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution import sink_effect_lifecycle
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_finalization import SinkEffectFinalizationMember
from elspeth.core.landscape.execution.sink_effect_identity import compute_pipeline_effect_identity, resolve_sink_effect_members
from elspeth.core.landscape.execution.sink_effect_lifecycle import SinkEffectAttemptRequest, SinkEffectAttemptResult
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import node_states_table, sink_effect_attempts_table
from elspeth.engine.clock import MockClock
from elspeth.engine.executors.sink_effects import (
    SinkEffectCoordinator,
    SinkEffectExecutionRequest,
    SinkEffectExecutionSeam,
    SinkEffectInjectedFault,
    SinkEffectLeaseHeld,
)
from elspeth.plugins.sinks import _local_file_effects as local_effects
from elspeth.plugins.sinks.csv_sink import CSVSink
from elspeth.plugins.sinks.document_sink import DocumentSink
from tests.fixtures.base_classes import inject_write_failure
from tests.fixtures.landscape import make_factory, make_landscape_db, register_test_node
from tests.fixtures.sink_effects import DuplicateObservableSink, DuplicateObservableTarget
from tests.fixtures.stores import MockPayloadStore
from tests.unit.core.landscape.test_sink_effect_reservation import _pipeline_members, _pipeline_request

_ROOT = Path(__file__).parents[3]


@dataclass(slots=True)
class _CumulativeTarget:
    effect_id: str | None = None
    descriptor: ArtifactDescriptor | None = None
    published_rows: list[list[dict[str, object]]] = field(default_factory=list)


class _CumulativeObservableSink:
    effect_call_type = CallType.FILESYSTEM

    def __init__(self, target: _CumulativeTarget) -> None:
        self._target = target
        self._rows_by_effect: dict[str, list[dict[str, object]]] = {}
        self._accepted_by_effect: dict[str, tuple[int, ...]] = {}
        self.inspect_calls = 0
        self.prepare_calls = 0
        self.reconcile_calls = 0
        self.commit_calls = 0

    def inspect_effect(
        self,
        request: SinkEffectInspectionRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectInspection:
        del request, ctx
        self.inspect_calls += 1
        return SinkEffectInspection(
            mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            reference="no-inspection-required:v1",
            evidence={},
        )

    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        del ctx
        self.prepare_calls += 1
        assert isinstance(request.effect_input, SinkEffectPipelineMembersInput)
        rows = [deep_thaw(member.row) for member in request.effect_input.target_snapshot_members]
        assert all(isinstance(row, dict) for row in rows)
        payload_hash = stable_hash(rows)
        descriptor = ArtifactDescriptor.for_file(
            path="file:///tmp/cumulative-observable.jsonl",
            content_hash=payload_hash,
            size_bytes=len(rows),
        )
        self._rows_by_effect[request.effect_id] = rows
        self._accepted_by_effect[request.effect_id] = tuple(member.ordinal for member in request.effect_input.members)
        return SinkEffectPlan(
            effect_id=request.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=request.input_kind,
            descriptor_mode=SinkEffectDescriptorMode.PRECOMPUTED,
            inspection_mode=request.inspection.mode,
            target=descriptor.path_or_uri,
            plan_hash=stable_hash(
                {
                    "descriptor": descriptor.content_hash,
                    "effect_id": request.effect_id,
                    "schema": "cumulative-observable-plan-v1",
                }
            ),
            payload_hash=payload_hash,
            expected_descriptor=descriptor,
            safe_evidence={"inspection_reference": request.inspection.reference},
        )

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del ctx
        self.commit_calls += 1
        assert plan.expected_descriptor is not None
        self._target.effect_id = plan.effect_id
        self._target.descriptor = plan.expected_descriptor
        self._target.published_rows.append(self._rows_by_effect[plan.effect_id])
        return SinkEffectCommitResult(
            descriptor=plan.expected_descriptor,
            evidence={"effect_id": plan.effect_id},
            accepted_ordinals=self._accepted_by_effect[plan.effect_id],
            diverted_ordinals=(),
        )

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult:
        del ctx
        self.reconcile_calls += 1
        if self._target.effect_id == plan.effect_id and self._target.descriptor == plan.expected_descriptor:
            assert self._target.descriptor is not None
            return SinkEffectReconcileResult.applied(self._target.descriptor, evidence={"effect_id": plan.effect_id})
        return SinkEffectReconcileResult.not_applied(evidence={"target": "not_applied"})


class _PrepareFailsOnceSink(_CumulativeObservableSink):
    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        if self.prepare_calls == 0:
            self.prepare_calls += 1
            raise RuntimeError("injected prepare failure after durable inspection")
        return super().prepare_effect(request, ctx)


class _PrecomputedDivertingSink(_CumulativeObservableSink):
    """Remote-object shape: diversion is fixed durably when the plan binds."""

    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        plan = super().prepare_effect(request, ctx)
        return SinkEffectPlan(
            effect_id=plan.effect_id,
            protocol_version=plan.protocol_version,
            input_kind=plan.input_kind,
            descriptor_mode=plan.descriptor_mode,
            inspection_mode=plan.inspection_mode,
            target=plan.target,
            plan_hash=plan.plan_hash,
            payload_hash=plan.payload_hash,
            expected_descriptor=plan.expected_descriptor,
            safe_evidence={
                **dict(plan.safe_evidence),
                "accepted_ordinals": [0],
                "diversion_attribution": [
                    {
                        "error_hash": "b" * 16,
                        "ordinal": 1,
                        "reason_hash": "a" * 64,
                    }
                ],
                "diverted_ordinals": [1],
            },
        )

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del ctx
        self.commit_calls += 1
        assert plan.expected_descriptor is not None
        self._target.effect_id = plan.effect_id
        self._target.descriptor = plan.expected_descriptor
        self._target.published_rows.append([self._rows_by_effect[plan.effect_id][0]])
        return SinkEffectCommitResult(
            descriptor=plan.expected_descriptor,
            evidence={"effect_id": plan.effect_id},
            accepted_ordinals=(0,),
            diverted_ordinals=(1,),
        )


class _ResultDerivedReconciledSink(_CumulativeObservableSink):
    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        del ctx
        self.prepare_calls += 1
        assert isinstance(request.effect_input, SinkEffectPipelineMembersInput)
        payload_hash = stable_hash([deep_thaw(member.row) for member in request.effect_input.members])
        descriptor = ArtifactDescriptor(
            artifact_type="database",
            path_or_uri="database-result:sha256:" + "a" * 64,
            content_hash=payload_hash,
            size_bytes=1,
            metadata={"table": "output", "row_count": 1},
        )
        self._target.effect_id = request.effect_id
        self._target.descriptor = descriptor
        return SinkEffectPlan(
            effect_id=request.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=request.input_kind,
            descriptor_mode=SinkEffectDescriptorMode.RESULT_DERIVED,
            inspection_mode=request.inspection.mode,
            target=descriptor.path_or_uri,
            plan_hash=stable_hash({"effect_id": request.effect_id, "payload_hash": payload_hash}),
            payload_hash=payload_hash,
            expected_descriptor=None,
            safe_evidence={"inspection_reference": request.inspection.reference},
        )

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult:
        del ctx
        self.reconcile_calls += 1
        assert self._target.descriptor is not None
        descriptor = self._target.descriptor
        return SinkEffectReconcileResult.applied(
            descriptor,
            evidence={
                "accepted_ordinals": [0],
                "descriptor": {
                    "artifact_type": descriptor.artifact_type,
                    "content_hash": descriptor.content_hash,
                    "metadata": deep_thaw(descriptor.metadata),
                    "path_or_uri": descriptor.path_or_uri,
                    "size_bytes": descriptor.size_bytes,
                },
                "diversion_attribution": [
                    {
                        "error_hash": "b" * 16,
                        "ordinal": 1,
                        "reason_hash": "a" * 64,
                    }
                ],
                "diverted_ordinals": [1],
            },
            accepted_ordinals=(0,),
            diverted_ordinals=(1,),
        )

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del plan, ctx
        raise AssertionError("exact result-derived reconciliation must not commit again")


def _execution_request(run_id: str, sink_id: str, members: tuple[object, ...]) -> SinkEffectExecutionRequest:
    typed_members = tuple(members)
    reservation = _pipeline_request(run_id, sink_id, typed_members, replacing_target=True)  # type: ignore[arg-type]
    identity = compute_pipeline_effect_identity(
        run_id=run_id,
        sink_node_id=sink_id,
        role=reservation.role,
        sink_config={"name": "sink"},
        target_config={"path": "out.jsonl"},
        members=reservation.members,
    )
    return SinkEffectExecutionRequest(
        reservation=reservation,
        effect_input=SinkEffectPipelineMembersInput(identity.members, identity.members, len(identity.members)),
        finalization_members=tuple(
            SinkEffectFinalizationMember(
                ordinal=member.ordinal,
                output_data={"row": dict(member.row)},
                duration_ms=0.0,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="cumulative-observable",
            )
            for member in identity.members
        ),
    )


def _coordinator_durable_image(
    factory: RecorderFactory,
    run_id: str,
) -> tuple[
    tuple[tuple[str, int, str | None, bool, bool], ...],
    tuple[tuple[str, str, int], ...],
    tuple[int, ...],
    tuple[tuple[str, bool], ...],
]:
    effects = factory.execution.sink_effects.get_effects_for_run(run_id)
    return (
        tuple(
            (
                effect.state.value,
                effect.generation,
                effect.lease_owner,
                effect.plan_hash is not None,
                effect.inspection_attempt_id is not None,
            )
            for effect in effects
        ),
        tuple(
            (attempt.action.value, attempt.state.value, attempt.generation)
            for attempt in factory.execution.sink_effects.get_attempts_for_run(run_id)
        ),
        tuple(len(factory.execution.sink_effects.get_members(effect.effect_id)) for effect in effects),
        tuple((operation.status, operation.sink_effect_id is not None) for operation in factory.execution.get_operations_for_run(run_id)),
    )


@pytest.mark.parametrize(
    ("seam_value", "expected_image"),
    [
        ("before_reservation", ((), (), (), ())),
        (
            "after_reservation",
            ((("reserved", 0, None, False, False),), (), (1,), (("open", True),)),
        ),
        (
            "after_preparation_claim",
            ((("reserved", 1, "worker-a", False, False),), (), (1,), (("open", True),)),
        ),
        (
            "after_inspection",
            (
                (("reserved", 1, "worker-a", False, False),),
                (("inspect", "returned", 1),),
                (1,),
                (("open", True),),
            ),
        ),
        (
            "after_plan_cas",
            (
                (("prepared", 1, None, True, True),),
                (("inspect", "returned", 1),),
                (1,),
                (("open", True),),
            ),
        ),
    ],
)
def test_committed_coordinator_seam_has_exact_durable_image_and_retry_converges(
    seam_value: str,
    expected_image: tuple[
        tuple[tuple[str, int, str | None, bool, bool], ...],
        tuple[tuple[str, str, int], ...],
        tuple[int, ...],
        tuple[tuple[str, bool], ...],
    ],
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        request = _execution_request(run_id, sink_id, members)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        seam = SinkEffectExecutionSeam(seam_value)
        observed_images: list[object] = []

        def stop_at_committed_boundary(observed: SinkEffectExecutionSeam) -> None:
            if observed is seam:
                observed_images.append(_coordinator_durable_image(factory, run_id))
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                fault_hook=stop_at_committed_boundary,
            ).execute(request, sink)

        assert observed_images == [expected_image]
        assert _coordinator_durable_image(make_factory(db), run_id) == expected_image

        recovered_factory = make_factory(db)
        recovered = SinkEffectCoordinator(factory=recovered_factory, worker_id="worker-a").execute(request, sink)

        assert recovered.effect.state.value == "finalized"
        assert target.published_rows == [[{"ordinal": 0}]]
        assert [operation.status for operation in recovered_factory.execution.get_operations_for_run(run_id)] == ["completed"]
    finally:
        db.close()


def test_sink_effect_capabilities_cannot_be_forged_by_attributes() -> None:
    class _CapabilityPretender(_CumulativeObservableSink):
        supports_member_effects = True

        def restage_effect(self, *args: object) -> None:
            del args

        def commit_member_effect(self, *args: object) -> SinkEffectCommitResult:
            del args
            raise AssertionError("capability probe must not invoke the pretender")

        def reconcile_member_effect(self, *args: object) -> SinkEffectReconcileResult:
            del args
            raise AssertionError("capability probe must not invoke the pretender")

    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        effect_input = _execution_request(run_id, sink_id, members).effect_input
        pretender = _CapabilityPretender(_CumulativeTarget())

        assert not SinkEffectCoordinator._is_restaging_adapter(pretender, effect_input)
        assert not SinkEffectCoordinator._is_member_effect_adapter(pretender, effect_input)
    finally:
        db.close()


def test_sink_effect_capabilities_require_nominal_opt_in() -> None:
    class _DeclaredCapabilities(
        _CumulativeObservableSink,
        MemberSinkEffectCapability,
        RestagingSinkEffectCapability,
    ):
        def restage_effect(
            self,
            plan: SinkEffectPlan,
            effect_input: SinkEffectPipelineMembersInput,
            ctx: RestrictedSinkEffectContext,
        ) -> None:
            del plan, effect_input, ctx

        def commit_member_effect(
            self,
            plan: SinkEffectPlan,
            member: SinkEffectMember,
            effect_input: SinkEffectPipelineMembersInput,
            ctx: RestrictedSinkEffectContext,
        ) -> SinkEffectCommitResult:
            del plan, member, effect_input, ctx
            raise AssertionError("capability probe must not invoke the adapter")

        def reconcile_member_effect(
            self,
            plan: SinkEffectPlan,
            member: SinkEffectMember,
            effect_input: SinkEffectPipelineMembersInput,
            ctx: RestrictedSinkEffectContext,
        ) -> SinkEffectReconcileResult:
            del plan, member, effect_input, ctx
            raise AssertionError("capability probe must not invoke the adapter")

    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        effect_input = _execution_request(run_id, sink_id, members).effect_input
        adapter = _DeclaredCapabilities(_CumulativeTarget())

        assert SinkEffectCoordinator._is_restaging_adapter(adapter, effect_input)
        assert SinkEffectCoordinator._is_member_effect_adapter(adapter, effect_input)
    finally:
        db.close()


_SINK_PROTOCOL_ANNOTATIONS = frozenset({"SinkEffectProtocol", "SinkProtocol"})


def _is_sink_protocol_annotation(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name) and node.id in _SINK_PROTOCOL_ANNOTATIONS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _SINK_PROTOCOL_ANNOTATIONS:
            return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(name in node.value for name in _SINK_PROTOCOL_ANNOTATIONS)
        ):
            return True
    return False


def _cast_sink_protocol(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Name) or call.func.id != "cast" or not call.args:
        return False
    return _is_sink_protocol_annotation(call.args[0])


def _annotation_type_name(annotation: ast.expr | None) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Attribute):
        return annotation.attr
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value.rsplit(".", maxsplit=1)[-1]
    return None


class _ScopeSinkBindings(ast.NodeVisitor):
    """Collect sink-protocol bindings without descending into nested scopes."""

    def __init__(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        arguments = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        self.local_names = {argument.arg for argument in arguments}
        self.names = {argument.arg for argument in arguments if _is_sink_protocol_annotation(argument.annotation)}
        self.object_types = {
            argument.arg: type_name for argument in arguments if (type_name := _annotation_type_name(argument.annotation)) is not None
        }
        for variadic in (node.args.vararg, node.args.kwarg):
            if variadic is not None:
                self.local_names.add(variadic.arg)
                if _is_sink_protocol_annotation(variadic.annotation):
                    self.names.add(variadic.arg)
        self._aliases: list[tuple[str, str]] = []
        for statement in node.body:
            self.visit(statement)
        while True:
            discovered = {target for target, source in self._aliases if source in self.names}
            if discovered <= self.names:
                break
            self.names.update(discovered)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        del node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        del node

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        del node

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self.local_names.add(node.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _is_sink_protocol_annotation(node.annotation):
            self.names.add(node.target.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if isinstance(node.value, ast.Name):
            self._aliases.extend((target, node.value.id) for target in targets)
        elif isinstance(node.value, ast.Call) and _cast_sink_protocol(node.value):
            self.names.update(targets)
        self.generic_visit(node)


def _is_bound_sink_receiver(
    receiver: ast.expr,
    *,
    sink_bindings: frozenset[str],
    object_types: dict[str, str],
    class_sink_attributes: dict[str, frozenset[str]],
) -> bool:
    if isinstance(receiver, ast.Name):
        return receiver.id in sink_bindings
    if isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name):
        receiver_type = object_types.get(receiver.value.id)
        return receiver_type is not None and receiver.attr in class_sink_attributes.get(receiver_type, ())
    return isinstance(receiver, ast.Call) and _cast_sink_protocol(receiver)


def _is_precise_non_sink_write(
    call: ast.Call,
    *,
    os_module_names: frozenset[str],
    local_names: frozenset[str],
) -> bool:
    """Exclude module APIs whose two-argument shape cannot dispatch a sink."""
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in os_module_names
        and call.func.value.id not in local_names
    )


class _LegacySinkPublicationVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        os_module_names: frozenset[str],
        class_sink_attributes: dict[str, frozenset[str]],
    ) -> None:
        self.violations: list[tuple[int, int, str]] = []
        self._sink_binding_scopes: list[frozenset[str]] = [frozenset()]
        self._local_name_scopes: list[frozenset[str]] = [frozenset()]
        self._object_type_scopes: list[dict[str, str]] = [{}]
        self._os_module_names = os_module_names
        self._class_sink_attributes = class_sink_attributes

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        bindings = _ScopeSinkBindings(node)
        self._sink_binding_scopes.append(frozenset(bindings.names))
        self._local_name_scopes.append(frozenset(bindings.local_names))
        self._object_type_scopes.append(bindings.object_types)
        for statement in node.body:
            self.visit(statement)
        self._object_type_scopes.pop()
        self._local_name_scopes.pop()
        self._sink_binding_scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"flush", "write"}:
            bound_sink = _is_bound_sink_receiver(
                node.func.value,
                sink_bindings=self._sink_binding_scopes[-1],
                object_types=self._object_type_scopes[-1],
                class_sink_attributes=self._class_sink_attributes,
            )
            # SinkProtocol.write has exactly the rows/context argument pair. Treat
            # that call shape as publication regardless of receiver spelling or
            # expression shape; only exact module functions that cannot dispatch
            # a sink are excluded. A typed sink receiver is also forbidden even
            # if a caller spells the arguments incorrectly or calls flush alone.
            legacy_write_shape = (
                node.func.attr == "write"
                and len(node.args) + len(node.keywords) == 2
                and not _is_precise_non_sink_write(
                    node,
                    os_module_names=self._os_module_names,
                    local_names=self._local_name_scopes[-1],
                )
            )
            if bound_sink or legacy_write_shape:
                self.violations.append((node.lineno, node.col_offset, node.func.attr))
        self.generic_visit(node)


def _legacy_sink_publication_calls(source_path: Path) -> list[tuple[int, int, str]]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    os_module_names = frozenset(
        alias.asname or alias.name
        for statement in tree.body
        if isinstance(statement, ast.Import)
        for alias in statement.names
        if alias.name == "os"
    )
    class_sink_attributes = {
        statement.name: frozenset(
            member.target.id
            for member in statement.body
            if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name) and _is_sink_protocol_annotation(member.annotation)
        )
        for statement in tree.body
        if isinstance(statement, ast.ClassDef)
    }
    visitor = _LegacySinkPublicationVisitor(
        os_module_names=os_module_names,
        class_sink_attributes=class_sink_attributes,
    )
    visitor.visit(tree)
    return visitor.violations


def test_legacy_publication_guard_detects_renamed_sink_receiver(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "src/elspeth/engine/new_publication_path.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        """from elspeth.contracts import SinkProtocol

def publish(destination: SinkProtocol, rows: list[dict[str, object]], context: object) -> None:
    destination.write(rows, context)
    destination.flush()

def publish_through_attribute(runtime: object, rows: list[dict[str, object]], context: object) -> None:
    runtime.destination.write(rows=rows, ctx=context)

def shadow_module_name(os: object, rows: list[dict[str, object]], context: object) -> None:
    os.write(rows, context)

class Runtime:
    destination: SinkProtocol

def flush_through_attribute(runtime: Runtime) -> None:
    runtime.destination.flush()
""",
        encoding="utf-8",
    )

    assert _legacy_sink_publication_calls(source_path) == [
        (4, 4, "write"),
        (5, 4, "flush"),
        (8, 4, "write"),
        (11, 4, "write"),
        (17, 4, "flush"),
    ]


def test_legacy_publication_guard_excludes_unrelated_write_and_flush_apis(tmp_path: Path) -> None:
    source_path = tmp_path / "src/elspeth/core/unrelated_io.py"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(
        """import os

def persist(stream: object, descriptor: int, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()
    os.write(descriptor, payload)
""",
        encoding="utf-8",
    )

    assert _legacy_sink_publication_calls(source_path) == []


def test_production_tree_has_no_legacy_write_or_flush_publication_boundary() -> None:
    violations = {
        str(source_path.relative_to(_ROOT)): calls
        for source_path in sorted((_ROOT / "src/elspeth").rglob("*.py"))
        if (calls := _legacy_sink_publication_calls(source_path))
    }

    assert violations == {}


def test_result_derived_reconciliation_finalizes_exact_marker_partition() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 2)
        sink = _ResultDerivedReconciledSink(_CumulativeTarget())

        result = SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(
            _execution_request(run_id, sink_id, members),
            sink,
        )

        assert len(result.state_ids) == 1
        assert len(result.outcome_ids) == 1
        assert sink.commit_calls == 0
        assert sink.reconcile_calls == 1
    finally:
        db.close()


def test_replacing_successor_prepares_cumulative_predecessor_and_current_members() -> None:
    db = make_landscape_db()
    try:
        payload_store = MockPayloadStore()
        factory = make_factory(db, payload_store=payload_store)
        run_id, sink_id, members = _pipeline_members(factory, 2)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)

        SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(_execution_request(run_id, sink_id, members[:1]), sink)
        recovered_factory = make_factory(db, payload_store=payload_store)
        successor = SinkEffectCoordinator(factory=recovered_factory, worker_id="worker-b").execute(
            _execution_request(run_id, sink_id, members[1:]), sink
        )

        assert successor.effect.stream_sequence == 1
        assert target.published_rows == [[{"ordinal": 0}], [{"ordinal": 0}, {"ordinal": 1}]]
    finally:
        db.close()


def test_append_mode_successor_preserves_pre_run_baseline(tmp_path: Path) -> None:
    db = make_landscape_db()
    try:
        payload_store = MockPayloadStore()
        factory = make_factory(db, payload_store=payload_store)
        run_id, sink_id, members = _pipeline_members(factory, 2)
        target = tmp_path / "append.csv"
        target.write_text("ordinal\n99\n")
        config = {
            "path": str(target),
            "schema": {"mode": "observed"},
            "mode": "append",
            "collision_policy": "append_or_create",
        }

        SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(
            _execution_request(run_id, sink_id, members[:1]), CSVSink(config)
        )
        assert target.read_text() == "ordinal\n99\n0\n"

        successor_factory = make_factory(db, payload_store=payload_store)
        SinkEffectCoordinator(factory=successor_factory, worker_id="worker-b").execute(
            _execution_request(run_id, sink_id, members[1:]), CSVSink(config)
        )
        assert target.read_text() == "ordinal\n99\n0\n1\n"
    finally:
        db.close()


def test_predecessor_snapshot_excludes_diverted_members() -> None:
    """Cumulative successors must not replay members the predecessor diverted
    away from the target (elspeth-0278416cc5)."""
    db = make_landscape_db()
    try:
        payload_store = MockPayloadStore()
        factory = make_factory(db, payload_store=payload_store)
        run_id, sink_id, members = _pipeline_members(factory, 3)
        # Predecessor accepts ordinal 0 and diverts ordinal 1.
        first_sink = _ResultDerivedReconciledSink(_CumulativeTarget())
        SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(
            _execution_request(run_id, sink_id, members[:2]),
            first_sink,
        )

        target = _CumulativeTarget()
        successor_sink = _CumulativeObservableSink(target)
        successor_factory = make_factory(db, payload_store=payload_store)
        SinkEffectCoordinator(factory=successor_factory, worker_id="worker-b").execute(
            _execution_request(run_id, sink_id, members[2:]),
            successor_sink,
        )

        # The diverted row {"ordinal": 1} never reached the target, so the
        # cumulative successor must publish only the accepted predecessor
        # member plus its own current member.
        assert target.published_rows == [[{"ordinal": 0}, {"ordinal": 2}]]
    finally:
        db.close()


def _document_pipeline_members(
    factory: RecorderFactory,
    values: tuple[str, ...],
) -> tuple[str, str, tuple[SinkEffectMember, ...]]:
    """`_pipeline_members`, but rows carry a document field so a real
    DocumentSink can be driven through the coordinator."""
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source_id = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
    sink_id = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="sink")
    candidates: list[SinkEffectMemberCandidate] = []
    for ordinal, value in enumerate(values):
        payload = {"announcement_text": value}
        row = factory.data_flow.create_row(
            run_id=run.run_id,
            source_node_id=source_id,
            row_index=ordinal,
            data=payload,
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        token = factory.data_flow.create_token(row.row_id)
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink_id,
            run_id=run.run_id,
            step_index=0,
            input_data=payload,
        )
        candidates.append(SinkEffectMemberCandidate(token_id=token.token_id, row=payload))
    return run.run_id, sink_id, resolve_sink_effect_members(factory, candidates)


def test_document_one_value_rule_refuses_across_sink_instances(tmp_path: Path) -> None:
    """A FRESH sink instance must still see the whole run's delivery (elspeth-694f771c69).

    Instance A's two rows both divert under the one-value rule, so they are
    durably recorded but deliberately dropped from the cumulative snapshot —
    and absent from anything instance B holds in memory. Every count B can
    see locally reads one, yet the run truly delivered three rows to this
    output, so publishing a lone-row document would report success while
    silently discarding two-thirds of the delivery. The delivery count must
    be a durable fact of the target's effect stream, not of the instance.
    """
    db = make_landscape_db()
    try:
        payload_store = MockPayloadStore()
        factory = make_factory(db, payload_store=payload_store)
        run_id, sink_id, members = _document_pipeline_members(factory, ("alpha", "beta", "gamma"))
        target = tmp_path / "announcement.txt"
        config = {
            "path": str(target),
            "field": "announcement_text",
            "schema": {"mode": "observed"},
        }

        instance_a = inject_write_failure(DocumentSink(config))
        first = SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(
            _execution_request(run_id, sink_id, members[:2]), instance_a
        )
        first_durable = factory.execution.sink_effects.get_members(first.effect.effect_id)
        assert [member.prepared_disposition for member in first_durable] == ["diverted", "diverted"]
        assert not target.exists()

        instance_b = inject_write_failure(DocumentSink(config))
        successor_factory = make_factory(db, payload_store=payload_store)
        second = SinkEffectCoordinator(factory=successor_factory, worker_id="worker-b").execute(
            _execution_request(run_id, sink_id, members[2:]), instance_b
        )

        second_durable = successor_factory.execution.sink_effects.get_members(second.effect.effect_id)
        assert [member.prepared_disposition for member in second_durable] == ["diverted"]
        assert not target.exists(), "a run that delivered three rows must never publish a lone-row document"
    finally:
        db.close()


def test_mixed_overlap_recovers_open_effect_and_executes_new_partition() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 4)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        coordinator = SinkEffectCoordinator(factory=factory, worker_id="worker-a")

        first = coordinator.execute(_execution_request(run_id, sink_id, members[:1]), sink)
        opened = factory.execution.sink_effects.reserve(_pipeline_request(run_id, sink_id, members[1:3], replacing_target=True)).new_effect
        assert opened is not None and opened.predecessor_effect_id == first.effect.effect_id

        result = coordinator.execute(_execution_request(run_id, sink_id, members), sink)

        assert result.effect.stream_sequence == 2
        assert target.published_rows == [
            [{"ordinal": 0}],
            [{"ordinal": 0}, {"ordinal": 1}, {"ordinal": 2}],
            [{"ordinal": 0}, {"ordinal": 1}, {"ordinal": 2}, {"ordinal": 3}],
        ]
    finally:
        db.close()


def test_second_preparer_refuses_while_preparation_claim_is_live() -> None:
    """A rival worker must never run side-effecting preparation while another
    worker's durable preparation claim is live (elspeth-3f87c0c055)."""
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        request = _execution_request(run_id, sink_id, members)
        rival_factory = make_factory(db)
        rival_sink = _CumulativeObservableSink(target)

        class _RacingSink(_CumulativeObservableSink):
            def prepare_effect(
                self,
                inner_request: SinkEffectPrepareRequest,
                ctx: RestrictedSinkEffectContext,
            ) -> SinkEffectPlan:
                if self.prepare_calls == 0:
                    # Simulate a concurrent worker arriving mid-preparation:
                    # it must refuse before invoking any adapter method.
                    with pytest.raises(SinkEffectLeaseHeld, match="preparation"):
                        SinkEffectCoordinator(factory=rival_factory, worker_id="worker-b").execute(request, rival_sink)
                return super().prepare_effect(inner_request, ctx)

        sink = _RacingSink(target)
        result = SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, sink)

        assert result.effect.state.value == "finalized"
        assert sink.prepare_calls == 1
        # The rival never mutated staging: no inspect, prepare, or commit calls.
        assert (rival_sink.inspect_calls, rival_sink.prepare_calls, rival_sink.commit_calls) == (0, 0, 0)
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


@pytest.mark.parametrize(
    ("missing_container", "missing_key"),
    [
        ("plan", "expected_descriptor"),
        ("descriptor", "metadata"),
    ],
)
def test_load_plan_rejects_missing_required_durable_fields(
    missing_container: str,
    missing_key: str,
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)

        def fail_before_effect(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                fault_hook=fail_before_effect,
            ).execute(
                _execution_request(run_id, sink_id, members),
                _CumulativeObservableSink(_CumulativeTarget()),
            )

        effects = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert len(effects) == 1
        assert effects[0].plan_json is not None
        payload = json.loads(effects[0].plan_json)
        assert type(payload) is dict
        if missing_container == "plan":
            del payload[missing_key]
        else:
            descriptor = payload["expected_descriptor"]
            assert type(descriptor) is dict
            del descriptor[missing_key]

        malformed = replace(effects[0], plan_json=json.dumps(payload))
        with pytest.raises(LandscapeRecordError, match="durable plan is incomplete or divergent") as exc_info:
            SinkEffectCoordinator._load_plan(malformed)

        assert isinstance(exc_info.value.__cause__, KeyError)
        assert exc_info.value.__cause__.args == (missing_key,)
    finally:
        db.close()


def test_preparation_claim_stays_live_while_adapter_installs_local_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow preparer must keep its claim while adapter I/O is in progress."""
    db = make_landscape_db()
    release_install = threading.Event()
    install_entered = threading.Event()
    rival_errors: list[BaseException] = []
    stale_errors: list[BaseException] = []
    lease_ttl = timedelta(milliseconds=50)
    target = tmp_path / "out.csv"
    sink_config = {"path": str(target), "schema": {"mode": "observed"}}
    original_replace = local_effects.os.replace

    def block_stale_install(source: str | Path, destination: str | Path) -> None:
        if threading.current_thread().name == "stale-preparer" and str(destination).endswith(".stage"):
            install_entered.set()
            if not release_install.wait(timeout=5):
                raise AssertionError("timed out waiting to release stale stage installation")
        original_replace(source, destination)

    def fail_before_effect(seam: SinkEffectExecutionSeam) -> None:
        if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
            raise SinkEffectInjectedFault(seam)

    monkeypatch.setattr(local_effects.os, "replace", block_stale_install)
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        request = _execution_request(run_id, sink_id, members)

        def run_stale_preparer() -> None:
            try:
                SinkEffectCoordinator(
                    factory=factory,
                    worker_id="worker-a",
                    lease_ttl=lease_ttl,
                    fault_hook=fail_before_effect,
                ).execute(request, CSVSink(sink_config))
            except BaseException as exc:
                stale_errors.append(exc)

        stale_thread = threading.Thread(target=run_stale_preparer, name="stale-preparer")
        stale_thread.start()
        assert install_entered.wait(timeout=5), "stale preparer never reached stage installation"

        # Stay blocked for multiple original lease windows. A preparation
        # heartbeat must keep worker B out before it invokes adapter I/O.
        threading.Event().wait(lease_ttl.total_seconds() * 3)
        try:
            SinkEffectCoordinator(
                factory=make_factory(db),
                worker_id="worker-b",
                lease_ttl=timedelta(seconds=1),
                fault_hook=fail_before_effect,
            ).execute(request, CSVSink(sink_config))
        except BaseException as exc:
            rival_errors.append(exc)

        release_install.set()
        stale_thread.join(timeout=5)
        assert not stale_thread.is_alive(), "stale preparer thread wedged"

        effects = factory.execution.sink_effects.get_effects_for_run(run_id)
        assert len(effects) == 1
        plan = SinkEffectCoordinator._load_plan(effects[0])
        staging = Path(str(plan.safe_evidence["staging_path"]))
        assert local_effects._snapshot(staging).file_id == plan.safe_evidence["staged_file_id"], (
            "durable plan points at staging inode replaced by stale preparer"
        )
        assert len(rival_errors) == 1
        assert isinstance(rival_errors[0], SinkEffectLeaseHeld)
        assert len(stale_errors) == 1
        assert isinstance(stale_errors[0], SinkEffectInjectedFault)
    finally:
        release_install.set()
        db.close()


def test_mixed_overlap_waits_for_live_open_partition_before_executing_new() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 2)
        sink = _CumulativeObservableSink(_CumulativeTarget())

        def fail_before_effect(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                fault_hook=fail_before_effect,
            ).execute(_execution_request(run_id, sink_id, members[:1]), sink)
        calls_before_wait = (sink.inspect_calls, sink.prepare_calls, sink.reconcile_calls, sink.commit_calls)

        with pytest.raises(SinkEffectLeaseHeld, match="live lease"):
            SinkEffectCoordinator(factory=factory, worker_id="worker-b").execute(_execution_request(run_id, sink_id, members), sink)

        assert (sink.inspect_calls, sink.prepare_calls, sink.reconcile_calls, sink.commit_calls) == calls_before_wait
        reserved_new = factory.execution.sink_effects.reserve(
            _pipeline_request(run_id, sink_id, members, replacing_target=True)
        ).open_effect_ids
        assert len(reserved_new) == 2
    finally:
        db.close()


@pytest.mark.parametrize("terminal_status", (NodeStateStatus.COMPLETED, NodeStateStatus.FAILED))
def test_non_open_latest_state_refuses_before_sink_io(terminal_status: NodeStateStatus) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        with db.engine.begin() as conn:
            conn.execute(
                update(node_states_table)
                .where(
                    node_states_table.c.run_id == run_id,
                    node_states_table.c.node_id == sink_id,
                    node_states_table.c.token_id == members[0].token_id,
                )
                .values(status=terminal_status.value)
            )
        sink = _CumulativeObservableSink(_CumulativeTarget())

        with pytest.raises(ValueError, match="latest sink-node state must be open"):
            SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(
                _execution_request(run_id, sink_id, members),
                sink,
            )

        assert (sink.inspect_calls, sink.prepare_calls, sink.reconcile_calls, sink.commit_calls) == (0, 0, 0, 0)
    finally:
        db.close()


def test_retry_reuses_durable_returned_inspection_without_second_provider_call() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        sink = _PrepareFailsOnceSink(target)
        request = _execution_request(run_id, sink_id, members)

        with pytest.raises(RuntimeError, match="prepare failure"):
            SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, sink)
        result = SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, sink)

        assert result.effect.state.value == "finalized"
        assert sink.inspect_calls == 1
        assert sink.prepare_calls == 2
    finally:
        db.close()


def test_same_generation_retry_closes_abandoned_commit_intent_before_new_call() -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        request = _execution_request(run_id, sink_id, members)

        def fail_before_effect(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                fault_hook=fail_before_effect,
            ).execute(request, sink)
        SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, sink)

        with db.read_only_connection() as conn:
            commits = conn.execute(
                sink_effect_attempts_table.select()
                .where(sink_effect_attempts_table.c.action == "commit")
                .order_by(sink_effect_attempts_table.c.started_at, sink_effect_attempts_table.c.attempt_id)
            ).fetchall()
        # Generation 1 is consumed by the preparation claim; the execution
        # lease (and thus both commit attempts) run at generation 2.
        assert [(row.generation, row.state) for row in commits] == [(2, "response_lost"), (2, "returned")]
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


def test_precomputed_response_loss_reconciles_with_durable_diversion_partition() -> None:
    """A remote write is finalized once from its prepared partition after response loss."""
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 2)
        target = _CumulativeTarget()
        sink = _PrecomputedDivertingSink(target)
        request = _execution_request(run_id, sink_id, members)

        def lose_response_after_publication(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                fault_hook=lose_response_after_publication,
            ).execute(request, sink)

        result = SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, sink)

        assert result.effect.state.value == "finalized"
        assert len(result.state_ids) == 1
        assert len(result.outcome_ids) == 1
        assert sink.commit_calls == 1
        assert sink.reconcile_calls == 2
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


def test_takeover_closes_stale_abandoned_intent_before_new_generation_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        request = _execution_request(run_id, sink_id, members)
        clock = MockClock(start=datetime.now(UTC).timestamp())
        lease_ttl = timedelta(seconds=2)
        monkeypatch.setattr(sink_effect_lifecycle, "now", clock.now_utc)

        def fail_before_effect(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.BEFORE_EFFECT:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                lease_ttl=lease_ttl,
                fault_hook=fail_before_effect,
                clock=clock,
            ).execute(request, sink)
        clock.advance(lease_ttl.total_seconds() + 1)
        SinkEffectCoordinator(factory=factory, worker_id="worker-b", clock=clock).execute(request, sink)

        with db.read_only_connection() as conn:
            commits = conn.execute(
                sink_effect_attempts_table.select()
                .where(sink_effect_attempts_table.c.action == "commit")
                .order_by(sink_effect_attempts_table.c.generation)
            ).fetchall()
        # Generation 1 is the preparation claim, 2 the abandoned execution
        # lease, 3 the takeover under which the retry returns.
        assert [(row.generation, row.state) for row in commits] == [(2, "response_lost"), (3, "returned")]
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


@pytest.mark.parametrize("takeover", (False, True))
def test_retry_consumes_returned_commit_without_another_reconcile(
    takeover: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        request = _execution_request(run_id, sink_id, members)
        clock = MockClock(start=datetime.now(UTC).timestamp())
        takeover_ttl = timedelta(seconds=2)
        monkeypatch.setattr(sink_effect_lifecycle, "now", clock.now_utc)

        def fail_after_return(seam: SinkEffectExecutionSeam) -> None:
            if seam is SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE:
                raise SinkEffectInjectedFault(seam)

        with pytest.raises(SinkEffectInjectedFault):
            SinkEffectCoordinator(
                factory=factory,
                worker_id="worker-a",
                lease_ttl=takeover_ttl if takeover else timedelta(seconds=30),
                fault_hook=fail_after_return,
                clock=clock,
            ).execute(request, sink)
        if takeover:
            clock.advance(takeover_ttl.total_seconds() + 1)
        SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-b" if takeover else "worker-a",
            clock=clock,
        ).execute(request, sink)

        assert sink.commit_calls == 1
        assert sink.reconcile_calls == 1
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


@pytest.mark.parametrize("takeover", (False, True))
def test_retry_consumes_returned_reconcile_before_commit(takeover: bool) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        target = _CumulativeTarget()
        sink = _CumulativeObservableSink(target)
        request = _execution_request(run_id, sink_id, members)
        reserved = factory.execution.sink_effects.reserve(request.reservation).new_effect
        assert reserved is not None
        inspection = SinkEffectInspection(
            mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            reference="no-inspection-required:v1",
            evidence={},
        )
        plan = sink.prepare_effect(
            SinkEffectPrepareRequest(
                effect_id=reserved.effect_id,
                effect_input=request.effect_input,  # type: ignore[arg-type]
                inspection=inspection,
            ),
            RestrictedSinkEffectContext(
                run_id=run_id,
                run_started_at=factory.run_lifecycle.get_run(run_id).started_at,  # type: ignore[union-attr]
                operation_id=next(
                    operation.operation_id
                    for operation in factory.execution.get_operations_for_run(run_id)
                    if operation.sink_effect_id == reserved.effect_id
                ),
                sink_node_id=sink_id,
            ),
        )
        claim = factory.execution.sink_effects.claim_preparation(
            reserved.effect_id,
            owner="worker-a",
            ttl=timedelta(seconds=30),
        )
        factory.execution.sink_effects.complete_plan(reserved.effect_id, plan, claim=claim)
        lease = factory.execution.sink_effects.acquire_lease(
            reserved.effect_id,
            owner="worker-a",
            ttl=timedelta(microseconds=1) if takeover else timedelta(seconds=30),
        )
        reconciliation = SinkEffectReconcileResult.not_applied(evidence={"target": "not_applied"})
        attempt = factory.execution.sink_effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=reserved.effect_id,
                member_ordinal=None,
                generation=lease.generation,
                action=SinkEffectAttemptAction.RECONCILE,
                call_kind=CallType.FILESYSTEM,
                request_hash=SinkEffectCoordinator._reconcile_request_hash(plan),
            )
        )
        factory.execution.sink_effects.record_attempt_result(
            SinkEffectAttemptResult(
                attempt_id=attempt.attempt_id,
                evidence=encode_sink_effect_returned_result(reconciliation),
                latency_ms=0.0,
            )
        )

        SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-b" if takeover else "worker-a",
        ).execute(request, sink)

        assert sink.reconcile_calls == 0
        assert sink.commit_calls == 1
        assert target.published_rows == [[{"ordinal": 0}]]
    finally:
        db.close()


@pytest.mark.parametrize("seam", list(SinkEffectExecutionSeam))
def test_fresh_executor_retry_publishes_once(seam: SinkEffectExecutionSeam) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        identity = compute_pipeline_effect_identity(
            run_id=run_id,
            sink_node_id=sink_id,
            role=_pipeline_request(run_id, sink_id, members).role,
            sink_config={"name": "duplicate-observable"},
            target_config={"path": "duplicate-observable.jsonl"},
            members=members,
        )
        # Reservation and input identity are independently constructed from the
        # same public configuration, so the coordinator must exact-check them.
        reservation = _pipeline_request(run_id, sink_id, identity.members)
        effect_input = SinkEffectPipelineMembersInput(
            members=identity.members,
            target_snapshot_members=identity.members,
            target_delivered_member_count=len(identity.members),
        )
        request = SinkEffectExecutionRequest(
            reservation=reservation,
            effect_input=effect_input,
            finalization_members=(
                SinkEffectFinalizationMember(
                    ordinal=0,
                    output_data={"ordinal": 0},
                    duration_ms=0.0,
                    outcome=TerminalOutcome.SUCCESS,
                    path=TerminalPath.DEFAULT_FLOW,
                    sink_name="duplicate-observable",
                ),
            ),
        )
        target = DuplicateObservableTarget()
        calls = 0

        def fail_once(observed: SinkEffectExecutionSeam) -> None:
            nonlocal calls
            if observed is seam and calls == 0:
                calls += 1
                raise SinkEffectInjectedFault(seam)

        first = SinkEffectCoordinator(
            factory=factory,
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=30),
            fault_hook=fail_once,
        )
        with pytest.raises(SinkEffectInjectedFault):
            first.execute(request, DuplicateObservableSink(target))

        recovered = SinkEffectCoordinator(
            factory=make_factory(db),
            worker_id="worker-a",
            lease_ttl=timedelta(seconds=30),
        ).execute(request, DuplicateObservableSink(target))

        assert target.publication_count == 1
        assert recovered.effect.effect_id == target.effect_id
        assert recovered.artifact.content_hash == target.descriptor.content_hash  # type: ignore[union-attr]
    finally:
        db.close()


def test_unknown_reconciliation_never_commits() -> None:
    """A divergent external target is a hard stop, never permission to publish."""
    db = make_landscape_db()
    try:
        factory: RecorderFactory = make_factory(db)
        run_id, sink_id, members = _pipeline_members(factory, 1)
        identity = compute_pipeline_effect_identity(
            run_id=run_id,
            sink_node_id=sink_id,
            role=_pipeline_request(run_id, sink_id, members).role,
            sink_config={"name": "duplicate-observable"},
            target_config={"path": "duplicate-observable.jsonl"},
            members=members,
        )
        request = SinkEffectExecutionRequest(
            reservation=_pipeline_request(run_id, sink_id, identity.members),
            effect_input=SinkEffectPipelineMembersInput(identity.members, identity.members, len(identity.members)),
            finalization_members=(
                SinkEffectFinalizationMember(
                    ordinal=0,
                    output_data={"ordinal": 0},
                    duration_ms=0.0,
                    outcome=TerminalOutcome.SUCCESS,
                    path=TerminalPath.DEFAULT_FLOW,
                    sink_name="duplicate-observable",
                ),
            ),
        )
        target = DuplicateObservableTarget(publication_count=1, effect_id="f" * 64)
        with pytest.raises(Exception, match=r"UNKNOWN|unknown|divergent"):
            SinkEffectCoordinator(factory=factory, worker_id="worker-a").execute(request, DuplicateObservableSink(target))
        assert target.publication_count == 1
    finally:
        db.close()
