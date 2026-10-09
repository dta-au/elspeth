"""Private bounded AST contracts; an empty local result is not admission.

Contracts enumerate complete executed bodies, argument binding, guard order,
all branch arms and effects. They are explicit opcode trees, not source hashes
or selected snippets. An outside origin/effect proof must protect the returned
dependencies before these local relationships can become certificates.
"""

import ast
import builtins
import copy


def n(name):
    return ast.Name(id=name, ctx=ast.Load())


def c(value):
    return ast.Constant(value=value)


def a(value, member):
    return ast.Attribute(value=value, attr=member, ctx=ast.Load())


def s(member):
    return a(n("self"), member)


def call(function, *args, **kwargs):
    return ast.Call(
        func=n(function) if isinstance(function, str) else function,
        args=list(args),
        keywords=[ast.keyword(arg=k, value=v) for k, v in kwargs.items()],
    )


def cmp(left, op, right):
    return ast.Compare(left=left, ops=[op()], comparators=[right])


def either(*values):
    return ast.BoolOp(op=ast.Or(), values=list(values))


def both(*values):
    return ast.BoolOp(op=ast.And(), values=list(values))


def neg(value):
    return ast.UnaryOp(op=ast.Not(), operand=value)


def seq(*values):
    return ast.Tuple(elts=list(values), ctx=ast.Load())


def index(value, key):
    return ast.Subscript(value=value, slice=key, ctx=ast.Load())


def store(expression):
    if isinstance(expression, (ast.Name, ast.Attribute, ast.Subscript, ast.Tuple, ast.List)):
        expression.ctx = ast.Store()
        if isinstance(expression, (ast.Tuple, ast.List)):
            for child in expression.elts:
                store(child)
        return expression
    raise ValueError("unsupported contract target")


def assign(target, value):
    return ast.Assign(targets=[store(target)], value=value, type_comment=None)


def annotate(target, annotation, value):
    return ast.AnnAssign(target=store(target), annotation=annotation, value=value, simple=int(isinstance(target, ast.Name)))


def expr(value):
    return ast.Expr(value=value)


def ret(value=None):
    return ast.Return(value=value)


def raising(kind):
    # Error prose does not establish capability identity. Its shape is checked
    # separately: precisely one literal string, no keyword or cause effects.
    return ("raise-literal-error", kind)


def iff(test, body, orelse=()):
    return ("if", test, list(body), list(orelse))


def with_(context, body):
    return ("with", context, list(body))


def await_(value):
    return ast.Await(value=value)


def aug(target):
    return ast.AugAssign(target=store(target), op=ast.Add(), value=c(1))


def generator(element, target, iterable):
    return ast.GeneratorExp(elt=element, generators=[ast.comprehension(target=store(n(target)), iter=iterable, ifs=[], is_async=0)])


def list_(*values):
    return ast.List(elts=list(values), ctx=ast.Load())


def union(left, right):
    return ast.BinOp(left=left, op=ast.BitOr(), right=right)


def delete(*names):
    return ast.Delete(targets=[ast.Name(id=name, ctx=ast.Del()) for name in names])


def try_(body, handlers, orelse=(), finalbody=()):
    return ("try-full", list(body), list(handlers), list(orelse), list(finalbody))


def handler(kind, name, body):
    return (kind, name, list(body))


def while_(test, body):
    return ("while", test, list(body))


def _same(actual, expected):
    if isinstance(expected, tuple) and expected and isinstance(expected[0], str):
        if expected[0] == "raise-literal-error":
            return (
                isinstance(actual, ast.Raise)
                and actual.cause is None
                and isinstance(actual.exc, ast.Call)
                and _same(actual.exc.func, expected[1])
                and len(actual.exc.args) == 1
                and isinstance(actual.exc.args[0], ast.Constant)
                and type(actual.exc.args[0].value) is str
                and not actual.exc.keywords
            )
        if expected[0] == "if":
            return (
                isinstance(actual, ast.If)
                and _same(actual.test, expected[1])
                and _body_same(actual.body, expected[2])
                and _body_same(actual.orelse, expected[3])
            )
        if expected[0] == "with":
            return (
                isinstance(actual, ast.With)
                and actual.type_comment is None
                and len(actual.items) == 1
                and actual.items[0].optional_vars is None
                and _same(actual.items[0].context_expr, expected[1])
                and _body_same(actual.body, expected[2])
            )
        if expected[0] == "for":
            return (
                isinstance(actual, ast.For)
                and actual.type_comment is None
                and _same(actual.target, expected[1])
                and _same(actual.iter, expected[2])
                and _body_same(actual.body, expected[3])
                and not actual.orelse
            )
        if expected[0] == "try":
            return (
                isinstance(actual, ast.Try)
                and _body_same(actual.body, expected[1])
                and len(actual.handlers) == 1
                and _same(actual.handlers[0].type, n("BaseException"))
                and actual.handlers[0].name is None
                and _body_same(actual.handlers[0].body, expected[2])
                and not actual.orelse
                and not actual.finalbody
            )
        if expected[0] == "try-full":
            return (
                isinstance(actual, ast.Try)
                and _body_same(actual.body, expected[1])
                and len(actual.handlers) == len(expected[2])
                and all(
                    _same(actual_handler.type, kind) and actual_handler.name == name and _body_same(actual_handler.body, body)
                    for actual_handler, (kind, name, body) in zip(actual.handlers, expected[2], strict=True)
                )
                and _body_same(actual.orelse, expected[3])
                and _body_same(actual.finalbody, expected[4])
            )
        if expected[0] == "while":
            return (
                isinstance(actual, ast.While)
                and _same(actual.test, expected[1])
                and _body_same(actual.body, expected[2])
                and not actual.orelse
            )
        raise ValueError("unknown contract opcode")
    if isinstance(expected, ast.AST):
        return type(actual) is type(expected) and all(_same(getattr(actual, field), getattr(expected, field)) for field in expected._fields)
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(_same(x, y) for x, y in zip(actual, expected, strict=True))
    return type(actual) is type(expected) and actual == expected


def _body_same(actual, expected):
    # Pass and literal expression statements have no executed capability effect.
    retained = [
        statement
        for statement in actual
        if not isinstance(statement, ast.Pass) and not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
    ]
    return len(retained) == len(expected) and all(_same(x, y) for x, y in zip(retained, expected, strict=True))


def _find(tree, qualified):
    body = tree.body
    selected = None
    for part in qualified.split("."):
        matches = [node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == part]
        if len(matches) != 1:
            return None
        selected = matches[0]
        body = selected.body
    return selected


def _signature(fn, positional, keyword=(), defaults=(), kw_defaults=(), asynchronous=False, decorators=()):
    return (
        type(fn) is (ast.AsyncFunctionDef if asynchronous else ast.FunctionDef)
        and not fn.args.posonlyargs
        and [arg.arg for arg in fn.args.args] == list(positional)
        and [arg.arg for arg in fn.args.kwonlyargs] == list(keyword)
        and fn.args.vararg is None
        and fn.args.kwarg is None
        and _same(fn.args.defaults, list(defaults))
        and _same(fn.args.kw_defaults, list(kw_defaults))
        and _same(fn.decorator_list, [n(value) for value in decorators])
        and not fn.type_params
        and fn.type_comment is None
    )


class _ExecutionScope(ast.NodeVisitor):
    """Own function scope, including string-valued exception/pattern bindings.

    Nested function/lambda/class bodies and comprehension targets have distinct
    binding scopes. Definition expressions still execute in the enclosing
    scope; comprehension assignment expressions retain their enclosing binding.
    """

    def __init__(self, *, postponed_annotations):
        self.bindings = set()
        self.redirected = set()
        self.yields = []
        self.postponed_annotations = postponed_annotations

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bindings.add(node.id)

    def visit_ExceptHandler(self, node):
        if node.name is not None:
            self.bindings.add(node.name)
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node):
        if node.name is not None:
            self.bindings.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node):
        if node.name is not None:
            self.bindings.add(node.name)

    def visit_MatchMapping(self, node):
        if node.rest is not None:
            self.bindings.add(node.rest)
        for key in node.keys:
            self.visit(key)
        for pattern in node.patterns:
            self.visit(pattern)

    def visit_Import(self, node):
        self.bindings.update(alias.asname or alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node):
        self.bindings.update(alias.asname or alias.name for alias in node.names)

    def visit_Global(self, node):
        self.redirected.update(node.names)

    def visit_Nonlocal(self, node):
        self.redirected.update(node.names)

    def visit_Yield(self, node):
        self.yields.append(node)
        if node.value is not None:
            self.visit(node.value)

    visit_YieldFrom = visit_Yield

    def _function_definition(self, node):
        self.bindings.add(node.name)
        for expression in (*node.decorator_list, *node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
            self.visit(expression)
        if not self.postponed_annotations:
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *((node.args.vararg,) if node.args.vararg is not None else ()),
                *((node.args.kwarg,) if node.args.kwarg is not None else ()),
            ):
                if arg.annotation is not None:
                    self.visit(arg.annotation)
            if node.returns is not None:
                self.visit(node.returns)

    visit_FunctionDef = _function_definition
    visit_AsyncFunctionDef = _function_definition

    def visit_Lambda(self, node):
        for expression in (*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)):
            self.visit(expression)

    def visit_ClassDef(self, node):
        self.bindings.add(node.name)
        for expression in (*node.decorator_list, *node.bases, *(keyword.value for keyword in node.keywords)):
            self.visit(expression)

    def visit_comprehension(self, node):
        # Comprehension targets are separate locals. NamedExpr in these
        # expressions still binds in the containing function's scope.
        self.visit(node.iter)
        for condition in node.ifs:
            self.visit(condition)


