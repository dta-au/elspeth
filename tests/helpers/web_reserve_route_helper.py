"""Three conditional route reserve receipts on caller-owned ASTs; never admission."""

import ast
import copy
import hashlib
import inspect
import json
import types

HELPERS = "src/elspeth/web/sessions/routes/_helpers.py"
TURN = "src/elspeth/web/sessions/composer_turn.py"
HANDLERS = ("_handle_convergence_error", "_handle_plugin_crash", "_handle_runtime_preflight_failure")


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
    "_handle_convergence_error": "8032fa7144f05421b9d59d93be654a0e800bb6090b8307f374c34296c99f45be",
    "_handle_plugin_crash": "c0345259ed743d014249dfc660177a0f614e7d48572bd85107b2dbf1cc33aa25",
    "_handle_runtime_preflight_failure": "fd3578fce1748f626a5c0e09fab7f7f60f15c1e8ee0e8c950f580c378f553865",
    "_run_composer_turn": "3667bfc35321aa7abb37478b1414835df3a630f70bb3107f90533d1a8fa5036e",
}
IMPORT_GRAMMARS = {
    "src/elspeth/web/sessions/routes/_helpers.py|elspeth.web.required_work": "24c410aedf7d87072b30e0483a70191b9729d6d67166d1ef15b55ff0013b5199",
    "src/elspeth/web/sessions/composer_turn.py|elspeth.web.required_work": "4572b3179e9539f528774bd9f251888fa3c9f2531b5be474427d6339e9ecc32a",
    "src/elspeth/web/sessions/composer_turn.py|elspeth.web.sessions.routes._helpers": "901b019d65c5e8523ee5ff9afdff54fe99f485ed5edc01283867fac310cf4ba6",
}


