"""Finite conditional worker-six source transfers on supplied ASTs, never admission."""

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
METHODS = (
    "_run_started",
    "_run_started_under_lease",
    "_reconcile_failed_adoption",
    "_reserve_failed_terminal_projection",
    "_settle_failure",
    "_recover_failed_adoption",
)


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


GRAMMARS = {
    "_run_started": "3bac6518c02e38c7556a14d4b2db4d98d07fe26f623486fd93d158f8af13804f",
    "_run_started_under_lease": "9102ab313b7bc66ae98b3516d9bce69be08a017686be541ad9435d06fa54e298",
    "_reconcile_failed_adoption": "275bf719b88e638c990f9da7217588ed581cdfdee326dba3813d547dd4f41ce4",
    "_reserve_failed_terminal_projection": "2eac0f1a376009eea11cac52de3d2d70f6fad279400b87162b3a162dcdcbbe66",
    "_settle_failure": "ebdb7c1fe90737788a107dda02489381741194830a0f995aaae600d3faa491fb",
    "_recover_failed_adoption": "cc360dcce49988785bb88b1b30241f9d099e19654599b0f784b1856f33fb469e",
}
IMPORT_GRAMMAR = "3b442f921115a868c5b933f82f0681432b524512986f3ff1478cf65b97c2bc64"
CONTINUATION_OWNER_GRAMMARS = {
    "ComposerAsyncWorker._job_v2": "a41545c808e9030345269056df65d47c85d5811b06bb665e4a6b92bc26a6021d",
    "ContinuationLeaseAdoption": "6c853b51d24a4cc2d73d8adc70fa44567b25eb671141e558a2cf08b0f5c7c87f",
}