def _postponed_annotations(unit):
    return any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in unit.tree.body
    )


def _definition_expressions(unit, fn):
    expressions = [*fn.decorator_list, *fn.args.defaults, *(d for d in fn.args.kw_defaults if d is not None)]
    if not _postponed_annotations(unit):
        expressions.extend(
            arg.annotation for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if arg.annotation is not None
        )
        if fn.args.vararg is not None and fn.args.vararg.annotation is not None:
            expressions.append(fn.args.vararg.annotation)
        if fn.args.kwarg is not None and fn.args.kwarg.annotation is not None:
            expressions.append(fn.args.kwarg.annotation)
        if fn.returns is not None:
            expressions.append(fn.returns)
    return expressions


def _definition_scope_failures(unit, fn):
    if _postponed_annotations(unit):
        return []
    annotations = [arg.annotation for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if arg.annotation is not None]
    if fn.returns is not None:
        annotations.append(fn.returns)
    supported = (ast.Name, ast.Load, ast.Attribute, ast.Subscript, ast.Tuple, ast.Constant, ast.BinOp, ast.BitOr)
    if any(not isinstance(node, supported) for annotation in annotations for node in ast.walk(annotation)):
        return ["evaluated definition annotation: unsupported executable effect/scope"]
    return []


def _binding_contracts():
    return {
        "RequiredWorkBinding.__post_init__": (
            ("self",),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(
                        cmp(call("type", s("coordinator")), ast.IsNot, n("RequiredWorkCoordinator")),
                        cmp(call("type", s("role")), ast.IsNot, n("RequiredWorkRole")),
                    ),
                    [raising(n("AuditIntegrityError"))],
                ),
                iff(
                    call(
                        "any",
                        generator(
                            either(cmp(call("type", n("value")), ast.IsNot, n("int")), cmp(n("value"), ast.Lt, c(0))),
                            "value",
                            seq(s("transition_ordinal"), s("semantic_ordinal")),
                        ),
                    ),
                    [raising(n("AuditIntegrityError"))],
                ),
            ],
        ),
        "RequiredWorkBinding.validate_context": (
            ("self", "context"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(
                    either(
                        cmp(call("type", n("context")), ast.IsNot, n("SessionOperationContext")),
                        cmp(a(a(s("coordinator"), "authority"), "context"), ast.NotEq, n("context")),
                    ),
                    [raising(n("AuditIntegrityError"))],
                ),
            ],
        ),
        "RequiredWorkBinding.reserve_pair": (
            ("self", "source", "projection"),
            (),
            (),
            (),
            False,
            (),
            [
                ret(
                    call(
                        a(s("coordinator"), "reserve_pair"),
                        n("source"),
                        n("projection"),
                        transition_ordinal=s("transition_ordinal"),
                        semantic_ordinal=s("semantic_ordinal"),
                    )
                ),
            ],
        ),
    }


def _provider_contracts():
    def reserve(source, recurrence):
        return call(
            a(a(n("binding"), "coordinator"), "reserve"),
            a(n("RequiredWorkSource"), source),
            transition_ordinal=a(n("binding"), "transition_ordinal"),
            semantic_ordinal=a(n("binding"), "semantic_ordinal"),
            recurrence_ordinal=s(recurrence),
        )

    return {
        "ProviderCallCustody.__init__": (
            ("self",),
            ("service", "context", "required_work"),
            (),
            (None, None, None),
            False,
            (),
            [
                iff(cmp(call("type", n("required_work")), ast.IsNot, n("RequiredWorkBinding")), [raising(n("AuditIntegrityError"))]),
                expr(call(a(n("required_work"), "validate_context"), n("context"))),
                iff(
                    cmp(a(n("context"), "operation_kind"), ast.IsNot, a(n("SessionOperationKind"), "COMPOSE")),
                    [raising(n("AuditIntegrityError"))],
                ),
                assign(s("_service"), n("service")),
                assign(s("_context"), n("context")),
                assign(s("_required_work"), n("required_work")),
                annotate(s("attempt"), ast.BinOp(left=n("ProviderAttempt"), op=ast.BitOr(), right=c(None)), c(None)),
                annotate(s("call"), ast.BinOp(left=n("ComposerLLMCall"), op=ast.BitOr(), right=c(None)), c(None)),
                assign(s("_sdk_entered"), c(False)),
                assign(s("_admitting"), c(False)),
                assign(s("_undispatched_recurrence"), c(0)),
                assign(s("_settlements"), c(0)),
            ],
        ),
        "ProviderCallCustody.required_work": (("self",), (), (), (), False, ("property",), [ret(s("_required_work"))]),
        "ProviderCallCustody.context": (("self",), (), (), (), False, ("property",), [ret(s("_context"))]),
        "ProviderCallCustody.service": (("self",), (), (), (), False, ("property",), [ret(s("_service"))]),
        "ProviderCallCustody._cancel_undispatched": (
            ("self",),
            ("model",),
            (),
            (None,),
            True,
            (),
            [
                iff(either(cmp(s("attempt"), ast.Is, c(None)), s("_sdk_entered")), [raising(n("AuditIntegrityError"))]),
                assign(n("binding"), s("required_work")),
                assign(n("ticket"), reserve("UNDISPATCHED_ATTEMPT_CANCELLATION_SQL", "_undispatched_recurrence")),
                aug(s("_undispatched_recurrence")),
                assign(
                    seq(n("_"), n("cancellations")),
                    await_(
                        call(
                            s("_join"),
                            call(
                                a(s("service"), "cancel_undispatched_provider_attempt"),
                                session_operation_context=s("context"),
                                attempt_id=a(s("attempt"), "attempt_id"),
                                requested_model=n("model"),
                                required_work=n("ticket"),
                            ),
                        )
                    ),
                ),
                assign(s("attempt"), c(None)),
                assign(s("call"), c(None)),
                ret(n("cancellations")),
            ],
        ),
        "ProviderCallCustody.settle": (
            ("self",),
            (),
            (),
            (),
            True,
            (),
            [
                iff(cmp(s("attempt"), ast.Is, c(None)), [ret()]),
                iff(either(neg(s("_sdk_entered")), cmp(s("call"), ast.Is, c(None))), [raising(n("AuditIntegrityError"))]),
                assign(n("binding"), s("required_work")),
                annotate(n("projection"), ast.BinOp(left=n("RequiredWorkTicket"), op=ast.BitOr(), right=c(None)), c(None)),
                iff(
                    cmp(a(n("binding"), "role"), ast.Is, a(n("RequiredWorkRole"), "TITLE")),
                    [
                        assign(n("sql"), reserve("TITLE_PROVIDER_SETTLEMENT_SQL", "_settlements")),
                        aug(s("_settlements")),
                    ],
                    [
                        assign(
                            seq(n("sql"), n("projection")),
                            call(
                                a(n("binding"), "reserve_pair"),
                                a(n("RequiredWorkSource"), "PROVIDER_SETTLEMENT_SQL"),
                                a(n("RequiredWorkSource"), "PROVIDER_SETTLEMENT_PROJECTION"),
                            ),
                        )
                    ],
                ),
                (
                    "try",
                    [
                        assign(
                            seq(n("_"), n("cancellations")),
                            await_(
                                call(
                                    s("_join"),
                                    call(
                                        a(s("service"), "finish_provider_attempt"),
                                        session_operation_context=s("context"),
                                        call=s("call"),
                                        required_work=n("sql"),
                                    ),
                                )
                            ),
                        )
                    ],
                    [
                        iff(
                            both(cmp(n("projection"), ast.IsNot, c(None)), a(n("sql"), "complete")),
                            [expr(call(a(n("projection"), "complete_without_submission")))],
                        ),
                        ast.Raise(exc=None, cause=None),
                    ],
                ),
                iff(
                    cmp(n("projection"), ast.IsNot, c(None)),
                    [expr(call(a(n("projection"), "begin_projection"))), expr(call(a(n("projection"), "complete_owned")))],
                ),
                assign(s("attempt"), c(None)),
                assign(s("call"), c(None)),
                assign(s("_sdk_entered"), c(False)),
                expr(call(s("raise_deferred_cancellations"), n("cancellations"))),
            ],
        ),
    }


