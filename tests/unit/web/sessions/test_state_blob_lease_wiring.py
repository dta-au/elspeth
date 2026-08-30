"""Static authority contracts for state import/export blob reads."""

from __future__ import annotations

import ast
import inspect

from elspeth.web.sessions.routes.composer import state as state_routes


def _function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    matches = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _calls(function: ast.AsyncFunctionDef, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == name) or (isinstance(node.func, ast.Name) and node.func.id == name)
        )
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr:
    matches = [keyword.value for keyword in call.keywords if keyword.arg == name]
    assert len(matches) == 1
    return matches[0]


def _assert_exact_route_lease(
    function: ast.AsyncFunctionDef,
    *,
    operation_kind: str,
    helper_name: str,
) -> None:
    acquire_calls = _calls(function, "acquire")
    assert len(acquire_calls) == 1
    acquire = acquire_calls[0]
    assert ast.unparse(acquire.func) == "SessionOperationLease.acquire"
    assert len(acquire.args) == 1
    assert ast.unparse(acquire.args[0]) == "service.session_operation_authority"
    assert ast.unparse(_keyword(acquire, "session_id")) == "session.id"
    assert ast.unparse(_keyword(acquire, "operation_kind")) == f"SessionOperationKind.{operation_kind}"
    assert ast.unparse(_keyword(acquire, "owner_instance_id")) == "service.session_operation_owner_instance_id"
    assert ast.unparse(_keyword(acquire, "lease_seconds")) == "service.session_operation_lease_seconds"

    helper_calls = [call for call in _calls(function, helper_name) if ast.unparse(call.func) == helper_name]
    assert len(helper_calls) == 1
    assert ast.unparse(_keyword(helper_calls[0], "session_operation_context")) == "lease.context"

    finally_closes = [
        statement
        for try_node in ast.walk(function)
        if isinstance(try_node, ast.Try)
        for statement in try_node.finalbody
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Await)
        and isinstance(statement.value.value, ast.Call)
        and ast.unparse(statement.value.value.func) == "lease.close"
        and not statement.value.value.args
        and not statement.value.value.keywords
    ]
    assert len(finally_closes) == 1


def test_state_import_owns_one_compose_lease_and_threads_its_context() -> None:
    tree = ast.parse(inspect.getsource(state_routes))
    endpoint = _function(tree, "import_state_yaml")
    _assert_exact_route_lease(
        endpoint,
        operation_kind="COMPOSE",
        helper_name="_state_with_imported_source_blobs",
    )

    helper = _function(tree, "_state_with_imported_source_blobs")
    context_arg = helper.args.kwonlyargs[-1]
    assert context_arg.arg == "session_operation_context"
    assert helper.args.kw_defaults[-1] is None
    get_blob_calls = _calls(helper, "get_blob")
    assert len(get_blob_calls) == 1
    assert ast.unparse(_keyword(get_blob_calls[0], "session_operation_context")) == "session_operation_context"


def test_yaml_export_owns_one_blob_read_lease_and_threads_its_context() -> None:
    tree = ast.parse(inspect.getsource(state_routes))
    endpoint = _function(tree, "get_state_yaml")
    _assert_exact_route_lease(
        endpoint,
        operation_kind="BLOB_READ",
        helper_name="_verified_yaml_export_blob_ids",
    )

    helper = _function(tree, "_verified_yaml_export_blob_ids")
    context_arg = helper.args.kwonlyargs[-1]
    assert context_arg.arg == "session_operation_context"
    assert helper.args.kw_defaults[-1] is None
    get_blob_calls = _calls(helper, "get_blob")
    assert len(get_blob_calls) == 1
    assert ast.unparse(_keyword(get_blob_calls[0], "session_operation_context")) == "session_operation_context"
