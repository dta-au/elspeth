"""RED gate for durable, exactly fenced composer progress.

The composer progress surface is a latest-value register plus durable request
liveness.  It is deliberately not an append-only event log.  These tests pin
the Task 5 contract before production is migrated away from the process-local
registry.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Engine

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.progress import ComposerProgressRegistry, ComposerProgressSnapshot
from elspeth.web.coordination.contracts import (
    SessionOperationContext,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions import protocol as sessions_protocol
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    composer_inflight_requests_table,
    composer_progress_snapshots_table,
    session_operation_fences_table,
)
from elspeth.web.sessions.models import (
    metadata as sessions_metadata,
)
from elspeth.web.sessions.schema import initialize_session_schema

_PROGRESS_TABLE_NAMES = frozenset(
    {
        composer_inflight_requests_table.name,
        composer_progress_snapshots_table.name,
    }
)


class InjectedProgressWriteFailure(RuntimeError):
    """Test-only failure raised from a target-table SQLAlchemy hook."""


ProgressCommitNotifier = Callable[[ComposerProgressSnapshot], Awaitable[None]]


@pytest.fixture
def durable_engines(tmp_path: Path) -> Iterator[tuple[Engine, Engine]]:
    """Two independent engines over one file-backed Sessions database."""
    database_url = f"sqlite:///{tmp_path / 'composer-progress.db'}"
    first = create_session_engine(database_url, connect_args={"check_same_thread": False})
    initialize_session_schema(first)
    second = create_session_engine(database_url, connect_args={"check_same_thread": False})
    try:
        yield first, second
    finally:
        first.dispose()
        second.dispose()


def _required_parameter(owner: object, method_name: str, parameter_name: str) -> inspect.Parameter:
    method = owner if method_name == "__init__" else getattr(owner, method_name, None)
    assert method is not None, f"{owner!r} has no required {method_name} contract"
    signature = inspect.signature(method)
    assert parameter_name in signature.parameters, f"{owner!r}.{method_name} has no {parameter_name} parameter"
    parameter = signature.parameters[parameter_name]
    assert parameter.default is inspect.Parameter.empty, f"{owner!r}.{method_name}.{parameter_name} must be mandatory"
    return parameter


def _registry(
    engine: Engine,
    authority: SQLiteLocalSessionOperationAuthority,
    *,
    notify_committed: ProgressCommitNotifier | None = None,
) -> ComposerProgressRegistry:
    """Construct only the durable registry contract, with a useful RED."""
    _required_parameter(ComposerProgressRegistry, "__init__", "engine")
    _required_parameter(ComposerProgressRegistry, "__init__", "session_operation_authority")
    signature = inspect.signature(ComposerProgressRegistry)
    assert "notify_committed" in signature.parameters, "durable registry has no local commit-notification boundary"
    assert signature.parameters["notify_committed"].default is None
    return ComposerProgressRegistry(  # type: ignore[call-arg]
        engine=engine,
        session_operation_authority=authority,
        notify_committed=notify_committed,
    )


def _create_session(
    authority: SQLiteLocalSessionOperationAuthority,
    *,
    user_id: str = "alice",
):
    return authority.create_session_with_initial_fence(
        user_id=user_id,
        title="Durable composer progress",
        auth_provider_type="local",
        owner_instance_id="progress-creator",
        lease_seconds=120,
    )


def _acquire(
    authority: SQLiteLocalSessionOperationAuthority,
    *,
    session_id,
    kind: SessionOperationKind = SessionOperationKind.COMPOSE,
    owner: str = "progress-owner-a",
) -> SessionOperationContext:
    return authority.acquire(
        session_id=session_id,
        operation_kind=kind,
        owner_instance_id=owner,
        lease_seconds=120,
    )


def _progress_event(phase: str, *, label: str = "current") -> ComposerProgressEvent:
    reason = None
    if phase == "complete":
        reason = "composer_complete"
    elif phase == "cancelled":
        reason = "client_cancelled"
    elif phase == "failed":
        reason = "service_setup_failed"
    return ComposerProgressEvent(
        phase=cast(Any, phase),
        headline=f"{label} {phase}",
        evidence=(f"{label} evidence",),
        likely_next=f"{label} next",
        reason=cast(Any, reason),
    )


async def _start(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str = "request-a",
    user_id: str = "alice",
    event_value: ComposerProgressEvent | None = None,
) -> ComposerProgressSnapshot:
    start_request = getattr(registry, "start_request", None)
    assert callable(start_request), "ComposerProgressRegistry.start_request is not implemented"
    return await start_request(
        session_operation_context=context,
        request_id=request_id,
        user_id=user_id,
        event=event_value or _progress_event("starting"),
    )


async def _publish(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str = "request-a",
    user_id: str = "alice",
    event_value: ComposerProgressEvent | None = None,
) -> ComposerProgressSnapshot:
    return await registry.publish(  # type: ignore[call-arg]
        session_operation_context=context,
        request_id=request_id,
        user_id=user_id,
        event=event_value or _progress_event("calling_model"),
    )


async def _finish(
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str = "request-a",
    user_id: str = "alice",
    terminal_event: ComposerProgressEvent | None = None,
) -> ComposerProgressSnapshot:
    finish_request = getattr(registry, "finish_request", None)
    assert callable(finish_request), "ComposerProgressRegistry.finish_request is not implemented"
    return await finish_request(
        session_operation_context=context,
        request_id=request_id,
        user_id=user_id,
        terminal_event=terminal_event,
    )


async def _invoke_progress_write(
    method_name: str,
    registry: ComposerProgressRegistry,
    context: SessionOperationContext,
    *,
    request_id: str = "request-a",
    user_id: str = "alice",
    event_value: ComposerProgressEvent | None = None,
) -> ComposerProgressSnapshot:
    if method_name == "start_request":
        return await _start(
            registry,
            context,
            request_id=request_id,
            user_id=user_id,
            event_value=event_value,
        )
    if method_name == "publish":
        return await _publish(
            registry,
            context,
            request_id=request_id,
            user_id=user_id,
            event_value=event_value,
        )
    if method_name == "finish_request":
        return await _finish(
            registry,
            context,
            request_id=request_id,
            user_id=user_id,
            terminal_event=event_value or _progress_event("complete"),
        )
    raise AssertionError(f"unknown test progress method: {method_name}")


def _retire_session_progress(
    authority: SQLiteLocalSessionOperationAuthority,
    context: SessionOperationContext,
) -> None:
    def retire(transaction) -> None:
        facet = transaction.composer_progress
        facet.retire_session_progress()

    authority.mutate(context, retire)


def _rows(engine: Engine) -> dict[str, tuple[dict[str, object], ...]]:
    with engine.connect() as connection:
        inflight = tuple(
            dict(row._mapping)
            for row in connection.execute(select(composer_inflight_requests_table).order_by(composer_inflight_requests_table.c.request_id))
        )
        snapshots = tuple(
            dict(row._mapping)
            for row in connection.execute(
                select(composer_progress_snapshots_table).order_by(composer_progress_snapshots_table.c.session_id)
            )
        )
    return {"inflight": inflight, "snapshots": snapshots}


def _all_sessions_state(engine: Engine) -> dict[str, tuple[str, ...]]:
    """Stable value snapshot of every Sessions table, including fence state."""
    state: dict[str, tuple[str, ...]] = {}
    with engine.connect() as connection:
        for table in sorted(sessions_metadata.tables.values(), key=lambda candidate: candidate.name):
            rows = connection.execute(select(table)).all()
            state[table.name] = tuple(sorted(repr(dict(row._mapping)) for row in rows))
    return state


@contextmanager
def _capture_progress_dml(engine: Engine) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(("insert ", "update ", "delete ")) and any(
            table_name in normalized for table_name in _PROGRESS_TABLE_NAMES
        ):
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@contextmanager
def _fail_target_write(engine: Engine, *, table_name: str, operation: str | None = None) -> Iterator[None]:
    def fail(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        is_target_operation = operation is None or normalized.startswith(operation)
        if is_target_operation and table_name in normalized:
            raise InjectedProgressWriteFailure(f"injected failure writing {table_name}")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@contextmanager
def _fail_snapshot_after_inflight_write(engine: Engine, *, inflight_operation: str) -> Iterator[None]:
    """Raise on snapshot DML only after the paired inflight DML executed."""
    inflight_written = False

    def observe_inflight(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        nonlocal inflight_written
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(inflight_operation) and composer_inflight_requests_table.name in normalized:
            inflight_written = True

    def fail_snapshot(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith(("insert ", "update ", "delete ")) and composer_progress_snapshots_table.name in normalized:
            assert inflight_written, "snapshot DML ran before the paired inflight DML"
            raise InjectedProgressWriteFailure("injected failure after paired inflight DML")

    event.listen(engine, "after_cursor_execute", observe_inflight)
    event.listen(engine, "before_cursor_execute", fail_snapshot)
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", observe_inflight)
        event.remove(engine, "before_cursor_execute", fail_snapshot)


@contextmanager
def _fail_second_progress_dml(engine: Engine, *, operation: str | None = None) -> Iterator[None]:
    completed_writes = 0

    def matches(normalized: str) -> bool:
        return (operation is None or normalized.startswith(operation)) and any(
            table_name in normalized for table_name in _PROGRESS_TABLE_NAMES
        )

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        nonlocal completed_writes
        normalized = " ".join(statement.lower().split())
        if matches(normalized):
            completed_writes += 1

    def fail(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        normalized = " ".join(statement.lower().split())
        if completed_writes == 1 and matches(normalized):
            raise InjectedProgressWriteFailure("injected failure after first progress DML")

    event.listen(engine, "after_cursor_execute", observe)
    event.listen(engine, "before_cursor_execute", fail)
    try:
        yield
    finally:
        event.remove(engine, "after_cursor_execute", observe)
        event.remove(engine, "before_cursor_execute", fail)


def _forge_context(
    context: SessionOperationContext,
    *,
    session_id: str | None = None,
    operation_id: str | None = None,
    lease_token: str | None = None,
    epoch: int | None = None,
) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id or context.fence.session_id,
            operation_id=operation_id or context.fence.operation_id,
            lease_token=lease_token or context.fence.lease_token,
            operation_epoch=epoch or context.fence.operation_epoch,
        ),
        operation_kind=context.operation_kind,
    )


def _is_awaited(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    current: ast.AST = node
    while current in parents and isinstance(parents[current], (ast.Attribute, ast.Call, ast.keyword)):
        current = parents[current]
    return current in parents and isinstance(parents[current], ast.Await)


def _alias_key(node: ast.expr) -> str | None:
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return ast.unparse(node)
    return None


def _assignment_bindings(node: ast.Assign | ast.AnnAssign) -> tuple[tuple[str, ast.expr], ...]:
    """Return simple and destructured alias bindings for static self-audit."""

    def bind(target: ast.expr, value: ast.expr) -> tuple[tuple[str, ast.expr], ...]:
        key = _alias_key(target)
        if key is not None:
            contained: list[tuple[str, ast.expr]] = []
            if isinstance(value, ast.Dict):
                contained.extend(
                    (f"{key}[{ast.unparse(item_key)}]", item_value)
                    for item_key, item_value in zip(value.keys, value.values, strict=True)
                    if item_key is not None
                )
            elif isinstance(value, (ast.Tuple, ast.List)):
                contained.extend((f"{key}[{index}]", item_value) for index, item_value in enumerate(value.elts))
            return ((key, value), *contained)
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            return tuple(
                pair for target_item, value_item in zip(target.elts, value.elts, strict=True) for pair in bind(target_item, value_item)
            )
        return ()

    if node.value is None:
        return ()
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(pair for target in targets for pair in bind(target, node.value))


def _literal_getattr_name(node: ast.expr) -> tuple[ast.expr, str] | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and type(node.args[1].value) is str
    ):
        return node.args[0], node.args[1].value
    return None


def _annotation_names_progress_registry(annotation: ast.expr | None) -> bool:
    return annotation is not None and "ComposerProgressRegistry" in ast.unparse(annotation)


def _progress_write_analysis(
    source: str,
    *,
    label: str,
) -> tuple[tuple[str, ...], Counter[tuple[str, str]], Counter[tuple[str, str]]]:
    tree = ast.parse(source, filename=label)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    registry_aliases = {
        argument.arg
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if _annotation_names_progress_registry(argument.annotation)
    }
    progress_kind_aliases: set[str] = set()
    acquire_callable_aliases: set[str] = set()
    write_callable_aliases: dict[str, str] = {}
    publish_helper_aliases = {"_publish_progress"}
    sink_helper_aliases = {"_composer_progress_sink"}
    for import_node in ast.walk(tree):
        if not isinstance(import_node, ast.ImportFrom):
            continue
        for imported in import_node.names:
            local_name = imported.asname or imported.name
            if imported.name == "_publish_progress":
                publish_helper_aliases.add(local_name)
            if imported.name == "_composer_progress_sink":
                sink_helper_aliases.add(local_name)

    def expression_is_registry(value: ast.expr) -> bool:
        key = _alias_key(value)
        if key is not None and (
            key in registry_aliases or (isinstance(value, ast.Attribute) and value.attr == "composer_progress_registry")
        ):
            return True
        return (
            isinstance(value, ast.Call)
            and (factory_key := _alias_key(value.func)) is not None
            and factory_key.rsplit(".", maxsplit=1)[-1] == "_get_composer_progress_registry"
        )

    def callable_alias_key(value: ast.expr) -> str | None:
        return _alias_key(value)

    def registry_method(value: ast.expr) -> str | None:
        if isinstance(value, ast.Attribute) and expression_is_registry(value.value):
            return value.attr
        dynamic = _literal_getattr_name(value)
        if dynamic is not None and expression_is_registry(dynamic[0]):
            return dynamic[1]
        if (
            isinstance(value, ast.Call)
            and (wrapper_key := _alias_key(value.func)) is not None
            and wrapper_key.rsplit(".", maxsplit=1)[-1] == "partial"
            and value.args
        ):
            return registry_method(value.args[0])
        key = callable_alias_key(value)
        return write_callable_aliases.get(key) if key is not None else None

    def expression_names_helper(value: ast.expr, aliases: set[str], canonical: str) -> bool:
        key = callable_alias_key(value)
        if key is not None and (key in aliases or (isinstance(value, ast.Attribute) and value.attr == canonical)):
            return True
        dynamic = _literal_getattr_name(value)
        if dynamic is not None and dynamic[1] == canonical:
            return True
        return (
            isinstance(value, ast.Call)
            and (wrapper_key := _alias_key(value.func)) is not None
            and wrapper_key.rsplit(".", maxsplit=1)[-1] == "partial"
            and bool(value.args)
            and expression_names_helper(value.args[0], aliases, canonical)
        )

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                if isinstance(node, ast.AnnAssign) and _annotation_names_progress_registry(node.annotation):
                    target = _alias_key(node.target)
                    if target is not None and target not in registry_aliases:
                        registry_aliases.add(target)
                        changed = True
                continue
            for target, bound_value in _assignment_bindings(node):
                annotation_names_registry = isinstance(node, ast.AnnAssign) and _annotation_names_progress_registry(node.annotation)
                if (expression_is_registry(bound_value) or annotation_names_registry) and target not in registry_aliases:
                    registry_aliases.add(target)
                    changed = True
                bound_key = callable_alias_key(bound_value)
                names_progress_kind = (isinstance(bound_value, ast.Attribute) and bound_value.attr == "PROGRESS") or (
                    bound_key is not None and bound_key in progress_kind_aliases
                )
                if names_progress_kind and target not in progress_kind_aliases:
                    progress_kind_aliases.add(target)
                    changed = True
                names_acquire = (isinstance(bound_value, ast.Attribute) and bound_value.attr == "acquire") or (
                    bound_key is not None and bound_key in acquire_callable_aliases
                )
                if names_acquire and target not in acquire_callable_aliases:
                    acquire_callable_aliases.add(target)
                    changed = True
                method_name = registry_method(bound_value)
                if (
                    method_name in {"begin_request", "end_request", "clear", "start_request", "publish", "finish_request"}
                    and write_callable_aliases.get(target) != method_name
                ):
                    write_callable_aliases[target] = method_name
                    changed = True
                if (
                    expression_names_helper(bound_value, publish_helper_aliases, "_publish_progress")
                    and target not in publish_helper_aliases
                ):
                    publish_helper_aliases.add(target)
                    changed = True
                if (
                    expression_names_helper(bound_value, sink_helper_aliases, "_composer_progress_sink")
                    and target not in sink_helper_aliases
                ):
                    sink_helper_aliases.add(target)
                    changed = True

    violations: list[str] = []
    publish_inventory: Counter[tuple[str, str]] = Counter()
    sink_inventory: Counter[tuple[str, str]] = Counter()

    def callable_reference_is_supported(node: ast.expr) -> bool:
        """Allow only direct invocation or an alias binding the analyzer resolves."""
        current: ast.AST = node
        while current in parents:
            parent = parents[current]
            if isinstance(parent, ast.Call):
                return parent.func is current
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                return True
            if not isinstance(parent, (ast.Tuple, ast.List, ast.Dict, ast.Starred, ast.keyword)):
                return False
            current = parent
        return False

    for reference in ast.walk(tree):
        if not isinstance(reference, (ast.Name, ast.Attribute, ast.Subscript, ast.Call)):
            continue
        is_registry_callable = registry_method(reference) in {
            "begin_request",
            "end_request",
            "clear",
            "start_request",
            "publish",
            "finish_request",
        }
        is_helper_callable = expression_names_helper(reference, publish_helper_aliases, "_publish_progress") or expression_names_helper(
            reference,
            sink_helper_aliases,
            "_composer_progress_sink",
        )
        if (is_registry_callable or is_helper_callable) and not callable_reference_is_supported(reference):
            violations.append(f"{label}:{reference.lineno}: progress callable escapes joined-call audit")

    def enclosing_function(node: ast.AST) -> str:
        containers: list[str] = []
        current = node
        while current in parents:
            current = parents[current]
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                containers.append(current.name)
        return ".".join(reversed(containers))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        receiver_is_registry = False
        method_name = registry_method(node.func)
        if isinstance(node.func, ast.Attribute):
            receiver_is_registry = expression_is_registry(node.func.value)
        elif method_name is None:
            dynamic = _literal_getattr_name(node.func)
            if dynamic is not None:
                receiver_is_registry = expression_is_registry(dynamic[0])
                method_name = dynamic[1]

        callable_key = callable_alias_key(node.func)
        is_write_alias = callable_key is not None and callable_key in write_callable_aliases
        is_known_progress_callable = receiver_is_registry or is_write_alias or registry_method(node.func) is not None
        if method_name in {"begin_request", "end_request", "clear"} and is_known_progress_callable:
            violations.append(f"{label}:{node.lineno}: legacy local {method_name}")
        if method_name in {"start_request", "publish", "finish_request"} and is_known_progress_callable:
            keyword_names = {keyword.arg for keyword in node.keywords}
            if "session_operation_context" not in keyword_names:
                violations.append(f"{label}:{node.lineno}: missing exact context")
            if not _is_awaited(node, parents):
                violations.append(f"{label}:{node.lineno}: progress write is not joined")

        if expression_names_helper(node.func, publish_helper_aliases, "_publish_progress"):
            publish_inventory[(label, enclosing_function(node))] += 1
            keyword_names = {keyword.arg for keyword in node.keywords}
            if "session_operation_context" not in keyword_names:
                violations.append(f"{label}:{node.lineno}: progress helper missing exact context")
            if not _is_awaited(node, parents):
                violations.append(f"{label}:{node.lineno}: progress helper write is not joined")
        if expression_names_helper(node.func, sink_helper_aliases, "_composer_progress_sink"):
            sink_inventory[(label, enclosing_function(node))] += 1
            keyword_names = {keyword.arg for keyword in node.keywords}
            if "session_operation_context" not in keyword_names:
                violations.append(f"{label}:{node.lineno}: progress sink missing exact context")

        is_acquire = (isinstance(node.func, ast.Attribute) and node.func.attr == "acquire") or (
            callable_key is not None and callable_key in acquire_callable_aliases
        )
        if is_acquire:
            operation_values = [keyword.value for keyword in node.keywords if keyword.arg == "operation_kind"]
            operation_values.extend(node.args)
            if any(
                (isinstance(value, ast.Attribute) and value.attr == "PROGRESS") or ((_alias_key(value) or "") in progress_kind_aliases)
                for value in operation_values
            ):
                violations.append(f"{label}:{node.lineno}: nested PROGRESS lease")
    return tuple(violations), publish_inventory, sink_inventory


def _progress_write_violations(source: str, *, label: str) -> tuple[str, ...]:
    return _progress_write_analysis(source, label=label)[0]


def test_registry_and_uow_expose_only_the_durable_compose_contract() -> None:
    """The registry delegates writes to one narrow COMPOSE-only UoW facet."""
    _required_parameter(ComposerProgressRegistry, "__init__", "engine")
    authority = _required_parameter(ComposerProgressRegistry, "__init__", "session_operation_authority")
    assert authority.kind in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    notify_committed = inspect.signature(ComposerProgressRegistry).parameters.get("notify_committed")
    assert notify_committed is not None
    assert notify_committed.default is None

    start_context = _required_parameter(ComposerProgressRegistry, "start_request", "session_operation_context")
    publish_context = _required_parameter(ComposerProgressRegistry, "publish", "session_operation_context")
    finish_context = _required_parameter(ComposerProgressRegistry, "finish_request", "session_operation_context")
    for parameter in (start_context, publish_context, finish_context):
        assert parameter.annotation in {SessionOperationContext, "SessionOperationContext"}

    facet = getattr(sessions_protocol, "SessionOperationComposerProgressMutations", None)
    assert facet is not None, "typed composer-progress UoW facet is missing"
    for method_name in ("start_request", "publish_progress", "finish_request", "retire_session_progress"):
        assert callable(getattr(facet, method_name, None)), f"composer-progress UoW facet has no {method_name}"

    transaction_property = getattr(sessions_protocol.SessionOperationMutationTransaction, "composer_progress", None)
    assert isinstance(transaction_property, property), "Sessions UoW has no composer_progress capability"
    source = inspect.getsource(ComposerProgressRegistry)
    assert "self._engine" in source
    assert "session_operation_authority" in source
    assert "composer_progress" in source
    assert "self._snapshots" not in source
    assert "self._user_index" not in source
    assert "self._inflight" not in source
    assert "datetime.now" not in source
    assert "threading" not in source


def test_production_progress_writes_are_awaited_context_bound_and_never_acquire_progress_kind() -> None:
    """Cancellation cannot strand a fire-and-forget local progress write."""
    source_root = Path(__file__).parents[4] / "src" / "elspeth"
    assert source_root.is_dir(), f"production source root not found: {source_root}"
    violations: list[str] = []
    publish_inventory: Counter[tuple[str, str]] = Counter()
    sink_inventory: Counter[tuple[str, str]] = Counter()
    for path in sorted(source_root.rglob("*.py")):
        path_violations, path_publishes, path_sinks = _progress_write_analysis(
            path.read_text(encoding="utf-8"),
            label=str(path.relative_to(source_root)),
        )
        violations.extend(path_violations)
        publish_inventory.update(path_publishes)
        sink_inventory.update(path_sinks)

    assert violations == []
    assert publish_inventory == Counter(
        {
            ("web/sessions/routes/messages.py", "register_message_routes.send_message"): 14,
            ("web/sessions/routes/composer/compose.py", "recompose"): 14,
            ("web/sessions/routes/composer/guided_chat_atomic.py", "post_guided_chat_schema8"): 4,
        }
    )
    assert sink_inventory == Counter(
        {
            ("web/sessions/routes/messages.py", "register_message_routes.send_message"): 1,
            ("web/sessions/routes/composer/compose.py", "recompose"): 1,
            ("web/sessions/routes/composer/guided.py", "post_guided_respond"): 3,
            ("web/sessions/routes/composer/guided_chat_atomic.py", "post_guided_chat_schema8"): 1,
            ("web/sessions/routes/composer/guided_plan.py", "post_guided_plan"): 1,
        }
    )


def test_progress_write_inventory_self_tests_join_context_and_lease_rules() -> None:
    canonical = """