def _telemetry_contracts():
    def descriptor(obj, name):
        return call("_class_descriptor", obj, c(name))

    def field(obj, name, owner):
        return call("_owned_field", n(obj), c(name), n(owner))

    def owned(obj, name, owner):
        return expr(call("_owned_method", n(obj), c(name), a(n(owner), name)))

    return {
        "_class_descriptor": (
            ("cls", "name"),
            (),
            (),
            (),
            False,
            (),
            [
                (
                    "for",
                    store(n("base")),
                    a(n("cls"), "__mro__"),
                    [
                        assign(n("namespace"), call("vars", n("base"))),
                        iff(cmp(n("name"), ast.In, n("namespace")), [ret(index(n("namespace"), n("name")))]),
                    ],
                ),
                raising(n("TelemetryCustodyUnresolved")),
            ],
        ),
        "_normal_lookup": (
            ("value",),
            (),
            (),
            (),
            False,
            (),
            [
                assign(n("cls"), call("type", n("value"))),
                iff(
                    either(
                        cmp(call("type", n("cls")), ast.IsNot, n("type")),
                        cmp(descriptor(n("cls"), "__getattribute__"), ast.IsNot, a(n("object"), "__getattribute__")),
                        cmp(descriptor(n("cls"), "__setattr__"), ast.IsNot, a(n("object"), "__setattr__")),
                        call("any", generator(cmp(c("__getattr__"), ast.In, call("vars", n("base"))), "base", a(n("cls"), "__mro__"))),
                        call("any", generator(cmp(c("__dict__"), ast.In, call("vars", n("base"))), "base", a(n("cls"), "__mro__"))),
                    ),
                    [raising(n("TelemetryCustodyUnresolved"))],
                ),
                ret(n("cls")),
            ],
        ),
        "_owned_field": (
            ("value", "name", "owner_class"),
            (),
            (),
            (),
            False,
            (),
            [
                assign(n("cls"), call("_normal_lookup", n("value"))),
                assign(n("expected"), call(a(call("vars", n("owner_class")), "get"), n("name"))),
                iff(
                    either(
                        cmp(call("type", n("expected")), ast.IsNot, n("MemberDescriptorType")),
                        cmp(call("_class_descriptor", n("cls"), n("name")), ast.IsNot, n("expected")),
                    ),
                    [raising(n("TelemetryCustodyUnresolved"))],
                ),
                ret(call(a(n("expected"), "__get__"), n("value"), n("cls"))),
            ],
        ),
        "_owned_method": (
            ("value", "name", "expected"),
            (),
            (),
            (),
            False,
            (),
            [
                assign(n("cls"), call("_normal_lookup", n("value"))),
                assign(n("descriptor"), call("_class_descriptor", n("cls"), n("name"))),
                iff(cmp(n("descriptor"), ast.IsNot, n("expected")), [raising(n("TelemetryCustodyUnresolved"))]),
                assign(n("selected"), call(a(n("object"), "__getattribute__"), n("value"), n("name"))),
                iff(
                    either(
                        cmp(call("type", n("selected")), ast.IsNot, n("MethodType")),
                        cmp(a(n("selected"), "__self__"), ast.IsNot, n("value")),
                        cmp(a(n("selected"), "__func__"), ast.IsNot, n("expected")),
                    ),
                    [raising(n("TelemetryCustodyUnresolved"))],
                ),
            ],
        ),
        "validate_telemetry_reservation_owner": (
            ("owner",),
            (),
            (),
            (),
            False,
            (),
            [
                assign(n("cls"), call("type", n("owner"))),
                iff(
                    either(
                        cmp(call("type", n("cls")), ast.IsNot, n("type")),
                        neg(call("issubclass", n("cls"), n("OperatorTelemetryCleanupOwner"))),
                    ),
                    [raising(n("TypeError"))],
                ),
                assign(n("cls"), call("_normal_lookup", n("owner"))),
                owned("owner", "assert_process", "OperatorTelemetryCleanupOwner"),
                assign(n("installation"), field("owner", "installation", "OperatorTelemetryCleanupOwner")),
                assign(n("owner_pid"), field("owner", "creator_pid", "OperatorTelemetryCleanupOwner")),
                assign(n("installation_cls"), call("_normal_lookup", n("installation"))),
                iff(neg(call("issubclass", n("installation_cls"), n("TelemetryInstallation"))), [raising(n("TelemetryCustodyUnresolved"))]),
                iff(neg(call("isinstance", n("installation"), n("TelemetryInstallation"))), [raising(n("TelemetryCustodyUnresolved"))]),
                assign(n("installation_pid"), field("installation", "creator_pid", "TelemetryInstallation")),
                assign(n("lock"), field("installation", "lock", "TelemetryInstallation")),
                assign(n("record"), field("installation", "record", "TelemetryInstallation")),
                iff(
                    either(
                        cmp(call("type", n("owner_pid")), ast.IsNot, n("int")),
                        cmp(call("type", n("installation_pid")), ast.IsNot, n("int")),
                    ),
                    [raising(n("TelemetryCustodyUnresolved"))],
                ),
                iff(cmp(call("type", n("lock")), ast.IsNot, n("RLock")), [raising(n("TelemetryCustodyUnresolved"))]),
                iff(
                    both(
                        cmp(n("record"), ast.IsNot, c(None)),
                        either(
                            cmp(call("type", n("record")), ast.IsNot, n("TelemetryInstallationRecord")),
                            cmp(call("type", a(n("record"), "state")), ast.IsNot, n("str")),
                        ),
                    ),
                    [raising(n("TelemetryCustodyUnresolved"))],
                ),
                owned("installation", "assert_process", "TelemetryInstallation"),
                owned("installation", "reserve", "TelemetryInstallation"),
                ret(n("installation")),
            ],
        ),
    }


def _installation_contracts():
    return {
        "TelemetryInstallation.__init__": (
            ("self",),
            (),
            (),
            (),
            False,
            (),
            [
                assign(s("creator_pid"), call(a(n("os"), "getpid"))),
                assign(s("lock"), call("RLock")),
                annotate(s("record"), ast.BinOp(left=n("TelemetryInstallationRecord"), op=ast.BitOr(), right=c(None)), c(None)),
                assign(s("registry"), n("REGISTRY")),
            ],
        ),
        "TelemetryInstallation.assert_process": (
            ("self",),
            (),
            (),
            (),
            False,
            (),
            [
                iff(cmp(s("creator_pid"), ast.NotEq, call(a(n("os"), "getpid"))), [raising(n("TelemetryCustodyUnresolved"))]),
            ],
        ),
        "TelemetryInstallation.reserve": (
            ("self", "owner"),
            (),
            (),
            (),
            False,
            (),
            [
                expr(call(s("assert_process"))),
                expr(call(a(n("owner"), "assert_process"))),
                with_(
                    s("lock"),
                    [
                        assign(n("record"), s("record")),
                        iff(
                            cmp(n("record"), ast.IsNot, c(None)),
                            [
                                iff(
                                    both(
                                        cmp(a(n("record"), "owner"), ast.Is, n("owner")),
                                        cmp(a(n("record"), "state"), ast.Eq, c("active")),
                                        cmp(a(n("record"), "runtime"), ast.IsNot, c(None)),
                                    ),
                                    [ret(a(n("record"), "runtime"))],
                                ),
                                raising(n("TelemetryCustodyUnresolved")),
                            ],
                        ),
                        assign(s("record"), call("TelemetryInstallationRecord", n("owner"), s("creator_pid"))),
                        ret(c(None)),
                    ],
                ),
            ],
        ),
    }


