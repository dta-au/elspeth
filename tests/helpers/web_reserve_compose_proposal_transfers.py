"""Finite local AST transfer receipts; common receiver/effect proof remains required."""

import ast
import copy
import hashlib
import json
from types import SimpleNamespace

TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/composer/service.py"
PROPOSALS = "src/elspeth/web/sessions/routes/composer/proposals.py"
SETTLEMENT = "src/elspeth/web/sessions/routes/composer/pipeline_settlement.py"


def _node(unit, path):
    body = unit.tree.body
    for part in path.split("."):
        matches = [n for n in body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
        if len(matches) != 1:
            return None
        current = matches[0]
        body = current.body
    return current


def _shape(node):
    if node is None:
        return None
    return ast.dump(node, include_attributes=False)


def _statement(source):
    return ast.parse(source).body[0]


def _exact(node, source):
    return _shape(node) == _shape(_statement(source))


def _calls(method, predicate):
    def executable(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            # The selected function is invoked later; its decorator, defaults,
            # type parameters and annotations are definition-time expressions,
            # not its executable body. A nested definition's body is not an
            # occurrence in its enclosing function.
            if node is method:
                for statement in node.body:
                    yield from executable(statement)
            return
        if isinstance(node, ast.TypeAlias):
            # PEP 695's alias value is resolved lazily, so it cannot stand in
            # for an actual call in the selected executed statement list.
            return
        yield node
        children = ([node.value] if node.value is not None else []) if isinstance(node, ast.AnnAssign) else ast.iter_child_nodes(node)
        for child in children:
            yield from executable(child)

    return [n for n in executable(method) if isinstance(n, ast.Call) and predicate(n)]


def _nested_body_calls(method, predicate):
    found = []
    for nested in ast.walk(method):
        if nested is method:
            continue
        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.extend(_calls(nested, predicate))
            headers = [*nested.decorator_list, *nested.args.defaults, *(value for value in nested.args.kw_defaults if value is not None)]
        elif isinstance(nested, ast.ClassDef):
            headers = [*nested.decorator_list, *nested.bases, *(value.value for value in nested.keywords), *nested.body]
        elif isinstance(nested, ast.Lambda):
            headers = [nested.body, *nested.args.defaults, *(value for value in nested.args.kw_defaults if value is not None)]
        else:
            continue
        found.extend(call for header in headers for call in ast.walk(header) if isinstance(call, ast.Call) and predicate(call))
    return found


def _executed_path(method, selected):
    """Return actual statement-list ancestry, refusing an unreviewed wrapper."""
    parents = {}
    for parent in ast.walk(method):
        for field, children in ast.iter_fields(parent):
            if isinstance(children, ast.AST):
                parents[id(children)] = (parent, field, None)
            elif isinstance(children, list):
                for index, child in enumerate(children):
                    if isinstance(child, ast.AST):
                        parents[id(child)] = (parent, field, index)
    path = []
    current = selected
    while current is not method:
        record = parents.get(id(current))
        if record is None:
            return ()
        parent, field, index = record
        if field == "body" and index is not None and any(isinstance(earlier, (ast.Return, ast.Raise)) for earlier in parent.body[:index]):
            return ()
        path.append((type(parent).__name__, field, index, type(current).__name__))
        current = parent
    return tuple(reversed(path))


def _direct_prefix_has_terminal(method, before):
    """A same-list terminal before the call prevents the claimed guard path."""
    return any(isinstance(statement, (ast.Return, ast.Raise)) for statement in method.body[:before])


def _formal(method, name, *, keyword_only):
    args = method.args.kwonlyargs if keyword_only else method.args.args
    return sum(argument.arg == name for argument in args) == 1 and len(_lexical_bindings(method, name)) == 1


def _keyword(call, name):
    values = [k.value for k in call.keywords if k.arg == name]
    return values[0] if len(values) == 1 else None


def _lexical_bindings(method, name):
    # A closure/class can carry a surprising name shadow or an evaluated
    # definition-time binder. Refuse all additional stores, not only top-level.
    bound = []
    for node in ast.walk(method):
        if (
            (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)))
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not method and node.name == name)
            or (isinstance(node, ast.arg) and node.arg == name)
        ):
            bound.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.extend(alias for alias in node.names if (alias.asname or alias.name.split(".")[0]) == name)
        elif (
            (isinstance(node, ast.ExceptHandler) and node.name == name)
            or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
            or (isinstance(node, ast.MatchMapping) and node.rest == name)
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
            or (isinstance(node, (ast.TypeVar, ast.TypeVarTuple, ast.ParamSpec)) and node.name == name)
        ):
            bound.append(node)
    return bound