def _route1_local_contracts(units):
    """Finite pinned grammar facts only; all identities belong to supplied trees."""
    receipt = {
        key: []
        for key in (
            "reserve_calls",
            "caller_calls",
            "import_nodes",
            "formal_nodes",
            "producer_stores",
            "sql_keyword_values",
            "return_nodes",
            "selected_definitions",
            "invocation_kinds",
        )
    }
    receipt["effect_dependencies"] = [
        {"identity": name, "qualification": "UNKNOWN", "requirement": requirement}
        for name, requirement in (
            ("IMPORTED_HELPERS", "actual imported callable/code origins match selected source, no import substitution"),
            (
                "REQUIRED_WORK_PRODUCERS",
                "actual observation/lease properties return the retained nominal coordinator with pinned ordinary semantics",
            ),
            (
                "COORDINATOR_RESERVE",
                "actual nominal receiver, reserve descriptor/code/member and source enum origins match reviewed implementation",
            ),
            (
                "INTERVENING_EFFECTS",
                "all callbacks, awaits, descriptors, argument evaluation and outside aliases preserve required_work/service/response origins",
            ),
            (
                "SQL_AND_RETURN",
                "actual service.save_composition_state owns physical SQL/join/accounting; returned record and response body semantics remain pinned",
            ),
        )
    ]
    units = list(units)
    index = {str(unit.path): unit for unit in units}
    if len(index) != len(units) or not all(path in index for path in (HELPERS, TURN)):
        return (["route helpers: missing/duplicate SourceUnit"], receipt)
    failures = []
    selected = {}
    for path in (HELPERS, TURN):
        tree = index[path].tree
        if not isinstance(tree, ast.Module):
            failures.append("route helpers: unsupported AST " + path)
            continue
        if not any(
            isinstance(n, ast.ImportFrom) and n.level == 0 and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        ):
            failures.append("route helpers: annotation execution mode changed " + path)
        modules = (
            ("elspeth.web.required_work",) if path == HELPERS else ("elspeth.web.required_work", "elspeth.web.sessions.routes._helpers")
        )
        for module in modules:
            imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == module]
            digest = (
                None
                if len(imports) != 1
                else hashlib.sha256(json.dumps(canonical(imports[0]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            )
            if digest != IMPORT_GRAMMARS[path + "|" + module]:
                failures.append("route helpers: canonical import binding changed " + module)
            else:
                receipt["import_nodes"].append((path, id(imports[0])))
        names = HANDLERS if path == HELPERS else ("_run_composer_turn",)
        for name in names:
            function, owners = find(tree, name)
            if not isinstance(function, ast.AsyncFunctionDef):
                failures.append("route helpers: selected native coroutine changed " + name)
                continue
            if grammar(function) != GRAMMARS[name]:
                failures.append("route helpers: complete finite grammar changed " + name)
                continue
            code, reason = code_for(tree, owners)
            mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
            flags = None if code is None else code.co_flags & mask
            if flags != inspect.CO_COROUTINE:
                failures.append("route helpers: compiled kind changed " + name)
                continue
            receipt["invocation_kinds"].append(
                {
                    "definition": (path, id(function)),
                    "kind_bits": flags,
                    "compiler_reason": reason,
                    "qualification": "SOURCE_KIND_ORIGIN_UNKNOWN",
                }
            )
            receipt["selected_definitions"].append((path, id(function)))
            selected[name] = function
    if failures:
        return (failures, receipt)
    for name in HANDLERS:
        function = selected[name]
        formals = [n for n in (*function.args.args, *function.args.kwonlyargs) if n.arg == "required_work"]
        calls = [n for n in ast.walk(function) if isinstance(n, ast.Call) and chain(n.func) == ("required_work", "reserve")]
        returns = [n for n in ast.walk(function) if isinstance(n, ast.Return)]
        sql_values = [
            k.value
            for n in ast.walk(function)
            if isinstance(n, ast.Call) and chain(n.func) == ("service", "save_composition_state")
            for k in n.keywords
            if k.arg == "required_work"
        ]
        if (
            len(formals) != 1
            or len(calls) != 1
            or [chain(n) for n in calls[0].args] != [("RequiredWorkSource", "RECOVERY_PARTIAL_STATE_SQL")]
            or calls[0].keywords
            or (len(returns) != 1)
            or (chain(returns[0].value) != ("response_body",))
            or (len(sql_values) != 1)
            or (chain(sql_values[0]) != ("partial_sql",))
        ):
            return (["route helpers: exact formal/reserve/SQL/return transfer changed " + name], receipt)
        receipt["formal_nodes"].append((HELPERS, id(formals[0])))
        receipt["reserve_calls"].append((HELPERS, id(calls[0])))
        receipt["sql_keyword_values"].append((HELPERS, id(sql_values[0])))
        receipt["return_nodes"].append((HELPERS, id(returns[0])))
    turn = selected["_run_composer_turn"]
    calls = [n for n in ast.walk(turn) if isinstance(n, ast.Call) and chain(n.func) in [(name,) for name in HANDLERS]]
    parents = {id(child): parent for parent in ast.walk(turn) for child in ast.iter_child_nodes(parent)}
    for call in calls:
        wrapper = parents.get(id(call))
        awaited = parents.get(id(wrapper))
        if (
            not isinstance(wrapper, ast.Call)
            or chain(wrapper.func) != ("_required_audit",)
            or len(wrapper.args) != 2
            or (chain(wrapper.args[0]) != ("observation",))
            or (wrapper.args[1] is not call)
            or wrapper.keywords
            or (not isinstance(awaited, ast.Await))
            or (awaited.value is not wrapper)
        ):
            return (["route helpers: exact required audit wrapper transfer changed"], receipt)
    if len(calls) != 4 or [sum(chain(c.func) == (name,) for c in calls) for name in HANDLERS] != [1, 1, 2]:
        return (["route helpers: four exact awaited callers changed"], receipt)
    for call in calls:
        values = [k.value for k in call.keywords if k.arg == "required_work"]
        if len(values) != 1 or chain(values[0]) != ("required_work",):
            return (["route helpers: caller coordinator keyword changed"], receipt)
        receipt["caller_calls"].append((TURN, id(call)))
    stores = [
        n for n in ast.walk(turn) if isinstance(n, ast.Assign) and len(n.targets) == 1 and (chain(n.targets[0]) == ("required_work",))
    ]
    if sorted(chain(n.value) for n in stores) != [("lease", "required_work"), ("observation", "required_work")]:
        return (["route helpers: coordinator producer stores changed"], receipt)
    receipt["producer_stores"] = [(TURN, id(n)) for n in stores]
    return ([], receipt)


WRAPPER_GRAMMARS = {
    "_required_audit": "0eabd9b2c616c3c1d8f88c3e88eb90473f06bda2f5dca5ff1035411e3cc0d1a3",
    "_join_freeform_owned_task": "a54704223659523840dc2a5d36040a795313f7043f66475e17b8d49486bb4040",
}


def generic_native_code(tree, function):
    """Finite actual compiler generic scope; compilation only, never execution."""
    try:
        module = compile(tree, "<route-generic-metadata>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as error:
        return (None, type(error).__name__)
    scopes = [
        n
        for n in module.co_consts
        if isinstance(n, types.CodeType)
        and n.co_name == "<generic parameters of " + function.name + ">"
        and (n.co_firstlineno == function.lineno)
    ]
    if len(scopes) != 1:
        return (None, "generic parameter compiler scope changed")
    codes = [
        n
        for n in scopes[0].co_consts
        if isinstance(n, types.CodeType) and n.co_name == function.name and (n.co_firstlineno == function.lineno)
    ]
    if len(codes) != 1:
        return (None, "generic coroutine compiler scope changed")
    return (codes[0], None)


def route_helper_local_contracts(units):
    """Compose route1 and exact wrapper/join transfers; runtime remains UNKNOWN."""
    units = list(units)
    failures, receipt = _route1_local_contracts(units)
    keys = (
        "wrapper_definitions",
        "wrapper_formals",
        "task_supplier_calls",
        "join_calls",
        "join_result_stores",
        "wrapper_return_nodes",
        "shield_calls",
        "current_task_calls",
        "task_result_calls",
        "join_return_nodes",
        "wrapper_import_nodes",
        "generic_invocation_kinds",
        "join_formals",
        "task_member_calls",
        "owner_store_nodes",
    )
    receipt.update({key: [] for key in keys})
    receipt["effect_dependencies"].extend(
        [
            {"identity": name, "qualification": "UNKNOWN", "requirement": requirement}
            for name, requirement in (
                ("LOCAL_REQUIRED_AUDIT_BINDING", "actual local callable/code object is the pinned generic coroutine, without rebinding"),
                ("IMPORTED_JOIN_FREEFORM_BINDING", "actual imported callable/code origin is the selected helper, without replacement"),
                (
                    "ASYNCIO_NORMAL_TASK_SUPPLIERS",
                    "actual asyncio.create_task/current_task/shield have normal pinned Python runtime semantics",
                ),
                (
                    "TASK_NORMAL_JOIN_MEMBERS",
                    "actual supplied Task.done/cancelled/result and owner.cancelling/uncancel have normal runtime semantics, no descriptor/member substitutions",
                ),
                (
                    "WRAPPER_JOIN_RESULT",
                    "actual coroutine work returns or raises according to pinned helper; exact joined result and cancellation objects retained, no outside writes",
                ),
            )
        ]
    )
    if failures:
        return (failures, receipt)
    index = {str(unit.path): unit for unit in units}
    selected = {}
    for path, name in ((TURN, "_required_audit"), (HELPERS, "_join_freeform_owned_task")):
        tree = index[path].tree
        function, _ = find(tree, name)
        if not isinstance(function, ast.AsyncFunctionDef) or grammar(function) != WRAPPER_GRAMMARS[name]:
            failures.append("route helpers: complete wrapper/join grammar changed " + name)
            continue
        imports = [n for n in tree.body if isinstance(n, ast.Import) and any(a.name == "asyncio" for a in n.names)]
        if len(imports) != 1 or [(a.name, a.asname) for a in imports[0].names] != [("asyncio", None)]:
            failures.append("route helpers: exact asyncio module supplier import changed " + path)
            continue
        code, reason = generic_native_code(tree, function)
        mask = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        bits = None if code is None else code.co_flags & mask
        if bits != inspect.CO_COROUTINE:
            failures.append("route helpers: actual generic native coroutine kind changed " + name)
            continue
        selected[name] = function
        receipt["wrapper_definitions"].append((path, id(function)))
        receipt["wrapper_import_nodes"].append((path, id(imports[0])))
        receipt["generic_invocation_kinds"].append(
            {
                "definition": (path, id(function)),
                "kind_bits": bits,
                "compiler_reason": reason,
                "qualification": "SOURCE_KIND_ORIGIN_UNKNOWN",
            }
        )
    if failures:
        receipt["reserve_calls"] = []
        receipt["caller_calls"] = []
        return (failures, receipt)
    wrapper = selected["_required_audit"]
    join = selected["_join_freeform_owned_task"]
    calls = [n for n in ast.walk(wrapper) if isinstance(n, ast.Call) and chain(n.func) == ("_join_freeform_owned_task",)]
    suppliers = [n for n in ast.walk(wrapper) if isinstance(n, ast.Call) and chain(n.func) == ("asyncio", "create_task")]
    if (
        len(calls) != 1
        or len(suppliers) != 1
        or calls[0].args != [suppliers[0]]
        or calls[0].keywords
        or ([chain(n) for n in suppliers[0].args] != [("work",)])
        or (len(suppliers[0].keywords) != 1)
        or (suppliers[0].keywords[0].arg != "name")
        or (not isinstance(suppliers[0].keywords[0].value, ast.Constant))
        or (suppliers[0].keywords[0].value.value != "composer-required-audit")
    ):
        return (["route helpers: exact task dispatch/join changed"], receipt)
    stores = [n for n in ast.walk(wrapper) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Await) and (n.value.value is calls[0])]
    returns = [n for n in ast.walk(wrapper) if isinstance(n, ast.Return)]
    if (
        len(stores) != 1
        or len(stores[0].targets) != 1
        or (not isinstance(stores[0].targets[0], ast.Tuple))
        or ([chain(n) for n in stores[0].targets[0].elts] != [("result",), ("deferred",)])
        or (len(returns) != 1)
        or (chain(returns[0].value) != ("result",))
    ):
        return (["route helpers: exact joined result/return changed"], receipt)
    receipt["task_supplier_calls"] = [(TURN, id(suppliers[0]))]
    receipt["join_calls"] = [(TURN, id(calls[0]))]
    receipt["join_result_stores"] = [(TURN, id(stores[0]))]
    receipt["wrapper_return_nodes"] = [(TURN, id(returns[0]))]
    receipt["wrapper_formals"] = [(TURN, id(n)) for n in wrapper.args.args]
    for callee, key in (
        (("asyncio", "shield"), "shield_calls"),
        (("asyncio", "current_task"), "current_task_calls"),
        (("task", "result"), "task_result_calls"),
    ):
        matches = [n for n in ast.walk(join) if isinstance(n, ast.Call) and chain(n.func) == callee]
        if len(matches) != 1:
            return (["route helpers: actual join supplier/member changed"], receipt)
        receipt[key] = [(HELPERS, id(matches[0]))]
    normal_returns = [n for n in ast.walk(join) if isinstance(n, ast.Return)]
    if len(normal_returns) != 2 or not all(
        isinstance(n.value, ast.Tuple) and len(n.value.elts) == 2 and (chain(n.value.elts[1]) == ("first_cancellation",))
        for n in normal_returns
    ):
        return (["route helpers: exact task result/cancellation returns changed"], receipt)
    receipt["join_return_nodes"] = [(HELPERS, id(n)) for n in normal_returns]
    receipt["join_formals"] = [(HELPERS, id(n)) for n in (*join.args.args, *join.args.kwonlyargs)]
    member_calls = [
        n
        for n in ast.walk(join)
        if isinstance(n, ast.Call)
        and chain(n.func) in (("task", "done"), ("task", "cancelled"), ("task", "result"), ("owner", "cancelling"), ("owner", "uncancel"))
    ]
    if len(member_calls) != 5:
        return (["route helpers: exact owned task member set changed"], receipt)
    receipt["task_member_calls"] = [(HELPERS, id(n)) for n in member_calls]
    owner_stores = [n for n in ast.walk(join) if isinstance(n, ast.Assign) and len(n.targets) == 1 and (chain(n.targets[0]) == ("owner",))]
    if len(owner_stores) != 1:
        return (["route helpers: exact current owner store changed"], receipt)
    receipt["owner_store_nodes"] = [(HELPERS, id(owner_stores[0]))]
    return ([], receipt)