def _lease_contracts():
    error = a(n("contract_errors"), "AuditIntegrityError")
    return {
        "SessionOperationLease.__init__": (
            ("self", "authority", "context"),
            ("lease_seconds", "renew_interval_seconds", "fork_authority", "required_work", "execution_obligation"),
            (),
            (None, None, c(None), c(None), c(None)),
            False,
            (),
            [
                iff(
                    cmp(n("required_work"), ast.IsNot, c(None)),
                    [
                        iff(cmp(call("type", n("required_work")), ast.IsNot, n("RequiredWorkCoordinator")), [raising(error)]),
                        iff(cmp(a(a(n("required_work"), "authority"), "context"), ast.NotEq, n("context")), [raising(error)]),
                    ],
                ),
                iff(
                    cmp(n("execution_obligation"), ast.IsNot, c(None)),
                    [
                        iff(
                            either(
                                cmp(call("type", n("execution_obligation")), ast.IsNot, n("ExecutionAcquisitionObligation")),
                                cmp(a(n("execution_obligation"), "lease"), ast.IsNot, n("self")),
                            ),
                            [raising(error)],
                        ),
                        iff(
                            either(
                                cmp(n("context"), ast.IsNot, a(n("execution_obligation"), "context")),
                                cmp(n("required_work"), ast.IsNot, c(None)),
                            ),
                            [raising(error)],
                        ),
                    ],
                ),
                assign(s("_execution_obligation"), n("execution_obligation")),
                assign(s("_required_work"), n("required_work")),
                assign(s("_renewal_ordinal"), c(0)),
                assign(s("_authority"), n("authority")),
                assign(s("_context"), n("context")),
                assign(s("_lease_seconds"), n("lease_seconds")),
                assign(s("_renew_interval_seconds"), n("renew_interval_seconds")),
                assign(s("_fork_authority"), n("fork_authority")),
                assign(s("_stop_renewal"), call(a(n("asyncio"), "Event"))),
                assign(s("_lost_event"), call(a(n("asyncio"), "Event"))),
                annotate(s("_renewal_error"), union(n("BaseException"), c(None)), c(None)),
                annotate(s("_renewal_attempt"), union(n("_RenewalAttemptObservation"), c(None)), c(None)),
                annotate(s("_owned_tasks"), index(n("set"), index(a(n("asyncio"), "Task"), n("Any"))), call("set")),
                annotate(s("_close_task"), union(index(a(n("asyncio"), "Task"), c(None)), c(None)), c(None)),
                annotate(s("_finish_mode"), union(index(n("Literal"), seq(c("close"), c("consume"))), c(None)), c(None)),
                assign(s("_disposition"), a(n("SessionOperationLeaseDisposition"), "ACTIVE")),
                assign(s("_closed"), c(False)),
                annotate(s("_renewal_task"), union(index(a(n("asyncio"), "Task"), c(None)), c(None)), c(None)),
                iff(
                    cmp(n("execution_obligation"), ast.IsNot, c(None)),
                    [expr(call(a(n("execution_obligation"), "declare_renewal_allocation"), n("self")))],
                ),
                assign(
                    s("_renewal_task"), call(a(n("asyncio"), "create_task"), call(s("_renew_forever")), name=c("session-operation-renewal"))
                ),
            ],
        ),
        "SessionOperationLease.required_work": (("self",), (), (), (), False, ("property",), [ret(s("_required_work"))]),
        "SessionOperationLease.context": (("self",), (), (), (), False, ("property",), [ret(s("_context"))]),
        "SessionOperationLease.bind_required_work": (
            ("self", "coordinator"),
            (),
            (),
            (),
            False,
            (),
            [
                iff(cmp(call("type", n("coordinator")), ast.IsNot, n("RequiredWorkCoordinator")), [raising(error)]),
                iff(
                    either(
                        cmp(a(a(n("coordinator"), "authority"), "context"), ast.NotEq, s("context")),
                        both(cmp(s("_required_work"), ast.IsNot, c(None)), cmp(s("_required_work"), ast.IsNot, n("coordinator"))),
                    ),
                    [raising(error)],
                ),
                assign(s("_required_work"), n("coordinator")),
            ],
        ),
    }


def _adopt_contract():
    error = a(n("contract_errors"), "AuditIntegrityError")
    return (
        ("cls", "authority", "context"),
        ("lease_seconds", "renew_interval_seconds", "required_work", "cancellation_observations"),
        (),
        (None, c(None), c(None), c(None)),
        True,
        ("classmethod",),
        [
            iff(cmp(call("type", n("context")), ast.IsNot, n("SessionOperationContext")), [raising(n("TypeError"))]),
            try_(
                [
                    assign(
                        n("interval"),
                        call(
                            "_validate_lifecycle_timing",
                            lease_seconds=n("lease_seconds"),
                            renew_interval_seconds=n("renew_interval_seconds"),
                        ),
                    ),
                    iff(
                        cmp(n("required_work"), ast.IsNot, c(None)),
                        [
                            iff(cmp(call("type", n("required_work")), ast.IsNot, n("RequiredWorkCoordinator")), [raising(error)]),
                            iff(cmp(a(a(n("required_work"), "authority"), "context"), ast.NotEq, n("context")), [raising(error)]),
                        ],
                    ),
                ],
                [
                    handler(
                        n("BaseException"),
                        "validation_error",
                        [
                            assign(
                                n("validation_cleanup"),
                                call(
                                    "_raise_adopt_failure_after_release",
                                    n("authority"),
                                    n("context"),
                                    n("validation_error"),
                                    phase=c("validation"),
                                    cancellation_observations=n("cancellation_observations"),
                                ),
                            ),
                            delete("validation_error"),
                            expr(await_(n("validation_cleanup"))),
                        ],
                    )
                ],
            ),
            assign(
                n("adoption_ticket"),
                ast.IfExp(
                    test=cmp(n("required_work"), ast.IsNot, c(None)),
                    body=call(a(n("required_work"), "reserve"), a(n("RequiredWorkSource"), "LEASE_ADOPTION")),
                    orelse=c(None),
                ),
            ),
            assign(
                n("compare_and_swap_work"),
                ast.IfExp(
                    test=cmp(n("adoption_ticket"), ast.IsNot, c(None)),
                    body=call("run_required_sql_in_worker", n("adoption_ticket"), a(n("authority"), "compare_and_swap"), n("context")),
                    orelse=call("run_stream_read_in_worker", a(n("authority"), "compare_and_swap"), n("context")),
                ),
            ),
            assign(
                n("compare_and_swap_tasks"),
                list_(call(a(n("asyncio"), "create_task"), n("compare_and_swap_work"), name=c("session-operation-adopt-compare-and-swap"))),
            ),
            try_(
                [expr(await_(call(a(n("asyncio"), "shield"), index(n("compare_and_swap_tasks"), c(0)))))],
                [
                    handler(
                        a(n("asyncio"), "CancelledError"),
                        "cancellation",
                        [
                            iff(
                                cmp(n("cancellation_observations"), ast.IsNot, c(None)),
                                [expr(call("_retain_cancellation", n("cancellation_observations"), n("cancellation")))],
                            ),
                            assign(
                                n("cleanup_tasks"),
                                list_(
                                    call(
                                        a(n("asyncio"), "create_task"),
                                        call(
                                            "_finish_cancelled_adopt",
                                            n("authority"),
                                            index(n("compare_and_swap_tasks"), c(0)),
                                            n("context"),
                                        ),
                                        name=c("session-operation-cancelled-adopt-cleanup"),
                                    )
                                ),
                            ),
                            assign(
                                seq(n("cancelled_compare_and_swap_error"), n("cancelled_release_error")),
                                await_(
                                    call(
                                        "_join_shielded_task_after_cancellation",
                                        index(n("cleanup_tasks"), c(0)),
                                        cancellation_observations=n("cancellation_observations"),
                                    )
                                ),
                            ),
                            expr(
                                call(
                                    "_retain_adoption_cancellation_outcome",
                                    n("cancellation_observations"),
                                    n("cancelled_compare_and_swap_error"),
                                )
                            ),
                            expr(
                                call("_retain_adoption_cancellation_outcome", n("cancellation_observations"), n("cancelled_release_error"))
                            ),
                            assign(
                                n("escaping"),
                                call(
                                    "_failure_after_cancellation",
                                    n("cancellation"),
                                    seq(
                                        seq(
                                            c("Session-operation adoption cancellation compare-and-swap"),
                                            n("cancelled_compare_and_swap_error"),
                                        ),
                                        seq(c("Session-operation adoption cancellation release"), n("cancelled_release_error")),
                                    ),
                                    group_message=c("Session operation adoption cancellation integrity failures"),
                                ),
                            ),
                            delete("cancelled_compare_and_swap_error", "cancelled_release_error"),
                            expr(call(a(n("cleanup_tasks"), "clear"))),
                            expr(call(a(n("compare_and_swap_tasks"), "clear"))),
                            iff(
                                cmp(n("escaping"), ast.Is, n("cancellation")),
                                [
                                    assign(a(n("cancellation"), "__cause__"), c(None)),
                                    assign(a(n("cancellation"), "__context__"), c(None)),
                                    ast.Raise(exc=n("cancellation"), cause=c(None)),
                                ],
                            ),
                            ast.Raise(
                                exc=n("escaping"),
                                cause=ast.IfExp(
                                    test=cmp(a(n("escaping"), "__cause__"), ast.IsNot, c(None)),
                                    body=a(n("escaping"), "__cause__"),
                                    orelse=n("cancellation"),
                                ),
                            ),
                        ],
                    ),
                    handler(
                        n("BaseException"),
                        "compare_and_swap_error",
                        [
                            expr(call(a(n("compare_and_swap_tasks"), "clear"))),
                            assign(
                                n("compare_and_swap_cleanup"),
                                call(
                                    "_raise_adopt_failure_after_release",
                                    n("authority"),
                                    n("context"),
                                    n("compare_and_swap_error"),
                                    phase=c("compare-and-swap"),
                                    cancellation_observations=n("cancellation_observations"),
                                ),
                            ),
                            delete("compare_and_swap_error"),
                            expr(await_(n("compare_and_swap_cleanup"))),
                        ],
                    ),
                ],
            ),
            ret(
                call(
                    "cls",
                    n("authority"),
                    n("context"),
                    lease_seconds=n("lease_seconds"),
                    renew_interval_seconds=n("interval"),
                    required_work=n("required_work"),
                )
            ),
        ],
    )


