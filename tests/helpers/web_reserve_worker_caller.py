"""Finite source-local worker caller transfers; receipts never admit a boundary.

The caller owns parsing. All returned identity pairs refer to its original ASTs.
Complete selected method grammars are finite, reviewed contracts, not a general
Python closure. Actual imports, constructors, descriptors, callbacks and outside
mutation remain UNKNOWN, even when their source-local spellings are canonical.
"""

import ast
import copy
import hashlib
import inspect
import json
import types

APP = "src/elspeth/web/app.py"
WORKER = "src/elspeth/web/sessions/composer_async_worker.py"
TURN = "src/elspeth/web/sessions/composer_turn.py"

# Populated from the actual mapped source by the standalone preparation script.
# Application grammar is the actual header and entire executable prefix through
# the direct constructor statement. No tail or source coordinates authorize it.
REVIEWED_GRAMMARS = {
    "ComposerAsyncWorker.__init__": "97e2ea8a5b6b2833eff8c5cb5df2fa99380efed05c2110f25f8dfa7571fa30b4",
    "ComposerAsyncWorker._job": "ebe08f000b83f6477d3035666cce0d85aaf8f85290c8bdceebf4a91b6bc21ab2",
    "ComposerAsyncWorker._run_started": "3bac6518c02e38c7556a14d4b2db4d98d07fe26f623486fd93d158f8af13804f",
    "ComposerAsyncWorker._run_started_under_lease": "daf38d630a65beb59e65af3a81a44ee074492814371c532a285113ad897c2b68",
    "_create_app": "8c48b00f4dfa903e0f9e8372d53807516b003209bf80076547ba9eddcfec7da2",
    "run_composer_turn": "3076e583c646de8005d6b1e20cd0df431c22384cd0ada0e9a96bf79b0aaae07a",
}
SELECTED = (
    (APP, "_create_app", "ORDINARY_FUNCTION"),
    (WORKER, "ComposerAsyncWorker.__init__", "ORDINARY_FUNCTION"),
    (WORKER, "ComposerAsyncWorker._job", "NATIVE_COROUTINE"),
    (WORKER, "ComposerAsyncWorker._run_started", "NATIVE_COROUTINE"),
    (WORKER, "ComposerAsyncWorker._run_started_under_lease", "NATIVE_COROUTINE"),
    (TURN, "run_composer_turn", "NATIVE_COROUTINE"),
)
RECEIPT_KEYS = (
    "app_worker_constructor_call",
    "app_worker_keyword_value",
    "worker_app_store",
    "job_dto_factory_call",
    "job_services_store",
    "job_turn_call",
    "wrapper_turn_call",
    "job_started_call",
    "started_under_lease_call",
    "reap_dto_factory_call",
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
            return None, []
        node = matches[0]
        owners.append(node)
        nodes = node.body
    return node, owners


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
        and type(clone.body[0].value.value) is str
    ):
        clone.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(canonical(clone), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def code_for(tree, owners):
    """Compile the caller's actual whole AST for metadata, never execute it."""
    try:
        code = compile(tree, "<worker-caller-source-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return None, type(error).__name__
    for owner in owners:
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or owner.type_params:
            return None, "unsupported definition owner/type parameters"
        start = min([owner.lineno] + [n.lineno for n in owner.decorator_list])
        matches = [
            item
            for item in code.co_consts
            if isinstance(item, types.CodeType) and item.co_name == owner.name and item.co_firstlineno == start
        ]
        if len(matches) != 1:
            return None, "ambiguous actual compiler owner"
        code = matches[0]
    return code, None


def direct_call_statement(statements, target, callee):
    matches = [
        n
        for n in statements
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and chain(n.targets[0]) == target
        and isinstance(n.value, ast.Call)
        and chain(n.value.func) == callee
    ]
    return matches[0] if len(matches) == 1 else None


def awaited_calls(function, callee):
    """Inspect calls only after the complete enclosing finite grammar matches."""
    return [
        n.value for n in ast.walk(function) if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and chain(n.value.func) == callee
    ]


def exact_call(call, callee, args, keywords):
    return (
        isinstance(call, ast.Call)
        and chain(call.func) == callee
        and [chain(n) for n in call.args] == list(args)
        and [(n.arg, chain(n.value)) for n in call.keywords] == list(keywords)
    )


def dependencies():
    requirements = [
        ("fastapi.FastAPI", "actual producer constructor, app/state descriptors, same returned app and callbacks"),
        (
            "elspeth.web.sessions.composer_async_worker.ComposerAsyncWorker",
            "actual imported ordinary class constructor, returned instance and _app store semantics",
        ),
        (
            "elspeth.web.sessions.composer_app_services.composer_app_services",
            "actual import/callable identity, current same-app state reads, DTO supplier and later keyword effects",
        ),
        (
            "elspeth.web.sessions.composer_async_worker.ComposerAsyncWorker._run_started",
            "ordinary member descriptor lookup, actual callable/code origin and receiver member stability",
        ),
        (
            "elspeth.web.sessions.composer_async_worker.ComposerAsyncWorker._run_started_under_lease",
            "ordinary member descriptor lookup, actual callable/code origin and receiver member stability",
        ),
        ("elspeth.web.sessions.composer_turn.run_composer_turn", "actual imported native coroutine and retained services argument effects"),
        (
            "elspeth.web.sessions.composer_turn._run_composer_turn",
            "actual imported/bound native coroutine, services/DTO member stability and provider dispatch",
        ),
        (
            "SELECTED_SOURCE_CODE_OBJECTS",
            "runtime callable objects and __code__ match actual compiled source; local compiler kind alone cannot prove origin",
        ),
        (
            "INTERVENING_WORKER_EFFECTS",
            "every await, call, context manager and callback before dispatch preserves actual _app, services, class members and DTO fields",
        ),
        (
            "OUTSIDE_WORKER_EFFECTS",
            "outside aliases, reflection, object/class/module writes and concurrent tasks preserve retained origins/members",
        ),
        (
            "PROVIDER_TEST_SEAMS",
            "factory reads current app.state; substitution identity carries forward, while fixture composer semantics remain fixture scoped",
        ),
    ]
    return [{"identity": identity, "requirement": requirement, "qualification": "UNKNOWN"} for identity, requirement in requirements]


def worker_caller_local_contracts(units):
    """Return failures plus conditional same-AST facts, never local admission."""
    receipt = dict.fromkeys(RECEIPT_KEYS)
    receipt.update(
        effect_dependencies=dependencies(),
        invocation_kinds=[],
        selected_definitions=[],
        job_dto_factory_consumer="elspeth.web.sessions.composer_async_worker.ComposerAsyncWorker._job",
    )
    units = list(units)
    index = {str(unit.path): unit for unit in units}
    if len(index) != len(units) or not all(path in index for path in (APP, WORKER, TURN)):
        return ["worker caller: missing/duplicate actual SourceUnit"], receipt
    failures = []
    selected = {}
    for path, qualified, expected in SELECTED:
        tree = index[path].tree
        if not isinstance(tree, ast.Module):
            failures.append("worker caller: unsupported source tree " + path)
            continue
        if not any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in tree.body
        ):
            failures.append("worker caller: unsupported annotation execution mode " + path)
        method, owners = find(tree, qualified)
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            failures.append("worker caller: missing/ambiguous definition " + qualified)
            continue
        selected[qualified] = method
        receipt["selected_definitions"].append((path, id(method)))
        code, reason = code_for(tree, owners)
        mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        bits = code.co_flags & mask if code is not None else None
        allowed = 0 if expected == "ORDINARY_FUNCTION" else inspect.CO_COROUTINE
        receipt["invocation_kinds"].append(
            {
                "path": path,
                "definition": (path, id(method)),
                "expected": expected,
                "actual_kind_bits": bits,
                "compiler_reason": reason,
                "qualification": "SOURCE_KIND_ORIGIN_UNKNOWN",
            }
        )
        if bits != allowed:
            failures.append("worker caller: unsupported actual callable kind " + qualified + (" (" + reason + ")" if reason else ""))
        prefix = None
        if path == APP:
            statement = direct_call_statement(method.body, ("app", "state", "composer_async_worker"), ("ComposerAsyncWorker",))
            if statement is None:
                failures.append("worker caller: direct unique app constructor statement changed")
                continue
            prefix = method.body[: method.body.index(statement) + 1]
        try:
            digest = grammar(method, prefix=prefix)
        except (TypeError, ValueError, OverflowError, RecursionError):
            failures.append("worker caller: unsupported executable AST literal " + qualified)
            continue
        if digest != REVIEWED_GRAMMARS.get(qualified):
            failures.append("worker caller: complete finite executed grammar changed " + qualified)
    if failures:
        return failures, receipt
    app = selected["_create_app"]
    init = selected["ComposerAsyncWorker.__init__"]
    job = selected["ComposerAsyncWorker._job"]
    started = selected["ComposerAsyncWorker._run_started"]
    under = selected["ComposerAsyncWorker._run_started_under_lease"]
    wrapper = selected["run_composer_turn"]
    worker_class, _ = find(index[WORKER].tree, "ComposerAsyncWorker")
    if worker_class.bases or worker_class.keywords or worker_class.decorator_list or worker_class.type_params:
        return ["worker caller: unsupported worker class construction/namespace"], receipt
    lookup = {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__", "__set__", "__delete__"}
    declarations = [
        n for n in worker_class.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and type(n.value.value) is str)
    ]
    if any(not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name in lookup for n in declarations):
        return ["worker caller: unsupported worker member/descriptor declaration"], receipt
    for member in declarations:
        defaults = member.args.defaults + [n for n in member.args.kw_defaults if n is not None]
        if (
            member.type_params
            or any(not isinstance(n, ast.Constant) for n in defaults)
            or any(chain(n) != ("staticmethod",) for n in member.decorator_list)
        ):
            return ["worker caller: unsupported worker definition-time member effect " + member.name], receipt
    store = next(n for n in init.body if isinstance(n, ast.Assign) and chain(n.targets[0]) == ("self", "_app"))
    app_constructor = direct_call_statement(app.body, ("app", "state", "composer_async_worker"), ("ComposerAsyncWorker",)).value
    keyword_values = [n.value for n in app_constructor.keywords if n.arg == "app"]
    if len(keyword_values) != 1 or chain(keyword_values[0]) != ("app",):
        return ["worker caller: same-call exact app keyword changed"], receipt
    # Local class namespace write forms are conservative refusals. No inference
    # about an outside alias or callback is drawn from their absence.
    for node in ast.walk(worker_class):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "_app"
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and node is not store.targets[0]
        ):
            failures.append("worker caller: alternate _app store/delete in worker namespace")
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "_app"
        ):
            failures.append("worker caller: dictionary/alias _app store/delete in worker namespace")
        if (
            isinstance(node, ast.Call)
            and chain(node.func) in {("setattr",), ("delattr",), ("object", "__setattr__"), ("object", "__delattr__")}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "_app"
        ):
            failures.append("worker caller: reflective _app mutation in worker namespace")
    factory_statement = direct_call_statement(job.body, ("services",), ("composer_app_services",))
    if factory_statement is None or not exact_call(factory_statement.value, ("composer_app_services",), [("self", "_app")], []):
        failures.append("worker caller: direct _job same-app factory binding changed")
    hops = [
        (job, ("self", "_run_started"), [("services",), ("running",), ("lease",)], [], "job_started_call"),
        (
            started,
            ("self", "_run_started_under_lease"),
            [("services",), ("running",), ("lease",), ("settlement",), ("setup_ticket",)],
            [],
            "started_under_lease_call",
        ),
        (
            under,
            ("run_composer_turn",),
            [("services",), ("turn",)],
            [
                ("lease", ("lease",)),
                ("running", ("running",)),
                ("request_lifecycle", ("lifecycle",)),
                ("observation", ("observation",)),
                ("budget_anchor", ("anchor",)),
            ],
            "job_turn_call",
        ),
        (
            wrapper,
            ("_run_composer_turn",),
            [("services",), ("turn",)],
            [
                ("lease", ("lease",)),
                ("running", ("running",)),
                ("request_lifecycle", ("request_lifecycle",)),
                ("observation", ("observation",)),
                ("budget_anchor", ("budget_anchor",)),
            ],
            "wrapper_turn_call",
        ),
    ]
    for owner, callee, args, keywords, key in hops:
        calls = awaited_calls(owner, callee)
        if len(calls) != 1 or not exact_call(calls[0], callee, args, keywords):
            failures.append("worker caller: exact awaited transfer changed " + key)
        else:
            receipt[key] = (TURN if owner is wrapper else WORKER, id(calls[0]))
    if failures:
        return failures, receipt
    receipt.update(
        app_worker_constructor_call=(APP, id(app_constructor)),
        app_worker_keyword_value=(APP, id(keyword_values[0])),
        worker_app_store=(WORKER, id(store.targets[0])),
        worker_app_assignment=(WORKER, id(store)),
        job_dto_factory_call=(WORKER, id(factory_statement.value)),
        job_services_store=(WORKER, id(factory_statement.targets[0])),
        job_services_assignment=(WORKER, id(factory_statement)),
    )
    # Distinct call, never confused with the selected _job factory provenance.
    reap, _ = find(index[WORKER].tree, "ComposerAsyncWorker.reap_once")
    if reap is not None:
        statement = direct_call_statement(reap.body, ("services",), ("composer_app_services",))
        if statement is not None:
            receipt["reap_dto_factory_call"] = (WORKER, id(statement.value))
    return [], receipt
