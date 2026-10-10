"""Finite terminal SQL same-input AST transfer facts, never admission."""

import ast
import copy
import hashlib
import inspect
import json
import types

from tests.helpers.web_reserve_proposal_child_transfers import copy_ast_fields

SERVICE = "src/elspeth/web/sessions/service.py"
REQUIRED = "src/elspeth/web/required_work.py"
WORKERS = "src/elspeth/web/async_workers.py"
AUTHORITY = "src/elspeth/web/coordination/composer_operation_authority.py"
CLASS = "SessionServiceImpl"
TERMINAL = "_run_composer_terminal_sql"
COMPLETE = "complete_composer_async_operation"
FAIL = "fail_composer_async_operation"
GRAMMARS = {
    TERMINAL: "a10083507d0d46ac9dae642785ff99692edad99350d2917862c2f9ff045c4300",
    COMPLETE: "748822c19f3c3284ea14ddde6ec7f90aef882871ba4e1fd3f4abcc0006e30147",
    FAIL: "c25306f67d7c37f4d473e445392f25edcb88751639f0069b67dfe64ebae09ff0",
}
NAMESPACE_GRAMMAR = "32b49d02b95825418cb15147bda3765144c356e12f76af1caa097b8fb65dda7c"
MASK = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
SUPPLIERS = (
    ("RequiredWorkCoordinator", "elspeth.web.required_work", REQUIRED),
    ("RequiredWorkTicket", "elspeth.web.required_work", REQUIRED),
    ("RequiredWorkSource", "elspeth.web.required_work", REQUIRED),
    ("ComposerAsyncOperationAuthority", "elspeth.web.coordination.composer_operation_authority", AUTHORITY),
    ("run_required_sql_in_worker", "elspeth.web.async_workers", WORKERS),
    ("run_stream_read_in_worker", "elspeth.web.async_workers", WORKERS),
)


def namespace_grammar(class_node):
    """Complete finite class declaration/suite, with deferred bodies inert.

    Definition headers and recursively executed nested classes stay visible;
    this hash is a conservative source refusal, not runtime class construction.
    """

    class Headers(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Pass()]
            return node

        def visit_AsyncFunctionDef(self, node):
            node.body = [ast.Pass()]
            return node

        def visit_Lambda(self, node):
            node.body = ast.Constant(value=None)
            return node

    return grammar(Headers().visit(copy_ast_fields(class_node)))