def _renew_contract():
    error = a(n("contract_errors"), "AuditIntegrityError")
    return (
        ("self",),
        (),
        (),
        (),
        True,
        (),
        [
            assign(n("obligation"), s("_execution_obligation")),
            iff(
                cmp(n("obligation"), ast.IsNot, c(None)),
                [
                    assign(n("actual_task"), call(a(n("asyncio"), "current_task"))),
                    iff(cmp(n("actual_task"), ast.Is, c(None)), [raising(error)]),
                    expr(call(a(n("obligation"), "bind_actual_renewal_task"), n("self"), n("actual_task"))),
                    assign(s("_renewal_task"), n("actual_task")),
                ],
            ),
            while_(
                neg(call(a(s("_stop_renewal"), "is_set"))),
                [
                    with_(
                        call("suppress", n("TimeoutError")),
                        [
                            expr(
                                await_(
                                    call(
                                        a(n("asyncio"), "wait_for"),
                                        call(a(s("_stop_renewal"), "wait")),
                                        timeout=s("_renew_interval_seconds"),
                                    )
                                )
                            )
                        ],
                    ),
                    iff(call(a(s("_stop_renewal"), "is_set")), [ret()]),
                    try_(
                        [
                            iff(
                                cmp(s("_fork_authority"), ast.Is, c(None)),
                                [
                                    iff(
                                        cmp(s("_required_work"), ast.Is, c(None)),
                                        [
                                            assign(
                                                n("renewed"),
                                                await_(
                                                    call(
                                                        "run_stream_read_in_worker",
                                                        a(s("_authority"), "renew"),
                                                        s("_context"),
                                                        lease_seconds=s("_lease_seconds"),
                                                    )
                                                ),
                                            ),
                                        ],
                                        [
                                            assign(
                                                n("ticket"),
                                                call(
                                                    a(s("_required_work"), "reserve"),
                                                    a(n("RequiredWorkSource"), "LEASE_RENEWAL"),
                                                    recurrence_ordinal=s("_renewal_ordinal"),
                                                ),
                                            ),
                                            aug(s("_renewal_ordinal")),
                                            assign(n("attempt"), call("_RenewalAttemptObservation", s("_context"), n("ticket"))),
                                            assign(s("_renewal_attempt"), n("attempt")),
                                            try_(
                                                [
                                                    assign(
                                                        a(n("attempt"), "task"),
                                                        call(
                                                            a(n("asyncio"), "create_task"),
                                                            call(s("_run_required_renewal_attempt"), n("attempt"), n("ticket")),
                                                            name=c("session-operation-renewal-attempt"),
                                                        ),
                                                    )
                                                ],
                                                [
                                                    handler(
                                                        n("BaseException"),
                                                        "allocation_error",
                                                        [
                                                            ast.ImportFrom(
                                                                module="elspeth.web.sessions.service",
                                                                names=[ast.alias(name="ComposerTerminalSQLCompletionUnknown", asname=None)],
                                                                level=0,
                                                            ),
                                                            assign(
                                                                n("unknown"),
                                                                call(
                                                                    "ComposerTerminalSQLCompletionUnknown",
                                                                    c("Renewal task allocation custody is unknown"),
                                                                ),
                                                            ),
                                                            assign(a(n("unknown"), "__cause__"), n("allocation_error")),
                                                            assign(a(n("attempt"), "allocation_failure"), n("unknown")),
                                                            ast.Raise(exc=n("unknown"), cause=n("allocation_error")),
                                                        ],
                                                    ),
                                                ],
                                            ),
                                            assign(n("errors"), await_(call("_join_renewal_attempt_observation", n("attempt")))),
                                            iff(
                                                cmp(call("len", n("errors")), ast.Eq, c(1)),
                                                [ast.Raise(exc=index(n("errors"), c(0)), cause=None)],
                                            ),
                                            iff(
                                                n("errors"),
                                                [
                                                    ast.Raise(
                                                        exc=call(
                                                            "BaseExceptionGroup",
                                                            c("Renewal attempt retained original outcomes"),
                                                            n("errors"),
                                                        ),
                                                        cause=None,
                                                    )
                                                ],
                                            ),
                                            iff(cmp(a(n("attempt"), "returned"), ast.Is, c(None)), [raising(error)]),
                                            assign(n("renewed"), a(n("attempt"), "returned")),
                                        ],
                                    ),
                                ],
                                [
                                    assign(
                                        n("renewed"),
                                        await_(
                                            call(
                                                "run_stream_read_in_worker",
                                                a(s("_authority"), "renew_fork_child_lease"),
                                                s("_fork_authority"),
                                                lease_seconds=s("_lease_seconds"),
                                            )
                                        ),
                                    )
                                ],
                            ),
                            iff(
                                either(
                                    cmp(call("type", n("renewed")), ast.IsNot, n("SessionOperationContext")),
                                    cmp(n("renewed"), ast.NotEq, s("_context")),
                                ),
                                [raising(n("RuntimeError"))],
                            ),
                        ],
                        [
                            handler(a(n("asyncio"), "CancelledError"), None, [ast.Raise(exc=None, cause=None)]),
                            handler(
                                n("BaseExceptionGroup"),
                                "renewal_error",
                                [expr(call(s("_record_renewal_error"), n("renewal_error"))), ret()],
                            ),
                            handler(n("Exception"), "renewal_error", [expr(call(s("_record_renewal_error"), n("renewal_error"))), ret()]),
                        ],
                    ),
                ],
            ),
        ],
    )


def _nonreturn_cleanup(unit):
    """Finite normal-completion proof; arbitrary effects still owe closure.

    A body containing no return/yield and whose final normal completion is a
    raise cannot supply an adopted successful receiver. Exceptions/nontermination
    also cannot continue the caller past its await. For Try, every handler and
    its normal-success else path must raise and finally may not override that.
    """
    fn = _find(unit.tree, "_raise_adopt_failure_after_release")
    failures = []
    if fn is None:
        return ["adoption cleanup: missing non-return supplier"], set()
    if not _signature(
        fn, ("authority", "context", "failure"), ("phase", "cancellation_observations"), (), (None, c(None)), asynchronous=True
    ):
        failures.append("adoption cleanup: callable binding/signature changed")
    if any(
        isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal, ast.FunctionDef, ast.ClassDef))
        or (isinstance(node, ast.AsyncFunctionDef) and node is not fn)
        for node in ast.walk(fn)
    ):
        failures.append("adoption cleanup: explicit normal return/unsupported execution scope")

    def raises_on_normal_exit(body):
        retained = [
            node
            for node in body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)) and not isinstance(node, ast.Pass)
        ]
        if not retained:
            return False
        last = retained[-1]
        if isinstance(last, ast.Raise):
            return True
        if isinstance(last, ast.If):
            return raises_on_normal_exit(last.body) and raises_on_normal_exit(last.orelse)
        if isinstance(last, ast.Try):
            return (
                not last.finalbody
                and bool(last.orelse)
                and raises_on_normal_exit(last.orelse)
                and all(raises_on_normal_exit(item.body) for item in last.handlers)
            )
        return False

    if not raises_on_normal_exit(fn.body):
        failures.append("adoption cleanup: a supported path can complete normally")
    return failures, _global_dependencies(unit, fn) | {"elspeth.web.coordination.lifecycle._raise_adopt_failure_after_release"}