def _source3_local_transfers(units):
    """Return exact call IDs only with the actual guarded producers intact.

    These local receipts do not establish import/receiver identity, method
    lookup, app.state stability, or arbitrary intervening callback effects.
    """
    selected = {}
    for unit in units:
        if unit.path in {TURN, SERVICE, PROPOSALS, SETTLEMENT}:
            if unit.path in selected:
                return ["composer transfer source duplicated " + unit.path], None
            selected[unit.path] = unit
    if set(selected) != {TURN, SERVICE, PROPOSALS, SETTLEMENT}:
        return ["composer transfer source missing"], None
    turn = _node(selected[TURN], "_run_composer_turn")
    compose_class = _node(selected[SERVICE], "ComposerServiceImpl")
    compose = _node(selected[SERVICE], "ComposerServiceImpl.compose")
    accept = _node(selected[PROPOSALS], "accept_composition_proposal")
    settle = _node(selected[SETTLEMENT], "settle_pipeline_proposal_under_compose_lock")
    if any(x is None for x in (turn, compose_class, compose, accept, settle)):
        return ["composer transfer exact producer or callee missing"], None
    if any(n.decorator_list or n.type_params for n in (turn, compose, settle)) or accept.type_params:
        return ["composer transfer decorator or type parameter unproved"], None
    route_decorator = ast.parse(
        "router.post('/{session_id}/proposals/{proposal_id}/accept', response_model=CompositionProposalResponse)", mode="eval"
    ).body
    if len(accept.decorator_list) != 1 or _shape(accept.decorator_list[0]) != _shape(route_decorator):
        return ["proposal route decorator or dispatch binding changed"], None
    if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for method in (turn, compose, accept, settle) for n in ast.walk(method)):
        return ["composer transfer generator execution unproved"], None
    if compose_class.bases or compose_class.keywords or compose_class.decorator_list or compose_class.type_params:
        return ["composer service normal class lookup unproved"], None
    if any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in {"__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__get__"}
        for n in compose_class.body
    ):
        return ["composer service custom lookup unproved"], None
    if any(_lexical_bindings(statement, "compose") for statement in compose_class.body if statement is not compose):
        return ["composer service compose lookup rebound"], None
    if len(turn.body) < 36 or not all(isinstance(turn.body[i], ast.Assign) for i in (17, 18)):
        return ["composer turn binding position changed"], None
    if not _exact(
        turn.body[8],
        "if type(required_work) is not RequiredWorkCoordinator:\n"
        "    raise AuditIntegrityError('Composer turn requires an owned required-work coordinator')",
    ):
        return ["composer turn nominal coordinator guard changed"], None
    if not _exact(turn.body[9], "lease.bind_required_work(required_work)"):
        return ["composer turn owned lease binding changed"], None
    if not _exact(turn.body[17], "provider_binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running)"):
        return ["composer turn exact provider binding producer changed"], None
    if not _exact(turn.body[18], "provider_owner = ProviderInvocationOwner(service=service, required_work=provider_binding)"):
        return ["composer turn exact provider owner producer changed"], None
    if len(_lexical_bindings(turn, "provider_binding")) != 1 or len(_lexical_bindings(turn, "provider_owner")) != 1:
        return ["composer turn provider owner or binding rebound"], None
    if len(_lexical_bindings(turn, "required_work")) != 2 or not _exact(
        turn.body[10],
        "if turn.budget_seconds != budget_anchor.remaining_at_running_seconds:\n"
        '    raise ValueError("composer turn budget_seconds is not its budget anchor\'s remaining_at_running_seconds")',
    ):
        return ["composer turn retained coordinator or budget prefix changed"], None
    composer_assignments = [
        n
        for n in ast.walk(turn)
        if isinstance(n, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "composer" for target in n.targets)
    ]
    if (
        len(composer_assignments) != 1
        or not _exact(composer_assignments[0], "composer = services.composer_service")
        or len(_lexical_bindings(turn, "composer")) != 1
    ):
        return ["composer app-services receiver transfer changed"], None

    def compose_predicate(n):
        return (
            isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "composer"
            and n.func.attr == "compose"
        )

    if _nested_body_calls(turn, compose_predicate):
        return ["composer call in unreviewed nested executable body"], None
    compose_calls = _calls(turn, compose_predicate)
    if len(compose_calls) != 1:
        return ["composer turn compose call universe changed"], None
    compose_call = compose_calls[0]
    if _executed_path(turn, compose_call) != (
        ("AsyncFunctionDef", "body", 35, "Try"),
        ("Try", "body", 12, "Try"),
        ("Try", "body", 0, "Assign"),
        ("Assign", "value", None, "Await"),
        ("Await", "value", None, "Call"),
    ) or _direct_prefix_has_terminal(turn, 35):
        return ["composer call executed path or preceding terminal changed"], None
    if (
        _shape(_keyword(compose_call, "required_work")) != _shape(ast.Name(id="provider_binding", ctx=ast.Load()))
        or _shape(_keyword(compose_call, "provider_owner")) != _shape(ast.Name(id="provider_owner", ctx=ast.Load()))
        or _shape(_keyword(compose_call, "session_operation_context"))
        != _shape(ast.Attribute(value=ast.Name(id="lease", ctx=ast.Load()), attr="context", ctx=ast.Load()))
    ):
        return ["composer turn exact paired call arguments changed"], None
    if not isinstance(turn.body[35], ast.Try) or compose_call not in tuple(ast.walk(turn.body[35])):
        return ["composer call no longer follows provider producer"], None
    if len(compose.body) < 5 or not _exact(
        compose.body[1],
        "if (required_work is None) != (provider_owner is None):\n"
        "    raise AuditIntegrityError('Required Composer provider ownership must be supplied with its turn binding')",
    ):
        return ["composer pair admission guard changed"], None
    if not (
        isinstance(compose.body[0], ast.Expr)
        and isinstance(compose.body[0].value, ast.Constant)
        and type(compose.body[0].value.value) is str
    ):
        return ["composer pre-admission executable prefix changed"], None
    if not all(_formal(compose, name, keyword_only=False) for name in ("required_work", "provider_owner", "session_operation_context")):
        return ["composer callee formal owner transfer changed"], None
    if not _exact(
        compose.body[2],
        "if provider_owner is not None:\n"
        "    if type(provider_owner) is not ProviderInvocationOwner or provider_owner.service is not self._require_sessions_service():\n"
        "        raise AuditIntegrityError('Composer provider owner belongs to another exact service')\n"
        "    if provider_owner.required_work is not required_work:\n"
        "        raise AuditIntegrityError('Composer provider owner replaced its actual turn binding')\n"
        "    provider_owner.required_work.validate_context(session_operation_context)",
    ):
        return ["composer owner, service, binding or context admission changed"], None

    def proposal_predicate(n):
        return isinstance(n.func, ast.Name) and n.func.id == "settle_pipeline_proposal_under_compose_lock"

    if _nested_body_calls(accept, proposal_predicate):
        return ["proposal settlement call in unreviewed nested executable body"], None
    proposal_calls = _calls(accept, proposal_predicate)
    if len(proposal_calls) != 1:
        return ["proposal settlement call universe changed"], None
    proposal_call = proposal_calls[0]
    if _executed_path(accept, proposal_call) != (
        ("AsyncFunctionDef", "body", 2, "AsyncWith"),
        ("AsyncWith", "body", 5, "If"),
        ("If", "body", 3, "Try"),
        ("Try", "body", 0, "Assign"),
        ("Assign", "value", None, "Await"),
        ("Await", "value", None, "Call"),
    ) or _direct_prefix_has_terminal(accept, 2):
        return ["proposal call executed path or preceding terminal changed"], None
    if (
        len(accept.body) < 3
        or not isinstance(accept.body[2], ast.AsyncWith)
        or len(accept.body[2].body) < 6
        or not isinstance(accept.body[2].body[5], ast.If)
        or _shape(accept.body[2].body[5].test) != _shape(ast.parse("pipeline_authority is not None", mode="eval").body)
    ):
        return ["proposal route owned branch changed"], None
    branch = accept.body[2].body[5]
    if (
        len(branch.body) < 4
        or not _exact(
            branch.body[0],
            "required_work = RequiredWorkCoordinator(RequiredWorkAuthority("
            "RequiredAuthorityKind.MANUAL_PROPOSAL, lease.context, proposal_id=str(proposal.id), "
            "invocation_id=str(uuid4()), tool_call_id=proposal.tool_call_id))",
        )
        or not _exact(branch.body[1], "lease.bind_required_work(required_work)")
        or not isinstance(branch.body[3], ast.Try)
        or proposal_call not in tuple(ast.walk(branch.body[3]))
        or len(_lexical_bindings(accept, "required_work")) != 1
    ):
        return ["proposal required-work producer or call dominance changed"], None
    if (
        _shape(_keyword(proposal_call, "required_work")) != _shape(ast.Name(id="required_work", ctx=ast.Load()))
        or _shape(_keyword(proposal_call, "required_binding"))
        != _shape(ast.parse("RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN)", mode="eval").body)
        or _shape(_keyword(proposal_call, "session_operation_context"))
        != _shape(ast.Attribute(value=ast.Name(id="lease", ctx=ast.Load()), attr="context", ctx=ast.Load()))
    ):
        return ["proposal exact paired settlement arguments changed"], None
    if (
        len(settle.body) < 6
        or not all(_formal(settle, name, keyword_only=True) for name in ("required_work", "required_binding", "session_operation_context"))
        or _direct_prefix_has_terminal(settle, 5)
        or not _exact(settle.body[2], "child: RequiredWorkCoordinator | None = None")
        or not _exact(settle.body[3], "producer = None")
        or not _exact(settle.body[4], "child_binding = None")
        or not _exact(
            settle.body[0],
            "if required_binding is not None and type(required_binding) is not RequiredWorkBinding:\n"
            "    raise AuditIntegrityError('Pipeline settlement requires an owned parent binding')",
        )
    ):
        return ["proposal nominal binding admission changed"], None
    if not _exact(
        settle.body[1],
        "if required_work is not None:\n"
        "    if type(required_work) is not RequiredWorkCoordinator:\n"
        "        raise AuditIntegrityError('Pipeline settlement requires an owned required-work coordinator')\n"
        "    if required_work.authority.context != session_operation_context:\n"
        "        raise AuditIntegrityError('Pipeline required-work scope disagrees with context')",
    ):
        return ["proposal coordinator and context admission changed"], None
    if not _exact(
        settle.body[5],
        "if required_work is not None:\n"
        "    parent_binding = RequiredWorkBinding(required_work, 0, 0, RequiredWorkRole.TURN, running) if required_binding is None else required_binding\n"
        "    if parent_binding.coordinator is not required_work:\n"
        "        raise AuditIntegrityError('Pipeline parent binding has a foreign coordinator')\n"
        "    parent_binding.validate_context(session_operation_context)\n"
        "    child, producer = required_work.begin_proposal_child(str(authority.row.id), authority.row.tool_call_id, transition_ordinal=parent_binding.transition_ordinal, semantic_ordinal=parent_binding.semantic_ordinal)\n"
        "    child_binding = RequiredWorkBinding(child, parent_binding.transition_ordinal, parent_binding.semantic_ordinal, parent_binding.role, parent_binding.running)",
    ):
        return ["proposal parent binding and child issuance changed"], None
    return [], {
        "validated_transfer_nodes": frozenset({(TURN, id(compose_call)), (PROPOSALS, id(proposal_call))}),
        "common_unknown": (
            "composer.app.state receiver and normal method binding",
            "import and callable identity of proposal settlement",
            "FastAPI route decorator registration and normal callable transfer",
            "outside writes, descriptors and intervening effects on retained owners",
            "exact class, binding, coordinator, authority and context suppliers",
        ),
    }