def chain(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = chain(node.value)
        return (*prefix, node.attr) if prefix else ()
    return ()


def find(tree, qualified):
    if not isinstance(tree, ast.Module):
        return (None, [])
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


def grammar(node):
    clone = copy.copy(node)
    clone.body = list(node.body)
    if (
        clone.body
        and isinstance(clone.body[0], ast.Expr)
        and isinstance(clone.body[0].value, ast.Constant)
        and (type(clone.body[0].value.value) is str)
    ):
        clone.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(canonical(clone), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def code_for(tree, owners):
    try:
        code = compile(tree, "<worker-whole-scope-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return (None, type(error).__name__)
    for owner in owners:
        if owner.type_params:
            return (None, "unsupported owner type parameters")
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
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and (statement.module == module)
        for alias in statement.names
        if alias.name == member and (alias.asname or alias.name) == binding
    ]
    if len(expected) != 1 or matches != [expected[0][1]]:
        return None
    return expected[0]


def owned_origin(tree, binding, definition):
    return not executed_class_global_relevance(tree, binding) and list(binding_nodes(scope_nodes(tree), binding)) == [definition]


def _executed_protected_loads(tree, protected):
    """Conservative direct module/class evaluated alias or effect relevance.

    Deferred function bodies and postponed annotations are excluded. A class
    suite can have a harmless local spelling; stores alone are not an escape.
    Any evaluated Load of a protected global supplier is refused rather than
    claiming a generic alias/descriptor closure. Actual classes' own implicit
    construction and all unlinked runtime effects remain conditional.
    """

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            expressions = [] if isinstance(node, ast.Lambda) else node.decorator_list
            expressions = list(expressions) + node.args.defaults + [x for x in node.args.kw_defaults if x is not None]
            for expression in expressions:
                yield from walk(expression)
            return
        if isinstance(node, ast.ClassDef):
            for expression in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                yield from walk(expression)
            for statement in node.body:
                yield from walk(statement)
            return
        if isinstance(node, ast.Name) and node.id in protected and isinstance(node.ctx, ast.Load):
            yield node
        for field, value in ast.iter_fields(node):
            if field in {"annotation", "returns", "type_params"}:
                continue
            if isinstance(value, ast.AST):
                yield from walk(value)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        yield from walk(child)

    return list(walk(tree))


def _id(node, path=SERVICE):
    return (path, id(node))


def _keyword(call, name):
    matches = [k for k in call.keywords if k.arg == name]
    return matches[0] if len(matches) == 1 else None


def _postponed_annotations(tree):
    return any(
        isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def terminal_sql_local_contracts(units):
    """Read caller-owned ASTs; return finite facts with mandatory UNKNOWNs."""
    receipt = {
        "terminal_definition": None,
        "terminal_required_work_formal": None,
        "reserve_calls": [],
        "reserve_receipts": {},
        "callers": [],
        "source_origins": [],
        "whole_scopes": [],
        "callback_origins": [],
        "effect_dependencies": [
            {
                "identity": "SERVICE_RECEIVER_AND_MEMBER_IDENTITY",
                "qualification": "UNKNOWN",
                "requirement": "common proof must qualify current exact self/service instance, runtime class/member lookup, descriptors, callable code and loaders",
            },
            {
                "identity": "NORMAL_REQUIRED_WORK_AND_AUTHORITY_SUPPLIERS",
                "qualification": "UNKNOWN",
                "requirement": "current exact coordinator/ticket/source/authority constructors and method behavior require common family/supplier proof",
            },
            {
                "identity": "SQL_WORKER_DATABASE_AND_CALLBACK_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "worker scheduling, threading/asyncio completion witnesses, transactions, fresh writer read, func/build_response callbacks and retry outcomes are source facts only; no database/process semantics are executed",
            },
            {
                "identity": "OUTSIDE_CONCURRENT_OR_DEFERRED_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "runtime aliases, evaluated definition effects, unselected deferred bodies and intervening/outside writers remain separate obligations",
            },
            {
                "identity": "FRESH_READS_AND_FIXTURE_SEAMS",
                "qualification": "UNKNOWN",
                "requirement": "retain current instance/authority/database reads and supplier/test substitution; no cached authority, provider bypass or tutorial path",
            },
        ],
    }
    index = {}
    for unit in units:
        path = str(unit.path)
        if path in index:
            return ["terminal SQL transfer: duplicate source unit " + path], receipt
        index[path] = unit
    required_paths = {SERVICE, REQUIRED, WORKERS, AUTHORITY}
    if any(path not in index or not isinstance(index[path].tree, ast.Module) for path in required_paths):
        return ["terminal SQL transfer: missing/unsupported selected SourceUnit AST"], receipt
    tree = index[SERVICE].tree
    failures = []
    cls, _ = find(tree, CLASS)
    if not isinstance(cls, ast.ClassDef) or not owned_origin(tree, CLASS, cls):
        return ["terminal SQL transfer: current service class source origin changed"], receipt
    for path in sorted(required_paths):
        if not _postponed_annotations(index[path].tree):
            failures.append("terminal SQL transfer: unsupported selected source annotation mode " + path)
    try:
        namespace_digest = namespace_grammar(cls)
    except (TypeError, ValueError, OverflowError, RecursionError):
        namespace_digest = None
    if namespace_digest != NAMESPACE_GRAMMAR:
        failures.append("terminal SQL transfer: finite current service declaration namespace changed")
    selected = {}
    protected = {CLASS, *(binding for binding, module, path in SUPPLIERS), "type", "range", "asyncio", "threading"}
    for name in (TERMINAL, COMPLETE, FAIL):
        method, owners = find(tree, CLASS + "." + name)
        if not isinstance(method, ast.AsyncFunctionDef):
            failures.append("terminal SQL transfer: actual async source owner missing " + name)
            continue
        selected[name] = method
        if method.decorator_list or method.type_params:
            failures.append("terminal SQL transfer: unqualified current method header " + name)
        code, reason = code_for(tree, owners)
        if code is None:
            failures.append("terminal SQL transfer: actual compiler metadata unavailable " + name + ":" + reason)
        else:
            if code.co_flags & MASK != inspect.CO_COROUTINE:
                failures.append("terminal SQL transfer: actual invocation kind changed " + name)
            for binding in sorted(protected):
                if binding in code.co_varnames + code.co_cellvars + code.co_freevars:
                    failures.append("terminal SQL transfer: whole-scope supplier localized/captured " + name + ":" + binding)
                if any(isinstance(n, (ast.Global, ast.Nonlocal)) and binding in n.names for n in body_nodes(method)):
                    failures.append("terminal SQL transfer: explicit supplier scope ambiguity " + name + ":" + binding)
            receipt["whole_scopes"].append(
                {
                    "definition": _id(method),
                    "qualified": CLASS + "." + name,
                    "actual_kind_bits": code.co_flags & MASK,
                    "locals": list(code.co_varnames),
                    "cells": list(code.co_cellvars),
                    "free": list(code.co_freevars),
                }
            )
        try:
            digest = grammar(method)
        except (TypeError, ValueError, OverflowError, RecursionError):
            digest = None
        if digest != GRAMMARS[name]:
            failures.append("terminal SQL transfer: unsupported complete finite whole-body grammar " + name)
    # The actual terminal_source default reads this imported enum member in
    # the executed class declaration. Its complete method/header grammar and
    # actual import origin are checked above; no other evaluated escape is
    # allowed by this exception. Runtime enum/descriptor effects stay UNKNOWN.
    expected_defaults = set()
    if TERMINAL in selected and selected[TERMINAL].args.kw_defaults:
        default = selected[TERMINAL].args.kw_defaults[-1]
        if isinstance(default, ast.Attribute) and chain(default) == ("RequiredWorkSource", "OPERATION_TERMINAL_SQL"):
            expected_defaults.add(id(default.value))
    if any(id(n) not in expected_defaults for n in _executed_protected_loads(tree, protected)):
        failures.append("terminal SQL transfer: evaluated protected module/class alias or effect unsupported")
    for builtin in ("type", "range"):
        if list(binding_nodes(scope_nodes(tree), builtin)) or executed_class_global_relevance(tree, builtin):
            failures.append("terminal SQL transfer: normal builtin source binding changed " + builtin)
    for binding, module, path in SUPPLIERS:
        origin = import_origin(tree, binding, module, binding)
        definition, _ = find(index[path].tree, binding)
        if origin is None or definition is None or not owned_origin(index[path].tree, binding, definition):
            failures.append("terminal SQL transfer: current supplier import/owned source origin changed " + binding)
            continue
        statement, alias = origin
        receipt["source_origins"].append(
            {
                "binding": binding,
                "source_origin": module + "." + binding,
                "import_statement": _id(statement),
                "import_alias": _id(alias),
                "supplier_definition": _id(definition, path),
                "qualification": "CURRENT_SOURCE_ORIGIN_RUNTIME_SUPPLIER_UNKNOWN",
            }
        )
    for path, names in (
        (REQUIRED, {"RequiredWorkCoordinator", "RequiredWorkTicket"}),
        (WORKERS, {"run_required_sql_in_worker", "run_stream_read_in_worker"}),
        (AUTHORITY, {"ComposerAsyncOperationAuthority"}),
    ):
        if _executed_protected_loads(index[path].tree, names):
            failures.append("terminal SQL transfer: evaluated supplier module/class alias or effect unsupported " + path)
    for binding in ("asyncio", "threading"):
        matches = list(binding_nodes(scope_nodes(tree), binding))
        origins = [
            (n, a) for n in tree.body if isinstance(n, ast.Import) for a in n.names if a.name == binding and (a.asname or a.name) == binding
        ]
        if len(origins) != 1 or matches != [origins[0][1]] or executed_class_global_relevance(tree, binding):
            failures.append("terminal SQL transfer: current stdlib source import changed " + binding)
        else:
            receipt["source_origins"].append(
                {
                    "binding": binding,
                    "source_origin": binding,
                    "import_statement": _id(origins[0][0]),
                    "import_alias": _id(origins[0][1]),
                    "qualification": "SOURCE_STDLIB_IMPORT_RUNTIME_FRAMEWORK_UNKNOWN",
                }
            )
    if failures or len(selected) != 3:
        return failures, receipt
    terminal = selected[TERMINAL]
    loop = terminal.body[4]
    witnessed = loop.body[3]
    callbacks = [(TERMINAL, terminal, witnessed)]
    for name, positions in ((COMPLETE, (12, 13)), (FAIL, (7, 8))):
        callbacks.extend((name, selected[name], selected[name].body[pos]) for pos in positions)
    for name, method, callback in callbacks:
        code, reason = code_for(tree, [cls, method, callback])
        if code is None or code.co_flags & MASK:
            failures.append(
                "terminal SQL transfer: exact nested callback ordinary compiler kind unavailable/changed " + name + ":" + callback.name
            )
        else:
            receipt["callback_origins"].append(
                {
                    "definition": _id(callback),
                    "parent_definition": _id(method),
                    "qualified": CLASS + "." + name + "." + callback.name,
                    "actual_kind_bits": code.co_flags & MASK,
                    "locals": list(code.co_varnames),
                    "cells": list(code.co_cellvars),
                    "free": list(code.co_freevars),
                    "qualification": "EXACT_SOURCE_CLOSURE_BODY_RUNTIME_EFFECT_UNKNOWN",
                }
            )
    if failures:
        return failures, receipt
    formal = next(a for a in terminal.args.kwonlyargs if a.arg == "required_work")
    func_formal = next(a for a in terminal.args.args if a.arg == "func")
    work_calls = [n for n in body_nodes(terminal) if isinstance(n, ast.Call) and chain(n.func) == ("required_work", "reserve")]
    if len(work_calls) != 2:
        return ["terminal SQL transfer: current two-reserve universe changed"], receipt
    parents = {id(child): parent for parent in ast.walk(terminal) for child in ast.iter_child_nodes(parent)}
    receipt.update(
        {
            "service_class": _id(cls),
            "terminal_definition": _id(terminal),
            "terminal_required_work_formal": _id(formal),
            "terminal_func_formal": _id(func_formal),
            "terminal_running_formal": _id(terminal.args.args[1]),
            "terminal_source_formal": _id(next(a for a in terminal.args.kwonlyargs if a.arg == "terminal_source")),
            "terminal_source_default": _id(terminal.args.kw_defaults[-1]),
            "nominal_authority_guard": _id(terminal.body[2]),
            "authority_constructor": _id(terminal.body[1].value),
            "authority_store": _id(terminal.body[1].targets[0]),
            "attempt_loop": _id(loop),
            "attempt_store": _id(loop.target),
            "witnessed_sql_definition": _id(witnessed),
            "witnessed_func_call": _id(witnessed.body[1].body[0].value),
            "witnessed_completion_finally": _id(witnessed.body[1].finalbody[0].value),
            "sql_worker_submission": _id(loop.body[5].value.args[0].body),
            "writer_read_worker_submission": _id(loop.body[9].handlers[0].body[2].value.args[0].body),
            "fresh_writer_read_member": _id(loop.body[9].handlers[0].body[2].value.args[0].body.args[1]),
            "terminal_returns": [_id(n) for n in body_nodes(terminal) if isinstance(n, ast.Return)],
            "first_failure_store": _id(loop.body[9].handlers[0].body[0].body[0].targets[0]),
            "closed_retry_guard": _id(loop.body[9].handlers[0].body[6]),
            "unknown_writer_read_raise": _id(loop.body[9].handlers[0].body[4].handlers[0].body[0]),
        }
    )
    for name, statement in ((COMPLETE, selected[COMPLETE].body[-1]), (FAIL, selected[FAIL].body[-1].body[0])):
        method = selected[name]
        call = statement.value.value
        kw = _keyword(call, "required_work")
        retry = _keyword(call, "can_retry")
        if chain(call.func) != ("self", TERMINAL) or chain(kw.value) != ("required_work",):
            return ["terminal SQL transfer: exact same-call coordinator transfer changed " + name], receipt
        callback_by_name = {c.name: c for owner, parent, c in callbacks if owner == name}
        record = {
            "name": name,
            "definition": _id(method),
            "call": _id(call),
            "member": _id(call.func),
            "receiver": _id(call.func.value),
            "await": _id(statement.value),
            "return": _id(statement),
            "required_work_formal": _id(next(a for a in method.args.kwonlyargs if a.arg == "required_work")),
            "required_work_keyword": _id(kw),
            "required_work_value": _id(kw.value),
            "running_value": _id(call.args[0]),
            "func_value": _id(call.args[1]),
            "func_definition": _id(callback_by_name["_sync"]),
            "can_retry_keyword": _id(retry),
            "can_retry_value": _id(retry.value),
            "can_retry_definition": _id(callback_by_name["_can_retry"]),
            "terminal_definition": receipt["terminal_definition"],
            "terminal_formal": receipt["terminal_required_work_formal"],
            "qualification": "SAME_AST_SOURCE_CALLER_TRANSFER_RUNTIME_UNKNOWN",
        }
        if name == FAIL:
            record["terminal_source_keyword"] = _id(_keyword(call, "terminal_source"))
            record["terminal_source_value"] = _id(_keyword(call, "terminal_source").value)
            record["nominal_projection_guard"] = _id(method.body[1])
            record["retained_error_handlers"] = [_id(n) for n in method.body[-1].handlers]
        receipt["callers"].append(record)
    for ordinal, call in enumerate(work_calls):
        pair = _id(call)
        conditional = parents[id(call)]
        assignment = parents[id(conditional)]
        receipt["reserve_calls"].append(pair)
        receipt["reserve_receipts"][pair] = {
            "ordinal": ordinal,
            "call": pair,
            "member": _id(call.func),
            "receiver": _id(call.func.value),
            "source_argument": _id(call.args[0]),
            "attempt_keyword": _id(call.keywords[0]),
            "attempt_value": _id(call.keywords[0].value),
            "guarded_expression": _id(conditional),
            "ticket_assignment": _id(assignment),
            "ticket_store": _id(assignment.targets[0]),
            "formal": receipt["terminal_required_work_formal"],
            "consumer_definition": receipt["terminal_definition"],
            "caller_calls": [r["call"] for r in receipt["callers"]],
            "qualification": "TWO_FINITE_SOURCE_SITES_RUNTIME_SQL_AND_SUPPLIER_UNKNOWN",
        }
    return [], receipt