def _lease_constructor(unit):
    """Check the complete pre-store guard and every field write in this class.

    The remaining constructor effects/async transfer are intentionally not
    certified. This narrows the residual without inventing guard dominance
    through the adoption exception handler.
    """
    cls = _find(unit.tree, "SessionOperationLease")
    fn = _find(unit.tree, "SessionOperationLease.__init__")
    if cls is None or fn is None:
        return ["lease field producer missing"], set()
    failures = []
    if not _signature(
        fn,
        ("self", "authority", "context"),
        ("lease_seconds", "renew_interval_seconds", "fork_authority", "required_work", "execution_obligation"),
        (),
        (None, None, c(None), c(None), c(None)),
    ):
        failures.append("lease constructor: source argument binding changed")
    error = a(n("contract_errors"), "AuditIntegrityError")
    guard = iff(
        cmp(n("required_work"), ast.IsNot, c(None)),
        [
            iff(cmp(call("type", n("required_work")), ast.IsNot, n("RequiredWorkCoordinator")), [raising(error)]),
            iff(cmp(a(a(n("required_work"), "authority"), "context"), ast.NotEq, n("context")), [raising(error)]),
        ],
    )
    if not fn.body or not _same(fn.body[0], guard):
        failures.append("lease constructor: optional exact owner/context guard changed")
    writes = []
    for method in cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr == "_required_work":
                writes.append((method.name, node))
    if [method for method, _ in writes] != ["__init__", "bind_required_work"]:
        failures.append("lease fields: all producer/store/delete sites disagree with two owned stores")
    # Check the actual constructor store and its top-level order, rather than
    # counting only stores whose value already happens to match the parameter.
    relevant = [
        node
        for node in fn.body
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(child, ast.Attribute) and child.attr == "_required_work" and isinstance(child.ctx, ast.Store)
            for child in ast.walk(node)
        )
    ]
    if len(relevant) != 1 or not _same(relevant[0], assign(s("_required_work"), n("required_work"))):
        failures.append("lease constructor: retained field value/receiver/store changed")
    else:
        prefix = [
            guard,
            iff(
                cmp(n("execution_obligation"), ast.IsNot, c(None)),
                [
                    iff(
                        either(
                            cmp(call("type", n("execution_obligation")), ast.IsNot, n("ExecutionAcquisitionObligation")),
                            cmp(a(n("execution_obligation"), "lease"), ast.IsNot, n("self")),
                        ),
                        [raising(error)],
                    ),
                    iff(
                        either(
                            cmp(n("context"), ast.IsNot, a(n("execution_obligation"), "context")),
                            cmp(n("required_work"), ast.IsNot, c(None)),
                        ),
                        [raising(error)],
                    ),
                ],
            ),
            assign(s("_execution_obligation"), n("execution_obligation")),
            assign(s("_required_work"), n("required_work")),
        ]
        if fn.body.index(relevant[0]) != 3 or not _body_same(fn.body[:4], prefix):
            failures.append("lease constructor: all guard-to-store intervening execution changed")
    return failures, _global_dependencies(unit, fn) | {
        "elspeth.web.coordination.lifecycle.SessionOperationLease",
        "elspeth.web.coordination.lifecycle.SessionOperationLease.__init__",
        "elspeth.web.coordination.lifecycle.SessionOperationLease._required_work",
        "elspeth.web.coordination.lifecycle.SessionOperationLease.adopt",
        "elspeth.web.coordination.lifecycle.SessionOperationLease._renew_forever",
    }


