"""Current Continuation worker reserves on supplied ASTs, never admission.

The historical module name is retained for callers. Deleted V1 worker methods
are replaced by actual adoption and driver reserves, not six empty exemptions.
"""

import ast
import copy
import hashlib
import inspect
import json
import types

from tests.helpers import web_reserve_worker_caller as worker_caller
from tests.helpers import web_worker_whole_scope_origin as source_origins

WORKER = "src/elspeth/web/sessions/composer_async_worker.py"
LIFECYCLE = "src/elspeth/web/coordination/lifecycle.py"
DRIVER = "src/elspeth/web/sessions/continuation_driver.py"


def chain(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = chain(node.value)
        return (*prefix, node.attr) if prefix else ()
    return ()


def find(tree, qualified):
    nodes = tree.body
    owners = []
    for name in qualified.split("."):
        matches = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name]
        if len(matches) != 1:
            return (None, [])
        node = matches[0]
        owners.append(node)
        nodes = node.body
    return (node, owners)


def canonical(value):
    if value is Ellipsis:
        return ["PYTHON_ELLIPSIS_LITERAL"]
    if isinstance(value, bytes):
        return ["PYTHON_BYTES_LITERAL", value.hex()]
    if isinstance(value, complex):
        return ["PYTHON_COMPLEX_LITERAL", value.real, value.imag]
    if isinstance(value, ast.AST):
        return [
            type(value).__name__,
            [[field, canonical(item)] for field, item in ast.iter_fields(value) if item is not None and item != []],
        ]
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def grammar(node, *, prefix=None):
    clone = copy.copy(node)
    clone.body = list(node.body if prefix is None else prefix)
    if (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and (type(clone.body[0].value.value) is str)
    ):
        clone.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(canonical(clone), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def code_for(tree, owners):
    """Compile the caller's actual whole AST for metadata, never execute it."""
    try:
        code = compile(tree, "<worker-caller-source-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return (None, type(error).__name__)
    for owner in owners:
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or owner.type_params:
            return (None, "unsupported definition owner/type parameters")
        start = min([owner.lineno] + [n.lineno for n in owner.decorator_list])
        matches = [
            item
            for item in code.co_consts
            if isinstance(item, types.CodeType) and item.co_name == owner.name and (item.co_firstlineno == start)
        ]
        if len(matches) != 1:
            return (None, "ambiguous actual compiler owner")
        code = matches[0]
    return (code, None)


CONTINUATION_OWNER_GRAMMARS = {
    "ComposerAsyncWorker._job": "aa8d9593bc27624ba0f57d95b39cd1c9c03eb1971e1ba8dcbfd2a07a38899150",
    "ContinuationLeaseAdoption": "6c853b51d24a4cc2d73d8adc70fa44567b25eb671141e558a2cf08b0f5c7c87f",
}

_CONTINUATION_START_PREFIX = """
running = ComposerOperationRunning(claim=claim, session_operation_context=context)
if observation.policy is None or observation.hard_expires_at is None or self._drain_signal is None:
    raise AuditIntegrityError("segment start lost its actual retained timing/process signal owners")
sql_deadline = ContinuationSQLDeadlineOwner(
    running,
    policy=observation.policy,
    drain_signal=self._drain_signal,
    drain_seconds=self._drain_seconds,
    hard_expires_at=observation.hard_expires_at,
)
coordinator = RequiredWorkCoordinator(
    RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt),
    sql_deadline_owner=sql_deadline,
)
self._required_coordinators[key] = coordinator
adoption = ContinuationLeaseAdoption(
    services.session_service.session_operation_authority,
    context,
    lease_seconds=services.session_service.session_operation_lease_seconds,
    required_work=coordinator,
)
self._continuation_adoptions[key] = adoption
adoption.cancellations.extend(cancellations)
"""

_CONTINUATION_ADOPTION_JOIN = """
async def join(self) -> SessionOperationLease:
    if self.lease is None:
        interval = _validate_lifecycle_timing(lease_seconds=self.lease_seconds, renew_interval_seconds=None)
        self.lease = SessionOperationLease.__new__(SessionOperationLease)
        SessionOperationLease.__init__(
            self.lease, self.authority, self.context, lease_seconds=self.lease_seconds,
            renew_interval_seconds=interval, required_work=self.required_work,
        )
    if self.task is None:
        ticket = self.required_work.reserve(RequiredWorkSource.LEASE_ADOPTION)
        self.task = asyncio.create_task(
            run_required_sql_finish_once(ticket, self.authority.compare_and_swap, self.context),
            name="continuation-adoption-compare-and-swap",
        )
    self.outcome = await _join_shielded_task_after_cancellation(self.task, cancellation_observations=self.cancellations)
    for cancellation in self.outcome.deferred_cancellations:
        _retain_cancellation(self.cancellations, cancellation)
    failures = [self.outcome.error] if type(self.outcome) is RequiredSQLRaised else []
    if type(self.outcome) not in (RequiredSQLReturned, RequiredSQLRaised):
        raise contract_errors.AuditIntegrityError("Continuation adoption lost its actual physical CAS result")
    _raise_lifecycle_originals(self.cancellations, failures)
    return self.lease
"""

_CONTINUATION_ADOPTION_CONSTRUCTOR = """
def __init__(
    self, authority: SessionOperationAuthority, context: SessionOperationContext, *,
    lease_seconds: int, required_work: RequiredWorkCoordinator,
) -> None:
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise contract_errors.AuditIntegrityError("Continuation adoption requires the exact started COMPOSE context")
    if type(required_work) is not RequiredWorkCoordinator or required_work.authority.context != context:
        raise contract_errors.AuditIntegrityError("Continuation adoption lost its exact required-work owner")
    self.authority = authority
    self.context = context
    self.lease_seconds = lease_seconds
    self.required_work = required_work
    self.lease: SessionOperationLease | None = None
    self.task: asyncio.Task[RequiredSQLFinishOnce[None]] | None = None
    self.outcome: RequiredSQLFinishOnce[None] | None = None
    self.cancellations: list[asyncio.CancelledError] = []
"""


def continuation_start_owner_contract(units):
    """The actual V2 start/adoption edge, never a generic constructor grant."""
    units = tuple(units)
    index = {unit.path: unit for unit in units}
    if len(index) != len(units) or WORKER not in index or LIFECYCLE not in index:
        return ["worker continuation start: lifecycle source owner missing"], None
    worker, lifecycle = index[WORKER], index[LIFECYCLE]
    worker_class, _ = find(worker.tree, "ComposerAsyncWorker")
    method, owners = find(worker.tree, "ComposerAsyncWorker._job")
    adoption, _ = find(lifecycle.tree, "ContinuationLeaseAdoption")
    if (
        not isinstance(method, ast.AsyncFunctionDef)
        or not isinstance(adoption, ast.ClassDef)
        or grammar(method) != CONTINUATION_OWNER_GRAMMARS["ComposerAsyncWorker._job"]
        or grammar(adoption) != CONTINUATION_OWNER_GRAMMARS["ContinuationLeaseAdoption"]
    ):
        return ["worker continuation start: complete original start/adoption source changed"], None
    expected_header = ast.parse("async def _job(self, claim: ComposerOperationClaim) -> None:\n    pass").body[0]
    if (
        method.decorator_list
        or method.type_params
        or ast.dump(method.args) != ast.dump(expected_header.args)
        or ast.dump(method.returns) != ast.dump(expected_header.returns)
    ):
        return ["worker continuation start: exact original method header changed"], None
    code, reason = code_for(worker.tree, owners)
    mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
    suppliers = {
        "ComposerOperationRunning",
        "ContinuationSQLDeadlineOwner",
        "RequiredWorkCoordinator",
        "RequiredWorkAuthority",
        "RequiredAuthorityKind",
        "ContinuationLeaseAdoption",
    }
    if (
        code is None
        or code.co_flags & mask != inspect.CO_COROUTINE
        or suppliers & set(code.co_varnames + code.co_cellvars + code.co_freevars)
        or any(isinstance(node, (ast.Global, ast.Nonlocal)) and suppliers & set(node.names) for node in ast.walk(method))
    ):
        return ["worker continuation start: actual coroutine/supplier scope changed"], None
    if (
        not source_origins.owned_origin(worker.tree, "ComposerAsyncWorker", worker_class)
        or not source_origins.owned_origin(lifecycle.tree, "ContinuationLeaseAdoption", adoption)
        or not isinstance(worker_class, ast.ClassDef)
        or worker_class.bases
        or worker_class.keywords
        or worker_class.decorator_list
        or worker_class.type_params
        or any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__", "__set__", "__delete__"}
            for node in worker_class.body
        )
        or adoption.bases
        or adoption.keywords
        or adoption.decorator_list
        or adoption.type_params
        or [node.name for node in adoption.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))] != ["__init__", "join"]
    ):
        return ["worker continuation start: actual plain owner binding/header changed"], None
    for cls in (worker_class, adoption):
        if any(
            not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str)
            for node in cls.body
        ):
            return ["worker continuation start: actual owner evaluated namespace changed"], None
    namespace_failures = worker_caller.worker_namespace_contract(worker_class)
    if namespace_failures:
        return namespace_failures, None
    imports = []
    for unit, binding, module in (
        (worker, "ComposerOperationRunning", "elspeth.web.sessions.composer_operations"),
        (worker, "ContinuationSQLDeadlineOwner", "elspeth.web.sessions.continuation_sql_deadline"),
        (worker, "RequiredWorkCoordinator", "elspeth.web.required_work"),
        (worker, "RequiredWorkAuthority", "elspeth.web.required_work"),
        (worker, "RequiredAuthorityKind", "elspeth.web.required_work"),
        (worker, "ContinuationLeaseAdoption", "elspeth.web.coordination.lifecycle"),
        (lifecycle, "RequiredWorkSource", "elspeth.web.required_work"),
        (lifecycle, "run_required_sql_finish_once", "elspeth.web.async_workers"),
        (lifecycle, "SessionOperationContext", "elspeth.web.coordination.contracts"),
        (lifecycle, "SessionOperationKind", "elspeth.web.coordination.contracts"),
        (lifecycle, "RequiredWorkCoordinator", "elspeth.web.required_work"),
        (lifecycle, "RequiredSQLReturned", "elspeth.web.required_sql_outcomes"),
        (lifecycle, "RequiredSQLRaised", "elspeth.web.required_sql_outcomes"),
    ):
        origin = source_origins.import_origin(unit.tree, binding, module, binding)
        if origin is None:
            return ["worker continuation start: actual source import changed " + binding], None
        imports.extend((unit.path, id(node)) for node in origin)
    expected = ast.parse(_CONTINUATION_START_PREFIX).body
    blocks = [
        value
        for node in ast.walk(method)
        for _field, value in ast.iter_fields(node)
        if isinstance(value, list) and len(value) >= len(expected) and all(isinstance(statement, ast.stmt) for statement in value)
    ]
    matches = [
        block[start : start + len(expected)]
        for block in blocks
        for start in range(len(block) - len(expected) + 1)
        if [ast.dump(node) for node in block[start : start + len(expected)]] == [ast.dump(node) for node in expected]
    ]
    if len(matches) != 1:
        return ["worker continuation start: exact timing/authority/adoption transfer changed"], None
    prefix = matches[0]
    running, deadline, coordinator, retained, constructed, adopted = (prefix[i] for i in (0, 2, 3, 4, 5, 6))
    joins = [node for node in ast.walk(method) if isinstance(node, ast.Call) and chain(node.func) == ("adoption", "join")]
    if len(joins) != 1 or joins[0].args or joins[0].keywords:
        return ["worker continuation start: same retained adoption join changed"], None
    join, _ = find(lifecycle.tree, "ContinuationLeaseAdoption.join")
    constructor, _ = find(lifecycle.tree, "ContinuationLeaseAdoption.__init__")
    if (
        not isinstance(constructor, ast.FunctionDef)
        or ast.dump(constructor) != ast.dump(ast.parse(_CONTINUATION_ADOPTION_CONSTRUCTOR).body[0])
        or not isinstance(join, ast.AsyncFunctionDef)
        or ast.dump(join) != ast.dump(ast.parse(_CONTINUATION_ADOPTION_JOIN).body[0])
    ):
        return ["worker continuation start: exact original adoption CAS/lease/outcome changed"], None
    reserves = [node for node in ast.walk(join) if isinstance(node, ast.Call) and chain(node.func) == ("self", "required_work", "reserve")]
    if (
        len(reserves) != 1
        or [chain(node) for node in reserves[0].args] != [("RequiredWorkSource", "LEASE_ADOPTION")]
        or reserves[0].keywords
    ):
        return ["worker continuation start: exact adoption reserve changed"], None
    owner_call = coordinator.value
    deadline_argument = next(keyword.value for keyword in owner_call.keywords if keyword.arg == "sql_deadline_owner")
    allocation = next(
        node for node in ast.walk(join) if isinstance(node, ast.Call) and chain(node.func) == ("SessionOperationLease", "__new__")
    )
    initialization = next(
        node for node in ast.walk(join) if isinstance(node, ast.Call) and chain(node.func) == ("SessionOperationLease", "__init__")
    )
    return [], {
        "definition": (WORKER, id(method)),
        "compiler_kind_bits": code.co_flags & mask,
        "compiler_reason": reason,
        "adoption_definition": (LIFECYCLE, id(adoption)),
        "join_definition": (LIFECYCLE, id(join)),
        "adoption_constructor_definition": (LIFECYCLE, id(constructor)),
        "lease_allocation": (LIFECYCLE, id(allocation)),
        "lease_initialization": (LIFECYCLE, id(initialization)),
        "running": (WORKER, id(running.value)),
        "deadline": (WORKER, id(deadline.value)),
        "coordinator": (WORKER, id(owner_call)),
        "authority": (WORKER, id(owner_call.args[0])),
        "retained_coordinator": (WORKER, id(retained)),
        "adoption": (WORKER, id(constructed.value)),
        "retained_adoption": (WORKER, id(adopted)),
        "join_call": (WORKER, id(joins[0])),
        "supplier_arguments": frozenset(
            {
                (WORKER, id(owner_call), id(owner_call.args[0])),
                (WORKER, id(owner_call), id(deadline_argument)),
            }
        ),
        "continuation_reserve_calls": frozenset({(LIFECYCLE, id(reserves[0]))}),
        "adoption_reserve_receiver": (LIFECYCLE, id(reserves[0].func.value)),
        "import_nodes": tuple(imports),
        "qualification": "SOURCE_LOCAL_ORIGINAL_OWNER_TRANSFER_COMMON_EFFECTS_REQUIRED",
    }


