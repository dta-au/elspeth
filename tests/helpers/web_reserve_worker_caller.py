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
DRIVER = "src/elspeth/web/sessions/continuation_driver.py"

# Populated from the actual mapped source by the standalone preparation script.
# Application grammar is the actual header and entire executable prefix through
# the direct constructor statement. No tail or source coordinates authorize it.
REVIEWED_GRAMMARS = {
    "ComposerAsyncWorker.__init__": "7675240c07be3c1db3e375b6ecb8b85aa0d7c890a4e3cfa2c1761adbe64ea402",
    "ComposerAsyncWorker._job": "ebe08f000b83f6477d3035666cce0d85aaf8f85290c8bdceebf4a91b6bc21ab2",
    "_create_app": "2c4a5bf83deefeaa62e0d35b1556f6542b7ecaa8e9af5b93ef27e37e41beaa98",
}
SELECTED = (
    (APP, "_create_app", "ORDINARY_FUNCTION"),
    (WORKER, "ComposerAsyncWorker.__init__", "ORDINARY_FUNCTION"),
    (WORKER, "ComposerAsyncWorker._job", "NATIVE_COROUTINE"),
)
RECEIPT_KEYS = (
    "app_worker_constructor_call",
    "app_worker_keyword_value",
    "worker_app_store",
    "job_dto_factory_call",
    "job_services_store",
    "job_segment_calls",
    "segment_driver_call",
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


def grammar(node, *, prefix=None):
    clone = copy.copy(node)
    clone.body = list(node.body if prefix is None else prefix)
    clone = adapter_grammar_projection(clone)
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
            "elspeth.web.sessions.composer_async_worker.ComposerAsyncWorker._job",
            "ordinary member descriptor lookup, actual callable/code origin and receiver member stability",
        ),
        (
            "elspeth.web.sessions.continuation_driver._SegmentDriver.run",
            "ordinary member descriptor lookup, actual callable/code origin and receiver member stability",
        ),
        (
            "elspeth.web.sessions.continuation_driver.run_continuation_segment",
            "actual imported native coroutine and retained services argument effects",
        ),
        (
            "elspeth.web.composer.continuation_live_phases.ContinuationLivePhases.advance",
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


def worker_namespace_contract(worker_class):
    """Keep the actual ordinary worker namespace and its one exact getter."""
    if (
        not isinstance(worker_class, ast.ClassDef)
        or worker_class.bases
        or worker_class.keywords
        or worker_class.decorator_list
        or worker_class.type_params
    ):
        return ["worker caller: unsupported worker class construction/namespace"]
    lookup = {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__", "__set__", "__delete__"}
    declarations = [
        node
        for node in worker_class.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str)
    ]
    if any(not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name in lookup for node in declarations):
        return ["worker caller: unsupported worker member/descriptor declaration"]
    expected_property = ast.parse(
        "@property\ndef drain_deadline(self) -> ComposerDrainDeadline | None:\n    return self._drain_deadline"
    ).body[0]
    getters = [node for node in declarations if node.name == "drain_deadline"]
    if len(getters) != 1 or ast.dump(getters[0]) != ast.dump(expected_property):
        return ["worker caller: exact original drain deadline property changed"]
    for member in declarations:
        if member is getters[0]:
            continue
        defaults = member.args.defaults + [node for node in member.args.kw_defaults if node is not None]
        if (
            member.type_params
            or any(not isinstance(node, ast.Constant) for node in defaults)
            or any(chain(node) != ("staticmethod",) for node in member.decorator_list)
        ):
            return ["worker caller: unsupported worker definition-time member effect " + member.name]
    return []


def _drain_owner_transfer(app, init, constructor):
    """Bind only the actual optional owner, event and worker field transfer."""
    arguments = [keyword.value for keyword in constructor.keywords if keyword.arg == "drain_signal"]
    events = [keyword.value for keyword in constructor.keywords if keyword.arg == "instance_draining"]
    if (
        len(arguments) != 1
        or chain(arguments[0]) != ("drain_signal",)
        or len(events) != 1
        or chain(events[0]) != ("app", "state", "instance_draining")
    ):
        return ["worker caller: original drain owner/event constructor transfer changed"], None
    expected_guard = ast.parse(
        "if drain_signal is not None and (type(drain_signal) is not ComposerDrainSignal "
        "or drain_signal.draining is not instance_draining):\n"
        "    raise AuditIntegrityError('Composer worker changed its actual process drain signal')"
    ).body[0]
    guards = [node for node in init.body if isinstance(node, ast.If) and ast.dump(node) == ast.dump(expected_guard)]
    expected_store = ast.parse("self._drain_signal = drain_signal").body[0]
    stores = [node for node in init.body if ast.dump(node) == ast.dump(expected_store)]
    state_store = [
        node for node in app.body if ast.dump(node) == ast.dump(ast.parse("app.state.instance_draining = instance_draining").body[0])
    ]
    if len(guards) != 1 or len(stores) != 1 or init.body.index(guards[0]) >= init.body.index(stores[0]) or len(state_store) != 1:
        return ["worker caller: nominal original drain owner/event guard or store changed"], None
    for function in (app, init):
        defaults = dict(zip((arg.arg for arg in function.args.kwonlyargs), function.args.kw_defaults, strict=True))
        if (
            "drain_signal" not in defaults
            or not isinstance(defaults["drain_signal"], ast.Constant)
            or defaults["drain_signal"].value is not None
        ):
            return ["worker caller: optional drain owner default changed"], None
    return [], {
        "argument": (APP, id(arguments[0])),
        "event_argument": (APP, id(events[0])),
        "guard": (WORKER, id(guards[0])),
        "store": (WORKER, id(stores[0])),
        "event_store": (APP, id(state_store[0])),
    }


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
    if len(index) != len(units) or not all(path in index for path in (APP, WORKER, DRIVER)):
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
    worker_class, _ = find(index[WORKER].tree, "ComposerAsyncWorker")
    namespace_failures = worker_namespace_contract(worker_class)
    if namespace_failures:
        return namespace_failures, receipt
    store = next(n for n in init.body if isinstance(n, ast.Assign) and chain(n.targets[0]) == ("self", "_app"))
    app_constructor = direct_call_statement(app.body, ("app", "state", "composer_async_worker"), ("ComposerAsyncWorker",)).value
    keyword_values = [n.value for n in app_constructor.keywords if n.arg == "app"]
    if len(keyword_values) != 1 or chain(keyword_values[0]) != ("app",):
        return ["worker caller: same-call exact app keyword changed"], receipt
    drain_failures, drain = _drain_owner_transfer(app, init, app_constructor)
    if drain_failures:
        return drain_failures, receipt
    receipt["drain_owner_transfer"] = drain
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
    flow_failures, flow = continuation_caller_contracts(units)
    failures.extend(flow_failures)
    if flow is not None:
        receipt.update(flow)
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


# These are current executable transport contracts, not replacements for the
# independently retained complete worker/source grammar pins. Deleted V1
# run-started/turn wrappers no longer transport a selected capability.
_SEGMENT_ENTRY = """
async def run_continuation_segment(
    worker: ComposerAsyncWorker, services: ComposerAppServices,
    running: ComposerOperationRunning, lease: SessionOperationLease, *,
    adoption_failure: BaseException | None = None,
) -> ContinuationSegmentOutcome:
    from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
    original: BaseException | None = None
    driver: _SegmentDriver | None = None
    try:
        driver = _SegmentDriver(worker, services, running, lease)
        return await driver.run(adoption_failure)
    except BaseException as failure:
        original = failure
        if driver is not None:
            original = driver.join_failure(failure)
        elif adoption_failure is not None:
            original = combine_failure_objects(adoption_failure, failure)
        if original is failure:
            raise
        raise original from original.__cause__
    finally:
        if lease.required_work is not None and lease.required_work.sql_deadline_owner is not None:
            lease.required_work.sql_deadline_owner.begin_settlement()
        cleanup_errors = list(driver._progress_cleanup_failures) if driver is not None else []
        if driver is not None:
            try:
                await driver.close_progress()
            except BaseException as cleanup:
                cleanup_errors.append(cleanup)
        try:
            await lease.close(deadline=worker.drain_deadline)
        except BaseException as cleanup:
            cleanup_errors.append(cleanup)
        if cleanup_errors:
            cleanup_failure = cleanup_errors[0]
            additional = (*cleanup_errors[1:], *((original,) if original is not None else ()))
            if driver is not None:
                combined = driver.join_failure(cleanup_failure, *additional)
                if combined is cleanup_failure:
                    raise cleanup_failure
                raise combined from combined.__cause__
            combined = combine_failure_objects(cleanup_failure, *additional)
            if combined is cleanup_failure:
                raise cleanup_failure
            raise combined from combined.__cause__
"""

_JOIN_OWNED = """
async def _join_owned[T](task: asyncio.Task[T], *, cancellation_observations: list[asyncio.CancelledError] | None = None) -> T:
    observed: list[asyncio.CancelledError] = []
    owner = asyncio.current_task()
    while not task.done():
        cancelling_before = owner.cancelling() if owner is not None else 0
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if not task.cancelled() or (owner is not None and owner.cancelling() > cancelling_before):
                observed.append(cancelled)
        except BaseException:
            break
    if cancellation_observations is not None:
        cancellation_observations.extend(observed)
    try:
        result = task.result()
    except BaseException as child_failure:
        if observed:
            combined = _combine_operation_failures(child_failure, tuple(observed))
            raise combined from combined.__cause__
        raise
    return result
"""

_OWNED = """
async def _owned[T](awaitable: Awaitable[T], *, cancellation_observations: list[asyncio.CancelledError] | None = None) -> T:
    return await _join_owned(asyncio.ensure_future(awaitable), cancellation_observations=cancellation_observations)
"""

_ADOPTION_TRANSFER = """
try:
    lease = await adoption.join()
except BaseException as original:
    retained_lease = adoption.lease
    if retained_lease is None:
        raise
    await run_continuation_segment(self, services, running, retained_lease, adoption_failure=original)
    raise
"""


def executable_shape(node):
    """Ignore only the inert leading docstring; retain all executable nodes."""
    clone = copy.copy(node)
    clone.body = list(node.body)
    if clone.body and isinstance(clone.body[0], ast.Expr):
        value = clone.body[0].value
        if isinstance(value, ast.Constant) and type(value.value) is str:
            clone.body = clone.body[1:]
    return ast.dump(clone, include_attributes=False)


def continuation_caller_contracts(units):
    """Actual adoption success/failure edges and one retained lease finalizer."""
    units = tuple(units)
    index = {str(unit.path): unit for unit in units}
    if len(index) != len(units) or any(path not in index for path in (WORKER, DRIVER)):
        return ["worker continuation caller: missing/duplicate units"], None
    job, _ = find(index[WORKER].tree, "ComposerAsyncWorker._job")
    entry, _ = find(index[DRIVER].tree, "run_continuation_segment")
    if not isinstance(job, ast.AsyncFunctionDef):
        return ["worker continuation caller: missing job"], None
    definitions = []
    for path, name, template in (
        (WORKER, "_owned", _OWNED),
        (WORKER, "_join_owned", _JOIN_OWNED),
        (DRIVER, "run_continuation_segment", _SEGMENT_ENTRY),
    ):
        definition, _ = find(index[path].tree, name)
        if not isinstance(definition, ast.AsyncFunctionDef) or executable_shape(definition) != executable_shape(
            ast.parse(template).body[0]
        ):
            return ["worker continuation caller: exact transport/cleanup changed " + name], None
        definitions.append((path, id(definition)))
    # Exact exception nesting matters: a retained lease is never confused with
    # successful adoption, and the original exception is re-raised afterward.
    blocks = [
        node for node in ast.walk(job) if isinstance(node, ast.Try) and ast.dump(node) == ast.dump(ast.parse(_ADOPTION_TRANSFER).body[0])
    ]
    if len(blocks) != 1:
        return ["worker continuation caller: retained adoption transfer changed"], None
    block = blocks[0]
    parents = [value for node in ast.walk(job) for _, value in ast.iter_fields(node) if isinstance(value, list) and block in value]
    expected = ast.parse("await run_continuation_segment(self, services, running, lease, adoption_failure=started_failure)").body[0]
    if len(parents) != 1:
        return ["worker continuation caller: ambiguous adoption owner"], None
    siblings = parents[0]
    offset = siblings.index(block)
    if offset + 1 >= len(siblings) or ast.dump(siblings[offset + 1]) != ast.dump(expected):
        return ["worker continuation caller: successful adoption transfer changed"], None
    calls = awaited_calls(job, ("run_continuation_segment",))
    expected_calls = [node for node in ast.walk(block) if isinstance(node, ast.Call) and chain(node.func) == ("run_continuation_segment",)]
    expected_calls.append(siblings[offset + 1].value.value)
    if len(calls) != 2 or {id(node) for node in calls} != {id(node) for node in expected_calls}:
        return ["worker continuation caller: extra/escaped segment dispatch"], None
    constructor = next(node for node in ast.walk(entry) if isinstance(node, ast.Call) and chain(node.func) == ("_SegmentDriver",))
    run = next(node for node in ast.walk(entry) if isinstance(node, ast.Call) and chain(node.func) == ("driver", "run"))
    close = next(node for node in ast.walk(entry) if isinstance(node, ast.Call) and chain(node.func) == ("lease", "close"))
    transport = [(WORKER, node) for node in calls] + [(DRIVER, constructor), (DRIVER, run), (DRIVER, close)]
    return [], {
        "job_segment_calls": tuple((WORKER, id(node)) for node in calls),
        "segment_driver_call": (DRIVER, id(constructor)),
        "segment_run_call": (DRIVER, id(run)),
        "segment_close_call": (DRIVER, id(close)),
        "transport_definitions": tuple(definitions),
        "transport_calls": frozenset((path, id(node)) for path, node in transport),
        "supplier_arguments": frozenset(
            (path, id(node), id(argument))
            for path, node in transport
            for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        ),
        "adoption_transfer": (WORKER, id(block)),
    }
