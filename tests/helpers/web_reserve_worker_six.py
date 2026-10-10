"""Finite conditional worker-six source transfers on supplied ASTs, never admission."""

import ast
import copy
import hashlib
import inspect
import json
import types

WORKER = "src/elspeth/web/sessions/composer_async_worker.py"
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
    "_run_started_under_lease": "daf38d630a65beb59e65af3a81a44ee074492814371c532a285113ad897c2b68",
    "_reconcile_failed_adoption": "275bf719b88e638c990f9da7217588ed581cdfdee326dba3813d547dd4f41ce4",
    "_reserve_failed_terminal_projection": "2eac0f1a376009eea11cac52de3d2d70f6fad279400b87162b3a162dcdcbbe66",
    "_settle_failure": "f43f4d6590616965f54fecd9c3b010f1fef1dd62b16946ad4e90aae3f6ba1347",
    "_recover_failed_adoption": "cc360dcce49988785bb88b1b30241f9d099e19654599b0f784b1856f33fb469e",
}
IMPORT_GRAMMAR = "3b442f921115a868c5b933f82f0681432b524512986f3ff1478cf65b97c2bc64"


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
    "elspeth.web.coordination.lifecycle": "3f71a5261ad1f880340240640fa10a6c784bba1726112c93621c177612d69909",
    "elspeth.contracts.session_operation": "64ee5d516987d7b041060d4e50942522660437bafdb01f40054ad67431417306",
    "elspeth.web.required_sql_outcomes": "6ab23511eebf3383ddcdcfbcc511b8dae4b1c358df3e02781732ffb460b741c7",
    "elspeth.web.sessions.composer_operations": "747dacdbbb06c576e01bb44de86d6df77b5eb26b914cf0ea296f68fed5b81758",
}