def _telemetry_transfer(unit):
    fn = _find(unit.tree, "bootstrap_operator_telemetry")
    if fn is None:
        return ["telemetry bootstrap missing"], set()
    failures = []
    if not _signature(fn, ("settings",), ("cleanup_owner", "factories"), (), (None, c(None))):
        failures.append("telemetry bootstrap: parameter/validator binding changed")
    body = [node for node in fn.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    prefix = [
        ast.Global(names=["_runtime"]),
        assign(n("installation"), call("validate_telemetry_reservation_owner", n("cleanup_owner"))),
        expr(call(a(n("cleanup_owner"), "assert_process"))),
        assign(n("existing"), call(a(n("installation"), "reserve"), n("cleanup_owner"))),
    ]
    if len(body) < 4 or not _body_same(body[:4], prefix):
        failures.append("telemetry bootstrap: validator→same owner process→same installation reserve transfer changed")
    # Any local binding elsewhere changes Python's lexical resolution even
    # if it is textually after the already checked reserve expression.
    forbidden = {"validate_telemetry_reservation_owner", "cleanup_owner"}
    scope = _ExecutionScope(postponed_annotations=_postponed_annotations(unit))
    for statement in fn.body:
        scope.visit(statement)
    if scope.yields:
        failures.append("telemetry bootstrap: generator execution does not run the validated prefix at call")
    if forbidden.intersection(scope.bindings):
        failures.append("telemetry bootstrap: protected lexical binding rebound")
    if forbidden.intersection(scope.redirected):
        failures.append("telemetry bootstrap: protected lexical origin redirected")
    failures.extend("telemetry bootstrap: " + failure for failure in _definition_scope_failures(unit, fn))
    # Retain the actual signature and annotation/type-parameter scope. Original
    # signature validation above refuses type parameters; projection must never
    # erase them before dependency recovery or an independent consumer sees it.
    selected = copy.deepcopy(fn)
    selected.body = copy.deepcopy(body[:4])
    return failures, _global_dependencies(unit, selected) | {
        "elspeth.web.operator_telemetry.bootstrap_operator_telemetry",
        "elspeth.web.operator_telemetry_dispatch.validate_telemetry_reservation_owner",
        "elspeth.web.operator_telemetry_custody.OperatorTelemetryCleanupOwner.assert_process",
        "elspeth.web.operator_telemetry_installation.TelemetryInstallation.reserve",
    }


def _telemetry_producers(indexed):
    failures, roots = [], set()
    installation = indexed.get("src/elspeth/web/operator_telemetry_installation.py")
    if installation is not None:
        failures.extend(
            _class_fields(
                installation.tree,
                "TelemetryInstallationRecord",
                [
                    ("owner", n("OperatorTelemetryCleanupOwner"), None),
                    ("creator_pid", n("int"), None),
                    ("state", n("str"), c("acquiring")),
                    ("stage", n("InstallationStage"), a(n("InstallationStage"), "NOT_ATTEMPTED")),
                    ("provider", ast.BinOp(left=n("OwnedMeterProvider"), op=ast.BitOr(), right=c(None)), c(None)),
                    ("runtime", ast.BinOp(left=n("OperatorTelemetryRuntime"), op=ast.BitOr(), right=c(None)), c(None)),
                ],
            )
        )
        failures.extend(_owned_class(installation, "TelemetryInstallation", ("__weakref__", "creator_pid", "lock", "record", "registry")))
    custody = indexed.get("src/elspeth/web/operator_telemetry_custody.py")
    if custody is None:
        failures.append("telemetry owner producer unit missing")
    else:
        found, dependencies = _check_contracts(
            custody,
            {
                "OperatorTelemetryCleanupOwner.__init__": (
                    ("self",),
                    ("installation",),
                    (),
                    (c(None),),
                    False,
                    (),
                    [
                        ast.ImportFrom(
                            module="elspeth.web.operator_telemetry_installation",
                            names=[
                                ast.alias(name="PRODUCTION_TELEMETRY_INSTALLATION", asname=None),
                                ast.alias(name="TelemetryInstallation", asname=None),
                            ],
                            level=0,
                        ),
                        assign(
                            n("selected"),
                            ast.IfExp(
                                test=cmp(n("installation"), ast.Is, c(None)),
                                body=n("PRODUCTION_TELEMETRY_INSTALLATION"),
                                orelse=n("installation"),
                            ),
                        ),
                        iff(neg(call("isinstance", n("selected"), n("TelemetryInstallation"))), [raising(n("TypeError"))]),
                        assign(s("installation"), n("selected")),
                        assign(s("creator_pid"), call(a(n("os"), "getpid"))),
                        assign(s("lock"), call(a(n("threading"), "Lock"))),
                        assign(s("state"), c("new")),
                        annotate(
                            s("readers"),
                            index(n("list"), union(n("OwnedPrometheusMetricReader"), n("OwnedPeriodicExportingMetricReader"))),
                            list_(),
                        ),
                        annotate(s("provider"), union(n("OwnedMeterProvider"), c(None)), c(None)),
                        annotate(s("errors"), index(n("list"), n("BaseException")), list_()),
                        annotate(s("diagnostics"), index(n("list"), n("BaseException")), list_()),
                        annotate(s("acquisition_defects"), index(n("list"), n("BaseException")), list_()),
                        annotate(s("exporters"), index(n("list"), n("RawMetricExporterCustodian")), list_()),
                        annotate(s("witness"), union(n("TelemetryCompletionWitness"), c(None)), c(None)),
                    ],
                ),
                "OperatorTelemetryCleanupOwner.assert_process": (
                    ("self",),
                    (),
                    (),
                    (),
                    False,
                    (),
                    [
                        iff(cmp(s("creator_pid"), ast.NotEq, call(a(n("os"), "getpid"))), [raising(n("TelemetryCustodyUnresolved"))]),
                        expr(call(a(s("installation"), "assert_process"))),
                    ],
                ),
            },
        )
        failures.extend(found)
        roots.update(dependencies)
        names = (
            "__weakref__",
            "acquisition_defects",
            "creator_pid",
            "diagnostics",
            "errors",
            "exporters",
            "installation",
            "lock",
            "provider",
            "readers",
            "state",
            "witness",
        )
        failures.extend(_owned_class(custody, "OperatorTelemetryCleanupOwner", names))
    roots.update(
        {
            "dataclasses.dataclass",
            "builtins.object",
            "builtins.type",
            "types.MemberDescriptorType",
            "types.MethodType",
            "_thread.RLock",
            "os.getpid",
        }
    )
    return failures, roots


def _class_fields(tree, name, fields, *, frozen=False):
    cls = _find(tree, name)
    if not isinstance(cls, ast.ClassDef) or cls.bases or cls.keywords or cls.type_params:
        return [f"{name}: unsupported class construction"]
    declarations = [
        node
        for node in cls.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    expected = [annotate(n(field), annotation, value) for field, annotation, value in fields]
    if not _body_same(declarations, expected):
        return [f"{name}: field/descriptor producer changed"]
    decorators = [n("final"), call("dataclass", frozen=c(True), slots=c(True))] if frozen else [call("dataclass", slots=c(True))]
    if not _same(cls.decorator_list, decorators):
        return [f"{name}: dataclass ownership flags changed"]
    methods = [node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    expected_methods = ["__post_init__", "reserve_pair", "validate_context"] if name == "RequiredWorkBinding" else []
    if methods != expected_methods:
        return [f"{name}: executable dataclass namespace changed"]
    return []


def _owned_class(unit, name, slots, decorators=()):
    cls = _find(unit.tree, name)
    if cls is None or cls.bases or cls.keywords or cls.type_params or not _same(cls.decorator_list, [n(value) for value in decorators]):
        return [f"{name}: ordinary owned class producer changed"]
    declarations = [
        node
        for node in cls.body
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    if not _body_same(declarations, [assign(n("__slots__"), seq(*(c(value) for value in slots)))]):
        return [f"{name}: owned slot/descriptor namespace changed"]
    methods = [node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(methods) != len(set(methods)):
        return [f"{name}: duplicate method overwrites owned namespace binding"]
    protected_lookup = {"__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__new__", "__init_subclass__"}
    if any(node.name in protected_lookup for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        return [f"{name}: owned lookup/construction dispatch changed"]
    for method in (node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        failures = _owned_method_definition_failures(unit, cls, method, slots)
        if failures:
            return failures
    return []


def _owned_method_definition_failures(unit, cls, method, slots):
    """Close current canonical class definition effects, including all methods.

    The actual modules postpone annotations. Method defaults are constants or
    the two actual retained supplier names; the only decorators are the three
    builtin descriptor constructors. Function bodies retain separate execution
    obligations; no arbitrary executable new definition grammar is admitted.
    """
    descriptor_names = {"property", "classmethod", "staticmethod"}
    default_names = {"REGISTRY", "_noop_archive_lifecycle_callback"}
    generic_parameter = {
        ("ProviderCallCustody", "_join"): "R",
        ("ProviderCallCustody", "join_title_update"): "R",
        ("SessionOperationLease", "create_task"): "T",
    }.get((cls.name, method.name))
    supported_parameters = (
        not method.type_params
        if generic_parameter is None
        else len(method.type_params) == 1
        and type(method.type_params[0]) is ast.TypeVar
        and method.type_params[0].name == generic_parameter
        and method.type_params[0].bound is None
        and getattr(method.type_params[0], "default_value", None) is None
    )
    if (
        not supported_parameters
        or method.type_comment is not None
        or not _postponed_annotations(unit)
        or method.name in set(slots) | {"__slots__"} | descriptor_names | default_names
        or len(method.decorator_list) > 1
        or any(not isinstance(decorator, ast.Name) or decorator.id not in descriptor_names for decorator in method.decorator_list)
    ):
        return [f"{cls.name}.{method.name}: unsupported class method definition scope/descriptor"]
    defaults = [*method.args.defaults, *(value for value in method.args.kw_defaults if value is not None)]
    if any(not isinstance(value, ast.Constant) and not (isinstance(value, ast.Name) and value.id in default_names) for value in defaults):
        return [f"{cls.name}.{method.name}: unsupported executed class method default"]
    return []


def owned_method_definitions(units):
    """Actual canonical class definition expressions; no method body expansion."""
    selected = {
        "src/elspeth/web/composer/provider_quota.py": "ProviderCallCustody",
        "src/elspeth/web/coordination/lifecycle.py": "SessionOperationLease",
        "src/elspeth/web/operator_telemetry_custody.py": "OperatorTelemetryCleanupOwner",
        "src/elspeth/web/operator_telemetry_installation.py": "TelemetryInstallation",
    }
    definitions = []
    for unit in units:
        name = selected.get(unit.path)
        if name is None:
            continue
        cls = _find(unit.tree, name)
        if isinstance(cls, ast.ClassDef):
            definitions.extend(
                (unit, name + "." + method.name, method)
                for method in cls.body
                if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
    return definitions


def _definition_dependencies(unit, fn):
    module = unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    imports = {}
    statements = list(unit.tree.body)
    for statement in unit.tree.body:
        if isinstance(statement, ast.If) and isinstance(statement.test, ast.Name) and statement.test.id == "TYPE_CHECKING":
            statements.extend(statement.body)
    for statement in statements:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
            for alias in statement.names:
                imports[alias.asname or alias.name] = statement.module + "." + alias.name
    return {
        imports.get(node.id, "builtins." + node.id if node.id in vars(builtins) else module + "." + node.id)
        for expression in _definition_expressions(unit, fn)
        for node in ast.walk(expression)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def _global_dependencies(unit, fn):
    """Discover all roots read by a reviewed body, including definition effects.

    Absolute module imports, the current TYPE_CHECKING import blocks and actual
    local imports in a selected method recover dependency seeds. SOURCE4 must
    prove these bindings: this collector is not an import/effect proof itself.
    Assignment aliases/reexports/foreign writes belong to SOURCE3 closure.
    """
    module = unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    imports = {}
    statements = list(unit.tree.body)
    for statement in unit.tree.body:
        if isinstance(statement, ast.If) and isinstance(statement.test, ast.Name) and statement.test.id == "TYPE_CHECKING":
            statements.extend(statement.body)
    for statement in statements:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
            for alias in statement.names:
                imports[alias.asname or alias.name] = statement.module + "." + alias.name
    definition_imports = dict(imports)
    for statement in ast.walk(fn):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
            for alias in statement.names:
                imports[alias.asname or alias.name] = statement.module + "." + alias.name
    local = {arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
    local.update(node.id for node in ast.walk(fn) if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)))
    local.update(node.name for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler) and node.name is not None)
    roots = set()
    for statement in fn.body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load) or node.id in local:
                continue
            roots.add(imports.get(node.id, "builtins." + node.id if node.id in vars(builtins) else module + "." + node.id))
    # Definition expressions run before arguments/body locals are bound. A
    # parameter with the same spelling cannot erase this global dependency.
    for expression in _definition_expressions(unit, fn):
        for node in ast.walk(expression):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                roots.add(definition_imports.get(node.id, "builtins." + node.id if node.id in vars(builtins) else module + "." + node.id))
    # Preserve nonexecuting annotation references for role classification. They
    # are not runtime producers and cannot erase semantic constructor roots.
    if _postponed_annotations(unit):
        annotations = [arg.annotation for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if arg.annotation is not None]
        if fn.returns is not None:
            annotations.append(fn.returns)
        for annotation in annotations:
            for node in ast.walk(annotation):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local:
                    roots.add(
                        definition_imports.get(node.id, "builtins." + node.id if node.id in vars(builtins) else module + "." + node.id)
                    )
    return roots


def _check_contracts(unit, contracts):
    failures, roots = [], set()
    module = unit.path.removeprefix("src/").removesuffix(".py").replace("/", ".")
    for qualified, contract in contracts.items():
        fn = _find(unit.tree, qualified)
        roots.add(module + "." + qualified)
        if fn is None:
            failures.append(f"{unit.path}:{qualified}: missing or ambiguous definition")
            continue
        positional, keyword, defaults, kw_defaults, asynchronous, decorators, body = contract
        if not _signature(fn, positional, keyword, defaults, kw_defaults, asynchronous, decorators):
            failures.append(f"{unit.path}:{qualified}: binding/signature/descriptor changed")
        if not _body_same(fn.body, body):
            failures.append(f"{unit.path}:{qualified}: closed execution contract changed (guard/transfer/effect/branch)")
        failures.extend(f"{unit.path}:{qualified}: " + failure for failure in _definition_scope_failures(unit, fn))
        roots.update(_global_dependencies(unit, fn))
        if "." in qualified:
            owner = qualified.rsplit(".", 1)[0]
            receiver = positional[0] if positional else None
            for expression in ast.walk(fn):
                if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name) and expression.value.id == receiver:
                    roots.add(module + "." + owner + "." + expression.attr)
    return failures, roots


def semantic_dependency_requirements(units):
    """Explicit source-owned producers required independently of textual reads.

    Self/classmethod dispatch and generated constructors can require their
    class objects without a Name load. Annotation classification cannot remove
    these requirements. Receiver-bound field/method effects still owe closure.
    """
    indexed = {unit.path: unit for unit in units}
    selected = (
        ("src/elspeth/web/required_work.py", "RequiredWorkBinding", "frozen dataclass constructor and exact retained coordinator fields"),
        (
            "src/elspeth/web/composer/provider_quota.py",
            "ProviderCallCustody",
            "owned bound custody receiver, constructor and retained-field descriptors",
        ),
        (
            "src/elspeth/web/coordination/lifecycle.py",
            "SessionOperationLease",
            "adopt cls constructor supplier, classmethod receiver and retained-field descriptors",
        ),
        (
            "src/elspeth/web/operator_telemetry_custody.py",
            "OperatorTelemetryCleanupOwner",
            "owned owner constructor and exact canonical field/process descriptors",
        ),
        (
            "src/elspeth/web/operator_telemetry_installation.py",
            "TelemetryInstallation",
            "owned installation constructor and exact canonical lock/record/reserve descriptors",
        ),
        (
            "src/elspeth/web/operator_telemetry_installation.py",
            "TelemetryInstallationRecord",
            "actual record constructor and owned slots dataclass field producer",
        ),
    )
    requirements = {}
    for path, name, reason in selected:
        unit = indexed.get(path)
        cls = _find(unit.tree, name) if unit is not None else None
        identity = path.removeprefix("src/").removesuffix(".py").replace("/", ".") + "." + name
        requirements[identity] = [
            {
                "role": "semantic_owned_class_constructor_origin",
                "reason": reason,
                "path": path,
                "symbol": name,
                "line": cls.lineno if cls is not None else None,
                "source_definition_present": cls is not None,
            }
        ]
    return requirements


def family_local_contracts(units):
    """Return local implementation premises, with no admission semantics.

    The common boundary must separately establish typed source origins and
    receiver/effect closure. Standalone callers use family_failures instead.
    """
    indexed = {unit.path: unit for unit in units}
    failures, roots = [], set()
    families = {
        "src/elspeth/web/required_work.py": _binding_contracts(),
        "src/elspeth/web/composer/provider_quota.py": _provider_contracts(),
        "src/elspeth/web/operator_telemetry_dispatch.py": _telemetry_contracts(),
        "src/elspeth/web/operator_telemetry_installation.py": _installation_contracts(),
        "src/elspeth/web/coordination/lifecycle.py": {
            **_lease_contracts(),
            "SessionOperationLease.adopt": _adopt_contract(),
            "SessionOperationLease._renew_forever": _renew_contract(),
        },
    }
    for path, contracts in families.items():
        unit = indexed.get(path)
        if unit is None:
            failures.append(f"{path}: missing selected family unit")
            continue
        found, dependencies = _check_contracts(unit, contracts)
        failures.extend(found)
        roots.update(dependencies)
    for path, checker in (
        ("src/elspeth/web/coordination/lifecycle.py", _lease_constructor),
        ("src/elspeth/web/coordination/lifecycle.py", _nonreturn_cleanup),
        ("src/elspeth/web/operator_telemetry.py", _telemetry_transfer),
    ):
        unit = indexed.get(path)
        if unit is None:
            failures.append(f"{path}: missing transfer unit")
        else:
            found, dependencies = checker(unit)
            failures.extend(found)
            roots.update(dependencies)
    found, dependencies = _telemetry_producers(indexed)
    failures.extend(found)
    roots.update(dependencies)
    provider = indexed.get("src/elspeth/web/composer/provider_quota.py")
    if provider is not None:
        failures.extend(
            _owned_class(
                provider,
                "ProviderCallCustody",
                (
                    "__weakref__",
                    "_admitting",
                    "_context",
                    "_required_work",
                    "_sdk_entered",
                    "_service",
                    "_settlements",
                    "_undispatched_recurrence",
                    "attempt",
                    "call",
                ),
            )
        )
    lifecycle = indexed.get("src/elspeth/web/coordination/lifecycle.py")
    if lifecycle is not None:
        failures.extend(
            _owned_class(
                lifecycle,
                "SessionOperationLease",
                (
                    "_authority",
                    "_close_task",
                    "_closed",
                    "_context",
                    "_disposition",
                    "_execution_obligation",
                    "_finish_mode",
                    "_fork_authority",
                    "_lease_seconds",
                    "_lost_event",
                    "_owned_tasks",
                    "_renew_interval_seconds",
                    "_renewal_attempt",
                    "_renewal_error",
                    "_renewal_ordinal",
                    "_renewal_task",
                    "_required_work",
                    "_stop_renewal",
                ),
                ("final",),
            )
        )
    required = indexed.get("src/elspeth/web/required_work.py")
    if required is not None:
        failures.extend(
            _class_fields(
                required.tree,
                "RequiredWorkBinding",
                [
                    ("coordinator", n("RequiredWorkCoordinator"), None),
                    ("transition_ordinal", n("int"), None),
                    ("semantic_ordinal", n("int"), None),
                    ("role", n("RequiredWorkRole"), None),
                    ("running", ast.BinOp(left=n("ComposerOperationRunning"), op=ast.BitOr(), right=c(None)), c(None)),
                ],
                frozen=True,
            )
        )
        roots.update({"typing.final", "dataclasses.dataclass", "elspeth.web.required_work.RequiredWorkBinding"})
    roots.update(
        {
            "elspeth.web.operator_telemetry_custody.OperatorTelemetryCleanupOwner",
            "elspeth.web.operator_telemetry_custody.OperatorTelemetryCleanupOwner.assert_process",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallationRecord",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallation",
            "elspeth.web.composer.provider_quota.ProviderCallCustody",
            "elspeth.web.composer.provider_quota.ProviderCallCustody._required_work",
            "elspeth.web.composer.provider_quota.ProviderCallCustody._join",
            "elspeth.web.composer.provider_quota.ProviderCallCustody.context",
            "elspeth.web.composer.provider_quota.ProviderCallCustody.service",
        }
    )
    roots.update(
        {
            "elspeth.web.operator_telemetry_custody.OperatorTelemetryCleanupOwner.installation",
            "elspeth.web.operator_telemetry_custody.OperatorTelemetryCleanupOwner.creator_pid",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallation.creator_pid",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallation.lock",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallation.record",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallationRecord.state",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallationRecord.owner",
            "elspeth.web.operator_telemetry_installation.TelemetryInstallationRecord.runtime",
            "elspeth.web.required_work.RequiredWorkBinding.coordinator",
            "elspeth.web.required_work.RequiredWorkBinding.transition_ordinal",
            "elspeth.web.required_work.RequiredWorkBinding.semantic_ordinal",
            "elspeth.web.required_work.RequiredWorkBinding.role",
        }
    )
    for unit, _qualified, method in owned_method_definitions(units):
        roots.update(_definition_dependencies(unit, method))
    return failures, roots


def family_failures(units):
    """Standalone refusal until an actual common consumer composes premises."""
    failures, roots = family_local_contracts(units)
    failures.append(
        "UNKNOWN consumer composition: local family contracts and dependency seeds require SOURCE4 protected-origin/effect closure and same public/descriptor consumers before admission"
    )
    return failures, roots