_DRIVER_ACTION_CLOCK = """
action_clock = ContinuationActionClock(action)
self._action_clock = action_clock
self.sql_deadline.begin_action(action, started_at=action_clock.started_monotonic)
self.phases.bind_action_clock(action_clock)
self._action_ordinal += 1
producer: RequiredWorkTicket | None = None
signal: RequiredWorkTicket | None = None
expiry: _ActionExpiry | None = None
stage_owner: ContinuationStage[_ActionResult] | None = None
stage_coroutine: Coroutine[object, object, _ActionResult] | None = None
"""

_DRIVER_STAGE_RESERVES = """
producer = self.work.coordinator.reserve(
    RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER,
    transition_ordinal=selected.stack.cursor, recurrence_ordinal=self._action_ordinal,
)
signal = self.work.coordinator.reserve(
    RequiredWorkSource.DEADLINE_SIGNAL,
    transition_ordinal=selected.stack.cursor, recurrence_ordinal=self._action_ordinal,
)
now = time.monotonic()
active_expiry = _ActionExpiry(self.phases, producer, signal, now + self.sql_deadline.remaining_dispatch(monotonic_now=now))
expiry = active_expiry
"""


def _unique_sequence(owner, template):
    expected = [ast.dump(node) for node in ast.parse(template).body]
    matches = []
    for node in ast.walk(owner):
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            for offset in range(len(value) - len(expected) + 1):
                actual = value[offset : offset + len(expected)]
                if all(isinstance(item, ast.AST) for item in actual) and [ast.dump(item) for item in actual] == expected:
                    matches.append((value, offset, actual))
    return matches[0] if len(matches) == 1 else None


