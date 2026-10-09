"""Finite AST contract for the two retained required-executor recovery cells.

This helper proves a source transition and returns its exact AST identities.
Physical watchdog/event/member suppliers remain common-certificate premises.
"""

import ast

WORKER = "elspeth.web.async_workers"
APP = "elspeth.web.app"
OWNER = "elspeth.web.application_finalizers.ApplicationFinalizerOwner"
RECOVERY = "elspeth.web.process_recovery.ProcessRecovery"
CELLS = {"_APPLICATION_FINALIZER_OWNER", "_INSTANCE_DRAINING"}
PROTECTED_MODULES = {WORKER, APP, "elspeth.web.application_finalizers", "elspeth.web.process_recovery", "threading"}
PROTECTED_BINDINGS = {
    WORKER + ".configure_required_executor_recovery",
    OWNER,
    RECOVERY,
    "threading.Event",
    "threading.Lock",
    APP + ".configure_required_executor_recovery",
    APP + ".ApplicationFinalizerOwner",
    APP + ".ProcessRecovery",
    APP + ".threading",
    "elspeth.web.application_finalizers.threading",
    "elspeth.web.process_recovery.threading",
}


CONFIGURE = """
def configure_required_executor_recovery(
    *, drain_seconds: float, instance_draining: threading.Event,
    generation_unavailable: threading.Event,
    recovery_callback: Callable[[RequiredGenerationDrainExpired], None],
    application_finalizer_owner: ApplicationFinalizerOwner | None = None,
) -> None:
    global _RECOVERY_CALLBACK, _INSTANCE_DRAINING, _GENERATION_UNAVAILABLE, _CAPTURED_DRAIN_SECONDS, _SHARED_SHUTDOWN_STARTED, _APPLICATION_FINALIZER_OWNER
    if isinstance(drain_seconds, bool) or not math.isfinite(drain_seconds) or drain_seconds <= 0:
        raise ValueError("Required generation drain must be positive")
    if _GENERATION_CUSTODIAN is not None and (
        _GENERATION_CUSTODIAN.escalated
        or (_GENERATION_CUSTODIAN.state == "quarantined" and not _GENERATION_CUSTODIAN.recovery_finished.is_set())
    ):
        raise RequiredGenerationUnavailable("Existing generation recovery cannot be replaced")
    if _SHARED_SHUTDOWN_STARTED and _SHARED_EXECUTOR is not None:
        raise RequiredGenerationUnavailable("Previous shared generation has not joined")
    _SHARED_SHUTDOWN_STARTED = False
    _APPLICATION_FINALIZER_OWNER = application_finalizer_owner
    _RECOVERY_CALLBACK = recovery_callback
    _INSTANCE_DRAINING = instance_draining
    _GENERATION_UNAVAILABLE = generation_unavailable
    _CAPTURED_DRAIN_SECONDS = drain_seconds
"""

OWNER_CONSTRUCTOR = """
def __init__(self) -> None:
    self._lock = threading.Lock()
    self._capabilities: dict[ApplicationFinalizerKind, ApplicationFinalizerCapability] = {}
    self._sealed = False
"""

RECOVERY_CONSTRUCTOR = """
def __init__(self, *, watchdog: ProcessWatchdogControl, instance_draining: threading.Event) -> None:
    if not isinstance(watchdog, ProcessWatchdogControl):
        raise TypeError("Process recovery requires an owned watchdog")
    self.watchdog = watchdog
    self.instance_draining = instance_draining
    self._shutdown_started = False
    self._escalation_task: asyncio.Task[None] | None = None
    self._monitor_task: asyncio.Task[None] | None = None
    self._observations: list[RequiredGenerationDrainExpired] = []
"""

RECOVERY_CALLBACK = """
def required_generation_expired(self, observation: RequiredGenerationDrainExpired) -> None:
    self._observations.append(observation)
    self._begin(RecoveryReason.REQUIRED_GENERATION_UNRESOLVED, signal_target=True)
"""

RECOVERY_BEGIN = """
def _begin(self, reason: RecoveryReason, *, signal_target: bool) -> None:
    self.instance_draining.set()
    if not self._shutdown_started:
        self._shutdown_started = True
        self._escalation_task = asyncio.create_task(
            self._escalate(reason, signal_target=signal_target), name="process-recovery-escalation"
        )
"""

CALL = """configure_required_executor_recovery(
    drain_seconds=resolved_settings.composer_async_drain_seconds,
    instance_draining=instance_draining,
    generation_unavailable=generation_unavailable,
    recovery_callback=process_recovery.required_generation_expired,
    application_finalizer_owner=finalizer_owner,
)"""

