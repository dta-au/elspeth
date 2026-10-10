"""Finite same-input proposal-child transfer facts, never graph admission.

The caller owns parsing and the full registry. This helper reads two supplied
ASTs, compiles metadata only, and returns IDs in those ASTs. Full current
wrapper/internal grammars bound relevant executed paths; runtime suppliers,
member lookup, awaited callbacks and outside effects remain explicit UNKNOWNs.
"""

import ast
import copy
import hashlib
import inspect
import json
import types


def copy_ast_fields(value):
    """Copy declared source fields without following parser parent metadata."""
    if isinstance(value, ast.AST):
        return type(value)(**{field: copy_ast_fields(item) for field, item in ast.iter_fields(value)})
    if isinstance(value, list):
        return [copy_ast_fields(item) for item in value]
    return value


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


SETTLEMENT = "src/elspeth/web/sessions/routes/composer/pipeline_settlement.py"
REQUIRED = "src/elspeth/web/required_work.py"
WRAPPER = "settle_pipeline_proposal_under_compose_lock"
INTERNAL = "_settle_pipeline_proposal_under_compose_lock"
GRAMMARS = {
    WRAPPER: "7c7465be32aeab7821c08a1a511541f23bb394aa39cd756b2f2215cda3cde599",
    INTERNAL: "482216ada06e3ec587f426c37387c0e37fafcf88f4ea42e00a43f60ab3fce027",
}
PRODUCER_GRAMMARS = {
    "begin_proposal_child": "12e18ac0cd24c08443b2170d4961359f3dfc0933dfe4f15661d1592a3f6b0534",
    "for_proposal": "ac4f3d19ab1b799bb5ad618074f3e864eb0aded708f4cb6c6b83af19fb979ecd",
}
NAMESPACE_GRAMMARS = {
    "RequiredWorkCoordinator": "b2c87a9e9270600679f8d9d6fccc13f88f644f940e6891538d5dc7deb9f22471",
    "RequiredWorkBinding": "e4e502c55b4ecd87baa32c1165c938a6fceafd737f930b55287a87fd392164b2",
}
SOURCES = (
    "PREPARATION_VALIDATION_PRODUCER",
    "POSTCOMMIT_REVIEW_READ_SQL",
    "POSTCOMMIT_REVIEW_PROJECTION",
    "PREPARATION_VALIDATION_PRODUCER",
    "PIPELINE_PUBLICATION_PROJECTION",
    "PIPELINE_PUBLICATION_SQL",
    "TRUST_REVOCATION_SQL",
    "TRUST_REVOCATION_PROJECTION",
)
SUPPLIERS = (
    "RequiredWorkCoordinator",
    "RequiredWorkBinding",
    "RequiredWorkAuthority",
    "RequiredWorkSource",
    "RequiredWorkRole",
    "RequiredAuthorityKind",
    "required_failure_leaves",
)
MASK = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE


def _keyword(call, name):
    matches = [k for k in call.keywords if k.arg == name]
    return matches[0] if len(matches) == 1 else None


def _identity(path, node):
    return (path, id(node))


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


