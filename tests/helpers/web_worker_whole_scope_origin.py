"""Finite whole-scope source origins and returned-app identity, never admission.

Input ASTs belong to the caller. Compilation obtains metadata only; source is
never reparsed or executed here. Import records establish source bindings, not
runtime loader, framework, callable-code or object-immutability guarantees.
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
POLICY_SETTINGS = "src/elspeth/web/composer/continuation_policy_settings.py"
POLICY_MODELS = "src/elspeth/web/composer/continuation_policy.py"
POLICY_SUPPLIER_GRAMMARS = {
    POLICY_SETTINGS: "10ed3196f604e8fa4d3a6dcb164a5a80176a1ef92bf474f4008f5d20a921cead",
    POLICY_MODELS: "ecc7e9efaf913b36c840a72ffeb3ea8bac25a92c9b62f4efbd4bb0ca10a48666",
}
POLICY_SUPPLIER_DEFINITIONS = {
    POLICY_SETTINGS: (
        "_guard_seconds",
        "_provider_action",
        "_local_action",
        "continuation_policy_from_settings",
        "_require_first_segment_fits_its_chain",
    ),
    POLICY_MODELS: (
        "ContinuationActionPolicy",
        "ContinuationSegmentPolicy",
        "ContinuationActionBounds",
        "ContinuationAdmissionPolicy",
        "decode_continuation_policy",
    ),
}
POLICY_SUPPLIER_ORIGINS = frozenset(
    path.removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + name
    for path, names in POLICY_SUPPLIER_DEFINITIONS.items()
    for name in names
) | frozenset(
    "elspeth.web.composer.continuation_policy_settings." + name
    for name in ("GUARD_STAGE_MARGIN_SECONDS", "_DEFAULT_GUARD_TIMEOUT_SECONDS", "_FIRST_SEGMENT_SESSION_SQL_ACTIONS")
)
POLICY_SUPPLIER_CLASS_ORIGINS = frozenset(
    "elspeth.web.composer.continuation_policy." + name
    for name in ("ContinuationActionPolicy", "ContinuationSegmentPolicy", "ContinuationActionBounds", "ContinuationAdmissionPolicy")
)
POLICY_SUPPLIER_FUNCTION_ORIGINS = frozenset(
    path.removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + name
    for path, names in POLICY_SUPPLIER_DEFINITIONS.items()
    for name in names
    if path != POLICY_MODELS or name == "decode_continuation_policy"
)
GRAMMARS = {
    "_create_app": "b57b0d1b932f77844d3474e125399ee60523f125474e255c16c04558a4e2facd",
    "ComposerAsyncWorker._run_started_under_lease": "9102ab313b7bc66ae98b3516d9bce69be08a017686be541ad9435d06fa54e298",
    "run_composer_turn": "3076e583c646de8005d6b1e20cd0df431c22384cd0ada0e9a96bf79b0aaae07a",
}
SELECTED = (
    (APP, "_create_app", 0),
    (WORKER, "ComposerAsyncWorker._run_started_under_lease", inspect.CO_COROUTINE),
    (TURN, "run_composer_turn", inspect.CO_COROUTINE),
)


def chain(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = chain(node.value)
        return (*prefix, node.attr) if prefix else ()
    return ()


def find(tree, qualified):
    if not isinstance(tree, ast.Module):
        return None, []
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
        return ["ELLIPSIS_LITERAL"]
    if isinstance(value, bytes):
        return ["BYTES_LITERAL", value.hex()]
    if isinstance(value, complex):
        return ["COMPLEX_LITERAL", value.real, value.imag]
    if isinstance(value, ast.AST):
        return [
            type(value).__name__,
            [[field, canonical(item)] for field, item in ast.iter_fields(value) if item is not None and item != []],
        ]
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


_YAML_ADAPTER_PREFLIGHT_CALL = "validate_pipeline(\n            state,\n            settings,\n            ComposerYamlSerializer(),\n            secret_service=app.state.scoped_secret_resolver,\n            secret_wiring_policy=runtime_secret_wiring_policy(settings.secret_wiring_allowlist),\n            user_id=user_id,\n            session_id=session_id,\n            blob_get_metadata=_blob_get_metadata,\n            blob_get_content=blob_get_content,\n            plugin_snapshot=plugin_snapshot,\n            profile_registry=app.state.operator_profile_registry,\n            catalog=app.state.catalog_service,\n        )"


def adapter_grammar_projection(node):
    """Project only the reviewed adapter operand onto the previous grammar.

    All receipts and compiler metadata still use the original input tree. This
    disposable comparison copy preserves every other node. The complete
    nominal-adapter receipt separately owns the class, import and lexical uses.
    """
    if not isinstance(node, ast.FunctionDef) or node.name != "_create_app":
        return node
    constructors = [child for child in ast.walk(node) if isinstance(child, ast.Call) and chain(child.func) == ("ComposerYamlSerializer",)]
    if not constructors:
        # Historical source remains a valid worker-only grammar; the combined
        # adapter receipt independently requires the actual new production tuple.
        return node
    clone = copy.deepcopy(node)
    functions = [child for child in clone.body if isinstance(child, ast.FunctionDef) and child.name == "_session_runtime_preflight"]
    if len(functions) != 1:
        raise ValueError("nominal YAML preflight owner is not unique")
    expected = ast.dump(ast.parse(_YAML_ADAPTER_PREFLIGHT_CALL, mode="eval").body, include_attributes=False)
    returns = [
        child
        for child in functions[0].body
        if isinstance(child, ast.Return)
        and isinstance(child.value, ast.Call)
        and ast.dump(child.value, include_attributes=False) == expected
    ]
    if len(constructors) != 1 or len(returns) != 1:
        raise ValueError("nominal YAML preflight argument delta is not exact")
    call = returns[0].value
    if len(call.args) != 3 or not isinstance(call.args[2], ast.Call) or call.args[2].args or call.args[2].keywords:
        raise ValueError("nominal YAML constructor acquired an argument")
    call.args[2] = ast.copy_location(ast.Name(id="yaml_generator_module", ctx=ast.Load()), call.args[2])
    return clone


def grammar(node):
    clone = copy.copy(node)
    clone.body = list(node.body)
    clone = adapter_grammar_projection(clone)
    if (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and type(clone.body[0].value.value) is str
    ):
        clone.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(canonical(clone), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def code_for(tree, owners):
    try:
        code = compile(tree, "<worker-whole-scope-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return None, type(error).__name__
    for owner in owners:
        if owner.type_params:
            return None, "unsupported owner type parameters"
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


def scope_nodes(node):
    """One lexical owner, including evaluated definition headers, never bodies.

    This enumerates relevant explicit global/nonlocal declarations and direct
    source uses, not compiler scope resolution. Compiler locals/cells/free are
    authoritative for all local binder forms, including comprehension walruses,
    dormant branches, bare annotations, exception/pattern targets and imports.
    """
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
        expressions = [] if isinstance(node, ast.Lambda) else node.decorator_list
        if isinstance(node, ast.ClassDef):
            expressions = list(expressions) + node.bases + [n.value for n in node.keywords]
        else:
            expressions = list(expressions) + node.args.defaults + [n for n in node.args.kw_defaults if n is not None]
        for expression in expressions:
            yield from scope_nodes(expression)
        return
    for field, value in ast.iter_fields(node):
        if field in {"annotation", "returns", "type_params"}:
            continue
        if isinstance(value, ast.AST):
            yield from scope_nodes(value)
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, ast.AST):
                    yield from scope_nodes(child)


def body_nodes(function):
    for statement in function.body:
        yield from scope_nodes(statement)


def binding_nodes(nodes, binding):
    """Conservative syntactic module binding universe; no runtime alias closure."""
    for node in nodes:
        if (isinstance(node, ast.Name) and node.id == binding and isinstance(node.ctx, (ast.Store, ast.Del))) or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == binding
        ):
            yield node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                if alias.name == "*" or bound == binding:
                    yield alias
        elif (isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name == binding) or (
            isinstance(node, ast.MatchMapping) and node.rest == binding
        ):
            yield node


def executed_class_global_relevance(tree, binding):
    """Conservatively refuse relevant Global declarations in executed classes.

    Module and recursively nested class suites execute at definition time.
    Function/lambda bodies do not: only their evaluated defaults/decorators
    belong to the parent execution. Both live and dormant class branches are
    included; a bare relevant Global suffices. Class-local same spellings have
    no such declaration and remain separate. This is no interprocedural effect
    engine: calls, loaders, descriptors and deferred invocations stay UNKNOWN.
    """

    def visit(node, in_class=False):
        if isinstance(node, ast.Global):
            return in_class and binding in node.names
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            expressions = [] if isinstance(node, ast.Lambda) else node.decorator_list
            expressions = list(expressions) + node.args.defaults + [n for n in node.args.kw_defaults if n is not None]
            return any(visit(expression, in_class) for expression in expressions)
        if isinstance(node, ast.ClassDef):
            expressions = node.decorator_list + node.bases + [n.value for n in node.keywords]
            return any(visit(expression, in_class) for expression in expressions) or any(visit(statement, True) for statement in node.body)
        for field, value in ast.iter_fields(node):
            if field in {"annotation", "returns", "type_params"}:
                continue
            if isinstance(value, ast.AST) and visit(value, in_class):
                return True
            if isinstance(value, list) and any(visit(child, in_class) for child in value if isinstance(child, ast.AST)):
                return True
        return False

    return visit(tree)


def import_origin(tree, binding, module, member):
    if executed_class_global_relevance(tree, binding):
        return None
    matches = list(binding_nodes(scope_nodes(tree), binding))
    expected = [
        (statement, alias)
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == module
        for alias in statement.names
        if alias.name == member and (alias.asname or alias.name) == binding
    ]
    if len(expected) != 1 or matches != [expected[0][1]]:
        return None
    return expected[0]


def owned_origin(tree, binding, definition):
    return not executed_class_global_relevance(tree, binding) and list(binding_nodes(scope_nodes(tree), binding)) == [definition]


def worker_whole_scope_origin_local_contracts(units):
    """Return same-input AST facts plus mandatory runtime UNKNOWN obligations."""
    receipt = {
        "callable_origins": [],
        "whole_scopes": [],
        "app_direct_call_uses": [],
        "app_fastapi_call": None,
        "app_binding_store": None,
        "app_worker_constructor_call": None,
        "job_turn_call": None,
        "app_final_return": None,
        "app_final_return_value": None,
        "effect_dependencies": [
            {
                "identity": "RUNTIME_IMPORT_LOADERS",
                "qualification": "UNKNOWN",
                "requirement": "source import bindings must resolve to actual suppliers; loader/module substitution and outside writes remain separate",
            },
            {
                "identity": "SOURCE_CALLABLE_CODE_ORIGIN",
                "qualification": "UNKNOWN",
                "requirement": "actual runtime class/functions/code/descriptors match the observed source owners",
            },
            {
                "identity": "EVALUATED_DEFINITION_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "evaluated defaults, decorators, bases, metaclasses and current owned-class construction may have supplier effects; deferred method bodies require separate invocation qualification",
            },
            {
                "identity": "APP_FRAMEWORK_AND_OUTSIDE_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "actual FastAPI/worker returned objects, app.state/current DTO fields, callbacks and outside/concurrent mutation require qualification",
            },
            {
                "identity": "APP_DIRECT_CALL_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "registration and weakref finalizer calls have the recorded app argument syntax; actual callees, monitored-object semantics and callback effects remain separate",
            },
            {
                "identity": "PROVIDER_TEST_SEAMS",
                "qualification": "UNKNOWN",
                "requirement": "preserve fresh actual app.state substitution; fixture semantics do not certify canonical provider behavior",
            },
        ],
    }
    units = list(units)
    index = {str(unit.path): unit for unit in units}
    if len(index) != len(units) or not all(path in index for path in (APP, WORKER, TURN)):
        return ["worker whole scope: missing/duplicate source unit"], receipt
    failures = []
    selected = {}
    scopes = {}
    names = {
        "_create_app": ("FastAPI", "ComposerAsyncWorker", "weakref", "continuation_policy_from_settings"),
        "ComposerAsyncWorker._run_started_under_lease": ("run_composer_turn",),
        "run_composer_turn": ("_run_composer_turn",),
    }
    for path, qualified, expected_kind in SELECTED:
        tree = index[path].tree
        fn, owners = find(tree, qualified)
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            failures.append("worker whole scope: missing/ambiguous actual owner " + qualified)
            continue
        if not any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in tree.body
        ):
            failures.append("worker whole scope: unsupported annotation mode " + qualified)
        selected[qualified] = fn
        code, reason = code_for(tree, owners)
        if code is None:
            failures.append("worker whole scope: actual compiler scope unsupported " + qualified + " (" + reason + ")")
            continue
        mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        if code.co_flags & mask != expected_kind:
            failures.append("worker whole scope: actual invocation kind changed " + qualified)
        scope = set(code.co_varnames + code.co_cellvars + code.co_freevars)
        scopes[qualified] = scope
        nodes = list(body_nodes(fn))
        for binding in names[qualified]:
            if binding in scope:
                failures.append("worker whole scope: whole-function callee localized/captured " + qualified + ":" + binding)
            if any(isinstance(n, (ast.Global, ast.Nonlocal)) and binding in n.names for n in nodes):
                failures.append("worker whole scope: explicit global/nonlocal callee ambiguity " + qualified + ":" + binding)
        receipt["whole_scopes"].append(
            {
                "definition": (path, id(fn)),
                "qualified": qualified,
                "locals": list(code.co_varnames),
                "cells": list(code.co_cellvars),
                "free": list(code.co_freevars),
                "selected_global_names": list(names[qualified]),
                "actual_kind_bits": code.co_flags & mask,
            }
        )
        try:
            digest = grammar(fn)
        except (TypeError, ValueError, OverflowError, RecursionError):
            failures.append("worker whole scope: unsupported complete AST literal " + qualified)
            continue
        if digest != GRAMMARS.get(qualified):
            failures.append("worker whole scope: complete finite whole-body grammar changed " + qualified)
    if len(selected) != len(SELECTED):
        return failures, receipt
    app = selected["_create_app"]
    under = selected["ComposerAsyncWorker._run_started_under_lease"]
    wrapper = selected["run_composer_turn"]
    class_node, _ = find(index[WORKER].tree, "ComposerAsyncWorker")
    core_turn, _ = find(index[TURN].tree, "_run_composer_turn")
    for path, binding, expected in (
        (APP, "_create_app", app),
        (WORKER, "ComposerAsyncWorker", class_node),
        (TURN, "run_composer_turn", wrapper),
        (TURN, "_run_composer_turn", core_turn),
    ):
        if expected is None or not owned_origin(index[path].tree, binding, expected):
            failures.append("worker whole scope: actual module definition origin changed " + binding)
    for path, binding, module, member, consumer in (
        (APP, "FastAPI", "fastapi", "FastAPI", "_create_app"),
        (APP, "ComposerAsyncWorker", "elspeth.web.sessions.composer_async_worker", "ComposerAsyncWorker", "_create_app"),
        (
            WORKER,
            "run_composer_turn",
            "elspeth.web.sessions.composer_turn",
            "run_composer_turn",
            "ComposerAsyncWorker._run_started_under_lease",
        ),
    ):
        origin = import_origin(index[path].tree, binding, module, member)
        if origin is None:
            failures.append("worker whole scope: canonical actual module import binding changed " + path + ":" + binding)
        else:
            statement, alias = origin
            receipt["callable_origins"].append(
                {
                    "binding": binding,
                    "source_origin": module + "." + member,
                    "import_statement": (path, id(statement)),
                    "import_alias": (path, id(alias)),
                    "consumer_definition": (path, id(selected[consumer])),
                    "qualification": "ACTUAL_SOURCE_IMPORT_AND_WHOLE_SCOPE_RUNTIME_UNKNOWN",
                }
            )
    if failures:
        return failures, receipt
    receipt["drain_owner_origins"] = []
    for path in (APP, WORKER):
        origin = import_origin(index[path].tree, "ComposerDrainSignal", "elspeth.web.sessions.composer_drain", "ComposerDrainSignal")
        if origin is None:
            failures.append("worker whole scope: actual nominal drain owner import changed " + path)
        else:
            statement, alias = origin
            receipt["drain_owner_origins"].append(
                {
                    "source_origin": "elspeth.web.sessions.composer_drain.ComposerDrainSignal",
                    "import_statement": (path, id(statement)),
                    "import_alias": (path, id(alias)),
                    "qualification": "ACTUAL_SOURCE_IMPORT_RUNTIME_OWNER_UNKNOWN",
                }
            )
    if failures:
        return failures, receipt
    policy_failures, policy_receipt = _continuation_policy_consumer(index[APP])
    if policy_failures:
        return policy_failures, receipt
    receipt["continuation_policy_consumer"] = policy_receipt
    nodes = list(body_nodes(app))
    app_stores = list(binding_nodes(nodes, "app"))
    declarations = [n for n in nodes if isinstance(n, (ast.Global, ast.Nonlocal)) and "app" in n.names]
    returns = [n for n in nodes if isinstance(n, ast.Return)]
    producer = [
        n
        for n in app.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and chain(n.targets[0]) == ("app",)
        and isinstance(n.value, ast.Call)
        and chain(n.value.func) == ("FastAPI",)
    ]
    workers = [
        n
        for n in app.body
        if isinstance(n, ast.Assign)
        and len(n.targets) == 1
        and chain(n.targets[0]) == ("app", "state", "composer_async_worker")
        and isinstance(n.value, ast.Call)
        and chain(n.value.func) == ("ComposerAsyncWorker",)
    ]
    if len(producer) != 1 or app_stores != [producer[0].targets[0]] or declarations or "app" not in scopes["_create_app"]:
        return ["worker whole scope: constructed app whole-function binding identity changed"], receipt
    if len(workers) != 1 or len(returns) != 1 or returns[0] is not app.body[-1] or chain(returns[0].value) != ("app",):
        return ["worker whole scope: unique actual final return of constructed app changed"], receipt
    args = [k.value for k in workers[0].value.keywords if k.arg == "app"]
    if len(args) != 1 or chain(args[0]) != ("app",) or app.body.index(producer[0]) >= app.body.index(workers[0]):
        return ["worker whole scope: worker constructor uses foreign/nonpreceding app binding"], receipt
    turn_calls = [
        n.value
        for n in body_nodes(under)
        if isinstance(n, ast.Await) and isinstance(n.value, ast.Call) and chain(n.value.func) == ("run_composer_turn",)
    ]
    if len(turn_calls) != 1:
        return ["worker whole scope: selected direct awaited turn call changed"], receipt
    receipt.update(
        app_fastapi_call=(APP, id(producer[0].value)),
        app_binding_store=(APP, id(producer[0].targets[0])),
        app_worker_constructor_call=(APP, id(workers[0].value)),
        job_turn_call=(WORKER, id(turn_calls[0])),
        app_final_return=(APP, id(returns[0])),
        app_final_return_value=(APP, id(returns[0].value)),
    )
    actual_calls = {
        "FastAPI": receipt["app_fastapi_call"],
        "ComposerAsyncWorker": receipt["app_worker_constructor_call"],
        "run_composer_turn": receipt["job_turn_call"],
    }
    actual_definitions = {"ComposerAsyncWorker": (WORKER, id(class_node)), "run_composer_turn": (TURN, id(wrapper))}
    for record in receipt["callable_origins"]:
        record["actual_call"] = actual_calls[record["binding"]]
        record["supplier_definition"] = actual_definitions.get(record["binding"])
    receipt["wrapper_core_definition"] = (TURN, id(core_turn))
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        actual_args = [arg for arg in node.args if chain(arg) == ("app",)] + [
            kw.value for kw in node.keywords if chain(kw.value) == ("app",)
        ]
        if not actual_args:
            continue
        callee = chain(node.func)
        record = {
            "call": (APP, id(node)),
            "app_arguments": [(APP, id(n)) for n in actual_args],
            "callee": list(callee),
            "qualification": "ACTUAL_SOURCE_APP_ARGUMENT_RUNTIME_EFFECT_UNKNOWN",
        }
        if callee == ("weakref", "finalize") and node.args and chain(node.args[0]) == ("app",) and len(node.args) >= 2:
            record.update(
                kind="MONITORED_OBJECT_POSITION",
                callback=(APP, id(node.args[1])),
                callback_argument_nodes=[(APP, id(n)) for n in node.args[2:]],
                app_in_callback_arguments=any(chain(n) == ("app",) for n in node.args[2:]),
            )
        elif callee == ("register_session_operation_exception_handlers",):
            record["kind"] = "REGISTRATION_APP_ARGUMENT"
        elif callee == ("ComposerAsyncWorker",):
            record["kind"] = "WORKER_APP_ARGUMENT"
        else:
            record["kind"] = "UNSUPPORTED_APP_ARGUMENT"
            failures.append("worker whole scope: unreviewed direct first-class app use")
        receipt["app_direct_call_uses"].append(record)
    return failures, receipt


def _continuation_policy_consumer(unit):
    """The one actual authority construction receives the original settings."""
    factory, _ = find(unit.tree, "_create_app")
    origin = import_origin(
        unit.tree,
        "continuation_policy_from_settings",
        "elspeth.web.composer.continuation_policy_settings",
        "continuation_policy_from_settings",
    )
    authority_origin = import_origin(
        unit.tree,
        "ComposerAsyncOperationAuthority",
        "elspeth.web.coordination.composer_operation_authority",
        "ComposerAsyncOperationAuthority",
    )
    if (
        not isinstance(factory, ast.FunctionDef)
        or not factory.args.args
        or factory.args.args[0].arg != "settings"
        or origin is None
        or authority_origin is None
    ):
        return ["worker policy supplier: canonical consumer import changed"], None
    code, _reason = code_for(unit.tree, [factory])
    if code is None or any(
        name in code.co_varnames + code.co_cellvars + code.co_freevars
        for name in ("continuation_policy_from_settings", "ComposerAsyncOperationAuthority")
    ):
        return ["worker policy supplier: consumer supplier localized/captured"], None
    nodes = list(body_nodes(factory))
    if list(binding_nodes(nodes, "settings")) or any(
        isinstance(node, (ast.Global, ast.Nonlocal)) and "settings" in node.names for node in nodes
    ):
        return ["worker policy supplier: original settings binding changed"], None
    constructors = [
        node
        for node in factory.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and chain(node.targets[0]) == ("composer_async_authority",)
        and isinstance(node.value, ast.Call)
        and chain(node.value.func) == ("ComposerAsyncOperationAuthority",)
    ]
    uses = [node for node in nodes if isinstance(node, ast.Name) and node.id == "continuation_policy_from_settings"]
    if len(constructors) != 1:
        return ["worker policy supplier: exact authority construction changed"], None
    authority_store = constructors[0].targets[0]
    if list(binding_nodes(nodes, "composer_async_authority")) != [authority_store]:
        return ["worker policy supplier: retained authority binding changed"], None
    call = constructors[0].value
    keywords = [keyword for keyword in call.keywords if keyword.arg == "continuation_policy"]
    if (
        len(keywords) != 1
        or not isinstance(keywords[0].value, ast.Call)
        or chain(keywords[0].value.func) != ("continuation_policy_from_settings",)
        or len(keywords[0].value.args) != 1
        or chain(keywords[0].value.args[0]) != ("settings",)
        or keywords[0].value.keywords
        or uses != [keywords[0].value.func]
    ):
        return ["worker policy supplier: exact settings-to-authority transfer changed"], None
    installed = [
        node
        for node in factory.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and chain(node.targets[0]) == ("app", "state", "composer_async_operation_authority")
        and chain(node.value) == ("composer_async_authority",)
    ]
    workers = [
        node.value
        for node in factory.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and chain(node.targets[0]) == ("app", "state", "composer_async_worker")
        and isinstance(node.value, ast.Call)
        and chain(node.value.func) == ("ComposerAsyncWorker",)
    ]
    if len(installed) != 1 or len(workers) != 1:
        return ["worker policy supplier: current authority installation changed"], None
    worker_authorities = [keyword.value for keyword in workers[0].keywords if keyword.arg == "authority"]
    authority_uses = [
        node for node in nodes if isinstance(node, ast.Name) and node.id == "composer_async_authority" and isinstance(node.ctx, ast.Load)
    ]
    if (
        len(worker_authorities) != 1
        or chain(worker_authorities[0]) != ("composer_async_authority",)
        or authority_uses != [installed[0].value, worker_authorities[0]]
    ):
        return ["worker policy supplier: current worker authority transfer changed"], None
    return [], {
        "authority_constructor": (unit.path, id(call)),
        "policy_call": (unit.path, id(keywords[0].value)),
        "settings_argument": (unit.path, id(keywords[0].value.args[0])),
        "authority_installation": (unit.path, id(installed[0])),
        "worker_authority_argument": (unit.path, id(worker_authorities[0])),
        "source_origins": ((unit.path, id(origin[1])), (unit.path, id(authority_origin[1]))),
    }


def continuation_policy_supplier_contracts(units):
    """Bind the complete value-policy suppliers in caller-owned current ASTs."""
    index = {}
    for unit in units:
        if unit.path in index:
            return ["worker policy supplier: duplicate source unit"], None
        index[unit.path] = unit
    if any(path not in index for path in (APP, POLICY_SETTINGS, POLICY_MODELS)):
        return ["worker policy supplier: missing current source"], None
    failures, consumer = _continuation_policy_consumer(index[APP])
    if failures:
        return failures, None
    definitions = []
    for path, expected in POLICY_SUPPLIER_GRAMMARS.items():
        unit = index[path]
        if not isinstance(unit.tree, ast.Module) or grammar(unit.tree) != expected:
            return ["worker policy supplier: complete current supplier changed " + path], None
        for name in POLICY_SUPPLIER_DEFINITIONS[path]:
            definition, _ = find(unit.tree, name)
            if definition is None or not owned_origin(unit.tree, name, definition):
                return ["worker policy supplier: owned producer binding changed " + path + ":" + name], None
            definitions.append((path, id(definition)))
    return [], {
        "supplier_definitions": tuple(definitions),
        "supplier_calls": (consumer["policy_call"],),
        "source_origins": consumer["source_origins"],
        "protected_origins": POLICY_SUPPLIER_ORIGINS,
        "function_origins": POLICY_SUPPLIER_FUNCTION_ORIGINS,
        "class_origins": POLICY_SUPPLIER_CLASS_ORIGINS,
        "consumer": consumer,
        "common_unknown": ("normal Pydantic/settings constructor and loader behavior", "outside or deferred supplier mutation"),
    }