async def persist(registry: ComposerProgressRegistry, authority, context, event):
    await registry.start_request(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    )
    await registry.publish(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    )
    await registry.finish_request(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        terminal_event=event,
    )
"""
    assert _progress_write_violations(canonical, label="canonical.py") == ()

    adversarial = """
async def persist(registry: ComposerProgressRegistry, authority, context, event):
    registry.begin_request('session')
    r = registry
    sink = r.publish
    asyncio.create_task(sink(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    ))
    helper = _publish_progress
    helper(session_operation_context=context, request_id='request', user_id='alice', event=event)
    dynamic = getattr(r, 'publish')
    asyncio.create_task(dynamic(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    ))
    holder.write = r.publish
    holder.write(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    )
    tuple_helper, tuple_sink = helpers._publish_progress, helpers._composer_progress_sink
    asyncio.create_task(tuple_helper(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    ))
    tuple_sink(session_operation_context=context, request_id='request', user_id='alice')
    helpers._publish_progress(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    )
    _composer_progress_sink(request_id='request', user_id='alice')
    r.clear('session')
    kind = SessionOperationKind.PROGRESS
    claim = authority.acquire
    claim(session_id, kind, owner_instance_id='nested', lease_seconds=30)
"""
    violations, publishes, sinks = _progress_write_analysis(adversarial, label="adversarial.py")
    assert any("legacy local begin_request" in violation for violation in violations)
    assert any("progress write is not joined" in violation for violation in violations)
    assert any("progress helper write is not joined" in violation for violation in violations)
    assert any("progress sink missing exact context" in violation for violation in violations)
    assert any("legacy local clear" in violation for violation in violations)
    assert any("nested PROGRESS lease" in violation for violation in violations)
    assert publishes == Counter({("adversarial.py", "persist"): 3})
    assert sinks == Counter({("adversarial.py", "persist"): 2})


@pytest.mark.parametrize(
    ("indirection", "expected_publishes"),
    (
        (
            "writer = getattr(registry, 'publish')\n    asyncio.create_task(writer({call_args}))",
            0,
        ),
        (
            "asyncio.create_task(getattr(registry, 'publish')({call_args}))",
            0,
        ),
        (
            "holder.writer = registry.publish\n    asyncio.create_task(holder.writer({call_args}))",
            0,
        ),
        (
            "helpers._publish_progress({call_args})",
            1,
        ),
        (
            "writer, unused = helpers._publish_progress, object()\n    asyncio.create_task(writer({call_args}))",
            1,
        ),
        (
            "holder.writer = helpers._publish_progress\n    asyncio.create_task(holder.writer({call_args}))",
            1,
        ),
        (
            "from somewhere import _publish_progress as emit\n    asyncio.create_task(emit({call_args}))",
            1,
        ),
        (
            "writer = functools.partial(registry.publish, session_operation_context=context)\n"
            "    asyncio.create_task(writer(request_id='request', user_id='alice', event=event))",
            0,
        ),
        (
            "writers = {'progress': registry.publish}\n    asyncio.create_task(writers['progress']({call_args}))",
            0,
        ),
    ),
)
def test_progress_write_inventory_rejects_each_fire_and_forget_indirection(
    indirection: str,
    expected_publishes: int,
) -> None:
    call_args = "session_operation_context=context, request_id='request', user_id='alice', event=event"
    source = f"""
