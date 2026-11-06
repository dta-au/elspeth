"""Finite Composer receiver transport facts. No admission or outside-effect claim."""

import ast
import inspect
import types

APP = "src/elspeth/web/app.py"
DTO = "src/elspeth/web/sessions/composer_app_services.py"
TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/composer/service.py"
DRIVER = "src/elspeth/web/sessions/continuation_driver.py"
PHASES = "src/elspeth/web/composer/continuation_live_phases.py"
INITIALIZATION = "src/elspeth/web/composer/continuation_composition_initialization.py"
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
    "composer_operation_authority",
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


# Retired V1 receiver-prefix grammar: _run_composer_turn no longer exists.


def module_name(path):
    parts = str(path).removeprefix("src/").removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def origin_inventory(
    units,
    *,
    class_origin="elspeth.web.composer.service.ComposerServiceImpl",
    members=frozenset({"advance_continuation_phase"}),
):
    """Canonical class imports/reexports and simple aliases; unknown transfers retained.

    This is a finite inventory, not an alias/heap closure theorem. Source6 owns
    qualification of container, parameter, reflection, and first-class effects.
    """
    modules = {module_name(u.path): u for u in units}
    bindings = {module: {} for module in modules}
    owner_module, _, owner_name = class_origin.rpartition(".")
    if owner_module in bindings:
        bindings[owner_module][owner_name] = "CLASS"
    fields = {"composer_service"} if class_origin == "elspeth.web.composer.service.ComposerServiceImpl" else set()
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
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in members | fields:
                root = chain(node.value)
                canonical = canonical_receiver(root)
                records.append(
                    {
                        "path": unit.path,
                        "node_id": id(node),
                        "line": node.lineno,
                        "kind": "CANONICAL_CLASS_METHOD_WRITE" if canonical and node.attr in members else "RECEIVER_FIELD_WRITE",
                        "receiver": root,
                        "member": node.attr,
                        "qualification": "LOCAL_REFUSAL" if canonical and node.attr in members else "UNKNOWN_OWNER_FLOW",
                    }
                )
            elif isinstance(node, ast.Call) and name(node.func, "setattr") and len(node.args) >= 2:
                if isinstance(node.args[1], ast.Constant) and node.args[1].value in members | fields:
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


# Retired V1 TITLE/compose branch grammars: dispatch now belongs to the continuation driver.