_CONTINUATION_START_PREFIX = """
running = ComposerOperationRunning(claim=claim, session_operation_context=context)
if observation.policy is None or observation.hard_expires_at is None or self._drain_signal is None:
    raise AuditIntegrityError("V2 start lost its actual retained timing/process signal owners")
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
    method, owners = find(worker.tree, "ComposerAsyncWorker._job_v2")
    adoption, _ = find(lifecycle.tree, "ContinuationLeaseAdoption")
    if (
        not isinstance(method, ast.AsyncFunctionDef)
        or not isinstance(adoption, ast.ClassDef)
        or grammar(method) != CONTINUATION_OWNER_GRAMMARS["ComposerAsyncWorker._job_v2"]
        or grammar(adoption) != CONTINUATION_OWNER_GRAMMARS["ContinuationLeaseAdoption"]
    ):
        return ["worker continuation start: complete original start/adoption source changed"], None
    expected_header = ast.parse("async def _job_v2(self, claim: ComposerOperationClaim) -> None:\n    pass").body[0]
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


def worker_six_local_contracts(units):
    """Failures gate every receipt; local facts require separate origin premises."""
    receipt = {
        "reserve_calls": frozenset(),
        "per_call": [],
        "caller_calls": [],
        "selected_definitions": [],
        "invocation_scopes": [],
        "import_nodes": [],
        "prerequisite_joins": [],
    }
    receipt["effect_dependencies"] = [
        {"identity": name, "qualification": "UNKNOWN", "requirement": requirement}
        for name, requirement in (
            ("WHOLE_SCOPE_SOURCE2", "compose the separately reviewed current worker whole-scope origin receipt on these exact ASTs"),
            ("WORKER_CALLER_V1", "compose separately reviewed worker caller app/job/started-under-lease receipts on these exact ASTs"),
            (
                "LEASE_REQUIRED_WORK",
                "actual nominal SessionOperationLease.required_work property yields same coordinator; no outside writes",
            ),
            (
                "COORDINATOR_MEMBERS",
                "normal pinned RequiredWorkCoordinator reserve/validation/authority/tickets semantics and enum origins",
            ),
            (
                "RECOVERED_CONSTRUCTORS",
                "actual imported RequiredWorkCoordinator/RequiredWorkAuthority constructors return exact owned authority under acquired recovery context",
            ),
            (
                "TASK_CALLBACK_AND_OUTSIDE_EFFECTS",
                "normal pinned callbacks/awaits/member lookups retain source-bound receiver/formal/result origins",
            ),
            (
                "SQL_AND_PHYSICAL_JOIN",
                "actual SQL/Future joins, fences, task lifecycle, release and terminal return ownership remain independently qualified",
            ),
        )
    ]
    units = list(units)
    index = {str(n.path): n for n in units}
    if len(index) != len(units) or WORKER not in index or (not isinstance(index[WORKER].tree, ast.Module)):
        return (["worker six: missing/duplicate/unsupported SourceUnit"], receipt)
    tree = index[WORKER].tree
    failures = []
    selected = {}
    if not any(
        isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    ):
        failures.append("worker six: annotation execution mode changed")
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "elspeth.web.required_work"]
    actual = (
        None
        if len(imports) != 1
        else hashlib.sha256(json.dumps(canonical(imports[0]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )
    if actual != IMPORT_GRAMMAR:
        failures.append("worker six: canonical required work import changed")
    else:
        receipt["import_nodes"] = [(WORKER, id(imports[0]))]
    for module, digest in SUPPLIER_IMPORT_GRAMMARS.items():
        matches = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == module]
        current = (
            None
            if len(matches) != 1
            else hashlib.sha256(json.dumps(canonical(matches[0]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        )
        if current != digest:
            failures.append("worker six: canonical nominal supplier import changed " + module)
        else:
            receipt["import_nodes"].append((WORKER, id(matches[0])))
    for name in METHODS:
        function, owners = find(tree, "ComposerAsyncWorker." + name)
        expected = ast.FunctionDef if name == "_reserve_failed_terminal_projection" else ast.AsyncFunctionDef
        if not isinstance(function, expected) or grammar(function) != GRAMMARS[name]:
            failures.append("worker six: complete finite method grammar changed " + name)
            continue
        code, reason = code_for(tree, owners)
        mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        bits = None if code is None else code.co_flags & mask
        if bits != (0 if expected is ast.FunctionDef else inspect.CO_COROUTINE):
            failures.append("worker six: actual compiler callable kind changed " + name)
            continue
        selected[name] = function
        receipt["selected_definitions"].append((WORKER, id(function)))
        receipt["invocation_scopes"].append(
            {
                "definition": (WORKER, id(function)),
                "kind_bits": bits,
                "locals": code.co_varnames,
                "cells": code.co_cellvars,
                "free": code.co_freevars,
                "compiler_reason": reason,
                "qualification": "SOURCE_SCOPE_KIND_ORIGIN_UNKNOWN",
            }
        )
    if failures:
        return (failures, receipt)
    rows = []
    for name, total in (
        ("_run_started", 1),
        ("_run_started_under_lease", 1),
        ("_reconcile_failed_adoption", 3),
        ("_reserve_failed_terminal_projection", 1),
    ):
        f = selected[name]
        calls = [
            n
            for n in ast.walk(f)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and (n.func.attr == "reserve")
            and (chain(n.func.value) in (("coordinator",), ("original_work",), ("recovered_work",)))
        ]
        if len(calls) != total:
            return (["worker six: selected reserve inventory changed " + name], receipt)
        for call in calls:
            symbol = call.func.value.id
            stores = [n for n in ast.walk(f) if isinstance(n, ast.Assign) and len(n.targets) == 1 and (chain(n.targets[0]) == (symbol,))]
            formals = [n for n in (*f.args.args, *f.args.kwonlyargs) if n.arg == symbol]
            guards = [
                n
                for n in ast.walk(f)
                if isinstance(n, ast.If) and any(isinstance(x, ast.Name) and x.id == symbol for x in ast.walk(n.test))
            ]
            returns = [n for n in ast.walk(f) if isinstance(n, ast.Return)]
            validation = [n for n in ast.walk(f) if isinstance(n, ast.Call) and chain(n.func) == (symbol, "validate_terminal_failure_work")]
            if symbol == "coordinator" and name in ("_run_started", "_run_started_under_lease"):
                if len(stores) != 1 or chain(stores[0].value) != ("lease", "required_work") or formals or (not guards):
                    return (["worker six: lease producer/guard changed"], receipt)
            elif symbol == "recovered_work":
                if (
                    len(stores) != 1
                    or not isinstance(stores[0].value, ast.Call)
                    or chain(stores[0].value.func) != ("RequiredWorkCoordinator",)
                    or formals
                ):
                    return (["worker six: recovered constructor changed"], receipt)
            elif symbol == "original_work":
                if len(formals) != 1 or stores or len(validation) != 1:
                    return (["worker six: original formal/authority validation changed"], receipt)
            elif symbol == "coordinator" and (len(formals) != 1 or stores or (not guards) or (len(validation) != 1)):
                return (["worker six: projection formal/nominal guard changed"], receipt)
            rows.append(
                {
                    "call": (WORKER, id(call)),
                    "method": (WORKER, id(f)),
                    "receiver_value": (WORKER, id(call.func.value)),
                    "producer_stores": [(WORKER, id(n)) for n in stores],
                    "formals": [(WORKER, id(n)) for n in formals],
                    "guards": [(WORKER, id(n)) for n in guards],
                    "authority_validation_calls": [(WORKER, id(n)) for n in validation],
                    "returns": [(WORKER, id(n)) for n in returns],
                    "source_local_only": True,
                }
            )
    receipt["reserve_calls"] = frozenset(row["call"] for row in rows)
    receipt["per_call"] = rows
    continuation_failures, continuation = continuation_start_owner_contract(units)
    if continuation_failures:
        return continuation_failures, receipt
    receipt["continuation_start_owner"] = continuation
    receipt["continuation_reserve_calls"] = continuation["continuation_reserve_calls"]
    expected = [
        ("_run_started", "_run_started_under_lease", 1),
        ("_recover_failed_adoption", "_reconcile_failed_adoption", 1),
        ("_settle_failure", "_reserve_failed_terminal_projection", 2),
        ("_reconcile_failed_adoption", "_reserve_failed_terminal_projection", 1),
    ]
    for owner, callee, total in expected:
        calls = [n for n in ast.walk(selected[owner]) if isinstance(n, ast.Call) and chain(n.func) == ("self", callee)]
        if len(calls) != total:
            return (["worker six: exact finite caller transfer changed"], receipt)
        receipt["caller_calls"].extend((WORKER, id(n)) for n in calls)
    receipt["prerequisite_joins"] = [
        {
            "package": "sol-web-worker-whole-scope-origin-2",
            "join_definition": (WORKER, id(selected["_run_started_under_lease"])),
            "qualification": "SEPARATE_REVIEWED_RECEIPT_REQUIRED",
        },
        {
            "package": "sol-web-worker-caller-transfer-1",
            "join_call": receipt["caller_calls"][0],
            "qualification": "SEPARATE_REVIEWED_RECEIPT_REQUIRED",
        },
    ]
    return ([], receipt)


SUPPLIER_IMPORT_GRAMMARS = {
    "elspeth.web.coordination.lifecycle": "386c21fb55b3289545251677448dd29fbe2a9b23220398ac49f774d445bc85eb",
    "elspeth.contracts.session_operation": "499e013091e47e73666eff45d82788d171510b6e5e4d596143c9f8d8790443bf",
    "elspeth.web.required_sql_outcomes": "6ab23511eebf3383ddcdcfbcc511b8dae4b1c358df3e02781732ffb460b741c7",
    "elspeth.web.sessions.composer_operations": "747dacdbbb06c576e01bb44de86d6df77b5eb26b914cf0ea296f68fed5b81758",
}