APP_PREFIX = """
configure_process_logging = settings is None
resolved_settings = settings_from_env() if settings is None else settings
instance_draining = threading.Event()
watchdog_factory = create_process_watchdog if process_watchdog_factory is None else process_watchdog_factory
watchdog = watchdog_factory(instance_draining)
if not isinstance(watchdog, ProcessWatchdogControl):
    raise TypeError("Factory requires an owned process watchdog")
process_recovery = ProcessRecovery(watchdog=watchdog, instance_draining=instance_draining)
generation_unavailable = threading.Event()
finalizer_owner = ApplicationFinalizerOwner()
"""

# Executable source contract, not a source-file fingerprint. Every retained
# producer, callback registration, successful return and failure path matters.
APP_CONTRACT = """
def create_app(settings: WebSettings | None = None, *, process_watchdog_factory: ProcessWatchdogFactory | None = None) -> FastAPI:
    global _FAILED_BOOTSTRAP_RECOVERY
    configure_process_logging = settings is None
    resolved_settings = settings_from_env() if settings is None else settings
    instance_draining = threading.Event()
    watchdog_factory = create_process_watchdog if process_watchdog_factory is None else process_watchdog_factory
    watchdog = watchdog_factory(instance_draining)
    if not isinstance(watchdog, ProcessWatchdogControl):
        raise TypeError("Factory requires an owned process watchdog")
    process_recovery = ProcessRecovery(watchdog=watchdog, instance_draining=instance_draining)
    generation_unavailable = threading.Event()
    finalizer_owner = ApplicationFinalizerOwner()
    construction_side_effects_started = False
    def mark_construction_side_effects() -> None:
        nonlocal construction_side_effects_started
        construction_side_effects_started = True
    session_engine_finalizer: weakref.finalize[..., FastAPI] | None = None
    auth_audit_finalizer: weakref.finalize[..., FastAPI] | None = None
    telemetry_owner: OperatorTelemetryCleanupOwner | None = None
    def register_auth_audit_finalizer(finalizer: weakref.finalize[..., FastAPI]) -> None:
        nonlocal auth_audit_finalizer
        auth_audit_finalizer = finalizer
    def register_session_engine_finalizer(finalizer: weakref.finalize[..., FastAPI]) -> None:
        nonlocal session_engine_finalizer
        if session_engine_finalizer is not None:
            raise RuntimeError("session engine finalizer registered more than once")
        session_engine_finalizer = finalizer
    try:
        watchdog.assert_watching()
        configure_required_executor_recovery(
            drain_seconds=resolved_settings.composer_async_drain_seconds,
            instance_draining=instance_draining,
            generation_unavailable=generation_unavailable,
            recovery_callback=process_recovery.required_generation_expired,
            application_finalizer_owner=finalizer_owner,
        )
        telemetry_owner = OperatorTelemetryCleanupOwner.create_for_application()
        return _create_app(
            resolved_settings, register_session_engine_finalizer, register_auth_audit_finalizer,
            process_recovery=process_recovery, watchdog=watchdog,
            instance_draining=instance_draining, generation_unavailable=generation_unavailable,
            mark_construction_side_effects=mark_construction_side_effects,
            configure_process_logging=configure_process_logging,
            finalizer_owner=finalizer_owner, telemetry_owner=telemetry_owner,
        )
    except BaseException as exc:
        failures: list[BaseException] = [exc]
        try:
            if construction_side_effects_started:
                _FAILED_BOOTSTRAP_RECOVERY = process_recovery
                watchdog.abort_bootstrap(RecoveryReason.FAILED_STARTUP)
            else:
                watchdog.complete_bootstrap(BootstrapCompletionWitness(watchdog.target))
        except BaseException as supervision_failure:
            failures.append(supervision_failure)
        try:
            if telemetry_owner is not None:
                telemetry_owner.shutdown_sync()
        except BaseException as telemetry_cleanup_failure:
            failures.append(telemetry_cleanup_failure)
        try:
            if auth_audit_finalizer is not None:
                _run_auth_audit_finalizer(auth_audit_finalizer)
        except BaseException as auth_cleanup_failure:
            failures.append(auth_cleanup_failure)
        try:
            if session_engine_finalizer is not None:
                _run_session_engine_finalizer(session_engine_finalizer)
        except BaseException as engine_cleanup_failure:
            failures.append(engine_cleanup_failure)
        if len(failures) != 1:
            raise BaseExceptionGroup("Application construction and owned cleanup failed", failures) from None
        raise
"""