# These four complete executed bodies, their definition headers, and the exact
# selected Call grammars are reviewed as one finite producer contract. The
# hashes bind structural ASTs, not import identity or physical custody; those
# remain explicit common-boundary obligations in the returned receipt.
_REVIEWED_METHOD_SHAPES = {
    TURN: "1be57b700fbae1925ba639b5e4003307096cd60b46445da1573f555cb3fe2671",
    SERVICE: "22eeb587214b5fde4cae2398efb2de9191e491d8ce9add29960a0f1203d97cb8",
    PROPOSALS: "23ef777eef8f72ed82e5943f633d94fdaa6d4f76cab9bd7ef9ec642adc59a8ca",
    SETTLEMENT: "7c7465be32aeab7821c08a1a511541f23bb394aa39cd756b2f2215cda3cde599",
}
_REVIEWED_ANNOTATIONS = {TURN: frozenset({"assistant_write", "parent_assistant_id"})}
_METHOD_PATHS = {
    TURN: "_run_composer_turn",
    SERVICE: "ComposerServiceImpl.compose",
    PROPOSALS: "accept_composition_proposal",
    SETTLEMENT: "settle_pipeline_proposal_under_compose_lock",
}


def _canonical_ast(value):
    """Serialize all nonempty AST fields identically on Python 3.12/3.13."""
    if isinstance(value, ast.AST):
        if isinstance(value, ast.Call):
            # A literal empty keyword spread adds no binding or producer.
            # Every nonempty or computed spread remains part of the shape.
            call = copy.copy(value)
            call.keywords = [
                keyword
                for keyword in value.keywords
                if not (keyword.arg is None and isinstance(keyword.value, ast.Dict) and not keyword.value.keys)
            ]
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "compose"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "composer"
            ):
                call.keywords = [
                    ast.keyword(arg="progress", value=ast.Constant(value="<unproved advisory effect>"))
                    if keyword.arg == "progress"
                    else keyword
                    for keyword in call.keywords
                ]
            value = call
        return [
            type(value).__name__,
            [[name, _canonical_ast(field)] for name, field in ast.iter_fields(value) if field is not None and field != []],
        ]
    if isinstance(value, list):
        return [_canonical_ast(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_ast(item) for item in value]
    return value


def _method_shape(method):
    # Prose may change without changing the executable program. Every other
    # field, including the entire signature and every nested branch, is bound.
    header = copy.copy(method)
    header.body = list(method.body)
    if (
        header.body
        and isinstance(header.body[0], ast.Expr)
        and isinstance(header.body[0].value, ast.Constant)
        and type(header.body[0].value.value) is str
    ):
        header.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(_canonical_ast(header), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _inert_annotation(method, statement, path):
    if isinstance(statement, ast.AnnAssign):
        if (
            statement.value is not None and not (isinstance(statement.value, ast.Constant) and statement.value.value is None)
        ) or not isinstance(statement.target, ast.Name):
            return False
        target = statement.target
        if target.id in _REVIEWED_ANNOTATIONS.get(path, ()):
            return False
    elif isinstance(statement, ast.TypeAlias):
        if not isinstance(statement.name, ast.Name):
            return False
        target = statement.name
    else:
        return False
    name = target.id
    # A local annotation under future annotations executes neither its type
    # expression nor a value assignment. It is inert only when its new local
    # name is not read, rebound, captured, or otherwise observed in the body.
    references = [node for node in ast.walk(method) if isinstance(node, ast.Name) and node.id == name]
    if references != [target]:
        return False
    return not any(isinstance(node, ast.arg) and node.arg == name for node in ast.walk(method))


def _strip_inert_annotations(method, path):
    def visit(node):
        changed = False
        updates = {}
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                items = []
                for item in value:
                    if isinstance(item, ast.AST) and _inert_annotation(method, item, path):
                        changed = True
                        continue
                    if isinstance(item, ast.AST):
                        next_item = visit(item)
                        changed |= next_item is not item
                        item = next_item
                    items.append(item)
                if changed:
                    updates[field] = items
            elif isinstance(value, ast.AST):
                next_value = visit(value)
                if next_value is not value:
                    changed = True
                    updates[field] = next_value
        if not changed:
            return node
        clone = copy.copy(node)
        for field, value in updates.items():
            setattr(clone, field, value)
        return clone

    return visit(method)


def compose_proposal_local_transfers(units):
    """Bind the full four-method execution grammar before local receipts."""
    selected = {}
    for unit in units:
        if unit.path not in _METHOD_PATHS:
            continue
        if unit.path in selected:
            return ["composer transfer source duplicated " + unit.path], None
        selected[unit.path] = unit
    if set(selected) != set(_METHOD_PATHS):
        return ["composer transfer source missing"], None
    normalized = {}
    for path, unit in selected.items():
        if not any(
            isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
            for node in unit.tree.body
        ):
            return ["composer inert annotation execution mode changed " + path], None
        method = _node(unit, _METHOD_PATHS[path])
        if method is None:
            return ["composer transfer method missing " + path], None
        normal_method = _strip_inert_annotations(method, path)
        try:
            shape = _method_shape(normal_method)
        except (TypeError, ValueError, OverflowError):
            return ["composer unsupported executable AST literal " + path], None
        if shape != _REVIEWED_METHOD_SHAPES[path]:
            return ["composer complete executed method grammar changed " + path], None
        if path == TURN and len(_lexical_bindings(method, "lease")) != 1:
            return ["composer retained lease lexical binding changed"], None
        tree = copy.copy(unit.tree)
        tree.body = list(tree.body)
        if "." in _METHOD_PATHS[path]:
            class_name = _METHOD_PATHS[path].split(".")[0]
            original_class = _node(unit, class_name)
            class_clone = copy.copy(original_class)
            class_clone.body = [normal_method if node is method else node for node in original_class.body]
            tree.body = [class_clone if node is original_class else node for node in tree.body]
        else:
            tree.body = [normal_method if node is method else node for node in tree.body]
        normalized[path] = SimpleNamespace(path=path, tree=tree)
    failures, receipt = _source3_local_transfers(tuple(normalized.values()))
    if failures:
        return failures, None
    return [], receipt