def composer_receiver_local_contracts(units):
    # The V1 turn/compose/recovery APIs were deleted; the live driver owns this transfer.
    from tests.helpers.web_reserve_provider_mint_contract import provider_driver_constructor_contract

    units = list(units)
    index = {str(u.path): u for u in units}
    failures = []
    receipt = {
        "status": "CONDITIONAL_CONTINUATION_RECEIVER_TRANSPORT_COMMON_UNKNOWN",
        "app_state_install": None,
        "dto_field": None,
        "dto_read": None,
        "dto_constructor": None,
        "write_inventory": [],
        "effect_dependencies": [],
        "invocation_kinds": [],
    }
    if len(index) != len(units) or not all(p in index for p in (APP, DTO, DRIVER, SERVICE, PHASES, INITIALIZATION)):
        return ["UNKNOWN Composer receiver: duplicate/missing source owner"], receipt
    trees = {p: index[p].tree for p in (APP, DTO, DRIVER, SERVICE, PHASES, INITIALIZATION)}

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
    turn = find(trees[DRIVER], "run_continuation_segment")
    driver = find(trees[DRIVER], "_SegmentDriver")
    driver_init = find(trees[DRIVER], "_SegmentDriver.__init__")
    driver_advance = find(trees[DRIVER], "_SegmentDriver.advance")
    phases = find(trees[PHASES], "ContinuationLivePhases")
    phase_advance = find(trees[PHASES], "ContinuationLivePhases.advance")
    initialization = find(trees[INITIALIZATION], "initialize_continuation_composition")
    require(plain_class(dto, dto=True), "frozen/slotted DTO namespace and dataclass field grammar")
    require(plain_class(service), "ordinary service namespace and definition-time effects")
    if not (
        isinstance(app, ast.FunctionDef)
        and isinstance(factory, ast.FunctionDef)
        and isinstance(dto, ast.ClassDef)
        and isinstance(service, ast.ClassDef)
        and isinstance(turn, ast.AsyncFunctionDef)
        and isinstance(driver, ast.ClassDef)
        and isinstance(driver_init, ast.FunctionDef)
        and isinstance(driver_advance, ast.AsyncFunctionDef)
        and isinstance(phases, ast.ClassDef)
        and isinstance(phase_advance, ast.AsyncFunctionDef)
        and isinstance(initialization, ast.AsyncFunctionDef)
    ):
        require(False, "missing/ambiguous concrete definition")
        return failures, receipt
    constructor = find(trees[SERVICE], "ComposerServiceImpl.__init__")
    method = find(trees[SERVICE], "ComposerServiceImpl.advance_continuation_phase")
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
        ("phase_method", SERVICE, [service, method], "NATIVE_COROUTINE"),
        ("phase_owner_method", PHASES, [phases, phase_advance], "NATIVE_COROUTINE"),
        ("driver_advance", DRIVER, [driver, driver_advance], "NATIVE_COROUTINE"),
        ("driver_entry", DRIVER, [turn], "NATIVE_COROUTINE"),
        ("initialization", INITIALIZATION, [initialization], "NATIVE_COROUTINE"),
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
        (DRIVER, "_SegmentDriver", driver),
        (DRIVER, "run_continuation_segment", turn),
        (PHASES, "ContinuationLivePhases", phases),
        (INITIALIZATION, "initialize_continuation_composition", initialization),
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
        expected[11] = ("app", "state", "composer_async_operation_authority")
        for index, (k, path) in enumerate(zip(call.keywords, expected, strict=True)):
            if index == 10:
                continue
            require(chain(k.value) == path, "DTO fresh identity read " + k.arg)
        last = call.keywords[10].value
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

    driver_failures, driver_receipt = provider_driver_constructor_contract(units)
    require(not driver_failures and driver_receipt is not None, "exact current driver work/provider/phase construction")
    if driver_receipt is None:
        return failures, receipt
    for path, binding, module, member in (
        (DRIVER, "ComposerServiceImpl", "elspeth.web.composer.service", "ComposerServiceImpl"),
        (DRIVER, "ContinuationLivePhases", "elspeth.web.composer.continuation_live_phases", "ContinuationLivePhases"),
        (
            DRIVER,
            "initialize_continuation_composition",
            "elspeth.web.composer.continuation_composition_initialization",
            "initialize_continuation_composition",
        ),
        (SERVICE, "AuditIntegrityError", "elspeth.contracts.errors", "AuditIntegrityError"),
        (PHASES, "dataclass", "dataclasses", "dataclass"),
        (PHASES, "field", "dataclasses", "field"),
        (PHASES, "ComposerServiceImpl", "elspeth.web.composer.service", "ComposerServiceImpl"),
        (PHASES, "ProviderInvocationOwner", "elspeth.web.composer.provider_quota", "ProviderInvocationOwner"),
        (INITIALIZATION, "ComposerServiceImpl", "elspeth.web.composer.service", "ComposerServiceImpl"),
        (
            INITIALIZATION,
            "ComposerAsyncOperationAuthority",
            "elspeth.web.coordination.composer_operation_authority",
            "ComposerAsyncOperationAuthority",
        ),
        (INITIALIZATION, "StoredContinuationSnapshot", "elspeth.web.sessions.continuation", "StoredContinuationSnapshot"),
        (INITIALIZATION, "RequiredWorkBinding", "elspeth.web.required_work", "RequiredWorkBinding"),
        (INITIALIZATION, "RequiredAuthorityKind", "elspeth.web.required_work", "RequiredAuthorityKind"),
        (INITIALIZATION, "ProviderInvocationOwner", "elspeth.web.composer.provider_quota", "ProviderInvocationOwner"),
        (INITIALIZATION, "ComposerOperationRunning", "elspeth.web.sessions.composer_operations", "ComposerOperationRunning"),
        (INITIALIZATION, "_sessions", "elspeth.web.composer.continuation_initialization", "_sessions"),
    ):
        require(imported(trees[path], binding, module, member), path + ": current canonical receiver supplier " + binding)
    for path in (DRIVER, PHASES, INITIALIZATION):
        require(builtin_binding(trees[path], "type"), path + ": canonical nominal type predicate")
    require(plain_class(driver), "ordinary driver namespace and stable definition-time effects")
    service_stores = [
        node
        for node in driver_init.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and chain(node.targets[0]) == ("self", "service")
        and chain(node.value) == ("services", "composer_service")
    ]
    require(len(service_stores) == 1, "single DTO-to-driver service identity binding")
    protected_driver_fields = {
        "worker",
        "services",
        "running",
        "lease",
        "service",
        "authority",
        "work",
        "provider",
        "phases",
        "sql_deadline",
    }
    constructor_targets = {
        id(node)
        for statement in driver_init.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and name(node.value, "self")
        and node.attr in protected_driver_fields
    }
    for declaration in body(driver):
        if not isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in runtime_walk(declaration):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                require(
                    not (name(node.value, "self") and node.attr in protected_driver_fields) or id(node) in constructor_targets,
                    "retained driver service/work/phase owner rebound in " + declaration.name,
                )
            if (
                isinstance(node, ast.Call)
                and chain(node.func) in {("setattr",), ("delattr",), ("object", "__setattr__"), ("object", "__delattr__")}
                and len(node.args) >= 2
                and name(node.args[0], "self")
            ):
                require(
                    isinstance(node.args[1], ast.Constant) and node.args[1].value not in protected_driver_fields,
                    "retained driver owner changed by reflection in " + declaration.name,
                )
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                require(
                    not (
                        chain(node.value) == ("self", "__dict__")
                        and (not isinstance(node.slice, ast.Constant) or node.slice.value in protected_driver_fields)
                    ),
                    "retained driver owner changed through instance dictionary in " + declaration.name,
                )
    entry_calls = [node for node in executed_body(turn) if isinstance(node, ast.Call) and chain(node.func) == ("_SegmentDriver",)]
    require(len(entry_calls) == 1, "one actual current segment driver construction")
    if len(entry_calls) == 1:
        entry_call = entry_calls[0]
        require(
            [chain(argument) for argument in entry_call.args] == [("worker",), ("services",), ("running",), ("lease",)]
            and not entry_call.keywords,
            "segment entry retains exact worker/DTO/running/lease arguments",
        )
        service_arguments = [node for node in runtime_walk(turn) if isinstance(node, ast.Name) and node.id == "services"]
        require(
            all(isinstance(node.ctx, ast.Load) and any(node is argument for argument in entry_call.args) for node in service_arguments),
            "entry DTO argument rebound or escaped",
        )
        receipt["driver_constructor_call"] = (DRIVER, id(entry_call))
    run_returns = [
        node
        for node in executed_body(turn)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Await)
        and isinstance(node.value.value, ast.Call)
        and ast.unparse(node.value.value) == "driver.run(adoption_failure)"
    ]
    require(len(run_returns) == 1, "actual driver native await/return path")
    require(
        [(bound, kind) for bound, kind, _node in lexical_binding_events(turn) if bound == "services"] == [("services", "PARAMETER")],
        "complete native entry DTO argument scope",
    )

    expected_method = ast.parse(
        '''async def advance_continuation_phase(
    self, *, owner: ContinuationLivePhases, authority: ComposerAsyncOperationAuthority,
    selected: SelectedContinuationState, work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
    phase_read: _CurrentPhaseRead | None = None,
) -> None:
    """Enter the retained phase owner without re-entering legacy PRIMARY."""
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    if (
        type(owner) is not ContinuationLivePhases or owner.service is not self
        or owner.authority is not authority or owner.work is not work or owner.provider_owner is not provider_owner
    ):
        raise AuditIntegrityError("continuation service dispatch changed its actual live owners")
    if phase_read is None:
        await owner.advance(selected)
    else:
        await owner.advance(selected, phase_read=phase_read)
'''
    ).body[0]
    require(
        isinstance(method, ast.AsyncFunctionDef)
        and ast.dump(method, include_attributes=False) == ast.dump(expected_method, include_attributes=False),
        "exact nominal live phase service dispatch and owner forwarding",
    )
    phase_dispatch = [
        node
        for node in runtime_walk(driver_advance)
        if isinstance(node, ast.Call) and chain(node.func) == ("self", "service", "advance_continuation_phase")
    ]
    require(len(phase_dispatch) == 1, "one current driver-to-service phase dispatch")
    expected_dispatch = ast.parse(
        "self.service.advance_continuation_phase(owner=self.phases, authority=self.authority, selected=selected, work=self.work, provider_owner=self.provider, phase_read=phase_read)",
        mode="eval",
    ).body
    if len(phase_dispatch) == 1:
        require(
            ast.dump(phase_dispatch[0], include_attributes=False) == ast.dump(expected_dispatch, include_attributes=False),
            "driver dispatch keeps exact selected service/work/provider/phase owners",
        )
    expected_initialization = ast.parse(
        "initialize_continuation_composition(service=self.service, authority=self.authority, selected=stored, required_work=self.work, provider_owner=self.provider, progress=self.phases.progress)",
        mode="eval",
    ).body
    initialization_calls = [
        node
        for node in runtime_walk(driver_advance)
        if isinstance(node, ast.Call) and chain(node.func) == ("initialize_continuation_composition",)
    ]
    require(
        len(initialization_calls) == 2
        and all(
            ast.dump(node, include_attributes=False) == ast.dump(expected_initialization, include_attributes=False)
            for node in initialization_calls
        ),
        "both current initialization paths keep the retained service/work/provider identity",
    )
    bind_calls = [
        node
        for node in runtime_walk(driver_advance)
        if isinstance(node, ast.Call) and chain(node.func) == ("self", "phases", "bind_initial")
    ]
    require(
        len(bind_calls) == 2 and all(ast.unparse(node) == "self.phases.bind_initial(runtime)" for node in bind_calls),
        "actual initialization runtime is handed to its retained phase owner",
    )
    expected_bind = ast.parse(
        """def bind_initial(self, runtime: RestoredContinuationRuntime) -> None:
    if (
        self.runtime is not None or type(runtime) is not RestoredContinuationRuntime
        or runtime.materialization.service is not self.service
        or runtime.materialization.authority is not self.authority
        or runtime.bindings.required_work is not self.work
        or runtime.bindings.provider_owner is not self.provider_owner
        or runtime.bindings.progress is not self.progress
    ):
        raise AuditIntegrityError("live phase initialization changed its actual runtime owners")
    self.runtime = runtime
"""
    ).body[0]
    phase_bind = find(trees[PHASES], "ContinuationLivePhases.bind_initial")
    require(
        phase_bind is not None and ast.dump(phase_bind, include_attributes=False) == ast.dump(expected_bind, include_attributes=False),
        "actual initial runtime source/work/provider owner guard",
    )
    expected_phase_advance = ast.parse(
        """async def advance(self, selected: SelectedContinuationState, *, phase_read: _CurrentPhaseRead | None = None) -> None:
    if phase_read is not None:
        self._require_phase_read(selected, phase_read)
    if self.terminal_completion is not None:
        await self._persist_batch(selected)
        return
    top = selected.stack.frames[-1]
    if type(top) is ComposeFrame and top.phase == "response_retained":
        await self._initialize_batch(selected, phase_read=phase_read)
        return
    if phase_read is not None:
        raise AuditIntegrityError("live phase handoff belongs only to its retained PRIMARY response")
    if type(top) is ToolBatchFrame and top.phase == "tool_ready":
        runtime, _inventory = await self.current(selected)
        if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "call_ready":
            await self._execute_one(selected)
            return
        if runtime.graph.batch is not None and runtime.graph.batch.batch.phase == "dispatch_ready":
            await self._persist_batch(selected)
            return
    raise AuditIntegrityError("selected phase has no matching live tool action")
"""
    ).body[0]
    require(
        ast.dump(phase_advance, include_attributes=False) == ast.dump(expected_phase_advance, include_attributes=False),
        "closed selected live phase native dispatch grammar",
    )
    require(
        not phases.bases
        and not phases.keywords
        and not phases.type_params
        and [ast.unparse(node) for node in phases.decorator_list] == ["dataclass(slots=True)"]
        and all(
            isinstance(node, ast.AnnAssign)
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name not in LOOKUP | {"__init__"})
            or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str)
            for node in phases.body
        ),
        "plain generated phase namespace and normal method lookup",
    )
    expected_phase_fields = (
        ast.parse(
            """class ContinuationLivePhases:
    service: ComposerServiceImpl
    authority: ComposerAsyncOperationAuthority
    work: RequiredWorkBinding
    provider_owner: ProviderInvocationOwner
    lease: SessionOperationLease
    progress: ComposerProgressSink | None = None
    runtime: RestoredContinuationRuntime | None = None
    primary: PrimaryProviderResult | RetainedPrimaryPhaseData | None = None
    p4: ContinuationLiveP4 | None = None
    latest_snapshot: StoredContinuationSnapshot | None = None
    cancellation_requested: asyncio.Event = field(default_factory=asyncio.Event)
    business_stage: ContinuationStage[None] | None = None
    failure_custody: ContinuationToolFailureCustody | None = None
    terminal_completion: TerminalServiceCompletion | None = None
    finished_batch: tuple[_DispatchOutcome, _PersistOutcome | ColdPostBatchPersistData, CompositionState] | None = None
    action_clock: ContinuationActionClock | None = None
"""
        )
        .body[0]
        .body
    )
    require(
        [ast.dump(node, include_attributes=False) for node in phases.body if isinstance(node, ast.AnnAssign)]
        == [ast.dump(node, include_attributes=False) for node in expected_phase_fields],
        "exact generated live phase field/descriptor layout",
    )
    expected_initial_guard = ast.parse(
        """if (
    type(service) is not ComposerServiceImpl
    or type(authority) is not ComposerAsyncOperationAuthority
    or type(selected) is not StoredContinuationSnapshot
    or type(required_work) is not RequiredWorkBinding
    or type(provider_owner) is not ProviderInvocationOwner
    or provider_owner.required_work is not required_work
    or provider_owner.service is not _sessions(service)
    or _sessions(service)._engine is not authority._engine
):
    raise AuditIntegrityError("first Compose initialization requires actual service/SQL/provider-work owners")
running = required_work.running
custody = required_work.coordinator.authority
if (
    type(running) is not ComposerOperationRunning
    or custody.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
    or custody.durable_operation_id != running.claim.operation_id
    or custody.claim_attempt != running.claim.attempt
):
    raise AuditIntegrityError("first Compose initialization changed its current required-work claim")
required_work.validate_context(running.session_operation_context)
"""
    ).body
    require(
        [ast.dump(node, include_attributes=False) for node in body(initialization)[:5]]
        == [ast.dump(node, include_attributes=False) for node in expected_initial_guard],
        "actual initialization nominal service/SQL/claim/provider-work owner guards",
    )
    protected_phase_fields = {"service", "authority", "work", "provider_owner", "lease"}
    for node in ast.walk(phases):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            require(
                not (name(node.value, "self") and node.attr in protected_phase_fields),
                "retained live phase service/work/provider/context rebound",
            )
        if (
            isinstance(node, ast.Call)
            and chain(node.func) in {("setattr",), ("delattr",), ("object", "__setattr__"), ("object", "__delattr__")}
            and len(node.args) >= 2
            and name(node.args[0], "self")
        ):
            require(
                isinstance(node.args[1], ast.Constant) and node.args[1].value not in protected_phase_fields,
                "retained live phase owner changed by reflection",
            )
    for source, member_names in ((service, {"advance_continuation_phase"}), (phases, {"advance", "bind_initial"})):
        for declaration in body(source):
            if not isinstance(declaration, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in runtime_walk(declaration):
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    require(
                        not (name(node.value, "self") and node.attr in member_names),
                        "instance callable shadowing in " + declaration.name,
                    )
                if isinstance(node, ast.Call) and name(node.func, "setattr") and len(node.args) >= 2 and name(node.args[0], "self"):
                    require(
                        isinstance(node.args[1], ast.Constant) and node.args[1].value not in member_names,
                        "instance reflective callable shadowing in " + declaration.name,
                    )
                if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    require(
                        not (
                            chain(node.value) == ("self", "__dict__")
                            and (not isinstance(node.slice, ast.Constant) or node.slice.value in member_names)
                        ),
                        "instance dictionary callable shadowing in " + declaration.name,
                    )
                if isinstance(node, ast.Call) and HARMFUL_REFLECTOR(node):
                    require(
                        not (name(node.args[0], "self") and node.args[1].value in member_names),
                        "instance low-level descriptor shadowing in " + declaration.name,
                    )
    phase_owner_calls = (
        [node for node in ast.walk(method) if isinstance(node, ast.Call) and chain(node.func) == ("owner", "advance")]
        if method is not None
        else []
    )
    require(len(phase_owner_calls) == 2, "both checked service branches await the actual phase owner")
    records, origin = origin_inventory(units)
    phase_records, _phase_origin = origin_inventory(
        units,
        class_origin="elspeth.web.composer.continuation_live_phases.ContinuationLivePhases",
        members=frozenset({"advance", "bind_initial"}),
    )
    records.extend(phase_records)
    receipt["write_inventory"] = records
    for record in records:
        if record["qualification"] == "LOCAL_REFUSAL":
            require(False, "canonical method write " + record["path"] + ":" + str(record["line"]))
    receipt.update(
        {
            "driver_class": (DRIVER, id(driver)),
            "driver_constructor": (DRIVER, id(driver_init)),
            "driver_entry": (DRIVER, id(turn)),
            "driver_advance": (DRIVER, id(driver_advance)),
            "driver_service_read": (DRIVER, id(service_stores[0].value)) if len(service_stores) == 1 else None,
            "driver_service_binding": (DRIVER, id(service_stores[0])) if len(service_stores) == 1 else None,
            "phase_class": (PHASES, id(phases)),
            "phase_method": (SERVICE, id(method)) if method is not None else None,
            "phase_owner_method": (PHASES, id(phase_advance)),
            "phase_bind_method": (PHASES, id(phase_bind)) if phase_bind is not None else None,
            "phase_dispatch_call": (DRIVER, id(phase_dispatch[0])) if len(phase_dispatch) == 1 else None,
            "phase_owner_calls": frozenset((SERVICE, id(node)) for node in phase_owner_calls),
            "initialization_calls": frozenset((DRIVER, id(node)) for node in initialization_calls),
            "initialization_binding_calls": frozenset((DRIVER, id(node)) for node in bind_calls),
            "driver_constructor_stores": driver_receipt["owner_constructor_stores"] | driver_receipt["binding_constructor_stores"],
            "current_binding_reads": frozenset(
                (DRIVER, id(node))
                for call in (*phase_dispatch, *initialization_calls)
                for node in ast.walk(call)
                if isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and name(node.value, "self")
                and node.attr in {"service", "authority", "work", "provider", "phases"}
            ),
        }
    )
    dependencies = [
        ("fastapi.FastAPI", "application constructor/state and external metaclass"),
        ("starlette.datastructures.State", "state storage/read identity and no external replacement"),
        ("dataclasses.dataclass", "generated DTO and phase constructor/member descriptors"),
        (origin, "installed ordinary service instance and external mutation closure"),
        (origin + ".advance_continuation_phase", "normal descriptor and retained receiver/class stability"),
        ("elspeth.web.sessions.continuation_driver.run_continuation_segment", "actual native entry/custody/close ownership and callers"),
        ("elspeth.web.sessions.continuation_driver._SegmentDriver.advance", "current selected action before dispatch and external effects"),
        (
            "elspeth.web.composer.continuation_composition_initialization.initialize_continuation_composition",
            "current guarded runtime construction, retained service/work/provider and original state",
        ),
        (
            "elspeth.web.composer.continuation_live_phases.ContinuationLivePhases",
            "actual generated phase fields and outside class/instance mutation closure",
        ),
        (
            "elspeth.web.composer.continuation_live_phases.ContinuationLivePhases._require_phase_read",
            "exact selected/read/runtime/lease provenance before live phase dispatch",
        ),
        ("elspeth.web.sessions.composer_app_services.ComposerAppServices", "generated immutable fields and outside class mutation"),
        ("elspeth.web.sessions.composer_app_services.composer_app_services", "factory callers carry the actual retained application"),
        (
            "elspeth.web.sessions.routes._helpers.composer_session_lock_registry",
            "later constructor effect cannot replace prior read service",
        ),
        ("elspeth.web.sessions.composer_async_worker", "actual app/DTO argument and native segment-entry transport"),
    ]
    receipt["effect_dependencies"] = [
        {"identity": identity, "requirement": requirement, "qualification": "UNKNOWN", "dependency_kind": "SUPPLIER_OR_RECEIVER_EFFECT"}
        for identity, requirement in dependencies
    ]
    imports = {
        alias.asname or alias.name: statement.module + "." + alias.name
        for statement in trees[DTO].body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module
        for alias in statement.names
        if alias.name != "*"
    }
    receipt["dto_annotation_interpretation"] = []
    for declaration in body(dto):
        if not isinstance(declaration, ast.AnnAssign) or not isinstance(declaration.target, ast.Name):
            continue
        annotation = declaration.annotation
        while isinstance(annotation, (ast.Subscript, ast.BinOp)):
            annotation = annotation.value if isinstance(annotation, ast.Subscript) else annotation.left
        head = chain(annotation)
        supplier = imports.get(head[0]) if head else None
        receipt["dto_annotation_interpretation"].append(
            {
                "field": declaration.target.id,
                "annotation_node_id": id(declaration.annotation),
                "head": head,
                "canonical_binding": supplier,
                "python_execution_role": "POSTPONED_ANNOTATION_ONLY",
                "dataclass_role": "FIELD_DISCOVERY_CLASSVAR_INITVAR_KW_ONLY_EXCLUSION_UNKNOWN",
            }
        )
        if supplier:
            receipt["effect_dependencies"].append(
                {
                    "identity": supplier,
                    "dependency_kind": "DATACLASS_FIELD_INTERPRETATION",
                    "qualification": "UNKNOWN",
                    "requirement": "Stable annotation head "
                    + declaration.target.id
                    + " cannot become a ClassVar/InitVar/KW_ONLY discriminator",
                }
            )
    receipt["return_relation"] = "DTO_FIELD_IS_CURRENT_APP_STATE_VALUE; DRIVER_SERVICE_IS_DTO_FIELD; PHASE_OWNER_IS_DRIVER_CONSTRUCTION"
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