def _scope_nodes(node):
    """Lexical owner nodes; nested bodies and comprehension targets differ."""
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        for header in [
            *node.args.defaults,
            *(v for v in node.args.kw_defaults if v is not None),
            *(node.decorator_list if not isinstance(node, ast.Lambda) else []),
        ]:
            yield from _scope_nodes(header)
        return
    if isinstance(node, ast.ClassDef):
        for header in [*node.bases, *node.decorator_list, *(k.value for k in node.keywords)]:
            yield from _scope_nodes(header)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # Iterators and walrus values can affect the enclosing frame; iteration
        # targets are bound by the separate comprehension frame.
        for generator in node.generators:
            yield from _scope_nodes(generator.iter)
            for condition in generator.ifs:
                yield from _scope_nodes(condition)
        for value in [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]:
            yield from _scope_nodes(value)
        return
    for child in ast.iter_child_nodes(node):
        yield from _scope_nodes(child)


def _binder_names(node):
    """All Python binding spellings, including binders stored as strings."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return (node.id,)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return (node.name,)
    if isinstance(node, ast.Import):
        return tuple(a.asname or a.name.partition(".")[0] for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return tuple(a.asname or a.name for a in node.names if a.name != "*")
    if isinstance(node, ast.ExceptHandler) and node.name:
        return (node.name,)
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        return (node.name,)
    if isinstance(node, ast.MatchMapping) and node.rest:
        return (node.rest,)
    if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
        return (node.name.id,)
    return ()


def _executable_shape(node):
    """Ignore only postponed annotations and inert function docstrings."""
    if isinstance(node, list):
        return tuple(_executable_shape(value) for value in node)
    if not isinstance(node, ast.AST):
        return node
    fields = []
    for field, value in ast.iter_fields(node):
        if (
            (isinstance(node, ast.arg) and field == "annotation")
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and field == "returns")
            or (isinstance(node, ast.AnnAssign) and field == "annotation")
            or field == "type_comment"
        ):
            continue
        if field == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            value = _body(node)
        fields.append((field, _executable_shape(value)))
    return (type(node).__name__, tuple(fields))


def _caller_body_contract(caller):
    expected = ast.parse(APP_CONTRACT).body[0]
    protected = {"instance_draining", "generation_unavailable", "finalizer_owner", "process_recovery", "watchdog"}
    expected_nodes = [n for statement in _body(expected) for n in _scope_nodes(statement)]
    reserved = {n.id for n in ast.walk(expected) if isinstance(n, ast.Name)}
    reserved.update(name for n in expected_nodes for name in _binder_names(n))
    original_defs = {n.name for n in _body(expected) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    body, extras = [], set()
    for statement in _body(caller):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name not in original_defs:
            # This separate body is never called/captured by the finite actual
            # body. Defining it has no evaluated defaults/decorators/type params.
            if (
                statement.name in reserved
                or statement.name in extras
                or statement.decorator_list
                or statement.type_params
                or statement.args.defaults
                or any(v is not None for v in statement.args.kw_defaults)
            ):
                return ["recovery caller added definition changes retained binding/evaluated header"], []
            if any(isinstance(n, (ast.Global, ast.Nonlocal)) and set(n.names) & protected for n in ast.walk(statement)):
                return ["recovery caller retained producer captured for rebinding"], []
            extras.add(statement.name)
            continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        body.append(statement)
    actual_nodes = [n for statement in body for n in _scope_nodes(statement)]
    if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await)) for n in actual_nodes):
        return ["recovery caller ordinary synchronous execution kind changed"], []
    if _executable_shape(caller.args) != _executable_shape(expected.args):
        return ["recovery caller evaluated parameter contract changed"], []
    if _executable_shape(body) != _executable_shape(_body(expected)):
        return ["recovery caller complete executed producer/dominance/transfer contract changed"], []
    return [], body


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _body(function):
    body = function.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def _function_contract(actual, source):
    expected = ast.parse(source).body[0]
    return isinstance(actual, ast.FunctionDef) and (
        _dump(actual.args) == _dump(expected.args)
        and _dump(actual.returns) == _dump(expected.returns)
        and not actual.decorator_list
        and not actual.type_params
        and [_dump(node) for node in _body(actual)] == [_dump(node) for node in _body(expected)]
    )


def _named(body, name, kind):
    found = [node for node in body if isinstance(node, kind) and node.name == name]
    return found[0] if len(found) == 1 else None


def _postponed(tree):
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _owned_class(cls):
    if cls is None or cls.bases or cls.keywords or cls.decorator_list or cls.type_params:
        return False
    names = []
    for node in _body(cls):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        names.append(node.name)
        if node.name in {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__init_subclass__"}:
            return False
        if node.decorator_list or node.type_params or node.args.defaults or any(value is not None for value in node.args.kw_defaults):
            return False
    return len(names) == len(set(names))


def _module(unit):
    path = unit.path
    if not path.startswith("src/") or not path.endswith(".py"):
        return None
    module = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    return module.removesuffix(".__init__")


def _namespace_effects(units, configured_function):
    """Narrow source-visible namespace mutations; not a universal effect proof."""
    failures = []
    references = {}
    configure_calls = []
    for unit in units:
        module = _module(unit)
        if module is None:
            continue
        module_env = {name: frozenset({"builtins." + name}) for name in {"setattr", "delattr", "vars", "getattr", "globals", "locals"}}
        environments = {}
        parents = {id(child): node for node in ast.walk(unit.tree) for child in ast.iter_child_nodes(node)}

        def string_binding_effect(node, scope, globals_, unit=unit, module=module):
            if scope is not unit.tree and not (set(_binder_names(node)) & globals_):
                return
            for name in _binder_names(node):
                if scope is not unit.tree and name not in globals_:
                    continue
                if module == WORKER and name in CELLS:
                    failures.append("recovery cell outside producer string binding " + unit.path)
                original = module + "." + name
                if original not in PROTECTED_BINDINGS:
                    continue
                canonical = node is configured_function
                canonical |= (
                    scope is unit.tree and isinstance(node, ast.ClassDef) and original in {OWNER, RECOVERY} and node in unit.tree.body
                )
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports = {
                        a.asname or (a.name.partition(".")[0] if isinstance(node, ast.Import) else a.name): (
                            a.name if a.asname else a.name.partition(".")[0]
                        )
                        if isinstance(node, ast.Import)
                        else (node.module or "") + "." + a.name
                        for a in node.names
                    }
                    expected = {
                        APP + ".ApplicationFinalizerOwner": OWNER,
                        APP + ".ProcessRecovery": RECOVERY,
                        APP + ".configure_required_executor_recovery": WORKER + ".configure_required_executor_recovery",
                        APP + ".threading": "threading",
                        "elspeth.web.application_finalizers.threading": "threading",
                        "elspeth.web.process_recovery.threading": "threading",
                    }
                    canonical = (
                        not (isinstance(node, ast.ImportFrom) and node.level)
                        and imports.get(name) == expected.get(original)
                        and original in expected
                    )
                if not canonical:
                    failures.append("recovery protected producer string binding replaced " + unit.path)

        def qualify(value, env, module=module):
            if value is None:
                return frozenset()
            if isinstance(value, ast.Name):
                return env.get(value.id, frozenset({module + "." + value.id}))
            if isinstance(value, ast.Attribute):
                roots = qualify(value.value, env)
                if value.attr == "__dict__" and roots & (PROTECTED_MODULES | {OWNER, RECOVERY}):
                    return frozenset(root + ".__namespace__" for root in roots & (PROTECTED_MODULES | {OWNER, RECOVERY}))
                if value.attr == "__globals__" and WORKER + ".configure_required_executor_recovery" in roots:
                    return frozenset({WORKER + ".__namespace__"})
                return frozenset(root + "." + value.attr for root in roots if root.count(".") < 8)
            if isinstance(value, ast.IfExp):
                return qualify(value.body, env) | qualify(value.orelse, env)
            if isinstance(value, ast.NamedExpr):
                return qualify(value.value, env)
            if isinstance(value, ast.Call):
                operator = qualify(value.func, env)
                if operator & {OWNER, RECOVERY, "threading.Event"}:
                    return frozenset(root + ".__instance__" for root in operator & {OWNER, RECOVERY, "threading.Event"})
                if isinstance(value.func, ast.Name) and value.func.id in {"globals", "locals"} and not value.args and module == WORKER:
                    return frozenset({WORKER + ".__namespace__"})
                if operator == frozenset({"builtins.vars"}) and len(value.args) == 1:
                    return frozenset(root + ".__namespace__" for root in qualify(value.args[0], env))
                if (
                    operator & {"importlib.import_module"}
                    and len(value.args) == 1
                    and isinstance(value.args[0], ast.Constant)
                    and isinstance(value.args[0].value, str)
                ):
                    return frozenset({value.args[0].value})
                if (
                    operator == frozenset({"builtins.getattr"})
                    and len(value.args) >= 2
                    and isinstance(value.args[1], ast.Constant)
                    and isinstance(value.args[1].value, str)
                ):
                    return frozenset(root + "." + value.args[1].value for root in qualify(value.args[0], env))
            if (
                isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Attribute)
                and isinstance(value.value.value, ast.Name)
                and value.value.value.id == "sys"
                and value.value.attr == "modules"
                and isinstance(value.slice, ast.Constant)
                and isinstance(value.slice.value, str)
            ):
                return frozenset({value.slice.value})
            return frozenset()

        def scan(node, env, scope, globals_, unit=unit, module=module, environments=environments, module_env=module_env, parents=parents):
            references[id(node)] = qualify(node, env)
            environments[id(node)] = dict(env)
            if isinstance(
                node,
                (
                    ast.FunctionDef,
                    ast.AsyncFunctionDef,
                    ast.ClassDef,
                    ast.Import,
                    ast.ImportFrom,
                    ast.ExceptHandler,
                    ast.MatchAs,
                    ast.MatchStar,
                    ast.MatchMapping,
                    ast.TypeAlias,
                ),
            ):
                string_binding_effect(node, scope, globals_)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for header in [*node.decorator_list, *node.args.defaults, *(value for value in node.args.kw_defaults if value is not None)]:
                    scan(header, env, scope, globals_)
                local_env = dict(env)
                own_nodes = [child for statement in node.body for child in _scope_nodes(statement)]
                declared = {name for child in own_nodes if isinstance(child, ast.Global) for name in child.names}
                nonlocals = {name for child in own_nodes if isinstance(child, ast.Nonlocal) for name in child.names}
                bound = {name for child in own_nodes for name in _binder_names(child)}
                bound.update(argument.arg for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs])
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
                for name in bound - declared - nonlocals:
                    local_env[name] = frozenset()
                for name in declared:
                    local_env[name] = module_env.get(name, frozenset({module + "." + name}))
                for statement in node.body:
                    scan(statement, local_env, node, declared)
                env[node.name] = frozenset({module + "." + node.name})
                return
            if isinstance(node, ast.ClassDef):
                for header in [*node.bases, *node.decorator_list, *(keyword.value for keyword in node.keywords)]:
                    scan(header, env, scope, globals_)
                class_env = dict(env)
                for statement in node.body:
                    scan(statement, class_env, node, set())
                env[node.name] = frozenset({module + "." + node.name})
                return
            for child in ast.iter_child_nodes(node):
                scan(child, env, scope, globals_)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if isinstance(node, ast.Import):
                        env[alias.asname or alias.name.partition(".")[0]] = frozenset(
                            {alias.name if alias.asname else alias.name.partition(".")[0]}
                        )
                    elif not node.level and node.module:
                        if alias.name == "*" and node.module in PROTECTED_MODULES:
                            failures.append("recovery producer namespace wildcard is unsupported " + unit.path)
                        env[alias.asname or alias.name] = frozenset({node.module + "." + alias.name})
            if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar, ast.MatchMapping)):
                for name in _binder_names(node):
                    env[name] = frozenset()
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        original = module + "." + target.id
                        if (scope is unit.tree or target.id in globals_) and original in PROTECTED_BINDINGS:
                            failures.append("recovery constructor/callable global binding replaced " + unit.path)
                        if (
                            module == WORKER
                            and target.id in CELLS
                            and (scope is unit.tree or target.id in globals_)
                            and scope is not configured_function
                        ):
                            canonical = scope is unit.tree and (
                                (
                                    target.id == "_INSTANCE_DRAINING"
                                    and isinstance(node, ast.Assign)
                                    and _dump(node.value) == _dump(ast.parse("threading.Event()", mode="eval").body)
                                )
                                or (
                                    target.id == "_APPLICATION_FINALIZER_OWNER"
                                    and isinstance(node, ast.AnnAssign)
                                    and isinstance(node.value, ast.Constant)
                                    and node.value.value is None
                                )
                            )
                            if not canonical:
                                failures.append("recovery cell outside producer replacement " + unit.path)
                        env[target.id] = qualify(node.value, env)
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Del)
                and module == WORKER
                and node.id in CELLS
                and (scope is unit.tree or node.id in globals_)
            ):
                failures.append("recovery cell deletion " + unit.path)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and (scope is unit.tree or node.id in globals_):
                parent = parents.get(id(node))
                if module == WORKER and node.id in CELLS:
                    canonical_initial = parent in unit.tree.body and (
                        (
                            node.id == "_INSTANCE_DRAINING"
                            and isinstance(parent, ast.Assign)
                            and _dump(parent.value) == _dump(ast.parse("threading.Event()", mode="eval").body)
                        )
                        or (
                            node.id == "_APPLICATION_FINALIZER_OWNER"
                            and isinstance(parent, ast.AnnAssign)
                            and isinstance(parent.value, ast.Constant)
                            and parent.value.value is None
                        )
                    )
                    if scope is not configured_function and not canonical_initial:
                        failures.append("recovery cell outside producer lexical store " + unit.path)
                elif module + "." + node.id in PROTECTED_BINDINGS:
                    failures.append("recovery protected global lexical store " + unit.path)
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Del)
                and (scope is unit.tree or node.id in globals_)
                and module + "." + node.id in PROTECTED_BINDINGS
            ):
                failures.append("recovery protected global deletion " + unit.path)
            if (
                isinstance(node, (ast.Global, ast.Nonlocal))
                and module == WORKER
                and set(node.names) & CELLS
                and scope is not configured_function
            ):
                failures.append("recovery cell outside producer declaration " + unit.path)
            if isinstance(node, ast.Call) and WORKER + ".configure_required_executor_recovery" in qualify(node.func, env):
                configure_calls.append((unit, node, scope))

        for statement in unit.tree.body:
            scan(statement, module_env, unit.tree, set())

        def protected(qualified):
            return (
                qualified in PROTECTED_BINDINGS
                or any(qualified == WORKER + "." + cell or qualified.startswith(WORKER + "." + cell + ".") for cell in CELLS)
                or any(
                    qualified.startswith(owner + ".")
                    for owner in {OWNER, RECOVERY, "threading.Event", WORKER + ".configure_required_executor_recovery"}
                )
            )

        for node in ast.walk(unit.tree):
            env = environments.get(id(node), module_env)
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and any(protected(root) for root in qualify(node, env))
            ):
                failures.append("recovery producer qualified binding/member replacement " + unit.path)
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                roots = qualify(node.value, env)
                selector = node.slice.value if isinstance(node.slice, ast.Constant) else None
                if any(
                    root.endswith(".__namespace__")
                    and (
                        (
                            root.removesuffix(".__namespace__") == WORKER
                            and (selector is None or selector in CELLS or protected(WORKER + "." + str(selector)))
                        )
                        or (root.removesuffix(".__namespace__") in {OWNER, RECOVERY})
                        or (selector is None or protected(root.removesuffix(".__namespace__") + "." + str(selector)))
                    )
                    for root in roots
                ):
                    failures.append("recovery cell namespace dictionary replacement " + unit.path)
            if (
                isinstance(node, (ast.Name, ast.Attribute))
                and isinstance(node.ctx, ast.Load)
                and WORKER + ".configure_required_executor_recovery" in qualify(node, env)
            ):
                parent = parents.get(id(node))
                if not isinstance(parent, ast.Call) or parent.func is not node:
                    failures.append("recovery registration callable escape " + unit.path)
            if isinstance(node, ast.Call):
                op = qualify(node.func, env)
                terminal = (
                    node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
                )
                if (
                    terminal in {"setattr", "delattr", "__setattr__", "__delattr__"} or op & {"builtins.setattr", "builtins.delattr"}
                ) and len(node.args) >= 2:
                    field = node.args[1].value if isinstance(node.args[1], ast.Constant) else None
                    roots = qualify(node.args[0], env)
                    if any(
                        (root in PROTECTED_MODULES and (field is None or protected(root + "." + str(field)))) or root in {OWNER, RECOVERY}
                        for root in roots
                    ):
                        failures.append("recovery producer reflected replacement " + unit.path)
                if (
                    isinstance(node.func, ast.Attribute)
                    and any(root.endswith(".__namespace__") for root in qualify(node.func.value, env))
                    and node.func.attr not in {"get", "items", "keys", "values", "copy"}
                ):
                    failures.append("recovery protected namespace mutation/unsupported effect " + unit.path)
    return failures, references, configure_calls


def recovery_cell_contract(units):
    units = tuple(units)
    by_module = {}
    for unit in units:
        module = _module(unit)
        if module is None:
            continue
        if module in by_module:
            return ["recovery source module identity duplicated " + module], frozenset(), frozenset()
        by_module[module] = unit
    required = {WORKER, APP, OWNER.rpartition(".")[0], RECOVERY.rpartition(".")[0]}
    if not required <= set(by_module):
        return ["recovery producer/constructor source missing"], frozenset(), frozenset()
    worker, app = by_module[WORKER], by_module[APP]
    owned_unit, recovery_unit = by_module[OWNER.rpartition(".")[0]], by_module[RECOVERY.rpartition(".")[0]]
    if not all(_postponed(unit.tree) for unit in [worker, app, owned_unit, recovery_unit]):
        return ["recovery producer definition annotations must be postponed"], frozenset(), frozenset()
    function = _named(worker.tree.body, "configure_required_executor_recovery", ast.FunctionDef)
    if not _function_contract(function, CONFIGURE):
        return ["recovery registration complete argument/guard/store contract changed"], frozenset(), frozenset()
    owner = _named(owned_unit.tree.body, "ApplicationFinalizerOwner", ast.ClassDef)
    recovery = _named(recovery_unit.tree.body, "ProcessRecovery", ast.ClassDef)
    if not _owned_class(owner) or not _owned_class(recovery):
        return ["recovery owner/class lookup or definition contract changed"], frozenset(), frozenset()
    for cls, name, contract in [
        (owner, "__init__", OWNER_CONSTRUCTOR),
        (recovery, "__init__", RECOVERY_CONSTRUCTOR),
        (recovery, "required_generation_expired", RECOVERY_CALLBACK),
        (recovery, "_begin", RECOVERY_BEGIN),
    ]:
        if not _function_contract(_named(cls.body, name, ast.FunctionDef), contract):
            return ["recovery owned constructor/receiver/callback contract changed " + cls.name + "." + name], frozenset(), frozenset()
    caller = _named(app.tree.body, "create_app", ast.FunctionDef)
    if caller is None or caller.decorator_list or caller.type_params:
        return ["recovery application caller identity changed"], frozenset(), frozenset()
    caller_failures, body = _caller_body_contract(caller)
    if caller_failures:
        return caller_failures, frozenset(), frozenset()
    if not body or not isinstance(body[0], ast.Global) or body[0].names != ["_FAILED_BOOTSTRAP_RECOVERY"]:
        return ["recovery caller scope contract changed"], frozenset(), frozenset()
    expected_prefix = ast.parse(APP_PREFIX).body
    if [_dump(node) for node in body[1 : 1 + len(expected_prefix)]] != [_dump(node) for node in expected_prefix]:
        return ["recovery caller retained constructor prefix changed"], frozenset(), frozenset()
    expected_call = ast.parse(CALL, mode="eval").body
    tries = [
        node
        for node in body
        if isinstance(node, ast.Try)
        and len(node.body) >= 2
        and isinstance(node.body[1], ast.Expr)
        and isinstance(node.body[1].value, ast.Call)
        and _dump(node.body[1].value) == _dump(expected_call)
    ]
    if len(tries) != 1 or _dump(tries[0].body[0]) != _dump(ast.parse("watchdog.assert_watching()").body[0]):
        return ["recovery retained callback/cell call and owner witness changed"], frozenset(), frozenset()
    call = tries[0].body[1].value
    protected_locals = {"instance_draining", "generation_unavailable", "finalizer_owner", "process_recovery", "watchdog"}
    allowed_stores = {
        id(target)
        for statement in body[1 : 1 + len(expected_prefix)]
        if isinstance(statement, ast.Assign)
        for target in statement.targets
        if isinstance(target, ast.Name) and target.id in protected_locals
    }
    own_nodes = [node for statement in _body(caller) for node in _scope_nodes(statement)]
    for node in own_nodes:
        if set(_binder_names(node)) & protected_locals and id(node) not in allowed_stores:
            return ["recovery caller retained producer lexical binding changed"], frozenset(), frozenset()
        if isinstance(node, (ast.Global, ast.Nonlocal)) and set(node.names) & protected_locals:
            return ["recovery caller retained producer scope changed"], frozenset(), frozenset()
    if any(isinstance(node, ast.Nonlocal) and set(node.names) & protected_locals for node in ast.walk(caller)):
        return ["recovery caller retained producer captured for rebinding"], frozenset(), frozenset()
    failures, references, calls = _namespace_effects(units, function)
    parents = {id(child): node for node in ast.walk(caller) for child in ast.iter_child_nodes(node)}
    for node in own_nodes:
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load) or node.id not in protected_locals - {"watchdog"}:
            continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.keyword):
            transport = parents.get(id(parent))
            name = transport.func.id if isinstance(transport, ast.Call) and isinstance(transport.func, ast.Name) else None
            allowed = {
                "configure_required_executor_recovery": {"instance_draining", "generation_unavailable", "finalizer_owner"},
                "ProcessRecovery": {"instance_draining"},
                "_create_app": {"process_recovery", "instance_draining", "generation_unavailable", "finalizer_owner"},
            }
            if node.id in allowed.get(name, set()):
                continue
        if (
            node.id == "instance_draining"
            and isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "watchdog_factory"
            and parent is body[5].value
        ):
            continue
        if (
            node.id == "process_recovery"
            and isinstance(parent, ast.Attribute)
            and parent.value is node
            and parent.attr == "required_generation_expired"
            and isinstance(parents.get(id(parent)), ast.keyword)
            and parents[id(parent)].arg == "recovery_callback"
        ):
            continue
        if (
            node.id == "process_recovery"
            and isinstance(parent, ast.Assign)
            and len(parent.targets) == 1
            and isinstance(parent.targets[0], ast.Name)
            and parent.targets[0].id == "_FAILED_BOOTSTRAP_RECOVERY"
            and parent.value is node
        ):
            continue
        failures.append("recovery retained caller capability has unsupported transfer " + node.id)
    for node, expected in [
        (call.func, WORKER + ".configure_required_executor_recovery"),
        (body[3].value.func, "threading.Event"),
        (body[7].value.func, RECOVERY),
        (body[8].value.func, "threading.Event"),
        (body[9].value.func, OWNER),
    ]:
        if references.get(id(node)) != frozenset({expected}):
            failures.append("recovery caller constructor/call source origin changed " + expected)
    lock_call = _body(_named(owner.body, "__init__", ast.FunctionDef))[0].value
    if references.get(id(lock_call.func)) != frozenset({"threading.Lock"}):
        failures.append("recovery owner physical lock supplier origin changed")
    if len(calls) != 1 or calls[0][0] is not app or calls[0][1] is not call or calls[0][2] is not caller:
        failures.append("recovery registration call/escape universe changed")
    module_initializers = {
        cell: [
            node
            for node in worker.tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == cell
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
        ]
        for cell in CELLS
    }
    if any(len(nodes) != 1 for nodes in module_initializers.values()):
        failures.append("recovery initial module cell producer is not unique")
    if failures:
        return failures, frozenset(), frozenset()
    stores = frozenset(
        id(node) for node in ast.walk(function) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in CELLS
    )
    globals_ = frozenset(id(node) for node in ast.walk(function) if isinstance(node, ast.Global) and set(node.names) >= CELLS)
    if len(stores) != 2 or len(globals_) != 1:
        return ["recovery exact authorized transition nodes changed"], frozenset(), frozenset()
    return [], stores, globals_


def recovery_dependency_roles(units):
    """Expose actual operator nodes; every effect is still a common premise.

    This API does not convert body-shape agreement into supplier qualification.
    It inventories ALL calls in the checked caller, registration, constructors
    and callback methods, including unresolved factory and receiver operators.
    """
    units = tuple(units)
    failures, _, _ = recovery_cell_contract(units)
    if failures:
        return failures, ()
    by_module = {_module(unit): unit for unit in units if _module(unit) is not None}
    worker = by_module[WORKER]
    configured = _named(worker.tree.body, "configure_required_executor_recovery", ast.FunctionDef)
    _, references, _ = _namespace_effects(units, configured)
    regions = [(by_module[APP], _named(by_module[APP].tree.body, "create_app", ast.FunctionDef)), (worker, configured)]
    for qualified, members in [(OWNER, ("__init__",)), (RECOVERY, ("__init__", "required_generation_expired", "_begin"))]:
        unit = by_module[qualified.rpartition(".")[0]]
        cls = _named(unit.tree.body, qualified.rpartition(".")[2], ast.ClassDef)
        regions.extend((unit, _named(cls.body, name, ast.FunctionDef)) for name in members)
    records = []
    for unit, region in regions:
        for node in ast.walk(region):
            if isinstance(node, ast.Call):
                records.append(
                    {
                        "unit_path": unit.path,
                        "region": region.name,
                        "node_id": id(node),
                        "operator_node_id": id(node.func),
                        "operator_ast": _dump(node.func),
                        "source_origins": tuple(sorted(references.get(id(node.func), frozenset()))),
                        "qualification": "COMMON_ORIGIN_RECEIVER_EFFECT_PROOF_REQUIRED",
                    }
                )
    return [], tuple(records)