async def persist(registry: ComposerProgressRegistry, context, event):
    {indirection.replace("{call_args}", call_args)}
"""

    violations, publishes, sinks = _progress_write_analysis(source, label="indirect.py")

    assert any("not joined" in violation for violation in violations)
    assert sum(publishes.values()) == expected_publishes
    assert sinks == Counter()


def test_progress_write_inventory_rejects_callable_escape_to_higher_order_scheduler() -> None:
    source = """
def schedule(writer, context, event):
    asyncio.create_task(writer(
        session_operation_context=context,
        request_id='request',
        user_id='alice',
        event=event,
    ))

async def persist(registry: ComposerProgressRegistry, context, event):
    schedule(registry.publish, context, event)
"""

    violations, publishes, sinks = _progress_write_analysis(source, label="higher_order.py")

    assert any("progress callable escapes joined-call audit" in violation for violation in violations)
    assert publishes == Counter()
    assert sinks == Counter()


@pytest.mark.asyncio
async def test_independent_engines_reconnect_to_start_publish_and_finish(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    context = _acquire(first_authority, session_id=session.id)
    first_registry = _registry(first_engine, first_authority)
    second_registry = _registry(second_engine, second_authority)

    starting = await _start(first_registry, context)
    reconnected_start = await second_registry.get_latest(str(session.id))
    assert reconnected_start == starting
    assert reconnected_start.inflight_requests == 1

    published = await _publish(
        first_registry,
        context,
        event_value=_progress_event("using_tools", label="published"),
    )
    third_registry = _registry(second_engine, second_authority)
    assert await third_registry.get_latest(str(session.id)) == published

    finished = await _finish(
        first_registry,
        context,
        terminal_event=_progress_event("complete", label="finished"),
    )
    reconnected_finish = await second_registry.get_latest(str(session.id))
    assert reconnected_finish == finished
    assert reconnected_finish.inflight_requests == 0
    assert await third_registry.list_active(user_id="alice") == ()


@pytest.mark.asyncio
async def test_multiple_publishes_coalesce_to_one_latest_snapshot(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)

    first = await _publish(registry, context, event_value=_progress_event("calling_model", label="first"))
    second = await _publish(registry, context, event_value=_progress_event("validating", label="second"))

    assert first.updated_at < second.updated_at
    assert await registry.get_latest(str(session.id)) == second
    with first_engine.connect() as connection:
        count = connection.execute(
            select(func.count())
            .select_from(composer_progress_snapshots_table)
            .where(composer_progress_snapshots_table.c.session_id == str(session.id))
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_concurrent_same_operation_publishes_serialize_and_coalesce(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    context = _acquire(first_authority, session_id=session.id)
    first_registry = _registry(first_engine, first_authority)
    second_registry = _registry(second_engine, second_authority)
    await _start(first_registry, context)

    start_barrier = threading.Barrier(2)

    def emit_from_independent_thread(
        registry: ComposerProgressRegistry,
        event_value: ComposerProgressEvent,
    ) -> ComposerProgressSnapshot:
        start_barrier.wait(timeout=5)
        return asyncio.run(_publish(registry, context, event_value=event_value))

    first, second = await asyncio.gather(
        asyncio.to_thread(
            emit_from_independent_thread,
            first_registry,
            _progress_event("using_tools", label="concurrent-a"),
        ),
        asyncio.to_thread(
            emit_from_independent_thread,
            second_registry,
            _progress_event("validating", label="concurrent-b"),
        ),
    )

    assert first.updated_at != second.updated_at
    latest = await _registry(second_engine, second_authority).get_latest(str(session.id))
    assert latest == max((first, second), key=lambda snapshot: snapshot.updated_at)
    with second_engine.connect() as connection:
        rows = connection.execute(
            select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == str(session.id))
        ).all()
    assert len(rows) == 1
    assert rows[0].operation_id == context.fence.operation_id
    assert rows[0].operation_epoch == context.fence.operation_epoch


@pytest.mark.asyncio
async def test_exact_start_and_finish_retries_are_idempotent(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)

    first = await _start(registry, context)
    assert await _start(registry, context) == first
    before_active_conflict = _rows(first_engine)
    before_active_conflict_all = _all_sessions_state(first_engine)
    with _capture_progress_dml(first_engine) as active_conflict_dml, pytest.raises(AuditIntegrityError):
        await _start(
            registry,
            context,
            event_value=_progress_event("starting", label="conflicting-active-start"),
        )
    assert active_conflict_dml == []
    assert _rows(first_engine) == before_active_conflict
    assert _all_sessions_state(first_engine) == before_active_conflict_all
    terminal = _progress_event("complete")
    finished = await _finish(registry, context, terminal_event=terminal)
    assert await _finish(registry, context, terminal_event=terminal) == finished
    assert len(_rows(first_engine)["inflight"]) == 1
    assert len(_rows(first_engine)["snapshots"]) == 1

    for conflicting_action in (
        lambda: _start(
            registry,
            context,
            event_value=_progress_event("starting", label="conflicting-start"),
        ),
        lambda: _finish(
            registry,
            context,
            terminal_event=_progress_event("failed", label="conflicting-finish"),
        ),
    ):
        before = _rows(first_engine)
        with _capture_progress_dml(first_engine) as statements, pytest.raises(AuditIntegrityError):
            await conflicting_action()
        assert statements == []
        assert _rows(first_engine) == before


@pytest.mark.asyncio
async def test_terminal_snapshot_remains_active_until_exact_finish_commits(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)

    terminal = await _publish(registry, context, event_value=_progress_event("complete"))
    active = await registry.list_active(user_id="alice")
    assert active == (terminal.model_copy(update={"inflight_requests": 1}),)


@pytest.mark.asyncio
async def test_nonterminal_snapshot_is_inactive_after_finish(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)
    await _publish(registry, context, event_value=_progress_event("using_tools"))

    await _finish(registry, context)

    latest = await registry.get_latest(str(session.id))
    assert latest.phase == "using_tools"
    assert latest.inflight_requests == 0
    assert await registry.list_active(user_id="alice") == ()


@pytest.mark.asyncio
async def test_released_incomplete_request_is_not_live_even_with_future_expiry(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    observer = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)
    with first_engine.begin() as connection:
        connection.execute(
            update(composer_inflight_requests_table)
            .where(composer_inflight_requests_table.c.request_id == "request-a")
            .values(expires_at=datetime.now(UTC) + timedelta(days=30))
        )

    authority.release(context)

    assert await observer.list_active(user_id="alice") == ()
    latest = await observer.get_latest(str(session.id))
    assert latest.inflight_requests == 0


@pytest.mark.asyncio
async def test_expired_fence_makes_incomplete_request_inactive(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)
    with first_engine.begin() as connection:
        connection.execute(
            update(composer_inflight_requests_table)
            .where(composer_inflight_requests_table.c.request_id == "request-a")
            .values(expires_at=datetime.now(UTC) + timedelta(days=30))
        )
        connection.execute(
            update(session_operation_fences_table)
            .where(session_operation_fences_table.c.session_id == str(session.id))
            .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
        )

    observer = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    assert await observer.list_active(user_id="alice") == ()
    assert (await observer.get_latest(str(session.id))).inflight_requests == 0


@pytest.mark.asyncio
async def test_user_scoping_is_reconstructed_from_durable_rows(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    registry = _registry(first_engine, authority)
    contexts: list[SessionOperationContext] = []
    for user_id in ("alice", "bob"):
        session = _create_session(authority, user_id=user_id)
        context = _acquire(authority, session_id=session.id, owner=f"owner-{user_id}")
        contexts.append(context)
        await _start(
            registry,
            context,
            request_id=f"request-{user_id}",
            user_id=user_id,
            event_value=_progress_event("starting", label=user_id),
        )

    fresh = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    alice = await fresh.list_active(user_id="alice")
    bob = await fresh.list_active(user_id="bob")
    assert [snapshot.session_id for snapshot in alice] == [contexts[0].fence.session_id]
    assert [snapshot.session_id for snapshot in bob] == [contexts[1].fence.session_id]


@pytest.mark.asyncio
async def test_stale_worker_after_takeover_has_zero_progress_dml_or_state_change(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    stale = _acquire(first_authority, session_id=session.id, owner="owner-a")
    first_registry = _registry(first_engine, first_authority)
    second_registry = _registry(second_engine, second_authority)
    await _start(first_registry, stale, request_id="request-a")
    first_authority.release(stale)
    current = _acquire(second_authority, session_id=session.id, owner="owner-b")
    await _start(
        second_registry,
        current,
        request_id="request-b",
        event_value=_progress_event("starting", label="winner"),
    )
    winner_active = await second_registry.list_active(user_id="alice")
    assert len(winner_active) == 1
    assert winner_active[0].request_id == "request-b"
    assert winner_active[0].inflight_requests == 1

    for action in (
        lambda: _start(
            first_registry,
            stale,
            request_id="request-a",
            event_value=_progress_event("starting", label="stale-retry"),
        ),
        lambda: _publish(first_registry, stale, request_id="request-a", event_value=_progress_event("saving", label="stale")),
        lambda: _finish(first_registry, stale, request_id="request-a", terminal_event=_progress_event("failed", label="stale")),
    ):
        before = _rows(second_engine)
        before_all = _all_sessions_state(second_engine)
        with _capture_progress_dml(first_engine) as statements, pytest.raises(SessionOperationFenceLost):
            await action()
        assert statements == []
        assert _rows(second_engine) == before
        assert _all_sessions_state(second_engine) == before_all


@pytest.mark.parametrize("method_name", ("start_request", "publish", "finish_request"))
@pytest.mark.parametrize("wrong_kind", [kind for kind in SessionOperationKind if kind is not SessionOperationKind.COMPOSE])
@pytest.mark.asyncio
async def test_every_wrong_live_operation_kind_performs_zero_progress_dml(
    durable_engines,
    wrong_kind,
    method_name,
) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    registry = _registry(first_engine, authority)
    if method_name == "start_request":
        context = _acquire(authority, session_id=session.id, kind=wrong_kind)
        expected_rows = {"inflight": (), "snapshots": ()}
    else:
        valid = _acquire(authority, session_id=session.id)
        await _start(registry, valid)
        authority.release(valid)
        context = _acquire(authority, session_id=session.id, kind=wrong_kind)
        expected_rows = _rows(first_engine)
    expected_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises((AuditIntegrityError, SessionOperationFenceLost)):
        await _invoke_progress_write(method_name, registry, context)

    assert statements == []
    assert _rows(first_engine) == expected_rows
    assert _all_sessions_state(first_engine) == expected_all


@pytest.mark.parametrize("method_name", ("start_request", "publish", "finish_request"))
@pytest.mark.parametrize("wrong_kind", [kind for kind in SessionOperationKind if kind is not SessionOperationKind.COMPOSE])
@pytest.mark.asyncio
async def test_forged_wrong_kind_on_current_compose_fence_performs_zero_progress_dml(
    durable_engines,
    wrong_kind,
    method_name,
) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    valid = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    if method_name != "start_request":
        await _start(registry, valid)
    forged = SessionOperationContext(fence=valid.fence, operation_kind=wrong_kind)
    expected_rows = _rows(first_engine)
    expected_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises((AuditIntegrityError, SessionOperationFenceLost)):
        await _invoke_progress_write(
            method_name,
            registry,
            forged,
            request_id="request-unique" if method_name == "start_request" else "request-a",
        )

    assert statements == []
    assert _rows(first_engine) == expected_rows
    assert _all_sessions_state(first_engine) == expected_all


@pytest.mark.parametrize(
    ("method_name", "request_id"),
    (
        pytest.param("start_request", "request-a", id="start-exact-retry"),
        pytest.param("start_request", "request-unique", id="start-unique-request"),
        pytest.param("publish", "request-a", id="publish"),
        pytest.param("finish_request", "request-a", id="finish"),
    ),
)
@pytest.mark.parametrize("invalidated_by", ("release", "expiry"))
@pytest.mark.asyncio
async def test_released_or_expired_compose_fence_rejects_progress_before_takeover(
    durable_engines,
    invalidated_by,
    method_name,
    request_id,
) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)
    if invalidated_by == "release":
        authority.release(context)
    else:
        with first_engine.begin() as connection:
            connection.execute(
                update(session_operation_fences_table)
                .where(session_operation_fences_table.c.session_id == str(session.id))
                .values(lease_expires_at=datetime(2000, 1, 1, tzinfo=UTC))
            )
    expected_rows = _rows(first_engine)
    expected_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises(SessionOperationFenceLost):
        await _invoke_progress_write(method_name, registry, context, request_id=request_id)

    assert statements == []
    assert _rows(first_engine) == expected_rows
    assert _all_sessions_state(first_engine) == expected_all


@pytest.mark.parametrize("method_name", ("publish", "finish_request"))
@pytest.mark.asyncio
async def test_wrong_request_and_user_identities_fail_before_progress_dml(durable_engines, method_name) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)

    for request_id, user_id in (("request-other", "alice"), ("request-a", "mallory")):
        before = _rows(first_engine)
        before_all = _all_sessions_state(first_engine)
        with _capture_progress_dml(first_engine) as statements, pytest.raises(AuditIntegrityError):
            await _invoke_progress_write(
                method_name,
                registry,
                context,
                request_id=request_id,
                user_id=user_id,
            )
        assert statements == []
        assert _rows(first_engine) == before
        assert _all_sessions_state(first_engine) == before_all


@pytest.mark.asyncio
async def test_start_rejects_user_who_does_not_own_session_without_progress_dml(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority, user_id="alice")
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    before_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises(AuditIntegrityError):
        await _start(registry, context, user_id="mallory")

    assert statements == []
    assert _rows(first_engine) == {"inflight": (), "snapshots": ()}
    assert _all_sessions_state(first_engine) == before_all


@pytest.mark.asyncio
async def test_start_rejects_request_id_already_bound_to_another_session(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    registry = _registry(first_engine, authority)
    first_session = _create_session(authority)
    first_context = _acquire(authority, session_id=first_session.id, owner="first-owner")
    await _start(registry, first_context, request_id="request-shared")
    second_session = _create_session(authority)
    second_context = _acquire(authority, session_id=second_session.id, owner="second-owner")
    before = _rows(first_engine)
    before_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises(AuditIntegrityError):
        await _start(registry, second_context, request_id="request-shared")

    assert statements == []
    assert _rows(first_engine) == before
    assert _all_sessions_state(first_engine) == before_all


@pytest.mark.parametrize("method_name", ("start_request", "publish", "finish_request"))
@pytest.mark.asyncio
async def test_wrong_session_operation_id_token_and_epoch_have_zero_progress_dml(durable_engines, method_name) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    if method_name != "start_request":
        await _start(registry, context)
    foreign_session = _create_session(authority)
    invalid_session_context = _forge_context(context, session_id=str(foreign_session.id))
    invalid_contexts = (
        (invalid_session_context, SessionOperationFenceLost),
        (_forge_context(context, operation_id="wrong-operation-id"), SessionOperationFenceLost),
        (_forge_context(context, lease_token="wrong-lease-token"), SessionOperationFenceLost),
        (_forge_context(context, epoch=context.fence.operation_epoch + 1), SessionOperationFenceLost),
    )

    for invalid, expected_error in invalid_contexts:
        before = _rows(first_engine)
        before_all = _all_sessions_state(first_engine)
        with _capture_progress_dml(first_engine) as statements, pytest.raises(expected_error):
            await _invoke_progress_write(
                method_name,
                registry,
                invalid,
                request_id="request-unique" if method_name == "start_request" else "request-a",
            )
        assert statements == []
        assert _rows(first_engine) == before
        assert _all_sessions_state(first_engine) == before_all


@pytest.mark.asyncio
async def test_live_archive_context_atomically_retires_progress_and_inflight(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    compose = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, compose)
    authority.release(compose)
    archive = _acquire(
        authority,
        session_id=session.id,
        kind=SessionOperationKind.ARCHIVE,
        owner="archive-owner",
    )

    _retire_session_progress(authority, archive)

    assert _rows(first_engine) == {"inflight": (), "snapshots": ()}
    observer = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    assert (await observer.get_latest(str(session.id))).phase == "idle"
    assert await observer.list_active(user_id="alice") == ()


@pytest.mark.asyncio
async def test_progress_retirement_rolls_back_both_rows_when_second_delete_fails(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    compose = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, compose)
    authority.release(compose)
    archive = _acquire(
        authority,
        session_id=session.id,
        kind=SessionOperationKind.ARCHIVE,
        owner="archive-owner",
    )
    before = _rows(first_engine)

    with _fail_second_progress_dml(first_engine, operation="delete "), pytest.raises(InjectedProgressWriteFailure):
        _retire_session_progress(authority, archive)

    assert _rows(first_engine) == before


@pytest.mark.asyncio
async def test_wrong_kind_and_stale_archive_cannot_retire_progress(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    compose = _acquire(first_authority, session_id=session.id)
    registry = _registry(first_engine, first_authority)
    await _start(registry, compose)
    before = _rows(first_engine)
    before_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as wrong_kind_dml, pytest.raises(AuditIntegrityError):
        _retire_session_progress(first_authority, compose)
    assert wrong_kind_dml == []
    assert _rows(first_engine) == before
    assert _all_sessions_state(first_engine) == before_all

    first_authority.release(compose)
    stale_archive = _acquire(
        first_authority,
        session_id=session.id,
        kind=SessionOperationKind.ARCHIVE,
        owner="archive-a",
    )
    first_authority.release(stale_archive)
    current_archive = _acquire(
        second_authority,
        session_id=session.id,
        kind=SessionOperationKind.ARCHIVE,
        owner="archive-b",
    )
    before_stale = _all_sessions_state(second_engine)
    with _capture_progress_dml(first_engine) as stale_dml, pytest.raises(SessionOperationFenceLost):
        _retire_session_progress(first_authority, stale_archive)
    assert stale_dml == []
    assert _rows(second_engine) == before
    assert _all_sessions_state(second_engine) == before_stale
    assert current_archive.operation_kind is SessionOperationKind.ARCHIVE


@pytest.mark.parametrize(
    ("request_id", "user_id", "event_value"),
    (
        ("", "alice", _progress_event("starting")),
        ("request-a", "", _progress_event("starting")),
        (cast(Any, True), "alice", _progress_event("starting")),
        ("request-a", cast(Any, object()), _progress_event("starting")),
        ("request-a", "alice", cast(Any, object())),
    ),
)
@pytest.mark.parametrize("method_name", ("start_request", "publish", "finish_request"))
@pytest.mark.asyncio
async def test_malformed_request_identities_fail_closed_without_dml(
    durable_engines,
    method_name,
    request_id,
    user_id,
    event_value,
) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    if method_name != "start_request":
        await _start(registry, context)
    before = _rows(first_engine)
    before_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises((TypeError, ValueError)):
        await _invoke_progress_write(
            method_name,
            registry,
            context,
            request_id=request_id,
            user_id=user_id,
            event_value=event_value,
        )

    assert statements == []
    assert _rows(first_engine) == before
    assert _all_sessions_state(first_engine) == before_all


@pytest.mark.parametrize("method_name", ("start_request", "publish", "finish_request"))
@pytest.mark.asyncio
async def test_non_context_object_fails_closed_without_dml(durable_engines, method_name) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    registry = _registry(first_engine, authority)
    if method_name != "start_request":
        context = _acquire(authority, session_id=session.id)
        await _start(registry, context)
    before = _rows(first_engine)
    before_all = _all_sessions_state(first_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises(TypeError):
        await _invoke_progress_write(method_name, registry, cast(SessionOperationContext, object()))

    assert statements == []
    assert _rows(first_engine) == before
    assert _all_sessions_state(first_engine) == before_all


@pytest.mark.asyncio
async def test_database_clock_rollback_never_regresses_snapshot_timestamp(durable_engines, monkeypatch) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)
    future = datetime.now(UTC) + timedelta(days=1)
    with first_engine.begin() as connection:
        connection.execute(
            update(composer_progress_snapshots_table)
            .where(composer_progress_snapshots_table.c.session_id == str(session.id))
            .values(updated_at=future)
        )
        connection.execute(
            update(composer_inflight_requests_table)
            .where(composer_inflight_requests_table.c.request_id == "request-a")
            .values(updated_at=future)
        )
    rolled_back = datetime.now(UTC) - timedelta(days=1)
    monkeypatch.setattr(authority, "_database_now", lambda _connection: rolled_back)

    published = await _publish(registry, context, event_value=_progress_event("saving"))

    assert published.updated_at > future
    assert (published.updated_at - future) >= timedelta(microseconds=1)
    with first_engine.connect() as connection:
        inflight_updated_at = connection.execute(
            select(composer_inflight_requests_table.c.updated_at).where(composer_inflight_requests_table.c.request_id == "request-a")
        ).scalar_one()
    assert inflight_updated_at > future
    assert (inflight_updated_at - future) >= timedelta(microseconds=1)


@pytest.mark.asyncio
async def test_higher_epoch_under_clock_rollback_is_visible_and_monotonic(durable_engines, monkeypatch) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    first_context = _acquire(first_authority, session_id=session.id, owner="owner-a")
    first_registry = _registry(first_engine, first_authority)
    await _start(first_registry, first_context, request_id="request-a")
    await _finish(first_registry, first_context, request_id="request-a")
    first_authority.release(first_context)
    second_context = _acquire(second_authority, session_id=session.id, owner="owner-b")
    future = datetime.now(UTC) + timedelta(days=1)
    with first_engine.begin() as connection:
        connection.execute(
            update(composer_progress_snapshots_table)
            .where(composer_progress_snapshots_table.c.session_id == str(session.id))
            .values(updated_at=future)
        )
    rolled_back = datetime.now(UTC) - timedelta(days=1)
    monkeypatch.setattr(second_authority, "_database_now", lambda _connection: rolled_back)
    second_registry = _registry(second_engine, second_authority)

    snapshot = await _start(
        second_registry,
        second_context,
        request_id="request-b",
        event_value=_progress_event("starting", label="takeover"),
    )

    assert second_context.fence.operation_epoch > first_context.fence.operation_epoch
    assert snapshot.updated_at > future
    with second_engine.connect() as connection:
        row = connection.execute(
            select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == str(session.id))
        ).one()
    assert row.operation_epoch == second_context.fence.operation_epoch
    assert row.operation_id == second_context.fence.operation_id


@pytest.mark.asyncio
async def test_start_rolls_back_inflight_when_snapshot_write_fails(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    notifications: list[ComposerProgressSnapshot] = []

    async def notify_committed(snapshot: ComposerProgressSnapshot) -> None:
        notifications.append(snapshot)

    registry = _registry(first_engine, authority, notify_committed=notify_committed)

    with (
        _fail_snapshot_after_inflight_write(first_engine, inflight_operation="insert "),
        pytest.raises(InjectedProgressWriteFailure),
    ):
        await _start(registry, context)

    assert _rows(first_engine) == {"inflight": (), "snapshots": ()}
    assert notifications == []
    assert not hasattr(registry, "_snapshots")
    assert not hasattr(registry, "_inflight")


@pytest.mark.asyncio
async def test_start_inflight_insert_failure_has_no_snapshot_or_notification(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    notifications: list[ComposerProgressSnapshot] = []

    async def notify_committed(snapshot: ComposerProgressSnapshot) -> None:
        notifications.append(snapshot)

    registry = _registry(first_engine, authority, notify_committed=notify_committed)
    with (
        _fail_target_write(
            first_engine,
            table_name=composer_inflight_requests_table.name,
            operation="insert ",
        ),
        pytest.raises(InjectedProgressWriteFailure),
    ):
        await _start(registry, context)

    assert _rows(first_engine) == {"inflight": (), "snapshots": ()}
    assert notifications == []


@pytest.mark.asyncio
async def test_failed_publish_preserves_previous_committed_snapshot(durable_engines) -> None:
    first_engine, _second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    notifications: list[ComposerProgressSnapshot] = []

    async def notify_committed(snapshot: ComposerProgressSnapshot) -> None:
        notifications.append(snapshot)

    registry = _registry(first_engine, authority, notify_committed=notify_committed)
    previous = await _start(registry, context)
    notifications.clear()
    before = _rows(first_engine)
    before_all = _all_sessions_state(first_engine)

    with (
        _fail_snapshot_after_inflight_write(first_engine, inflight_operation="update "),
        pytest.raises(InjectedProgressWriteFailure),
    ):
        await _publish(registry, context, event_value=_progress_event("using_tools", label="must-rollback"))

    assert _rows(first_engine) == before
    assert _all_sessions_state(first_engine) == before_all
    assert await registry.get_latest(str(session.id)) == previous
    assert notifications == []


@pytest.mark.asyncio
async def test_failed_finish_leaves_request_durably_active(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    notifications: list[ComposerProgressSnapshot] = []

    async def notify_committed(snapshot: ComposerProgressSnapshot) -> None:
        notifications.append(snapshot)

    registry = _registry(first_engine, authority, notify_committed=notify_committed)
    previous = await _start(registry, context)
    notifications.clear()

    with (
        _fail_second_progress_dml(first_engine),
        pytest.raises(InjectedProgressWriteFailure),
    ):
        await _finish(registry, context, terminal_event=_progress_event("complete", label="must-rollback"))

    observer = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    assert await observer.get_latest(str(session.id)) == previous.model_copy(update={"inflight_requests": 1})
    assert len(await observer.list_active(user_id="alice")) == 1
    assert notifications == []


@pytest.mark.asyncio
async def test_every_progress_write_notifies_once_only_after_independent_observer_sees_commit(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    observations: list[tuple[ComposerProgressSnapshot, dict[str, object], object]] = []

    async def notify_committed(snapshot: ComposerProgressSnapshot) -> None:
        with second_engine.connect() as connection:
            row = connection.execute(
                select(composer_progress_snapshots_table).where(composer_progress_snapshots_table.c.session_id == str(session.id))
            ).one()
            completed_at = connection.execute(
                select(composer_inflight_requests_table.c.completed_at).where(composer_inflight_requests_table.c.request_id == "request-a")
            ).scalar_one()
        observations.append((snapshot, dict(row._mapping), completed_at))

    registry = _registry(first_engine, authority, notify_committed=notify_committed)
    started = await _start(registry, context)
    assert len(observations) == 1
    assert observations[0][0] == started
    assert observations[0][1]["phase"] == started.phase
    assert observations[0][2] is None

    published = await _publish(registry, context, event_value=_progress_event("validating", label="committed"))
    assert len(observations) == 2
    assert observations[1][0] == published
    assert observations[1][1]["phase"] == published.phase
    assert observations[1][2] is None

    finished = await _finish(
        registry,
        context,
        terminal_event=_progress_event("complete", label="committed-finish"),
    )
    assert len(observations) == 3
    notified, committed, completed_at = observations[2]
    assert notified == finished
    assert committed["phase"] == finished.phase
    assert committed["headline"] == finished.headline
    assert committed["operation_id"] == context.fence.operation_id
    assert committed["operation_epoch"] == context.fence.operation_epoch
    assert completed_at is not None


@pytest.mark.asyncio
async def test_cancellation_finish_is_durable_before_lease_close(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    authority = SQLiteLocalSessionOperationAuthority(first_engine)
    session = _create_session(authority)
    context = _acquire(authority, session_id=session.id)
    registry = _registry(first_engine, authority)
    await _start(registry, context)

    cancelled = await _finish(registry, context, terminal_event=_progress_event("cancelled"))
    authority.release(context)

    observer = _registry(second_engine, SQLiteLocalSessionOperationAuthority(second_engine))
    assert (await observer.get_latest(str(session.id))) == cancelled.model_copy(update={"inflight_requests": 0})
    assert await observer.list_active(user_id="alice") == ()


@pytest.mark.asyncio
async def test_delayed_cancellation_child_after_takeover_has_zero_dml_and_cannot_replace_winner(durable_engines) -> None:
    first_engine, second_engine = durable_engines
    first_authority = SQLiteLocalSessionOperationAuthority(first_engine)
    second_authority = SQLiteLocalSessionOperationAuthority(second_engine)
    session = _create_session(first_authority)
    stale = _acquire(first_authority, session_id=session.id, owner="cancel-owner-a")
    stale_notifications: list[ComposerProgressSnapshot] = []

    async def notify_stale(snapshot: ComposerProgressSnapshot) -> None:
        stale_notifications.append(snapshot)

    first_registry = _registry(first_engine, first_authority, notify_committed=notify_stale)
    second_registry = _registry(second_engine, second_authority)
    await _start(first_registry, stale, request_id="request-a")
    stale_notifications.clear()
    resume_child = asyncio.Event()

    async def delayed_cancellation() -> ComposerProgressSnapshot:
        await resume_child.wait()
        return await _finish(
            first_registry,
            stale,
            request_id="request-a",
            terminal_event=_progress_event("cancelled", label="stale-cancellation"),
        )

    child = asyncio.create_task(delayed_cancellation())
    await asyncio.sleep(0)
    first_authority.release(stale)
    winner = _acquire(second_authority, session_id=session.id, owner="cancel-owner-b")
    await _start(
        second_registry,
        winner,
        request_id="request-b",
        event_value=_progress_event("starting", label="winner"),
    )
    before = _rows(second_engine)
    before_all = _all_sessions_state(second_engine)

    with _capture_progress_dml(first_engine) as statements, pytest.raises(SessionOperationFenceLost):
        resume_child.set()
        await child

    assert statements == []
    assert stale_notifications == []
    assert _rows(second_engine) == before
    assert _all_sessions_state(second_engine) == before_all