def worker_six_local_contracts(units):
    """Compose actual owner proofs and return only original AST reserve nodes.

    Current complete source guards remain mandatory at the central boundary.
    These facts do not admit arbitrary methods of the driver or coordinator.
    """
    # Imported here to keep the source-origin helper's module initialization
    # independent of the provider proof, which consumes its origin primitives.
    from tests.helpers.web_reserve_provider_mint_contract import provider_driver_constructor_contract

    units = tuple(units)
    receipt = {
        "reserve_calls": frozenset(),
        "driver_reserve_calls": frozenset(),
        "adoption_reserve_calls": frozenset(),
        "per_call": [],
        "caller_calls": [],
        "selected_definitions": [],
        "invocation_scopes": [],
        "import_nodes": [],
        "prerequisite_joins": [],
        "effect_dependencies": [
            {
                "identity": name,
                "qualification": "UNKNOWN",
                "requirement": requirement,
            }
            for name, requirement in (
                ("CURRENT_COMPLETE_SOURCE", "central complete current source guards must hold on these exact ASTs"),
                ("CURRENT_WORKER_CALLER", "same-input app/job/segment caller and whole-scope origins must compose"),
                ("COORDINATOR_MEMBERS", "owned coordinator reserve, source enums and ticket semantics remain separately proved"),
                ("LEASE_AND_OUTSIDE_EFFECTS", "actual lease fields, descriptors and external writes preserve current ownership"),
            )
        ],
    }
    index = {str(unit.path): unit for unit in units}
    if len(index) != len(units) or any(path not in index for path in (WORKER, LIFECYCLE, DRIVER)):
        return ["worker continuation reserves: missing/duplicate source unit"], receipt
    start_failures, start = continuation_start_owner_contract(units)
    if start_failures:
        return start_failures, receipt
    flow_failures, flow = worker_caller.continuation_caller_contracts(units)
    if flow_failures:
        return flow_failures, receipt
    constructor_failures, constructor = provider_driver_constructor_contract(units)
    if constructor_failures:
        return constructor_failures, receipt
    driver = index[DRIVER]
    cls, _ = find(driver.tree, "_SegmentDriver")
    run, run_owners = find(driver.tree, "_SegmentDriver.run")
    settle, settle_owners = find(driver.tree, "_SegmentDriver.settle")
    if not isinstance(run, ast.AsyncFunctionDef) or not isinstance(settle, ast.AsyncFunctionDef):
        return ["worker continuation reserves: missing actual driver methods"], receipt
    for method, owners in ((run, run_owners), (settle, settle_owners)):
        code, reason = code_for(driver.tree, owners)
        mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        if (
            code is None
            or code.co_flags & mask != inspect.CO_COROUTINE
            or "RequiredWorkSource" in code.co_varnames + code.co_cellvars + code.co_freevars
        ):
            return ["worker continuation reserves: actual source scope changed " + method.name], receipt
        receipt["selected_definitions"].append((DRIVER, id(method)))
        receipt["invocation_scopes"].append(
            {
                "definition": (DRIVER, id(method)),
                "kind_bits": code.co_flags & mask,
                "locals": code.co_varnames,
                "cells": code.co_cellvars,
                "free": code.co_freevars,
                "compiler_reason": reason,
            }
        )
    origin = source_origins.import_origin(driver.tree, "RequiredWorkSource", "elspeth.web.required_work", "RequiredWorkSource")
    if origin is None:
        return ["worker continuation reserves: source enum origin changed"], receipt
    receipt["import_nodes"].append((DRIVER, id(origin[0])))
    clock = _unique_sequence(run, _DRIVER_ACTION_CLOCK)
    stage = _unique_sequence(run, _DRIVER_STAGE_RESERVES)
    if clock is None or stage is None:
        return ["worker continuation reserves: original action/clock/stage reserve sequence changed"], receipt
    block, offset, statements = clock
    if offset + len(statements) >= len(block):
        return ["worker continuation reserves: stage no longer follows original clock"], receipt
    stage_try = block[offset + len(statements)]
    if not isinstance(stage_try, ast.Try) or stage[0] is not stage_try.body or stage[1] != 0:
        return ["worker continuation reserves: stage reserve moved outside original clock custody"], receipt
    failure_template = ast.parse("projection = self.work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_PROJECTION)").body[0]
    failure_assignments = [node for node in ast.walk(settle) if ast.dump(node) == ast.dump(failure_template)]
    if len(failure_assignments) != 1:
        return ["worker continuation reserves: owned terminal projection changed"], receipt
    rows = [(run, stage[2][0].value), (run, stage[2][1].value), (settle, failure_assignments[0].value)]
    actual = [
        node
        for node in ast.walk(cls)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
    ]
    if {id(node) for node in actual} != {id(node) for _, node in rows} or len(actual) != 3:
        return ["worker continuation reserves: unreviewed driver reserve or receiver"], receipt
    for method, call in rows:
        receipt["per_call"].append(
            {
                "call": (DRIVER, id(call)),
                "method": (DRIVER, id(method)),
                "receiver_value": (DRIVER, id(call.func.value)),
                "source_argument": (DRIVER, id(call.args[0])),
                "producer_stores": tuple(constructor["binding_constructor_stores"]),
                "source_local_only": True,
            }
        )
    driver_calls = frozenset((DRIVER, id(call)) for _, call in rows)
    adoption_calls = start["continuation_reserve_calls"]
    if not adoption_calls or driver_calls & adoption_calls:
        return ["worker continuation reserves: missing/overlapping actual adoption reserve"], receipt
    receipt.update(
        reserve_calls=driver_calls | adoption_calls,
        driver_reserve_calls=driver_calls,
        adoption_reserve_calls=adoption_calls,
        continuation_reserve_calls=adoption_calls,
        continuation_start_owner=start,
        continuation_transport=flow,
        driver_constructor=constructor,
        caller_calls=list(flow["transport_calls"]),
        supplier_arguments=start["supplier_arguments"] | flow["supplier_arguments"],
        action_clock_statements=tuple((DRIVER, id(node)) for node in statements),
        prerequisite_joins=[
            {"package": "current-worker-caller", "join_calls": flow["job_segment_calls"]},
            {"package": "current-provider-driver-constructor", "join_stores": constructor["binding_constructor_stores"]},
        ],
    )
    return [], receipt