def _postponed_annotations(tree):
    return any(
        isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def proposal_child_local_contracts(units):
    """Return bounded facts plus unresolved dependencies on the same ASTs."""
    receipt = {
        "proposal_wrapper": None,
        "child_producer": None,
        "child_binding_ctor": None,
        "child_transfer_call": None,
        "internal_required_work_formal": None,
        "reserve_calls": [],
        "reserve_receipts": {},
        "source_origins": [],
        "whole_scopes": [],
        "effect_dependencies": [
            {
                "identity": "NORMAL_REQUIRED_WORK_SUPPLIERS",
                "qualification": "UNKNOWN",
                "requirement": "common family/supplier proof must qualify current exact class construction, authority, begin_proposal_child result pair, bindings, reserve and completion semantics",
            },
            {
                "identity": "NORMAL_CALLABLE_AND_MEMBER_LOOKUP",
                "qualification": "UNKNOWN",
                "requirement": "source definitions/import aliases and compiler kind are facts; actual loaders, modules, callable code, decorators, descriptors and builtins must agree",
            },
            {
                "identity": "INTERVENING_AWAIT_CALLBACK_EFFECTS",
                "qualification": "UNKNOWN",
                "requirement": "existing preparation, service/provider, projection, cancellation, cleanup and framework effects cannot mutate retained coordinators or members; source-local transfer alone cannot establish that",
            },
            {
                "identity": "OUTSIDE_CONCURRENT_MUTATION",
                "qualification": "UNKNOWN",
                "requirement": "runtime aliases, external writers, evaluated definition effects and deferred unselected bodies remain separate obligations",
            },
            {
                "identity": "FRESH_SERVICES_AND_TEST_SEAMS",
                "qualification": "UNKNOWN",
                "requirement": "retain fresh services.session_service and interpretation_surfacing reads and existing supplier/fixture substitution; no provider or tutorial bypass is authorized",
            },
        ],
    }
    units = list(units)
    index = {}
    for unit in units:
        path = str(unit.path)
        if path in index:
            return (["proposal child transfer: duplicate source unit " + path], receipt)
        index[path] = unit
    if any(path not in index or not isinstance(index[path].tree, ast.Module) for path in (SETTLEMENT, REQUIRED)):
        return (["proposal child transfer: missing/unsupported selected SourceUnit AST"], receipt)
    tree = index[SETTLEMENT].tree
    required_tree = index[REQUIRED].tree
    failures = []
    for path, selected_tree in ((SETTLEMENT, tree), (REQUIRED, required_tree)):
        if not _postponed_annotations(selected_tree):
            failures.append("proposal child transfer: unsupported selected source annotation mode " + path)
    selected = {}
    protected = {WRAPPER, INTERNAL, *SUPPLIERS, "type", "str"}
    for name in (WRAPPER, INTERNAL):
        fn, owners = find(tree, name)
        if not isinstance(fn, ast.AsyncFunctionDef):
            failures.append("proposal child transfer: missing/foreign async owner " + name)
            continue
        selected[name] = fn
        if fn.decorator_list or fn.type_params:
            failures.append("proposal child transfer: unqualified definition header " + name)
        code, reason = code_for(tree, owners)
        if code is None:
            failures.append("proposal child transfer: compiler metadata unavailable " + name + ":" + reason)
        else:
            if code.co_flags & MASK != inspect.CO_COROUTINE:
                failures.append("proposal child transfer: actual invocation kind changed " + name)
            scope = set(code.co_varnames + code.co_cellvars + code.co_freevars)
            for binding in protected:
                if binding in scope:
                    failures.append("proposal child transfer: whole-function supplier localized/captured " + name + ":" + binding)
                if any(isinstance(n, (ast.Global, ast.Nonlocal)) and binding in n.names for n in body_nodes(fn)):
                    failures.append("proposal child transfer: explicit supplier scope ambiguity " + name + ":" + binding)
            receipt["whole_scopes"].append(
                {
                    "definition": _identity(SETTLEMENT, fn),
                    "qualified": name,
                    "actual_kind_bits": code.co_flags & MASK,
                    "locals": list(code.co_varnames),
                    "cells": list(code.co_cellvars),
                    "free": list(code.co_freevars),
                }
            )
        try:
            actual_grammar = grammar(fn)
        except (TypeError, ValueError, OverflowError, RecursionError):
            actual_grammar = None
        if actual_grammar != GRAMMARS[name]:
            failures.append("proposal child transfer: unsupported complete finite whole-body grammar " + name)
        if not owned_origin(tree, name, fn):
            failures.append("proposal child transfer: owned callable module binding changed " + name)
    if _executed_protected_loads(tree, protected):
        failures.append("proposal child transfer: evaluated module/class protected alias or effect unsupported")
    for builtin in ("type", "str"):
        if list(binding_nodes(scope_nodes(tree), builtin)) or executed_class_global_relevance(tree, builtin):
            failures.append("proposal child transfer: normal builtin source binding changed " + builtin)
    for binding in SUPPLIERS:
        origin = import_origin(tree, binding, "elspeth.web.required_work", binding)
        definition, _ = find(required_tree, binding)
        if origin is None or definition is None or (not owned_origin(required_tree, binding, definition)):
            failures.append("proposal child transfer: canonical current supplier source origin changed " + binding)
            continue
        statement, alias = origin
        receipt["source_origins"].append(
            {
                "binding": binding,
                "source_origin": "elspeth.web.required_work." + binding,
                "import_statement": _identity(SETTLEMENT, statement),
                "import_alias": _identity(SETTLEMENT, alias),
                "supplier_definition": _identity(REQUIRED, definition),
                "qualification": "SOURCE_BINDING_RUNTIME_SUPPLIER_UNKNOWN",
            }
        )
        if binding in NAMESPACE_GRAMMARS:
            try:
                namespace_digest = namespace_grammar(definition)
            except (TypeError, ValueError, OverflowError, RecursionError):
                namespace_digest = None
            if namespace_digest != NAMESPACE_GRAMMARS[binding]:
                failures.append("proposal child transfer: finite supplier class namespace changed " + binding)
    if _executed_protected_loads(required_tree, {WRAPPER, INTERNAL, "RequiredWorkCoordinator", "RequiredWorkBinding"}):
        failures.append("proposal child transfer: evaluated supplier module/class protected alias or effect unsupported")
    supplier_members = {}
    for member in ("begin_proposal_child", "for_proposal", "reserve", "complete_proposal_child", "assert_completed", "failure_receipts"):
        definition, owners = find(required_tree, "RequiredWorkCoordinator." + member)
        if not isinstance(definition, ast.FunctionDef) or definition.decorator_list or definition.type_params:
            failures.append("proposal child transfer: normal current supplier member source unavailable " + member)
            continue
        code, reason = code_for(required_tree, owners)
        if code is None or code.co_flags & MASK:
            failures.append("proposal child transfer: supplier member compiler kind unavailable/changed " + member)
            continue
        if member in PRODUCER_GRAMMARS:
            try:
                producer_digest = grammar(definition)
            except (TypeError, ValueError, OverflowError, RecursionError):
                producer_digest = None
            if producer_digest != PRODUCER_GRAMMARS[member]:
                failures.append("proposal child transfer: complete finite child producer grammar changed " + member)
        if "RequiredWorkCoordinator" in code.co_varnames + code.co_cellvars + code.co_freevars:
            failures.append("proposal child transfer: whole-function child class origin localized " + member)
        supplier_members[member] = definition
        receipt["source_origins"].append(
            {
                "binding": "RequiredWorkCoordinator." + member,
                "supplier_definition": _identity(REQUIRED, definition),
                "actual_kind_bits": code.co_flags & MASK,
                "qualification": "ORDINARY_SOURCE_MEMBER_BEHAVIOR_AND_RUNTIME_UNKNOWN",
            }
        )
    if failures or len(selected) != 2:
        return (failures, receipt)
    wrapper = selected[WRAPPER]
    internal = selected[INTERNAL]
    issued = wrapper.body[5].body[3]
    binding = wrapper.body[5].body[4]
    consumer_assignment = wrapper.body[6].body[0]
    consumer_await = consumer_assignment.value
    consumer = consumer_await.value
    work_keyword = _keyword(consumer, "required_work")
    binding_keyword = _keyword(consumer, "required_binding")
    formal = next(arg for arg in internal.args.kwonlyargs if arg.arg == "required_work")
    proposal_alias = internal.body[4].body[1]
    fresh_owner = internal.body[4].orelse[1]
    nodes = list(body_nodes(internal))
    reserves = [n for n in nodes if isinstance(n, ast.Call) and chain(n.func) == ("proposal_work", "reserve")]
    if len(reserves) != len(SOURCES) or tuple(chain(n.args[0])[-1] for n in reserves) != SOURCES:
        return (["proposal child transfer: finite current reserve universe changed"], receipt)
    if chain(work_keyword.value) != ("child",) or chain(binding_keyword.value) != ("child_binding",):
        return (["proposal child transfer: same-call child and binding transfer changed"], receipt)
    parents = {id(child): parent for parent in ast.walk(internal) for child in ast.iter_child_nodes(parent)}
    receipt.update(
        {
            "proposal_wrapper": _identity(SETTLEMENT, wrapper),
            "internal_definition": _identity(SETTLEMENT, internal),
            "wrapper_required_work_formal": _identity(SETTLEMENT, next(a for a in wrapper.args.kwonlyargs if a.arg == "required_work")),
            "wrapper_nominal_binding_guard": _identity(SETTLEMENT, wrapper.body[0]),
            "wrapper_nominal_coordinator_guard": _identity(SETTLEMENT, wrapper.body[1]),
            "wrapper_parent_binding_guard": _identity(SETTLEMENT, wrapper.body[5].body[1]),
            "wrapper_parent_context_call": _identity(SETTLEMENT, wrapper.body[5].body[2].value),
            "child_producer": _identity(SETTLEMENT, issued.value),
            "child_producer_member": _identity(SETTLEMENT, issued.value.func),
            "child_producer_assignment": _identity(SETTLEMENT, issued),
            "child_store": _identity(SETTLEMENT, issued.targets[0].elts[0]),
            "producer_store": _identity(SETTLEMENT, issued.targets[0].elts[1]),
            "child_binding_ctor": _identity(SETTLEMENT, binding.value),
            "child_binding_argument": _identity(SETTLEMENT, binding.value.args[0]),
            "child_binding_store": _identity(SETTLEMENT, binding.targets[0]),
            "child_transfer_call": _identity(SETTLEMENT, consumer),
            "child_transfer_await": _identity(SETTLEMENT, consumer_await),
            "child_transfer_keyword": _identity(SETTLEMENT, work_keyword),
            "child_transfer_value": _identity(SETTLEMENT, work_keyword.value),
            "binding_transfer_keyword": _identity(SETTLEMENT, binding_keyword),
            "binding_transfer_value": _identity(SETTLEMENT, binding_keyword.value),
            "internal_required_work_formal": _identity(SETTLEMENT, formal),
            "internal_nominal_coordinator_guard": _identity(SETTLEMENT, internal.body[1]),
            "internal_proposal_authority_guard": _identity(SETTLEMENT, internal.body[4].body[0]),
            "internal_child_alias_store": _identity(SETTLEMENT, proposal_alias.targets[0]),
            "internal_child_alias_value": _identity(SETTLEMENT, proposal_alias.value),
            "internal_fresh_owner_ctor": _identity(SETTLEMENT, fresh_owner.value),
            "internal_fresh_owner_store": _identity(SETTLEMENT, fresh_owner.targets[0]),
            "internal_binding_guard": _identity(SETTLEMENT, internal.body[5]),
            "internal_binding_owner_guard": _identity(SETTLEMENT, internal.body[7]),
            "internal_binding_context_call": _identity(SETTLEMENT, internal.body[8].value),
            "fresh_session_service_member": _identity(SETTLEMENT, internal.body[2].value),
            "wrapper_result_store": _identity(SETTLEMENT, consumer_assignment.targets[0]),
            "wrapper_return": _identity(SETTLEMENT, wrapper.body[-1]),
            "wrapper_return_value": _identity(SETTLEMENT, wrapper.body[-1].value),
            "internal_returns": [_identity(SETTLEMENT, n) for n in nodes if isinstance(n, ast.Return)],
            "child_completion_calls": [
                _identity(SETTLEMENT, n)
                for n in body_nodes(wrapper)
                if isinstance(n, ast.Call) and chain(n.func) == ("required_work", "complete_proposal_child")
            ],
            "child_failure_receipts_call": [
                _identity(SETTLEMENT, n)
                for n in body_nodes(wrapper)
                if isinstance(n, ast.Call) and chain(n.func) == ("child", "failure_receipts")
            ],
            "child_assert_completed_call": [
                _identity(SETTLEMENT, n)
                for n in body_nodes(wrapper)
                if isinstance(n, ast.Call) and chain(n.func) == ("child", "assert_completed")
            ],
        }
    )
    for ordinal, call in enumerate(reserves):
        pair = _identity(SETTLEMENT, call)
        receipt["reserve_calls"].append(pair)
        receipt["reserve_receipts"][pair] = {
            "ordinal": ordinal,
            "source": SOURCES[ordinal],
            "call": pair,
            "member": _identity(SETTLEMENT, call.func),
            "receiver": _identity(SETTLEMENT, call.func.value),
            "source_argument": _identity(SETTLEMENT, call.args[0]),
            "ticket_assignment": _identity(SETTLEMENT, parents[id(call)]),
            "ticket_store": _identity(SETTLEMENT, parents[id(call)].targets[0]),
            "consumer_definition": receipt["internal_definition"],
            "formal": receipt["internal_required_work_formal"],
            "child_producer": receipt["child_producer"],
            "caller": receipt["child_transfer_call"],
            "qualification": "FINITE_SOURCE_TRANSFER_RUNTIME_EFFECTS_UNKNOWN",
        }
    issuance = supplier_members["begin_proposal_child"]
    narrowing = supplier_members["for_proposal"]
    receipt["supplier_child_producer_call"] = _identity(REQUIRED, issuance.body[0].body[0].value)
    receipt["supplier_child_store"] = _identity(REQUIRED, issuance.body[0].body[0].targets[0])
    receipt["supplier_pair_return"] = _identity(REQUIRED, issuance.body[0].body[-1])
    receipt["supplier_pair_return_values"] = [_identity(REQUIRED, x) for x in issuance.body[0].body[-1].value.elts]
    receipt["supplier_proposal_child_ctor"] = _identity(REQUIRED, narrowing.body[2].body[3].value)
    receipt["supplier_proposal_returns"] = [_identity(REQUIRED, n) for n in body_nodes(narrowing) if isinstance(n, ast.Return)]
    return (failures, receipt)
