"""Finite Composer receiver transport facts. No admission or outside-effect claim."""

import ast
import inspect
import types

APP = "src/elspeth/web/app.py"
DTO = "src/elspeth/web/sessions/composer_app_services.py"
TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/composer/service.py"
FIELDS = (
    "session_service",
    "composer_service",
    "settings",
    "catalog_service",
    "operator_profile_registry",
    "scoped_secret_resolver",
    "session_engine",
    "interpretation_surfacing",
    "plugin_snapshot_for_user_id",
    "progress_registry",
    "compose_locks",
)
LOOKUP = {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__", "__set__", "__delete__"}


def name(node, value):
    return isinstance(node, ast.Name) and node.id == value


def chain(node):
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        prefix = chain(node.value)
        return (*prefix, node.attr) if prefix else ()
    return ()


def body(node):
    return [
        n
        for n in node.body
        if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
        and not isinstance(n, ast.Pass)
    ]


def runtime_walk(node, *, postponed=True, scope="module"):
    """Executable roles; local annotations never execute, definition mode is explicit."""
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for expression in list(getattr(node, "decorator_list", ())) + node.args.defaults + [n for n in node.args.kw_defaults if n]:
            yield from runtime_walk(expression, postponed=postponed, scope=scope)
        if not postponed and not isinstance(node, ast.Lambda):
            annotations = [a.annotation for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
            annotations += [getattr(node.args.vararg, "annotation", None), getattr(node.args.kwarg, "annotation", None), node.returns]
            for expression in annotations:
                if expression is not None:
                    yield from runtime_walk(expression, postponed=postponed, scope=scope)
        statements = [node.body] if isinstance(node, ast.Lambda) else node.body
        for statement in statements:
            yield from runtime_walk(statement, postponed=postponed, scope="function")
        return
    if isinstance(node, ast.AnnAssign):
        yield from runtime_walk(node.target, postponed=postponed, scope=scope)
        if node.value is not None:
            yield from runtime_walk(node.value, postponed=postponed, scope=scope)
        if not postponed and scope in {"module", "class"}:
            yield from runtime_walk(node.annotation, postponed=postponed, scope=scope)
        return
    for field, value in ast.iter_fields(node):
        if field in {"annotation", "returns", "type_params"}:
            continue
        if isinstance(value, ast.AST):
            yield from runtime_walk(
                value, postponed=postponed, scope="class" if isinstance(node, ast.ClassDef) and field == "body" else scope
            )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield from runtime_walk(
                        item, postponed=postponed, scope="class" if isinstance(node, ast.ClassDef) and field == "body" else scope
                    )


def immediate(node):
    """Enclosing execution, including definition defaults/decorators, excluding bodies."""
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        expressions = list(getattr(node, "decorator_list", ())) + node.args.defaults + [x for x in node.args.kw_defaults if x]
        for expression in expressions:
            yield from immediate(expression)
        return
    for field, value in ast.iter_fields(node):
        if field in {"annotation", "returns", "type_params"}:
            continue
        if isinstance(value, ast.AST):
            yield from immediate(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield from immediate(item)


def executed_body(function):
    for statement in function.body:
        yield from immediate(statement)


def find(tree, qualified):
    nodes = tree.body
    result = None
    for part in qualified.split("."):
        candidates = [n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
        if len(candidates) != 1:
            return None
        result = candidates[0]
        nodes = result.body
    return result


def imported(tree, binding, module, member):
    hits = []
    for node in immediate(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*" or (alias.asname or alias.name) == binding:
                    hits.append(node.module == module and node.level == 0 and alias.name == member and alias.asname in (None, binding))
        elif isinstance(node, ast.Import):
            hits.extend(False for alias in node.names if (alias.asname or alias.name.split(".")[0]) == binding)
        elif (isinstance(node, ast.Name) and node.id == binding and isinstance(node.ctx, (ast.Store, ast.Del))) or (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == binding
        ):
            hits.append(False)
    return hits == [True]


def safe_method_definition(node):
    if getattr(node, "type_params", ()):
        return False
    if not all(isinstance(n, ast.Constant) for n in node.args.defaults + [n for n in node.args.kw_defaults if n]):
        return False
    return all(isinstance(n, ast.Name) and n.id in {"property", "classmethod", "staticmethod"} for n in node.decorator_list)


def owned_binding(tree, binding, definition):
    hits = []
    for n in immediate(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == binding:
            hits.append(n is definition)
        elif isinstance(n, ast.Name) and n.id == binding and isinstance(n.ctx, (ast.Store, ast.Del)):
            hits.append(False)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            hits.extend(False for a in n.names if a.name == "*" or (a.asname or a.name.split(".")[0]) == binding)
    return hits == [True]


def builtin_binding(tree, binding):
    for n in immediate(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == binding:
            return False
        if isinstance(n, ast.Name) and n.id == binding and isinstance(n.ctx, (ast.Store, ast.Del)):
            return False
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
            a.name == "*" or (a.asname or a.name.split(".")[0]) == binding for a in n.names
        ):
            return False
    return True


def plain_class(node, *, dto=False):
    if not isinstance(node, ast.ClassDef) or node.bases or node.keywords or getattr(node, "type_params", ()):
        return False
    if dto:
        decorators = node.decorator_list
        if not (
            len(decorators) == 2
            and name(decorators[0], "final")
            and isinstance(decorators[1], ast.Call)
            and name(decorators[1].func, "dataclass")
            and not decorators[1].args
            and [(k.arg, k.value.value if isinstance(k.value, ast.Constant) else object()) for k in decorators[1].keywords]
            == [("frozen", True), ("slots", True)]
        ):
            return False
        declarations = body(node)
        return (
            len(declarations) == len(FIELDS)
            and all(
                isinstance(n, ast.AnnAssign) and n.simple == 1 and n.value is None and name(n.target, field)
                for n, field in zip(declarations, FIELDS, strict=False)
            )
            and name(declarations[1].annotation, "ComposerService")
            and not any(
                isinstance(n, (ast.Constant, ast.Call, ast.NamedExpr))
                or (isinstance(n, ast.Name) and n.id in {"ClassVar", "InitVar", "KW_ONLY"})
                for d in declarations
                for n in ast.walk(d.annotation)
            )
        )
    if node.decorator_list:
        return False
    return all(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name not in LOOKUP and safe_method_definition(n) for n in body(node)
    )


def parameters(node):
    return [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]


def actual_code(tree, owners):
    """Metadata for the exact owner path from the whole actual AST; no execution."""
    try:
        code = compile(tree, "<composer-receiver-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError):
        return None
    for owner in owners:
        if not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or getattr(owner, "type_params", ()):
            return None
        definition_line = min([owner.lineno] + [n.lineno for n in owner.decorator_list])
        matches = [
            c for c in code.co_consts if isinstance(c, types.CodeType) and c.co_name == owner.name and c.co_firstlineno == definition_line
        ]
        if len(matches) != 1:
            return None
        code = matches[0]
    return code


def function_binding_scope(tree, function):
    """Complete compiler local/cell/free bindings, preserving actual signature."""
    c = actual_code(tree, [function])
    if c is None:
        return None
    return frozenset(c.co_varnames + c.co_cellvars + c.co_freevars)


def invocation_kind(tree, owners, expected):
    """Exact selected callable invocation kind, not a subtree yield blacklist.

    Nested/deferred code is selected only through its real lexical owner path.
    Ordinary functions must not be generators or coroutine variants. Awaited
    functions must be native coroutines, excluding iterable and async generators.
    Runtime object/code mutation remains a separate common origin/effect premise.
    """
    code = actual_code(tree, owners)
    if code is None:
        return False, None
    mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
    bits = code.co_flags & mask
    allowed = 0 if expected == "ORDINARY_FUNCTION" else inspect.CO_COROUTINE if expected == "NATIVE_COROUTINE" else None
    return bits == allowed, {
        "expected_kind": expected,
        "actual_kind_bits": bits,
        "actual_code_flags": code.co_flags,
        "definition_node_id": id(owners[-1]),
        "owner_node_ids": [id(n) for n in owners],
        "qualification": "ACTUAL_COMPILED_SOURCE_INVOCATION_KIND_COMMON_CODE_ORIGIN_UNKNOWN",
    }


def lexical_binding_events(function):
    """Actual selected function scope; deferred/class/comprehension locals separate."""
    events = []
    args = function.args.posonlyargs + function.args.args + function.args.kwonlyargs
    args += [a for a in (function.args.vararg, function.args.kwarg) if a is not None]
    events.extend((a.arg, "PARAMETER", a) for a in args)

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                events.append((node.name, "DEFINITION", node))
            expressions = list(getattr(node, "decorator_list", ()))
            if isinstance(node, ast.ClassDef):
                expressions += node.bases + [k.value for k in node.keywords]
            else:
                expressions += node.args.defaults + [n for n in node.args.kw_defaults if n]
            for expression in expressions:
                visit(expression)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            # Comprehension iteration targets have their own compiler scope.
            # Assignment expressions bind the surrounding function; recurse
            # only to enumerate those, retaining lambda scope separation.
            def walrus(n):
                if isinstance(n, ast.Lambda):
                    for x in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                        walrus(x)
                    return
                if isinstance(n, ast.NamedExpr):
                    visit(n)
                    return
                for x in ast.iter_child_nodes(n):
                    walrus(x)

            for generator in node.generators:
                visit(generator.iter)
                for condition in generator.ifs:
                    walrus(condition)
            for field in ("elt", "key", "value"):
                expression = getattr(node, field, None)
                if expression is not None:
                    walrus(expression)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            events.append((node.id, "STORE_OR_DELETE", node))
        if isinstance(node, ast.ExceptHandler) and node.name:
            events.append((node.name, "EXCEPTION_TARGET", node))
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            events.append((node.name, "PATTERN_TARGET", node))
        if isinstance(node, ast.MatchMapping) and node.rest:
            events.append((node.rest, "PATTERN_REST", node))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            events.extend(
                (a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name), "IMPORT", node) for a in node.names
            )
        for field, value in ast.iter_fields(node):
            if field in {"annotation", "returns", "type_params"}:
                continue
            if isinstance(value, ast.AST):
                visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        visit(item)

    for statement in function.body:
        visit(statement)
    return events


def early_abrupt(statements):
    """Reject unsupported direct abrupt prefixes, no generic branch interpretation."""
    return any(isinstance(n, (ast.Return, ast.Raise, ast.Break, ast.Continue)) for n in statements)


def literal(node, value):
    return isinstance(node, ast.Constant) and type(node.value) is type(value) and node.value == value


def exact_call(node, callee, args=(), keywords=()):
    return (
        isinstance(node, ast.Call)
        and chain(node.func) == callee
        and len(node.args) == len(args)
        and all(chain(n) == expected for n, expected in zip(node.args, args, strict=False))
        and [n.arg for n in node.keywords] == [k for k, v in keywords]
        and all(chain(n.value) == expected for n, (k, expected) in zip(node.keywords, keywords, strict=False))
    )


def receiver_transfer_prefix(statements):
    """The complete actual receiver→await prefix, as explicit operation contracts.

    Calls are preserved as supplier effects to qualify, never trusted by name.
    The deferred settlement body is a separate captured-function effect premise.
    """
    nodes = [n for n in statements if not isinstance(n, ast.Pass)]
    if len(nodes) != 7:
        return False
    imp, deferred, error, settled, budget, state, guard = nodes
    if not (
        isinstance(imp, ast.ImportFrom)
        and imp.module == "openai"
        and imp.level == 0
        and [(a.name, a.asname) for a in imp.names] == [("OpenAIError", None)]
    ):
        return False
    if not (
        isinstance(deferred, ast.AsyncFunctionDef)
        and deferred.name == "settle_post_provider"
        and not deferred.decorator_list
        and not getattr(deferred, "type_params", ())
        and parameters(deferred) == ["result"]
        and not deferred.args.posonlyargs
        and not deferred.args.defaults
        and not deferred.args.kw_defaults
        and deferred.args.vararg is None
        and deferred.args.kwarg is None
    ):
        return False
    if not all(
        isinstance(n, ast.AnnAssign) and name(n.target, target) and n.simple == 1 and literal(n.value, None)
        for n, target in ((error, "post_provider_error"), (settled, "settled_record"))
    ):
        return False
    if not (
        isinstance(budget, ast.Assign)
        and len(budget.targets) == 1
        and name(budget.targets[0], "compose_budget_seconds")
        and isinstance(budget.value, ast.Call)
        and chain(budget.value.func) == ("budget_anchor", "remaining_seconds")
        and not budget.value.args
        and [k.arg for k in budget.value.keywords] == ["monotonic_now"]
        and exact_call(budget.value.keywords[0].value, ("time", "monotonic"))
    ):
        return False
    if not (
        isinstance(state, ast.Assign)
        and len(state.targets) == 1
        and chain(state.targets[0]) == ("observation", "compose_base_state_id")
        and name(state.value, "compose_base_state_id")
    ):
        return False
    if not (
        isinstance(guard, ast.If)
        and not guard.orelse
        and isinstance(guard.test, ast.Compare)
        and name(guard.test.left, "compose_budget_seconds")
        and len(guard.test.ops) == 1
        and isinstance(guard.test.ops[0], ast.LtE)
        and len(guard.test.comparators) == 1
        and literal(guard.test.comparators[0], 0)
        and len(guard.body) == 3
    ):
        return False
    terminal, progress, raise_ = guard.body
    if not (
        isinstance(terminal, ast.Assign)
        and len(terminal.targets) == 1
        and name(terminal.targets[0], "terminal_status")
        and literal(terminal.value, "timed_out")
    ):
        return False
    if not (isinstance(progress, ast.Expr) and isinstance(progress.value, ast.Await) and isinstance(progress.value.value, ast.Call)):
        return False
    publish = progress.value.value
    if not (
        chain(publish.func) == ("_publish_progress",)
        and len(publish.args) == 1
        and name(publish.args[0], "progress_sink")
        and len(publish.keywords) == 1
        and publish.keywords[0].arg == "event"
    ):
        return False
    event = publish.keywords[0].value
    if not (
        isinstance(event, ast.Call)
        and name(event.func, "convergence_progress_event")
        and not event.args
        and len(event.keywords) == 1
        and event.keywords[0].arg == "budget_exhausted"
        and literal(event.keywords[0].value, "timeout")
    ):
        return False
    return (
        isinstance(raise_, ast.Raise)
        and raise_.cause is None
        and exact_call(
            raise_.exc,
            ("ComposerTurnDeadlineExpired",),
            keywords=(
                ("session_id", ("turn", "session_id")),
                ("operation_id", ("turn", "operation_id")),
                ("remaining_seconds", ("compose_budget_seconds",)),
                ("budget_seconds_at_running", ("budget_anchor", "remaining_at_running_seconds")),
            ),
        )
    )


def module_name(path):
    parts = str(path).removeprefix("src/").removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def origin_inventory(units):
    """Canonical class imports/reexports and simple aliases; unknown transfers retained.

    This is a finite inventory, not an alias/heap closure theorem. Source6 owns
    qualification of container, parameter, reflection, and first-class effects.
    """
    modules = {module_name(u.path): u for u in units}
    class_origin = "elspeth.web.composer.service.ComposerServiceImpl"
    bindings = {module: {} for module in modules}
    if "elspeth.web.composer.service" in bindings:
        bindings["elspeth.web.composer.service"]["ComposerServiceImpl"] = "CLASS"
    changed = True
    while changed:
        changed = False
        for module, unit in modules.items():
            for node in unit.tree.body:
                additions = []
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in bindings:
                    additions = [(a.asname or a.name, bindings[node.module][a.name]) for a in node.names if a.name in bindings[node.module]]
                elif isinstance(node, ast.Import):
                    additions = [
                        (a.asname or a.name.split(".")[0], "MODULE:" + (a.name if a.asname else a.name.split(".")[0])) for a in node.names
                    ]
                elif (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bindings[module]
                ):
                    additions = [(node.targets[0].id, bindings[module][node.value.id])]
                for binding, role in additions:
                    if binding not in bindings[module]:
                        bindings[module][binding] = role
                        changed = True
    records = []
    for module, unit in modules.items():

        def canonical_receiver(root, module_bindings=bindings[module]):
            role = module_bindings.get(root[0]) if root else None
            return (role == "CLASS" and len(root) == 1) or bool(
                role and role.startswith("MODULE:") and ".".join([role[7:], *root[1:]]) == class_origin
            )

        postponed = any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in unit.tree.body
        )
        for node in runtime_walk(unit.tree, postponed=postponed):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and node.attr in {"compose", "composer_service"}
            ):
                root = chain(node.value)
                canonical = canonical_receiver(root)
                records.append(
                    {
                        "path": unit.path,
                        "node_id": id(node),
                        "line": node.lineno,
                        "kind": "CANONICAL_CLASS_METHOD_WRITE" if canonical and node.attr == "compose" else "RECEIVER_FIELD_WRITE",
                        "receiver": root,
                        "member": node.attr,
                        "qualification": "LOCAL_REFUSAL" if canonical and node.attr == "compose" else "UNKNOWN_OWNER_FLOW",
                    }
                )
            elif isinstance(node, ast.Call) and name(node.func, "setattr") and len(node.args) >= 2:
                if isinstance(node.args[1], ast.Constant) and node.args[1].value in {"compose", "composer_service"}:
                    root = chain(node.args[0])
                    canonical = canonical_receiver(root)
                    records.append(
                        {
                            "path": unit.path,
                            "node_id": id(node),
                            "line": node.lineno,
                            "kind": "CANONICAL_CLASS_REFLECTIVE_WRITE" if canonical else "REFLECTIVE_WRITE",
                            "receiver": root,
                            "member": node.args[1].value,
                            "qualification": "LOCAL_REFUSAL" if canonical else "UNKNOWN_OWNER_FLOW",
                        }
                    )
    return records, class_origin


def composer_receiver_local_contracts(units):
    units = list(units)
    index = {str(u.path): u for u in units}
    failures = []
    receipt = {
        "status": "CONDITIONAL_RECEIVER_TRANSPORT_COMMON_UNKNOWN",
        "compose_call": None,
        "app_state_install": None,
        "dto_field": None,
        "dto_read": None,
        "dto_constructor": None,
        "compose_method": None,
        "write_inventory": [],
        "effect_dependencies": [],
        "invocation_kinds": [],
    }
    if len(index) != len(units) or not all(p in index for p in (APP, DTO, TURN, SERVICE)):
        return ["UNKNOWN Composer receiver: duplicate/missing source owner"], receipt
    trees = {p: index[p].tree for p in (APP, DTO, TURN, SERVICE)}

    def require(condition, message):
        if not condition:
            failures.append("Composer receiver: " + message)

    for path, tree in trees.items():
        require(
            any(
                isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
                for n in tree.body
            ),
            path + ": actual postponed definition mode required",
        )
    for path, binding, module, member in (
        (APP, "ComposerServiceImpl", "elspeth.web.composer.service", "ComposerServiceImpl"),
        (APP, "FastAPI", "fastapi", "FastAPI"),
        (DTO, "dataclass", "dataclasses", "dataclass"),
        (DTO, "final", "typing", "final"),
        (DTO, "ComposerService", "elspeth.web.composer.protocol", "ComposerService"),
    ):
        require(imported(trees[path], binding, module, member), path + ": canonical import producer " + binding)
    app = find(trees[APP], "_create_app")
    factory = find(trees[DTO], "composer_app_services")
    dto = find(trees[DTO], "ComposerAppServices")
    service = find(trees[SERVICE], "ComposerServiceImpl")
    turn = find(trees[TURN], "_run_composer_turn")
    require(plain_class(dto, dto=True), "frozen/slotted DTO namespace and dataclass field grammar")
    require(plain_class(service), "ordinary service namespace and definition-time effects")
    if not (
        isinstance(app, ast.FunctionDef)
        and isinstance(factory, ast.FunctionDef)
        and isinstance(dto, ast.ClassDef)
        and isinstance(service, ast.ClassDef)
        and isinstance(turn, ast.AsyncFunctionDef)
    ):
        require(False, "missing/ambiguous concrete definition")
        return failures, receipt
    constructor = find(trees[SERVICE], "ComposerServiceImpl.__init__")
    method = find(trees[SERVICE], "ComposerServiceImpl.compose")
    require(
        isinstance(constructor, ast.FunctionDef)
        and not constructor.decorator_list
        and bool(parameters(constructor))
        and parameters(constructor)[0] == "self",
        "actual ordinary service initializer",
    )
    receipt.update(
        {
            "app_factory": (APP, id(app)),
            "dto_factory": (DTO, id(factory)),
            "dto_class": (DTO, id(dto)),
            "service_class": (SERVICE, id(service)),
            "service_constructor": (SERVICE, id(constructor)) if constructor else None,
            "service_definition_nodes": [(SERVICE, id(n)) for n in body(service)],
        }
    )
    kinds = {}
    for label, path, owners, expected in (
        ("application_factory", APP, [app], "ORDINARY_FUNCTION"),
        ("dto_factory", DTO, [factory], "ORDINARY_FUNCTION"),
        ("service_initializer", SERVICE, [service, constructor], "ORDINARY_FUNCTION"),
        ("compose_method", SERVICE, [service, method], "NATIVE_COROUTINE"),
        ("turn", TURN, [turn], "NATIVE_COROUTINE"),
    ):
        valid, record = invocation_kind(trees[path], owners, expected)
        kinds[label] = valid
        require(valid, "actual selected callable kind " + label + " must be " + expected)
        if record:
            receipt["invocation_kinds"].append(dict(record, label=label, path=path, local_kind_valid=valid))
    for path, binding, definition in (
        (DTO, "ComposerAppServices", dto),
        (DTO, "composer_app_services", factory),
        (SERVICE, "ComposerServiceImpl", service),
        (APP, "_create_app", app),
    ):
        require(owned_binding(trees[path], binding, definition), path + ": unique definition binding " + binding)
    require(
        not app.decorator_list and not getattr(app, "type_params", ()) and not set(parameters(app)) & {"FastAPI", "ComposerServiceImpl"},
        "application factory canonical producer scope",
    )
    app_scope = function_binding_scope(trees[APP], app)
    require(
        app_scope is not None and "app" in app_scope and not app_scope & {"FastAPI", "ComposerServiceImpl"},
        "compiler-derived application producer binding scope",
    )
    for n in executed_body(app):
        if isinstance(n, ast.Name) and n.id in {"FastAPI", "ComposerServiceImpl"} and isinstance(n.ctx, (ast.Store, ast.Del)):
            require(False, "application factory producer local/global overwrite " + n.id)
        if isinstance(n, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name.split(".")[0]) in {"FastAPI", "ComposerServiceImpl"} for a in n.names
        ):
            require(False, "application factory producer import overwrite")
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in {"FastAPI", "ComposerServiceImpl"}:
            require(False, "application factory producer definition overwrite")
        for binding in (
            getattr(n, "name", None) if isinstance(n, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) else None,
            getattr(n, "rest", None) if isinstance(n, ast.MatchMapping) else None,
        ):
            if binding in {"FastAPI", "ComposerServiceImpl"}:
                require(False, "application producer exception/pattern binding " + binding)
    for binding in ("property", "classmethod", "staticmethod"):
        require(builtin_binding(trees[SERVICE], binding), "canonical builtin descriptor producer " + binding)
    require(
        not factory.decorator_list
        and not getattr(factory, "type_params", ())
        and parameters(factory) == ["app"]
        and not factory.args.defaults
        and not factory.args.kw_defaults
        and factory.args.vararg is None
        and factory.args.kwarg is None,
        "DTO factory ordinary signature",
    )
    fb = body(factory)
    require(
        len(fb) == 2
        and isinstance(fb[0], ast.ImportFrom)
        and fb[0].module == "elspeth.web.sessions.routes._helpers"
        and fb[0].level == 0
        and [(a.name, a.asname) for a in fb[0].names] == [("composer_session_lock_registry", None)],
        "DTO factory import/effect prefix",
    )
    ret = fb[-1] if fb else None
    call = ret.value if isinstance(ret, ast.Return) else None
    require(
        isinstance(call, ast.Call)
        and name(call.func, "ComposerAppServices")
        and not call.args
        and [k.arg for k in call.keywords] == list(FIELDS),
        "DTO exact construction/return",
    )
    if isinstance(call, ast.Call) and [k.arg for k in call.keywords] == list(FIELDS):
        expected = [("app", "state", f) for f in FIELDS]
        expected[8] = ("app", "state", "plugin_snapshot_factory", "for_user_id")
        expected[9] = ("app", "state", "composer_progress_registry")
        for k, path in zip(call.keywords[:-1], expected[:-1], strict=False):
            require(chain(k.value) == path, "DTO fresh identity read " + k.arg)
        last = call.keywords[-1].value
        require(
            isinstance(last, ast.Call)
            and name(last.func, "composer_session_lock_registry")
            and len(last.args) == 1
            and name(last.args[0], "app")
            and not last.keywords,
            "DTO lock registry effect",
        )
        receipt["dto_constructor"] = (DTO, id(call))
        receipt["dto_read"] = (DTO, id(call.keywords[1].value))
    if plain_class(dto, dto=True):
        receipt["dto_field"] = (DTO, id(body(dto)[1]))
    installs = [
        n
        for n in executed_body(app)
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and chain(n.targets[0]) == ("app", "state", "composer_service")
    ]
    app_producers = [n for n in executed_body(app) if isinstance(n, ast.Name) and n.id == "app" and isinstance(n.ctx, (ast.Store, ast.Del))]
    app_assigns = [n for n in app.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and name(n.targets[0], "app")]
    require(
        len(app_producers) == 1
        and len(app_assigns) == 1
        and isinstance(app_assigns[0].value, ast.Call)
        and name(app_assigns[0].value.func, "FastAPI"),
        "fresh actual application producer",
    )
    app_events = [event for event in lexical_binding_events(app) if event[0] == "app"]
    require(
        len(app_events) == 1 and len(app_assigns) == 1 and app_events[0][2] is app_assigns[0].targets[0],
        "unique complete app-value lexical binding",
    )
    if len(app_assigns) == 1 and isinstance(app_assigns[0].value, ast.Call):
        receipt["app_constructor"] = (APP, id(app_assigns[0].value))
    require(len(installs) == 1, "unique app-state installation")
    if len(installs) == 1:
        install = installs[0]
        c = install.value
        require(
            isinstance(c, ast.Call)
            and name(c.func, "ComposerServiceImpl")
            and not c.args
            and [k.arg for k in c.keywords]
            == [
                "catalog",
                "settings",
                "sessions_service",
                "session_engine",
                "secret_service",
                "blob_service",
                "runtime_preflight_coordinator",
                "plugin_snapshot_factory",
                "operator_profile_registry",
            ],
            "concrete service constructor and complete keyword transfers",
        )
        if isinstance(c, ast.Call) and len(c.keywords) == 9:
            expected = [
                ("app", "state", "catalog_service"),
                ("settings",),
                ("session_service",),
                ("session_engine",),
                ("app", "state", "scoped_secret_resolver"),
                ("app", "state", "blob_service"),
                ("runtime_preflight_coordinator",),
                ("app", "state", "plugin_snapshot_factory", "for_user_id"),
                ("app", "state", "operator_profile_registry"),
            ]
            require(all(chain(k.value) == p for k, p in zip(c.keywords, expected, strict=False)), "constructor argument producer identity")
        receipt["app_state_install"] = (APP, id(install))
        if isinstance(c, ast.Call):
            receipt["app_service_constructor"] = (APP, id(c))
        active_install = any(n is install for n in app.body)
        returns = [n for n in executed_body(app) if isinstance(n, ast.Return)]
        require(active_install, "installation must be an immediate factory-body statement")
        require(
            len(returns) == 1 and returns[0] is app.body[-1] and name(returns[0].value, "app"),
            "factory returned-app path cannot bypass installation",
        )
        if active_install and len(app_assigns) == 1:
            producer_index = next((i for i, n in enumerate(app.body) if n is app_assigns[0]), None)
            install_index = next(i for i, n in enumerate(app.body) if n is install)
            require(
                producer_index is not None and producer_index < install_index < len(app.body) - 1,
                "fresh app producer dominates direct installation and final return",
            )
            require(not early_abrupt(app.body[:install_index]), "direct abrupt factory prefix before installation")
            if kinds["application_factory"]:
                receipt["app_return_path"] = {
                    "block_owner": (APP, id(app)),
                    "install_statement": (APP, id(install)),
                    "final_return": (APP, id(returns[0])) if len(returns) == 1 else None,
                    "qualification": "CONDITIONAL_SUCCESSFUL_FACTORY_RETURN",
                }
        # No further app/state aliases, stores or reflection are admitted locally.
        for node in executed_body(app):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                require(chain(node) != ("app", "state"), "application state container replacement")
                if chain(node) == ("app", "state", "composer_service"):
                    require(any(node is t for t in install.targets), "additional/deleted app-state service producer")
            if isinstance(node, ast.Assign) and node is not install:
                require(
                    not (
                        chain(node.value) in {("app", "state"), ("app", "state", "composer_service")}
                        and any(isinstance(t, ast.Name) for t in node.targets)
                    ),
                    "application state/service alias requires qualification",
                )
            if isinstance(node, ast.Call) and name(node.func, "setattr") and len(node.args) >= 2:
                require(
                    not (
                        chain(node.args[0]) == ("app", "state")
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == "composer_service"
                    ),
                    "reflective app-state overwrite",
                )
    bindings = [n for n in runtime_walk(turn) if isinstance(n, ast.Assign) and len(n.targets) == 1 and name(n.targets[0], "composer")]
    stores = [n for n in runtime_walk(turn) if isinstance(n, ast.Name) and n.id == "composer" and isinstance(n.ctx, (ast.Store, ast.Del))]
    require(not getattr(turn, "type_params", ()) and "services" in parameters(turn), "turn DTO parameter scope")
    require(
        not any(isinstance(n, ast.Name) and n.id == "services" and isinstance(n.ctx, (ast.Store, ast.Del)) for n in executed_body(turn)),
        "turn retained DTO argument binding",
    )
    turn_events = lexical_binding_events(turn)
    require(
        [(name_, kind) for name_, kind, node in turn_events if name_ == "services"] == [("services", "PARAMETER")],
        "complete retained DTO parameter bindings",
    )
    composer_events = [event for event in turn_events if event[0] == "composer"]
    require(
        len(composer_events) == 1 and len(bindings) == 1 and composer_events[0][2] is bindings[0].targets[0],
        "unique complete selected-owner receiver binding",
    )
    require(
        len(bindings) == 1 and len(stores) == 1 and chain(bindings[0].value) == ("services", "composer_service"),
        "turn single DTO receiver identity binding",
    )
    if len(bindings) == 1:
        receipt["turn_receiver_read"] = (TURN, id(bindings[0].value))
        receipt["turn_receiver_binding"] = (TURN, id(bindings[0]))
    calls = [n for n in runtime_walk(turn) if isinstance(n, ast.Call) and chain(n.func) == ("composer", "compose")]
    require(len(calls) == 1, "one executed compose call")
    if len(calls) == 1:
        receipt["compose_call"] = (TURN, id(calls[0]))
    # The supported actual path is a top-level Try body containing the receiver
    # assignment, followed by a direct inner Try whose first assignment awaits
    # this exact Call. No nested definition/lambda/branch occurrence authorizes it.
    active_path = False
    if len(bindings) == 1 and len(calls) == 1:
        owners = [n for n in turn.body if isinstance(n, ast.Try) and any(x is bindings[0] for x in n.body)]
        if len(owners) == 1:
            owner = owners[0]
            invoke_blocks = []
            for n in owner.body:
                if isinstance(n, ast.Try) and n.body and isinstance(n.body[0], ast.Assign):
                    value = n.body[0].value
                    if isinstance(value, ast.Await) and value.value is calls[0]:
                        invoke_blocks.append(n)
            if len(invoke_blocks) == 1:
                invoke = invoke_blocks[0]
                bi = next(i for i, n in enumerate(owner.body) if n is bindings[0])
                ci = next(i for i, n in enumerate(owner.body) if n is invoke)
                oi = next(i for i, n in enumerate(turn.body) if n is owner)
                active_path = bi < ci and not early_abrupt(owner.body[:ci]) and not early_abrupt(turn.body[:oi])
                active_path = active_path and receiver_transfer_prefix(owner.body[bi + 1 : ci])
                deferred = [n for n in owner.body[bi + 1 : ci] if isinstance(n, ast.AsyncFunctionDef) and n.name == "settle_post_provider"]
                deferred_valid = False
                if len(deferred) == 1:
                    deferred_valid, record = invocation_kind(trees[TURN], [turn, deferred[0]], "NATIVE_COROUTINE")
                    if record:
                        receipt["invocation_kinds"].append(
                            dict(record, label="settlement_callback", path=TURN, local_kind_valid=deferred_valid)
                        )
                require(deferred_valid, "actual selected settlement callback must be NATIVE_COROUTINE")
                active_path = active_path and kinds["turn"] and kinds["compose_method"] and deferred_valid
                receipt["receiver_dominance_path"] = {
                    "turn_owner": (TURN, id(turn)),
                    "outer_try": (TURN, id(owner)),
                    "receiver_statement": (TURN, id(bindings[0])),
                    "inner_try": (TURN, id(invoke)),
                    "await_statement": (TURN, id(invoke.body[0])),
                    "call": (TURN, id(calls[0])),
                    "qualification": "ACTUAL_DIRECT_TRY_BODY_AWAIT_PATH_SAME_RETAINED_BINDING",
                }
                receipt["receiver_transfer_prefix_nodes"] = [(TURN, id(n)) for n in owner.body[bi + 1 : ci]]
    require(active_path, "actual direct awaited path and same-block receiver dominance")
    turn_scope = function_binding_scope(trees[TURN], turn)
    require(
        turn_scope is not None and "composer" in turn_scope and "services" in turn_scope,
        "compiler-derived turn receiver/argument lexical ownership",
    )
    method = find(trees[SERVICE], "ComposerServiceImpl.compose")
    require(
        isinstance(method, ast.AsyncFunctionDef)
        and not method.decorator_list
        and not getattr(method, "type_params", ())
        and bool(parameters(method))
        and parameters(method)[0] == "self",
        "normal async function descriptor",
    )
    if method:
        receipt["compose_method"] = (SERVICE, id(method))
    for fn in body(service):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in runtime_walk(fn):
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    require(chain(n) != ("self", "compose"), "instance method shadowing in " + fn.name)
                if isinstance(n, ast.Call) and name(n.func, "setattr") and len(n.args) >= 2:
                    require(
                        not (name(n.args[0], "self") and isinstance(n.args[1], ast.Constant) and n.args[1].value == "compose"),
                        "instance reflective method shadowing in " + fn.name,
                    )
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    require(
                        not (chain(n.value) == ("self", "__dict__") and isinstance(n.slice, ast.Constant) and n.slice.value == "compose"),
                        "instance dictionary method shadowing in " + fn.name,
                    )
                if isinstance(n, ast.Call) and HARMFUL_REFLECTOR(n):
                    require(
                        not (name(n.args[0], "self") and n.args[1].value == "compose"),
                        "instance low-level descriptor shadowing in " + fn.name,
                    )
    records, origin = origin_inventory(units)
    receipt["write_inventory"] = records
    for r in records:
        if r["qualification"] == "LOCAL_REFUSAL":
            require(False, "canonical method write " + r["path"] + ":" + str(r["line"]))
    dependencies = [
        ("fastapi.FastAPI", "application constructor/state initial ownership and external metaclass"),
        ("starlette.datastructures.State", "state field storage/read identity and no descriptor/replacement effect"),
        ("dataclasses.dataclass", "frozen/slots generated constructor and exact member descriptor"),
        ("typing.final", "canonical decorator returns class and generated namespace effects"),
        ("builtins.property", "canonical service definition decorator and stable builtin supplier"),
        ("builtins.classmethod", "canonical service definition decorator and stable builtin supplier"),
        ("builtins.staticmethod", "canonical service definition decorator and stable builtin supplier"),
        ("builtins.__build_class__", "canonical class builder/default metaclass and stable class origins"),
        ("builtins.object", "default construction and ordinary instance attribute/method lookup"),
        (origin, "constructor actual return object/class origin, callback effects and no instance compose shadow"),
        (origin + ".compose", "function descriptor lookup on exact installed ordinary class; outside method/namespace mutation"),
        (
            "SELECTED_SOURCE_CODE_OBJECTS",
            "actual callable objects/code/decorators match compiled source owner/kind; outside __code__/function replacement cannot change invocation semantics",
        ),
        ("elspeth.web.sessions.composer_app_services.ComposerAppServices", "generated field member descriptor; outside class mutation"),
        (
            "elspeth.web.sessions.composer_app_services.composer_app_services",
            "factory callers carry the same actual app; outside rebinding",
        ),
        (
            "elspeth.web.sessions.routes._helpers.composer_session_lock_registry",
            "later constructor argument effect cannot mutate prior read service/DTO supplier",
        ),
        (
            "elspeth.web.sessions.composer_async_worker",
            "worker _app provenance, job DTO binding and awaiting/external effects before dispatch",
        ),
        (
            "elspeth.web.sessions.composer_turn._run_composer_turn",
            "services argument provenance, earlier awaited/callback effects and retained receiver stability",
        ),
        (
            "elspeth.web.sessions.composer_turn._run_composer_turn.<locals>.settle_post_provider",
            "actual deferred local function body/capture effects; no body occurrence authorizes outer dispatch",
        ),
        (
            "elspeth.web.sessions.routes._helpers._publish_progress",
            "actual awaited timeout-branch call effects and retained receiver/class stability",
        ),
        (
            "elspeth.web.sessions.routes._helpers.convergence_progress_event",
            "actual timeout event producer and retained receiver/class stability",
        ),
        (
            "elspeth.web.sessions.composer_turn.ComposerBudgetAnchor",
            "actual remaining_seconds receiver origin, field/descriptor effects and return producer",
        ),
        ("time.monotonic", "actual budget supplier call with no argument; callback/module stability"),
        ("openai.OpenAIError", "actual definition import before dispatch; module/loader effects"),
        (
            "PROVIDER_TEST_SEAMS",
            "fresh app.state read preserves replacement identity; fixture composers do not certify canonical compose semantics",
        ),
    ]
    receipt["effect_dependencies"] = [
        {
            "identity": a,
            "requirement": b,
            "qualification": "UNKNOWN",
            "dependency_kind": "SOURCE_CALLER_FLOW"
            if a.endswith(("composer_async_worker", "_run_composer_turn"))
            else "LOCAL_DEFERRED_CAPTURE"
            if "<locals>" in a
            else "FIXTURE_SCOPE_ONLY"
            if a == "PROVIDER_TEST_SEAMS"
            else "SUPPLIER_OR_RECEIVER_EFFECT",
        }
        for a, b in dependencies
    ]
    # Postponed annotations do not execute as Python expressions, but dataclass
    # field discovery interprets selected annotation names through module globals.
    # Keep that supplier premise distinct from nominal receiver classification.
    imports = {
        a.asname or a.name: n.module + "." + a.name
        for n in trees[DTO].body
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
        for a in n.names
        if a.name != "*"
    }
    receipt["dto_annotation_interpretation"] = []
    for declaration in body(dto):
        if isinstance(declaration, ast.AnnAssign) and isinstance(declaration.target, ast.Name):
            annotation = declaration.annotation
            while isinstance(annotation, (ast.Subscript, ast.BinOp)):
                annotation = annotation.value if isinstance(annotation, ast.Subscript) else annotation.left
            head = chain(annotation)
            origin = imports.get(head[0]) if head else None
            receipt["dto_annotation_interpretation"].append(
                {
                    "field": declaration.target.id,
                    "annotation_node_id": id(declaration.annotation),
                    "head": head,
                    "canonical_binding": origin,
                    "python_execution_role": "POSTPONED_ANNOTATION_ONLY",
                    "dataclass_role": "FIELD_DISCOVERY_CLASSVAR_INITVAR_KW_ONLY_EXCLUSION_UNKNOWN",
                }
            )
            if origin:
                receipt["effect_dependencies"].append(
                    {
                        "identity": origin,
                        "dependency_kind": "DATACLASS_FIELD_INTERPRETATION",
                        "qualification": "UNKNOWN",
                        "requirement": "Stable annotation head "
                        + declaration.target.id
                        + " cannot resolve to ClassVar/InitVar/KW_ONLY during dataclass field discovery; no nominal receiver proof",
                    }
                )
    receipt["return_relation"] = "DTO_FIELD_IS_CURRENT_APP_STATE_VALUE; TURN_RECEIVER_IS_DTO_FIELD"
    return failures, receipt


def HARMFUL_REFLECTOR(node):
    return (
        len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and chain(node.func) in {("object", "__setattr__"), ("object", "__delattr__"), ("type", "__setattr__"), ("type", "__delattr__")}
    )


def composer_receiver_failures(units):
    failures, receipt = composer_receiver_local_contracts(units)
    return [
        *failures,
        "UNKNOWN Composer receiver: actual constructor/state/descriptor/caller and outside effects require common qualification",
    ], receipt
