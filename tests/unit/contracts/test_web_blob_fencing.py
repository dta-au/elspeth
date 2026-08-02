"""Authoritative contract gate for standalone web-blob fencing."""

from __future__ import annotations

import ast
import hashlib
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.blobs import BlobServiceProtocol as L0BlobServiceProtocol
from elspeth.web.blobs import routes as blob_routes
from elspeth.web.blobs.protocol import BlobServiceProtocol as WebBlobServiceProtocol
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.coordination.contracts import (
    WEB_COORDINATION_PROTOCOL_VERSION,
    SessionOperationContext,
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    SESSION_SCHEMA_EPOCH,
    blobs_table,
    session_operation_fences_table,
)
from elspeth.web.sessions.schema import initialize_session_schema

_ROOT = Path(__file__).parents[3]
_BLOB_ROUTES = _ROOT / "src/elspeth/web/blobs/routes.py"
_CONTEXT_PARAMETER = "session_operation_context"


def test_blob_protocols_and_implementation_require_exact_operation_context() -> None:
    """No L0, web-protocol, or implementation compatibility bypass may remain."""
    methods = (
        "create_blob",
        "get_blob",
        "delete_blob",
        "read_blob_content",
        "read_blob_preview",
    )
    surfaces = (
        ("L0 BlobServiceProtocol", L0BlobServiceProtocol),
        ("web BlobServiceProtocol", WebBlobServiceProtocol),
        ("BlobServiceImpl", BlobServiceImpl),
    )
    violations: list[str] = []

    for surface_name, surface in surfaces:
        for method_name in methods:
            method = getattr(surface, method_name, None)
            if method is None:
                violations.append(f"{surface_name}.{method_name} is missing")
                continue
            parameter = inspect.signature(method).parameters.get(_CONTEXT_PARAMETER)
            if parameter is None:
                violations.append(f"{surface_name}.{method_name} lacks {_CONTEXT_PARAMETER}")
                continue
            if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
                violations.append(f"{surface_name}.{method_name}.{_CONTEXT_PARAMETER} is not keyword-only")
            if parameter.default is not inspect.Parameter.empty:
                violations.append(f"{surface_name}.{method_name}.{_CONTEXT_PARAMETER} has a default bypass")
            try:
                annotation = get_type_hints(method)[_CONTEXT_PARAMETER]
            except (KeyError, NameError, TypeError) as exc:
                violations.append(f"{surface_name}.{method_name}.{_CONTEXT_PARAMETER} does not resolve: {type(exc).__name__}")
            else:
                if annotation is not SessionOperationContext:
                    violations.append(f"{surface_name}.{method_name}.{_CONTEXT_PARAMETER} is not exact SessionOperationContext")

    assert not violations, "\n".join(violations)


def _owned_blob_helper_violations(tree: ast.AST) -> list[str]:
    definitions = _endpoint_definitions(tree, "_get_owned_blob")
    if len(definitions) != 1:
        return [f"_get_owned_blob: expected exactly one definition; found {len(definitions)}"]
    helper = definitions[0]
    calls = [
        candidate
        for candidate in _runtime_nodes(helper, root_is_scope=True)
        if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Attribute) and candidate.func.attr == "get_blob"
    ]
    if len(calls) != 1:
        return [f"_get_owned_blob: expected exactly one get_blob call; found {len(calls)}"]
    call = calls[0]
    violations: list[str] = []
    helper_nodes = _nodes_in_statements(helper.body)
    nested_protected_uses = _nested_scope_protected_uses(
        helper,
        {_CONTEXT_PARAMETER, "blob_service", "session_service"},
    )
    if nested_protected_uses:
        violations.append("_get_owned_blob: nested scopes must not capture, shadow, or mutate protected bindings")
    context_rebindings = [
        candidate
        for candidate in helper_nodes
        if isinstance(candidate, ast.Name) and candidate.id == _CONTEXT_PARAMETER and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    if context_rebindings:
        violations.append("_get_owned_blob: session_operation_context must not be rebound or deleted")
    receiver_rebindings = [
        candidate
        for candidate in helper_nodes
        if isinstance(candidate, ast.Name) and candidate.id == "blob_service" and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    if receiver_rebindings:
        violations.append("_get_owned_blob: blob_service must not be rebound or deleted")
    if _dotted_name(call.func.value) != "blob_service":
        violations.append("_get_owned_blob: get_blob must use exact receiver blob_service")
    directly_awaited = _directly_awaited_calls(helper_nodes)
    if call not in directly_awaited:
        violations.append("_get_owned_blob: blob_service.get_blob must itself be directly awaited")
    callable_references = [
        candidate
        for candidate in helper_nodes
        if isinstance(candidate, ast.Attribute) and _dotted_name(candidate) == "blob_service.get_blob"
    ]
    if len(callable_references) != 1 or callable_references[0] is not call.func:
        violations.append("_get_owned_blob: blob_service.get_blob callable must not escape its exact awaited call")
    context_values = [keyword.value for keyword in call.keywords if keyword.arg == _CONTEXT_PARAMETER]
    if len(context_values) != 1 or _dotted_name(context_values[0]) != _CONTEXT_PARAMETER:
        violations.append("_get_owned_blob: get_blob must forward exact session_operation_context")
    if not _call_matches_shape(call, positional=("blob_id",), keywords={}):
        violations.append("_get_owned_blob: get_blob must receive exact blob_id and only the exact context keyword")
    if _is_statically_unreachable(call, helper):
        violations.append("_get_owned_blob: get_blob effect is statically unreachable")
    return violations


def test_owned_blob_helper_requires_and_forwards_exact_operation_context() -> None:
    helper = blob_routes._get_owned_blob
    parameter = inspect.signature(helper).parameters.get(_CONTEXT_PARAMETER)
    assert parameter is not None, "_get_owned_blob lacks session_operation_context"
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(helper)[_CONTEXT_PARAMETER] is SessionOperationContext

    tree = ast.parse(_BLOB_ROUTES.read_text(encoding="utf-8"), filename=str(_BLOB_ROUTES))
    assert _owned_blob_helper_violations(tree) == []


@dataclass(frozen=True, slots=True)
class _EndpointContract:
    name: str
    operation_kind: str
    effects: tuple[tuple[str | None, str], ...]


_ENDPOINTS = (
    _EndpointContract("create_blob_upload", "CREATE", (("blob_service", "create_blob"),)),
    _EndpointContract("create_blob_inline", "CREATE", (("blob_service", "create_blob"),)),
    _EndpointContract("get_blob_metadata", "BLOB_READ", ((None, "_get_owned_blob"),)),
    _EndpointContract(
        "download_blob_content",
        "BLOB_READ",
        ((None, "_get_owned_blob"), ("blob_service", "read_blob_content")),
    ),
    _EndpointContract(
        "preview_blob_content",
        "BLOB_READ",
        ((None, "_get_owned_blob"), ("blob_service", "read_blob_preview")),
    ),
    _EndpointContract(
        "delete_blob",
        "ARCHIVE",
        (("blob_service", "delete_blob"),),
    ),
)

_EFFECT_CALL_SHAPES: dict[tuple[str, str], tuple[tuple[str, ...], dict[str, str]]] = {
    ("create_blob_upload", "create_blob"): (
        (),
        {
            "session_id": "session_id",
            "filename": "original_filename",
            "content": "content",
            "mime_type": "effective_mime_typed",
            "created_by": "'user'",
            "source_description": "'uploaded'",
        },
    ),
    ("create_blob_inline", "create_blob"): (
        (),
        {
            "session_id": "session_id",
            "filename": "body.filename",
            "content": "content_bytes",
            "mime_type": "body.mime_type",
            "created_by": "'user'",
            "source_description": "'created inline'",
        },
    ),
    ("get_blob_metadata", "_get_owned_blob"): (("blob_service", "session_id", "blob_id"), {}),
    ("download_blob_content", "_get_owned_blob"): (("blob_service", "session_id", "blob_id"), {}),
    ("download_blob_content", "read_blob_content"): (("blob_id",), {}),
    ("preview_blob_content", "_get_owned_blob"): (("blob_service", "session_id", "blob_id"), {}),
    ("preview_blob_content", "read_blob_preview"): (("blob_id",), {"limit_bytes": "limit"}),
    ("delete_blob", "delete_blob"): (("blob_id",), {}),
}

_TRUSTED_ROUTE_SYMBOLS = frozenset(
    {
        "SessionOperationLease",
        "SessionOperationKind",
        "_get_owned_blob",
        "_verify_session_and_get_blob_service",
    }
)
_TRUSTED_IMPORTS = {
    "SessionOperationLease": "elspeth.web.coordination.lifecycle",
    "SessionOperationKind": "elspeth.web.coordination.contracts",
}
_TRUSTED_HELPER_AST_SHA256 = {
    "_verify_session_and_get_blob_service": "b0fc32d53168fc347386727a7210e9249d6476184b3f3b96dc1c451136dc7c66",
    "_get_owned_blob": "167629725c2b540cacf24bf04737f14938b5f6d57948c76f4de04df591357cee",
}
_STATE_GUARD_AST_SHA256 = {
    "download_blob_content": "b8987e464a69eca9bd0811864d91569473f09c071d40fd1c13b07b54d285d235",
    "preview_blob_content": "843fb7d4cf14633e88035d1473d56b276eff5baf4de69745462969fe2fa1a99b",
}
_ENDPOINT_DECORATOR_AST_SHA256 = {
    "create_blob_upload": "70adccccda5fc5811fcd01e9f494fd659dedaefae4acd03867bea490c52a6391",
    "create_blob_inline": "53ec6496f26f9d5d5d20ef2b88465748f5533075e0224ff61b11ca363a4c5400",
    "get_blob_metadata": "54d077df6ef294179374cd903fa4837a31c53f5e1bdc9b86005efb00692d85cd",
    "download_blob_content": "1436e1af30043dc2e7fee2ea29fa14ba02477ff94b492dc7d6b81c4087db7da2",
    "preview_blob_content": "7531e417fcc8831507a4d29baa4ba3bf700d510dfec7eca930f18f3a602eb46c",
    "delete_blob": "38b47f8d4e607636275395970fd5db2094c5f38b30ea2d7f97bf5bf053bbeb69",
}
_CALLABLE_IMPORTS = {
    "cast": "typing",
    "quote": "urllib.parse",
    "APIRouter": "fastapi",
    "Depends": "fastapi",
    "File": "fastapi",
    "HTTPException": "fastapi",
    "Query": "fastapi",
    "Response": "fastapi.responses",
    "BlobMetadataResponse": "elspeth.web.blobs.schemas",
    "sanitize_filename": "elspeth.web.blobs.service",
    "detect_mime_type": "elspeth.web.blobs.sniff",
}
_LOCAL_CALLABLE_AST_SHA256 = {
    "_blob_response": "2e27d8812ee67db3f2a1cfce5b254acfd7c473a5f5ea5a0da3e5738e2fd0485e",
}
_PROTECTED_CALLABLES = frozenset(
    {
        *_CALLABLE_IMPORTS,
        *_LOCAL_CALLABLE_AST_SHA256,
        "len",
        "str",
        "_get_owned_blob",
        "_verify_session_and_get_blob_service",
    }
)
_ALLOWED_ENDPOINT_DEFINITION_CALLABLES = frozenset({"Depends", "File", "Query"})
_ROUTER_INITIALIZATION_AST_SHA256 = "fc6a65bb76b72bd5bae7edf46d00830d82f23dba110b4f66f4de075c96cc5a2e"
_LIST_BLOBS_COMPREHENSION_AST_SHA256 = "d388d724562b89fa61067804798f58e0acd6ed3e1c3aa9367b7573b20513f777"
_LIST_BLOBS_DECORATOR_AST_SHA256 = "1f1483306e65df8660a48e7366198171f46495342193709bcc4aeab15bf93e46"
_LIST_BLOBS_DEFINITION_AST_SHA256 = "2eb8a4851834d0e5dc100db418a2e44292caf4e5a7540d4be1d83aec114eed05"
_ROUTER_RETURN_AST_SHA256 = "6e2d7fb0f9ddb6700f71c439e8dfef4df255459f5f3519fe369eb92bc43bba4b"
_EXPECTED_FACTORY_ENDPOINTS = frozenset({*(contract.name for contract in _ENDPOINTS), "list_blobs"})
_EXPECTED_FACTORY_ENDPOINT_ORDER = (
    "create_blob_upload",
    "create_blob_inline",
    "list_blobs",
    "get_blob_metadata",
    "download_blob_content",
    "preview_blob_content",
    "delete_blob",
)
_SYNTHETIC_ENDPOINT_PREAMBLE_AST_SHA256 = "f21040d5058a9d53fc68eed0b0c5a1faebcd3cd886e49dbec1c74abe737ce71c"
_PRODUCTION_ENDPOINT_ARGUMENTS_AST_SHA256 = {
    "create_blob_upload": "f3ea34f58b88627a1b237badfd3f61899ca7ddca7dce33a24d8e8a834f8b0641",
    "create_blob_inline": "dd824b4197fba781c0289e3ef122966100fda67f440797213ab85146f45fb97d",
    "get_blob_metadata": "bb765c163e303a22525f149830e1d10da0e93540f2e7a93b713e661ac98a9d86",
    "download_blob_content": "bb765c163e303a22525f149830e1d10da0e93540f2e7a93b713e661ac98a9d86",
    "preview_blob_content": "a4764731afbae25a494ae77244cc4f26dc0f0af46820f3a2835e9de4377af512",
    "delete_blob": "bb765c163e303a22525f149830e1d10da0e93540f2e7a93b713e661ac98a9d86",
}
_PRODUCTION_ENDPOINT_RETURN_AST_SHA256 = {
    "create_blob_upload": "85730231b5f2dc35943769a2db7e336da38c167cd5a45f266db6d50cfc270426",
    "create_blob_inline": "85730231b5f2dc35943769a2db7e336da38c167cd5a45f266db6d50cfc270426",
    "get_blob_metadata": "85730231b5f2dc35943769a2db7e336da38c167cd5a45f266db6d50cfc270426",
    "download_blob_content": "a855a6523728c133f94d122025f838d9e934dec5ba15dea65fd1de4d2ce2ab5c",
    "preview_blob_content": "a855a6523728c133f94d122025f838d9e934dec5ba15dea65fd1de4d2ce2ab5c",
    "delete_blob": "8f2cda988d9ca73053c7f508ff6d429ab1df0a26a6352507088f780e596b398c",
}
_PRODUCTION_ENDPOINT_PREAMBLE_AST_SHA256 = {
    "create_blob_upload": "b47600b6894fbdb7d97a5e711cacfe6926c235db647fe56b4f49889f4c5bf18c",
    "create_blob_inline": "a21323e26b760d8f59134e38fee22a6a87c508644507103b86b0e5c16e99a6f5",
    "get_blob_metadata": "4f13d38282606a8945c2055a26b04e48816c1817f3de0f6480c55e703f3c454e",
    "download_blob_content": "7b362d54bb6b80f200f7dc8fa891f31ad124eb4eacc9658120850e6bfff7969f",
    "preview_blob_content": "62e2eb91414beff38eb50d5a5ae452771a343c985eb0fa5667d2ac0d3de5a189",
    "delete_blob": "d171169edec8a72426c9b94be633c4a49800070df6529a4fdec6dbefb41a58e9",
}
_PRODUCTION_IMPORTS_AST_SHA256 = "756c02a95bcad96e45baa4848b141b8a826426b0013bbad17ee381f646c7af86"
_SYNTHETIC_IMPORTS_AST_SHA256 = "94033e0618e2844484fe405c1390874e190827ea0928d790784c88ca22422c20"
_SYNTHETIC_DELETE_IMPORTS_AST_SHA256 = "495d568047a365b64f2ea5b19dc4f628b0a881f9580b622a6b524913ed180b99"
_ROUTER_FACTORY_SIGNATURE_AST_SHA256 = "6662cca5c620abce45dd4871654fd02fea18f10e2ebfcb15db05f466dd165115"
_PRODUCTION_TOP_LEVEL_DEFINITIONS = (
    (ast.FunctionDef, "_blob_response"),
    (ast.AsyncFunctionDef, "_verify_session_and_get_blob_service"),
    (ast.AsyncFunctionDef, "_get_owned_blob"),
    (ast.FunctionDef, "create_blobs_router"),
)
_PRODUCTION_ENDPOINT_POST_ACQUIRE_AST_SHA256 = {
    "create_blob_upload": "f0e2e38eef879ccb4cf32ce6ae44e0d55d987f61e5ba652dc26a356dca915b84",
    "create_blob_inline": "06259ae65e2a1df9b94e5335d754fec157a722be17c7056715817104c0db2c0f",
    "get_blob_metadata": "6f5fecde5c973519d1f0f19cd46daa3c95365844275ad8802eb6a1963e5907e1",
    "download_blob_content": "de533132ec1ef83d39add60f4c16b3ac3230ebc0a4142dc5ce36b27357cc40ec",
    "preview_blob_content": "7ccaecad1d53f7b5db494ff1d06e9f23ead181d5b9eca977cae4eab8be957468",
    "delete_blob": "baec4f40f5b083e876f15bace5f21536939eb9522fc1627ffc5aa02b38509a28",
}
_SYNTHETIC_ENDPOINT_POST_ACQUIRE_AST_SHA256 = {
    "create_blob_inline": "969cd7474df122b2bf6bb171dc1854298bf4ee6ca7592f49ca4fe9f52209c3bf",
    "download_blob_content": "8465f67db3c0f607de782d08c27bf7c04a3831acca3682f46b5ffe46a535ee01",
    "delete_blob": "baec4f40f5b083e876f15bace5f21536939eb9522fc1627ffc5aa02b38509a28",
}
_ALLOWED_ROUTE_CALLABLES = {
    "create_blob_upload": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "sanitize_filename",
            "str",
            "file.read",
            "len",
            "HTTPException",
            "chunks.append",
            "b''.join",
            "detect_mime_type",
            "cast",
            "SessionOperationLease.acquire",
            "blob_service.create_blob",
            "_blob_response",
            "lease.close",
        }
    ),
    "create_blob_inline": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "body.content.encode",
            "len",
            "HTTPException",
            "SessionOperationLease.acquire",
            "blob_service.create_blob",
            "str",
            "_blob_response",
            "lease.close",
        }
    ),
    "get_blob_metadata": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "SessionOperationLease.acquire",
            "_get_owned_blob",
            "_blob_response",
            "lease.close",
        }
    ),
    "download_blob_content": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "SessionOperationLease.acquire",
            "_get_owned_blob",
            "HTTPException",
            "blob_service.read_blob_content",
            "Response",
            "quote",
            "lease.close",
        }
    ),
    "preview_blob_content": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "SessionOperationLease.acquire",
            "_get_owned_blob",
            "HTTPException",
            "blob_service.read_blob_preview",
            "Response",
            "str",
            "lease.close",
        }
    ),
    "delete_blob": frozenset(
        {
            "_verify_session_and_get_blob_service",
            "SessionOperationLease.acquire",
            "_get_owned_blob",
            "blob_service.delete_blob",
            "HTTPException",
            "str",
            "lease.close",
        }
    ),
}
_TRUSTED_IMPORT_SOURCE = """\
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
"""

_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix is not None else None
    return None


def _call_matches_shape(
    call: ast.Call,
    *,
    positional: tuple[str, ...],
    keywords: dict[str, str],
) -> bool:
    if tuple(ast.unparse(argument) for argument in call.args) != positional:
        return False
    actual_keywords = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in call.keywords
        if keyword.arg is not None and keyword.arg != _CONTEXT_PARAMETER
    }
    return (
        all(keyword.arg is not None for keyword in call.keywords)
        and len(actual_keywords) == len(call.keywords) - 1
        and actual_keywords == keywords
    )


def _parent_map(root: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(root) for child in ast.iter_child_nodes(parent)}


_NO_LITERAL = object()


def _safe_literal_value(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        pass
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
        left = _safe_literal_value(node.left)
        right = _safe_literal_value(node.right)
        if (
            left is not _NO_LITERAL
            and right is not _NO_LITERAL
            and isinstance(left, (int, float))
            and not isinstance(left, bool)
            and isinstance(right, (int, float))
            and not isinstance(right, bool)
        ):
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            return left * right
    return _NO_LITERAL


def _constant_truth(node: ast.AST) -> bool | None:
    literal_value = _safe_literal_value(node)
    if literal_value is not _NO_LITERAL:
        return bool(literal_value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        operand_truth = _constant_truth(node.operand)
        return None if operand_truth is None else not operand_truth
    if isinstance(node, ast.BoolOp):
        values = [_constant_truth(value) for value in node.values]
        if isinstance(node.op, ast.And):
            if False in values:
                return False
            return True if all(value is True for value in values) else None
        if isinstance(node.op, ast.Or):
            if True in values:
                return True
            return False if all(value is False for value in values) else None
    if isinstance(node, ast.Compare):
        values = [_safe_literal_value(node.left), *(_safe_literal_value(comparator) for comparator in node.comparators)]
        if any(value is _NO_LITERAL for value in values):
            return None
        comparisons: list[bool] = []
        for left, operator, right in zip(values[:-1], node.ops, values[1:], strict=True):
            try:
                if isinstance(operator, ast.Eq):
                    result = left == right
                elif isinstance(operator, ast.NotEq):
                    result = left != right
                elif isinstance(operator, ast.Lt):
                    result = left < right
                elif isinstance(operator, ast.LtE):
                    result = left <= right
                elif isinstance(operator, ast.Gt):
                    result = left > right
                elif isinstance(operator, ast.GtE):
                    result = left >= right
                elif isinstance(operator, ast.Is):
                    result = left is right
                elif isinstance(operator, ast.IsNot):
                    result = left is not right
                elif isinstance(operator, ast.In):
                    result = left in right
                elif isinstance(operator, ast.NotIn):
                    result = left not in right
                else:
                    return None
            except (TypeError, ValueError):
                return None
            comparisons.append(result)
        return all(comparisons)
    return None


def _statements_provably_terminal(statements: list[ast.stmt]) -> bool:
    return any(_statement_provably_terminal(statement) for statement in statements)


def _loop_has_break(loop: ast.While | ast.For | ast.AsyncFor) -> bool:
    def visit(node: ast.AST) -> bool:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Break):
                return True
            if isinstance(child, (ast.While, ast.For, ast.AsyncFor, *_NESTED_SCOPES)):
                continue
            if visit(child):
                return True
        return False

    return any(isinstance(statement, ast.Break) or visit(statement) for statement in loop.body)


def _statement_provably_terminal(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
        return True
    if isinstance(statement, ast.Assert) and _constant_truth(statement.test) is False:
        return True
    if isinstance(statement, ast.If):
        test_truth = _constant_truth(statement.test)
        if test_truth is True:
            return _statements_provably_terminal(statement.body)
        if test_truth is False:
            return _statements_provably_terminal(statement.orelse)
        return bool(statement.orelse) and _statements_provably_terminal(statement.body) and _statements_provably_terminal(statement.orelse)
    if isinstance(statement, ast.While) and _constant_truth(statement.test) is True and not _loop_has_break(statement):
        return True
    if isinstance(statement, (ast.Try, ast.TryStar)):
        if _statements_provably_terminal(statement.finalbody):
            return True
        return _statements_provably_terminal(statement.body) and all(
            _statements_provably_terminal(handler.body) for handler in statement.handlers
        )
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        return _statements_provably_terminal(statement.body)
    return False


def _preceded_by_unconditional_exit(
    child: ast.AST,
    parent: ast.AST,
    allowed_prior_controls: set[ast.stmt],
) -> bool:
    for field in ("body", "orelse", "finalbody"):
        statements = getattr(parent, field, None)
        if not isinstance(statements, list) or child not in statements:
            continue
        earlier = statements[: statements.index(child)]
        if _statements_provably_terminal(earlier):
            return True
        control_statements = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Match, ast.Try, ast.TryStar, ast.With, ast.AsyncWith)
        return any(isinstance(statement, control_statements) and statement not in allowed_prior_controls for statement in earlier)
    return False


def _is_statically_unreachable(
    node: ast.AST,
    owner: ast.AST,
    *,
    allowed_prior_controls: set[ast.stmt] | None = None,
) -> bool:
    parents = _parent_map(owner)
    allowed_controls = allowed_prior_controls or set()
    child = node
    while child in parents:
        parent = parents[child]
        if _preceded_by_unconditional_exit(child, parent, allowed_controls):
            return True
        if isinstance(
            parent,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Match,
                ast.IfExp,
                ast.comprehension,
                ast.BoolOp,
                ast.With,
                ast.AsyncWith,
                ast.ExceptHandler,
            ),
        ):
            return True
        if isinstance(parent, ast.Try) and child not in parent.body:
            return True
        child = parent
    return False


def _structural_binding_names(node: ast.AST) -> set[str]:
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
        return {node.name}
    if isinstance(node, ast.MatchMapping) and node.rest is not None:
        return {node.rest}
    if isinstance(node, ast.ExceptHandler) and node.name is not None:
        return {node.name}
    return set()


def _definition_binding_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.alias):
        return {node.asname or node.name.partition(".")[0]}
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    return set()


def _trusted_symbol_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    parents = _parent_map(tree)
    exact_import_counts = dict.fromkeys(_TRUSTED_IMPORTS, 0)
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.Name) and candidate.id in _TRUSTED_ROUTE_SYMBOLS and isinstance(candidate.ctx, (ast.Store, ast.Del)):
            violations.append(f"trusted route symbol {candidate.id!r} must not be rebound or deleted")
        elif isinstance(candidate, ast.Name) and candidate.id in _TRUSTED_ROUTE_SYMBOLS and isinstance(candidate.ctx, ast.Load):
            parent = parents.get(candidate)
            grandparent = parents.get(parent) if parent is not None else None
            if candidate.id == "SessionOperationLease":
                is_approved_use = (
                    isinstance(parent, ast.Attribute)
                    and parent.value is candidate
                    and parent.attr == "acquire"
                    and isinstance(parent.ctx, ast.Load)
                    and isinstance(grandparent, ast.Call)
                    and grandparent.func is parent
                ) or (isinstance(parent, ast.AnnAssign) and parent.annotation is candidate)
            elif candidate.id == "SessionOperationKind":
                is_approved_use = (
                    isinstance(parent, ast.Attribute)
                    and parent.value is candidate
                    and parent.attr in {"CREATE", "BLOB_READ", "ARCHIVE"}
                    and isinstance(parent.ctx, ast.Load)
                )
            else:
                is_approved_use = isinstance(parent, ast.Call) and parent.func is candidate
            if not is_approved_use:
                violations.append(f"trusted route symbol {candidate.id!r} must not escape or be mutated")
        elif isinstance(candidate, ast.arg) and candidate.arg in _TRUSTED_ROUTE_SYMBOLS:
            violations.append(f"trusted route symbol {candidate.arg!r} must not be injected as a parameter")
        elif isinstance(candidate, (ast.Nonlocal, ast.Global)):
            for name in _TRUSTED_ROUTE_SYMBOLS.intersection(candidate.names):
                violations.append(f"trusted route symbol {name!r} must not be declared nonlocal/global")
        elif bound_names := _TRUSTED_ROUTE_SYMBOLS.intersection(_structural_binding_names(candidate)):
            for name in bound_names:
                violations.append(f"trusted route symbol {name!r} must not be structurally bound")
        elif isinstance(candidate, ast.ImportFrom):
            for alias in candidate.names:
                bound_name = alias.asname or alias.name
                trusted_name = alias.name if alias.name in _TRUSTED_ROUTE_SYMBOLS else bound_name
                if trusted_name not in _TRUSTED_ROUTE_SYMBOLS:
                    continue
                is_exact_import = (
                    candidate.module == _TRUSTED_IMPORTS.get(trusted_name)
                    and alias.name == trusted_name
                    and alias.asname is None
                    and isinstance(parents.get(candidate), ast.Module)
                )
                if is_exact_import:
                    exact_import_counts[trusted_name] += 1
                else:
                    violations.append(f"trusted route symbol {trusted_name!r} has forged import provenance")
        elif isinstance(candidate, ast.Import):
            for alias in candidate.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                if bound_name in _TRUSTED_ROUTE_SYMBOLS:
                    violations.append(f"trusted route symbol {bound_name!r} has forged import provenance")
        elif isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and candidate.name in _TRUSTED_ROUTE_SYMBOLS:
            if candidate.name in {"SessionOperationLease", "SessionOperationKind"}:
                violations.append(f"trusted route symbol {candidate.name!r} must not be locally defined")
                continue
            candidate_dump = ast.dump(candidate, include_attributes=False)
            candidate_digest = hashlib.sha256(candidate_dump.encode()).hexdigest()
            if not isinstance(parents.get(candidate), ast.Module) or candidate_digest != _TRUSTED_HELPER_AST_SHA256[candidate.name]:
                violations.append(f"trusted route helper {candidate.name!r} does not match its canonical definition")
    for trusted_name, count in exact_import_counts.items():
        if count != 1:
            violations.append(f"trusted route symbol {trusted_name!r} requires exactly one canonical top-level import; found {count}")
    return violations


def _callable_provenance_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    parents = _parent_map(tree)
    used_callables = {
        candidate.func.id
        for candidate in ast.walk(tree)
        if isinstance(candidate, ast.Call) and isinstance(candidate.func, ast.Name) and candidate.func.id in _PROTECTED_CALLABLES
    }
    exact_import_counts = dict.fromkeys(_CALLABLE_IMPORTS, 0)
    exact_local_counts = dict.fromkeys(_LOCAL_CALLABLE_AST_SHA256, 0)
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.Attribute, ast.Subscript)) and isinstance(candidate.ctx, (ast.Store, ast.Del)):
            violations.append("standalone blob route module must not mutate attributes or items")
            if _root_name(candidate) in _PROTECTED_CALLABLES:
                violations.append(f"protected callable {_root_name(candidate)!r} must not have attributes/items mutated")
        elif isinstance(candidate, ast.Name) and candidate.id in _PROTECTED_CALLABLES and isinstance(candidate.ctx, (ast.Store, ast.Del)):
            violations.append(f"protected callable {candidate.id!r} must not be rebound or deleted")
        elif isinstance(candidate, ast.arg) and candidate.arg in _PROTECTED_CALLABLES:
            violations.append(f"protected callable {candidate.arg!r} must not be injected as a parameter")
        elif _PROTECTED_CALLABLES.intersection(_structural_binding_names(candidate)):
            for name in sorted(_PROTECTED_CALLABLES.intersection(_structural_binding_names(candidate))):
                violations.append(f"protected callable {name!r} must not be structurally rebound")
        elif isinstance(candidate, ast.ImportFrom):
            for alias in candidate.names:
                bound_name = alias.asname or alias.name
                if bound_name not in _PROTECTED_CALLABLES:
                    continue
                is_exact = (
                    candidate.module == _CALLABLE_IMPORTS.get(bound_name)
                    and alias.name == bound_name
                    and alias.asname is None
                    and isinstance(parents.get(candidate), ast.Module)
                )
                if is_exact:
                    exact_import_counts[bound_name] += 1
                else:
                    violations.append(f"protected callable {bound_name!r} has forged import provenance")
        elif isinstance(candidate, ast.Import):
            for alias in candidate.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                if bound_name in _PROTECTED_CALLABLES:
                    violations.append(f"protected callable {bound_name!r} has forged import provenance")
        elif isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and candidate.name in _PROTECTED_CALLABLES:
            if candidate.name in _LOCAL_CALLABLE_AST_SHA256:
                digest = hashlib.sha256(ast.dump(candidate, include_attributes=False).encode()).hexdigest()
                if isinstance(parents.get(candidate), ast.Module) and digest == _LOCAL_CALLABLE_AST_SHA256[candidate.name]:
                    exact_local_counts[candidate.name] += 1
                else:
                    violations.append(f"protected callable {candidate.name!r} does not match its canonical definition")
            elif candidate.name not in _TRUSTED_HELPER_AST_SHA256:
                violations.append(f"protected callable {candidate.name!r} must not be locally defined")
    for name in used_callables.intersection(_CALLABLE_IMPORTS):
        if exact_import_counts[name] != 1:
            violations.append(f"used protected callable {name!r} requires exactly one canonical import")
    for name in used_callables.intersection(_LOCAL_CALLABLE_AST_SHA256):
        if exact_local_counts[name] != 1:
            violations.append(f"used protected callable {name!r} requires exactly one canonical definition")
    return violations


def _module_top_level_call_violations(tree: ast.AST) -> list[str]:
    parents = _parent_map(tree)
    violations: list[str] = []
    for call in (candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.Call)):
        owner: ast.AST | None = call
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        if owner is None:
            violations.append("standalone blob route module must not execute calls at module top level")
    return violations


def _import_surface_violations(tree: ast.AST) -> list[str]:
    if not isinstance(tree, ast.Module):
        return ["standalone blob route source must parse as a module"]
    imports = [statement for statement in tree.body if isinstance(statement, (ast.Import, ast.ImportFrom))]
    violations: list[str] = []
    if any(isinstance(statement, ast.ImportFrom) and any(alias.name == "*" for alias in statement.names) for statement in imports):
        violations.append("standalone blob route module must not use wildcard imports")
    import_payload = "|".join(ast.dump(statement, include_attributes=False) for statement in imports)
    import_digest = hashlib.sha256(import_payload.encode()).hexdigest()
    if import_digest not in {
        _PRODUCTION_IMPORTS_AST_SHA256,
        _SYNTHETIC_IMPORTS_AST_SHA256,
        _SYNTHETIC_DELETE_IMPORTS_AST_SHA256,
    }:
        violations.append("standalone blob routes require an exact closed canonical import inventory")
    factories = [statement for statement in tree.body if isinstance(statement, ast.FunctionDef) and statement.name == "create_blobs_router"]
    if (
        any(isinstance(candidate, ast.FunctionDef) and candidate.name == "create_blobs_router" for candidate in ast.walk(tree))
        and import_digest != _PRODUCTION_IMPORTS_AST_SHA256
    ):
        violations.append("production standalone blob routes require the exact closed canonical import inventory")
    if import_digest != _PRODUCTION_IMPORTS_AST_SHA256:
        forbidden_synthetic_imports = {"create_blobs_router", "get_current_user", "router"}
        for statement in imports:
            for alias in statement.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                if bound_name in forbidden_synthetic_imports:
                    violations.append(f"standalone blob route binding {bound_name!r} must not be imported")
    if import_digest == _PRODUCTION_IMPORTS_AST_SHA256:
        if len(factories) != 1:
            violations.append("production standalone blob routes require exactly one top-level create_blobs_router factory")
        else:
            signature_payload = (
                ast.dump(factories[0].args, include_attributes=False) + "|" + ast.dump(factories[0].returns, include_attributes=False)
            )
            if hashlib.sha256(signature_payload.encode()).hexdigest() != _ROUTER_FACTORY_SIGNATURE_AST_SHA256:
                violations.append("create_blobs_router: signature must match exact canonical factory identity")
    return violations


def _top_level_definition_inventory_violations(tree: ast.AST, endpoint_name: str) -> list[str]:
    if not isinstance(tree, ast.Module):
        return ["standalone blob route source must parse as a module"]
    imports = [statement for statement in tree.body if isinstance(statement, (ast.Import, ast.ImportFrom))]
    import_payload = "|".join(ast.dump(statement, include_attributes=False) for statement in imports)
    production_source = hashlib.sha256(import_payload.encode()).hexdigest() == _PRODUCTION_IMPORTS_AST_SHA256
    definitions = [statement for statement in tree.body if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    actual = tuple((type(definition), definition.name) for definition in definitions)
    expected = _PRODUCTION_TOP_LEVEL_DEFINITIONS if production_source else ((ast.AsyncFunctionDef, endpoint_name),)
    if actual == expected:
        return []
    return ["standalone blob routes require the exact closed top-level definition inventory"]


def _type_parameter_violations(tree: ast.AST) -> list[str]:
    definitions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    return [
        f"{candidate.name}: standalone blob route definitions must not declare type parameters"
        for candidate in ast.walk(tree)
        if isinstance(candidate, definitions) and candidate.type_params
    ]


def _endpoint_definition_call_violations(endpoint: ast.AsyncFunctionDef) -> list[str]:
    body_nodes = _nodes_in_statements(endpoint.body)
    decorator_nodes = {node for decorator in endpoint.decorator_list for node in ast.walk(decorator)}
    unexpected = {
        ast.unparse(candidate.func)
        for candidate in ast.walk(endpoint)
        if isinstance(candidate, ast.Call)
        and candidate not in body_nodes
        and candidate not in decorator_nodes
        and ast.unparse(candidate.func) not in _ALLOWED_ENDPOINT_DEFINITION_CALLABLES
    }
    if not unexpected:
        return []
    return [f"{endpoint.name}: endpoint defaults and annotations contain unapproved executable calls: {sorted(unexpected)!r}"]


def _implicit_execution_violations(tree: ast.AST) -> list[str]:
    parents = _parent_map(tree)
    violations: list[str] = []
    comprehensions = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.Yield, ast.YieldFrom)):
            violations.append("standalone blob routes must not use unapproved implicit protocol execution")
        elif isinstance(candidate, ast.Await) and not isinstance(candidate.value, ast.Call):
            violations.append("standalone blob routes must not await non-call attacker-controlled objects")
        elif isinstance(candidate, comprehensions):
            owner: ast.AST | None = candidate
            while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                owner = parents.get(owner)
            digest = hashlib.sha256(ast.dump(candidate, include_attributes=False).encode()).hexdigest()
            if not (
                isinstance(owner, ast.AsyncFunctionDef) and owner.name == "list_blobs" and digest == _LIST_BLOBS_COMPREHENSION_AST_SHA256
            ):
                violations.append("standalone blob routes contain an unapproved implicit-execution comprehension")
    return violations


def _router_binding_violations(tree: ast.AST) -> list[str]:
    factories = [
        candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.FunctionDef) and candidate.name == "create_blobs_router"
    ]
    if not factories:
        return []
    if len(factories) != 1:
        return [f"create_blobs_router: expected exactly one route factory; found {len(factories)}"]
    factory = factories[0]
    runtime_nodes = _runtime_nodes(factory, root_is_scope=True)
    mutations = [
        candidate
        for candidate in runtime_nodes
        if isinstance(candidate, ast.Name) and candidate.id == "router" and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    structural = [
        candidate
        for candidate in runtime_nodes
        if "router" in _structural_binding_names(candidate) or "router" in _definition_binding_names(candidate)
    ]
    initialization_statements = [
        statement for statement in factory.body if any(candidate in mutations for candidate in _runtime_nodes(statement))
    ]
    exact_initializations = [
        statement
        for statement in initialization_statements
        if hashlib.sha256(ast.dump(statement, include_attributes=False).encode()).hexdigest() == _ROUTER_INITIALIZATION_AST_SHA256
    ]
    outer_calls = [candidate for candidate in runtime_nodes if isinstance(candidate, ast.Call)]
    nested_endpoints = [statement for statement in factory.body if isinstance(statement, ast.AsyncFunctionDef)]
    endpoint_names = [definition.name for definition in nested_endpoints]
    list_endpoints = [definition for definition in nested_endpoints if definition.name == "list_blobs"]
    returns = [candidate for candidate in runtime_nodes if isinstance(candidate, ast.Return)]
    violations: list[str] = []
    if len(mutations) != 1 or len(exact_initializations) != 1:
        violations.append("create_blobs_router: router requires exactly one immutable canonical APIRouter binding")
    if structural:
        violations.append("create_blobs_router: router must not be structurally rebound")
    if len(outer_calls) != 1 or _dotted_name(outer_calls[0].func) != "APIRouter":
        violations.append("create_blobs_router: outer factory executable calls must be the sole canonical APIRouter construction")
    if len(endpoint_names) != len(_EXPECTED_FACTORY_ENDPOINTS) or set(endpoint_names) != _EXPECTED_FACTORY_ENDPOINTS:
        violations.append("create_blobs_router: nested endpoint inventory must match the exact closed canonical set")
    if tuple(endpoint_names) != _EXPECTED_FACTORY_ENDPOINT_ORDER:
        violations.append("create_blobs_router: nested endpoints must retain exact canonical order")
    non_endpoint_statements = [statement for statement in factory.body if not isinstance(statement, ast.AsyncFunctionDef)]
    if (
        len(non_endpoint_statements) != 3
        or factory.body[0] is not non_endpoint_statements[0]
        or not isinstance(non_endpoint_statements[0], ast.Expr)
        or not isinstance(non_endpoint_statements[0].value, ast.Constant)
        or not isinstance(non_endpoint_statements[0].value.value, str)
        or factory.body[1] is not non_endpoint_statements[1]
        or factory.body[-1] is not non_endpoint_statements[2]
    ):
        violations.append("create_blobs_router: outer statement inventory must be exact docstring, router construction, endpoints, return")
    if len(list_endpoints) != 1:
        violations.append("create_blobs_router: expected exactly one canonical list_blobs endpoint")
    else:
        decorator_payload = "|".join(ast.dump(decorator, include_attributes=False) for decorator in list_endpoints[0].decorator_list)
        if hashlib.sha256(decorator_payload.encode()).hexdigest() != _LIST_BLOBS_DECORATOR_AST_SHA256:
            violations.append("create_blobs_router: list_blobs decorator must match exact canonical registration")
        if hashlib.sha256(ast.dump(list_endpoints[0], include_attributes=False).encode()).hexdigest() != _LIST_BLOBS_DEFINITION_AST_SHA256:
            violations.append("create_blobs_router: list_blobs definition must match exact canonical implementation")
    if (
        len(returns) != 1
        or factory.body[-1] is not returns[0]
        or hashlib.sha256(ast.dump(returns[0], include_attributes=False).encode()).hexdigest() != _ROUTER_RETURN_AST_SHA256
    ):
        violations.append("create_blobs_router: factory must end with the sole exact return router statement")
    return violations


def _definition_expressions(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.expr]:
    arguments = definition.args
    expressions = [*definition.decorator_list, *arguments.defaults]
    expressions.extend(default for default in arguments.kw_defaults if default is not None)
    expressions.extend(
        argument.annotation
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
        if argument.annotation is not None
    )
    if arguments.vararg is not None and arguments.vararg.annotation is not None:
        expressions.append(arguments.vararg.annotation)
    if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
        expressions.append(arguments.kwarg.annotation)
    if definition.returns is not None:
        expressions.append(definition.returns)
    return expressions


def _is_safe_definition_expression(node: ast.AST) -> bool:
    return isinstance(node, (ast.Name, ast.Constant))


def _module_definition_surface_violations(tree: ast.AST) -> list[str]:
    if not isinstance(tree, ast.Module):
        return ["standalone blob route source must parse as a module"]
    violations: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if isinstance(statement, ast.AsyncFunctionDef) and statement.name in {contract.name for contract in _ENDPOINTS}:
                continue
            if isinstance(statement, ast.ClassDef):
                violations.append(f"{statement.name}: executable module-scope class definitions are not allowed")
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definition_expressions = _definition_expressions(statement)
            if statement.decorator_list or any(not _is_safe_definition_expression(expression) for expression in definition_expressions):
                violations.append(f"{statement.name}: module-scope definition surface contains executable expressions")
            continue
        violations.append("standalone blob route module contains unapproved executable module-scope statements")
    return violations


def _endpoint_definition_and_preamble_violations(tree: ast.AST, endpoint: ast.AsyncFunctionDef) -> list[str]:
    has_route_factory = any(
        isinstance(candidate, ast.FunctionDef) and candidate.name == "create_blobs_router" for candidate in ast.walk(tree)
    )
    violations: list[str] = []
    if has_route_factory:
        arguments_digest = hashlib.sha256(ast.dump(endpoint.args, include_attributes=False).encode()).hexdigest()
        if arguments_digest != _PRODUCTION_ENDPOINT_ARGUMENTS_AST_SHA256[endpoint.name]:
            violations.append(f"{endpoint.name}: endpoint arguments must match the exact canonical definition surface")
        return_digest = hashlib.sha256(ast.dump(endpoint.returns, include_attributes=False).encode()).hexdigest()
        if return_digest != _PRODUCTION_ENDPOINT_RETURN_AST_SHA256[endpoint.name]:
            violations.append(f"{endpoint.name}: endpoint return annotation must match the exact canonical definition surface")
    else:
        arguments = endpoint.args
        annotations = [
            argument.annotation
            for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
            if argument.annotation is not None
        ]
        if arguments.vararg is not None and arguments.vararg.annotation is not None:
            annotations.append(arguments.vararg.annotation)
        if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
            annotations.append(arguments.kwarg.annotation)
        if endpoint.returns is not None:
            annotations.append(endpoint.returns)
        if (
            arguments.defaults
            or any(default is not None for default in arguments.kw_defaults)
            or any(not isinstance(annotation, ast.Name) for annotation in annotations)
        ):
            violations.append(f"{endpoint.name}: endpoint definition surface contains unapproved executable expressions")

    acquisition_indexes = [
        index
        for index, statement in enumerate(endpoint.body)
        if any(isinstance(candidate, ast.Call) and _is_acquisition_like_call(candidate) for candidate in ast.walk(statement))
    ]
    if len(acquisition_indexes) != 1:
        return violations
    preamble = ast.Module(body=endpoint.body[: acquisition_indexes[0]], type_ignores=[])
    preamble_digest = hashlib.sha256(ast.dump(preamble, include_attributes=False).encode()).hexdigest()
    expected_digest = (
        _PRODUCTION_ENDPOINT_PREAMBLE_AST_SHA256[endpoint.name] if has_route_factory else _SYNTHETIC_ENDPOINT_PREAMBLE_AST_SHA256
    )
    if preamble_digest != expected_digest:
        violations.append(f"{endpoint.name}: pre-acquisition statements must match the exact approved preamble")
    post_acquire = ast.Module(body=endpoint.body[acquisition_indexes[0] + 1 :], type_ignores=[])
    post_acquire_digest = hashlib.sha256(ast.dump(post_acquire, include_attributes=False).encode()).hexdigest()
    expected_post_acquire_digest = (
        _PRODUCTION_ENDPOINT_POST_ACQUIRE_AST_SHA256[endpoint.name]
        if has_route_factory
        else _SYNTHETIC_ENDPOINT_POST_ACQUIRE_AST_SHA256.get(endpoint.name)
    )
    if expected_post_acquire_digest is None or post_acquire_digest != expected_post_acquire_digest:
        violations.append(f"{endpoint.name}: post-acquisition statements must match the exact approved fenced tail")
    nested_definitions = [
        candidate
        for candidate in ast.walk(endpoint)
        if candidate is not endpoint and isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
    ]
    if nested_definitions:
        violations.append(f"{endpoint.name}: nested definitions and lambdas are not allowed in standalone blob endpoints")
    return violations


def _endpoint_definitions(tree: ast.AST, name: str) -> list[ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name]


def _runtime_nodes(root: ast.AST, *, root_is_scope: bool = False) -> list[ast.AST]:
    """Walk executable nodes without crediting nested definitions or lambdas."""
    nodes = [root]
    if not root_is_scope and isinstance(root, _NESTED_SCOPES):
        return nodes
    for child in ast.iter_child_nodes(root):
        if isinstance(child, _NESTED_SCOPES):
            nodes.append(child)
            continue
        nodes.extend(_runtime_nodes(child))
    return nodes


def _nested_scope_protected_uses(owner: ast.AsyncFunctionDef, protected_names: set[str]) -> list[ast.AST]:
    """Capture protected-name use with a full, intentionally unpruned walk."""
    nested_scopes: list[ast.AST] = []

    def find_scopes(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                nested_scopes.append(child)
            else:
                find_scopes(child)

    find_scopes(owner)
    uses: list[ast.AST] = []
    seen: set[int] = set()
    for scope in nested_scopes:
        for candidate in ast.walk(scope):
            protected = (
                (isinstance(candidate, ast.Name) and candidate.id in protected_names)
                or (isinstance(candidate, ast.arg) and candidate.arg in protected_names)
                or (isinstance(candidate, (ast.Nonlocal, ast.Global)) and bool(protected_names.intersection(candidate.names)))
                or bool(protected_names.intersection(_structural_binding_names(candidate)))
                or bool(protected_names.intersection(_definition_binding_names(candidate)))
            )
            if protected and id(candidate) not in seen:
                uses.append(candidate)
                seen.add(id(candidate))
    return uses


def _bound_acquire(node: ast.AsyncFunctionDef) -> list[tuple[str, ast.Call, ast.Assign | ast.AnnAssign]]:
    acquisitions: list[tuple[str, ast.Call, ast.Assign | ast.AnnAssign]] = []
    for candidate in node.body:
        if isinstance(candidate, ast.Assign) and len(candidate.targets) == 1 and isinstance(candidate.targets[0], ast.Name):
            target = candidate.targets[0]
            value = candidate.value
        elif isinstance(candidate, ast.AnnAssign) and isinstance(candidate.target, ast.Name):
            target = candidate.target
            value = candidate.value
        else:
            continue
        if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
            continue
        call = value.value
        if _dotted_name(call.func) == "SessionOperationLease.acquire":
            acquisitions.append((target.id, call, candidate))
    return acquisitions


def _effect_calls(node: ast.AsyncFunctionDef, method_name: str) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for candidate in _runtime_nodes(node, root_is_scope=True):
        if not isinstance(candidate, ast.Call):
            continue
        if (isinstance(candidate.func, ast.Name) and candidate.func.id == method_name) or (
            isinstance(candidate.func, ast.Attribute) and candidate.func.attr == method_name
        ):
            calls.append(candidate)
    return calls


def _nodes_in_statements(statements: list[ast.stmt]) -> set[ast.AST]:
    return {node for statement in statements for node in _runtime_nodes(statement)}


def _directly_awaited_calls(nodes: set[ast.AST]) -> set[ast.Call]:
    return {node.value for node in nodes if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)}


def _is_exact_context_keyword(call: ast.Call, lease_name: str) -> bool:
    values = [keyword.value for keyword in call.keywords if keyword.arg == _CONTEXT_PARAMETER]
    return len(values) == 1 and _dotted_name(values[0]) == f"{lease_name}.context"


def _endpoint_parameter_names(endpoint: ast.AsyncFunctionDef) -> set[str]:
    arguments = endpoint.args
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _direct_assignment_value(statement: ast.stmt, binding: str) -> ast.expr | None:
    if (
        isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and statement.targets[0].id == binding
    ):
        return statement.value
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == binding:
        return statement.value
    return None


def _approved_service_initialization(statement: ast.stmt, binding: str) -> bool:
    value = _direct_assignment_value(statement, binding)
    if binding == "blob_service":
        if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
            return False
        call = value.value
        return (
            _dotted_name(call.func) == "_verify_session_and_get_blob_service"
            and [_dotted_name(argument) for argument in call.args] == ["session_id", "user", "request"]
            and not call.keywords
        )
    return binding == "session_service" and value is not None and _dotted_name(value) == "request.app.state.session_service"


def _service_binding_violations(
    endpoint: ast.AsyncFunctionDef,
    *,
    binding: str,
    acquisition_index: int,
    allow_parameter: bool = True,
) -> list[str]:
    endpoint_nodes = _runtime_nodes(endpoint, root_is_scope=True)
    mutations = [
        candidate
        for candidate in endpoint_nodes
        if isinstance(candidate, ast.Name) and candidate.id == binding and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    implicit_mutations = [
        candidate
        for candidate in endpoint_nodes
        if binding in _definition_binding_names(candidate) or binding in _structural_binding_names(candidate)
    ]
    if implicit_mutations:
        return [f"{endpoint.name}: {binding!r} must not be defined, imported, or structurally rebound"]
    if binding in _endpoint_parameter_names(endpoint):
        if not allow_parameter:
            return [f"{endpoint.name}: {binding!r} must use exact approved local initialization"]
        return [] if not mutations else [f"{endpoint.name}: parameter {binding!r} must not be rebound or deleted"]
    approved_targets = [
        target
        for statement in endpoint.body[:acquisition_index]
        if _approved_service_initialization(statement, binding)
        for target in _runtime_nodes(statement)
        if isinstance(target, ast.Name) and target.id == binding and isinstance(target.ctx, ast.Store)
    ]
    if len(approved_targets) != 1:
        return [f"{endpoint.name}: {binding!r} requires exactly one approved initialization before acquire"]
    if mutations != approved_targets:
        return [f"{endpoint.name}: {binding!r} must not be rebound or deleted outside its approved initialization"]
    return []


def _effect_callable_references(
    endpoint: ast.AsyncFunctionDef,
    *,
    receiver: str | None,
    method_name: str,
) -> list[ast.AST]:
    nodes = _runtime_nodes(endpoint, root_is_scope=True)
    if receiver is None:
        return [candidate for candidate in nodes if isinstance(candidate, ast.Name) and candidate.id == method_name]
    dotted = f"{receiver}.{method_name}"
    return [candidate for candidate in nodes if isinstance(candidate, ast.Attribute) and _dotted_name(candidate) == dotted]


def _endpoint_input_binding_violations(endpoint: ast.AsyncFunctionDef, required_inputs: set[str]) -> list[str]:
    violations: list[str] = []
    parameter_names = _endpoint_parameter_names(endpoint)
    for name in required_inputs - parameter_names:
        violations.append(f"{endpoint.name}: trusted endpoint input {name!r} must be a parameter")
    for candidate in _runtime_nodes(endpoint, root_is_scope=True):
        if isinstance(candidate, ast.Name) and candidate.id in required_inputs and isinstance(candidate.ctx, (ast.Store, ast.Del)):
            violations.append(f"{endpoint.name}: trusted endpoint input {candidate.id!r} must not be rebound or deleted")
        elif isinstance(candidate, (ast.Nonlocal, ast.Global)):
            for name in required_inputs.intersection(candidate.names):
                violations.append(f"{endpoint.name}: trusted endpoint input {name!r} must not be declared nonlocal/global")
        else:
            structural_names = required_inputs.intersection(_structural_binding_names(candidate))
            for name in structural_names:
                violations.append(f"{endpoint.name}: trusted endpoint input {name!r} must not be structurally bound")
            definition_names = required_inputs.intersection(_definition_binding_names(candidate))
            for name in definition_names:
                violations.append(f"{endpoint.name}: trusted endpoint input {name!r} must not be defined or imported locally")
    return violations


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for element in node.elts for name in _target_names(element)}
    return set()


def _expression_is_tainted(node: ast.AST, tainted_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in tainted_names
    if isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred, ast.Await)):
        return _expression_is_tainted(node.value, tainted_names)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(_expression_is_tainted(element, tainted_names) for element in node.elts)
    if isinstance(node, ast.Dict):
        return any(
            (key is not None and _expression_is_tainted(key, tainted_names)) or _expression_is_tainted(value, tainted_names)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        comprehension_names = set(tainted_names)
        for generator in node.generators:
            if _expression_is_tainted(generator.iter, comprehension_names):
                comprehension_names.update(_target_names(generator.target))
        if isinstance(node, ast.DictComp):
            return _expression_is_tainted(node.key, comprehension_names) or _expression_is_tainted(node.value, comprehension_names)
        return _expression_is_tainted(node.elt, comprehension_names)
    if isinstance(node, ast.NamedExpr):
        return _expression_is_tainted(node.value, tainted_names)
    if isinstance(node, ast.IfExp):
        return _expression_is_tainted(node.body, tainted_names) or _expression_is_tainted(node.orelse, tainted_names)
    if isinstance(node, ast.BoolOp):
        return any(_expression_is_tainted(value, tainted_names) for value in node.values)
    return False


def _protected_object_mutation_violations(endpoint: ast.AsyncFunctionDef, protected_names: set[str]) -> list[str]:
    endpoint_nodes = _runtime_nodes(endpoint, root_is_scope=True)
    tainted_names = set(protected_names)
    changed = True
    while changed:
        changed = False
        for candidate in endpoint_nodes:
            assignments: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(candidate, ast.Assign):
                assignments.extend((target, candidate.value) for target in candidate.targets)
            elif isinstance(candidate, (ast.AnnAssign, ast.NamedExpr)) and candidate.value is not None:
                assignments.append((candidate.target, candidate.value))
            elif isinstance(candidate, (ast.For, ast.AsyncFor)):
                assignments.append((candidate.target, candidate.iter))
            for target, value in assignments:
                if not _expression_is_tainted(value, tainted_names):
                    continue
                for name in _target_names(target) - tainted_names:
                    tainted_names.add(name)
                    changed = True

    violations: list[str] = []
    for candidate in endpoint_nodes:
        if (
            isinstance(candidate, (ast.Attribute, ast.Subscript))
            and isinstance(candidate.ctx, (ast.Store, ast.Del))
            and _expression_is_tainted(candidate, tainted_names)
        ):
            violations.append(f"{endpoint.name}: protected endpoint objects must not have attributes/items mutated")
        elif isinstance(candidate, (ast.Assign, ast.AnnAssign)):
            value = candidate.value
            if isinstance(value, ast.Name) and value.id in protected_names:
                violations.append(f"{endpoint.name}: protected endpoint bindings must not escape into aliases")
    return violations


def _is_acquire_getattr(call: ast.Call) -> bool:
    if _dotted_name(call.func) not in {"getattr", "builtins.getattr"}:
        return False
    receiver = _dotted_name(call.args[0]) if call.args else None
    return (receiver is not None and receiver.split(".")[-1] == "SessionOperationLease") or any(
        isinstance(argument, ast.Constant) and argument.value in {"SessionOperationLease", "acquire"} for argument in call.args
    )


def _dynamic_member_lookup_violations(tree: ast.AST) -> list[str]:
    forbidden_names = {
        "getattr",
        "vars",
        "globals",
        "locals",
        "eval",
        "exec",
        "__import__",
        "setattr",
        "delattr",
    }
    forbidden_members = {
        *forbidden_names,
        "__dict__",
        "__getattribute__",
        "__setattr__",
        "__delattr__",
        "attrgetter",
        "getattr_static",
        "methodcaller",
        "import_module",
        "modules",
    }
    forbidden_modules = {"builtins", "importlib", "inspect", "operator"}
    violations: list[str] = []
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.Name) and candidate.id in forbidden_names:
            violations.append(f"dynamic member lookup {candidate.id!r} is forbidden in standalone blob routes")
        elif isinstance(candidate, ast.Attribute) and candidate.attr in forbidden_members:
            violations.append(f"dynamic member lookup {candidate.attr!r} is forbidden in standalone blob routes")
        elif isinstance(candidate, ast.alias) and (
            candidate.name.partition(".")[0] in forbidden_modules or candidate.name.rpartition(".")[-1] in forbidden_members
        ):
            violations.append(f"dynamic member lookup import {candidate.name!r} is forbidden in standalone blob routes")
    return violations


def _is_acquisition_like_call(call: ast.Call) -> bool:
    return (isinstance(call.func, ast.Attribute) and call.func.attr == "acquire") or (
        isinstance(call.func, ast.Call) and _is_acquire_getattr(call.func)
    )


def _is_acquisition_like_reference(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr in {"SessionOperationLease", "acquire"}) or (
        isinstance(node, ast.Call) and _is_acquire_getattr(node)
    )


def _non_endpoint_acquisition_violations(tree: ast.AST) -> list[str]:
    parents = _parent_map(tree)
    allowed_owners = {definition for contract in _ENDPOINTS for definition in _endpoint_definitions(tree, contract.name)}
    violations: list[str] = []
    for reference in (candidate for candidate in ast.walk(tree) if _is_acquisition_like_reference(candidate)):
        owner = parents.get(reference)
        while owner is not None and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents.get(owner)
        if owner not in allowed_owners:
            violations.append("lease-authority acquisition reference must be owned directly by a standalone blob endpoint")
    return violations


def _route_violations(tree: ast.AST, contract: _EndpointContract) -> list[str]:
    endpoints = _endpoint_definitions(tree, contract.name)
    if len(endpoints) != 1:
        return [f"{contract.name}: expected exactly one endpoint definition; found {len(endpoints)}"]
    endpoint = endpoints[0]
    violations = _trusted_symbol_violations(tree)
    decorator_payload = "|".join(ast.dump(decorator, include_attributes=False) for decorator in endpoint.decorator_list)
    decorator_digest = hashlib.sha256(decorator_payload.encode()).hexdigest()
    if decorator_digest != _ENDPOINT_DECORATOR_AST_SHA256[contract.name]:
        violations.append(f"{contract.name}: endpoint decorators must match the exact canonical route registration")
    violations.extend(_callable_provenance_violations(tree))
    violations.extend(_import_surface_violations(tree))
    violations.extend(_top_level_definition_inventory_violations(tree, endpoint.name))
    violations.extend(_type_parameter_violations(tree))
    violations.extend(_module_top_level_call_violations(tree))
    violations.extend(_module_definition_surface_violations(tree))
    violations.extend(_endpoint_definition_call_violations(endpoint))
    violations.extend(_endpoint_definition_and_preamble_violations(tree, endpoint))
    violations.extend(_implicit_execution_violations(tree))
    violations.extend(_router_binding_violations(tree))
    violations.extend(_dynamic_member_lookup_violations(tree))
    violations.extend(_non_endpoint_acquisition_violations(tree))
    required_inputs = {"session_id", "request", "user"}
    if contract.operation_kind != "CREATE":
        required_inputs.add("blob_id")
    if contract.name == "create_blob_upload":
        required_inputs.add("file")
    elif contract.name == "create_blob_inline":
        required_inputs.add("body")
    elif contract.name == "preview_blob_content":
        required_inputs.add("limit")
    violations.extend(_endpoint_input_binding_violations(endpoint, required_inputs))
    body_nodes = _nodes_in_statements(endpoint.body)
    unexpected_callables = {
        ast.unparse(candidate.func)
        for candidate in body_nodes
        if isinstance(candidate, ast.Call) and ast.unparse(candidate.func) not in _ALLOWED_ROUTE_CALLABLES[contract.name]
    }
    if unexpected_callables:
        violations.append(
            f"{contract.name}: endpoint call targets must come from the exact callable allowlist; found {sorted(unexpected_callables)!r}"
        )
    endpoint_nodes = list(ast.walk(endpoint))
    all_acquires = [candidate for candidate in endpoint_nodes if isinstance(candidate, ast.Call) and _is_acquisition_like_call(candidate)]
    acquire_references = [candidate for candidate in endpoint_nodes if _is_acquisition_like_reference(candidate)]
    acquisitions = _bound_acquire(endpoint)
    if len(all_acquires) != 1 or len(acquisitions) != 1:
        violations.append(
            f"{contract.name}: expected exactly one awaited SessionOperationLease.acquire "
            f"bound directly in the endpoint body; found {len(all_acquires)} acquisition-like calls "
            f"and {len(acquisitions)} direct bindings"
        )
        return violations
    lease_name, acquire, assignment = acquisitions[0]
    violations.extend(
        _protected_object_mutation_violations(
            endpoint,
            {lease_name, "blob_service", "session_service", *required_inputs},
        )
    )
    if len(acquire_references) != 1 or acquire_references[0] is not acquire.func:
        violations.append(f"{contract.name}: SessionOperationLease.acquire reference must occur only as the canonical acquisition callable")
    nested_protected_uses = _nested_scope_protected_uses(
        endpoint,
        {
            lease_name,
            _CONTEXT_PARAMETER,
            "blob_service",
            "session_service",
            *_TRUSTED_ROUTE_SYMBOLS,
            *required_inputs,
        },
    )
    if nested_protected_uses:
        violations.append(f"{contract.name}: nested scopes must not capture, shadow, or mutate protected bindings")
    if contract.name == "delete_blob":
        forbidden_read_names = {
            "_get_owned_blob",
            "get_blob",
            "list_blobs",
            "read_blob_content",
            "read_blob_preview",
        }
        forbidden_pregets = [
            candidate
            for candidate in ast.walk(endpoint)
            if (isinstance(candidate, ast.Name) and candidate.id in forbidden_read_names)
            or (isinstance(candidate, ast.Attribute) and candidate.attr in forbidden_read_names)
        ]
        if forbidden_pregets:
            violations.append("delete_blob: preliminary ownership/blob read makes cleanup-only retry unreachable")

    if len(acquire.args) != 1 or _dotted_name(acquire.args[0]) != "session_service.session_operation_authority":
        violations.append(f"{contract.name}: acquire must use session_service.session_operation_authority")
    expected_acquire_keywords = {
        "session_id",
        "operation_kind",
        "owner_instance_id",
        "lease_seconds",
    }
    actual_acquire_keywords = [keyword.arg for keyword in acquire.keywords]
    if len(actual_acquire_keywords) != len(expected_acquire_keywords) or set(actual_acquire_keywords) != expected_acquire_keywords:
        violations.append(f"{contract.name}: acquire must use only the exact four named keywords")
    session_values = [keyword.value for keyword in acquire.keywords if keyword.arg == "session_id"]
    if len(session_values) != 1 or _dotted_name(session_values[0]) != "session_id":
        violations.append(f"{contract.name}: acquire must bind exact session_id=session_id")
    kind_values = [keyword.value for keyword in acquire.keywords if keyword.arg == "operation_kind"]
    if len(kind_values) != 1 or _dotted_name(kind_values[0]) != f"SessionOperationKind.{contract.operation_kind}":
        violations.append(f"{contract.name}: acquire must use SessionOperationKind.{contract.operation_kind}")
    owner_values = [keyword.value for keyword in acquire.keywords if keyword.arg == "owner_instance_id"]
    if len(owner_values) != 1 or _dotted_name(owner_values[0]) != "session_service.session_operation_owner_instance_id":
        violations.append(f"{contract.name}: acquire must use exact session-service owner identity")
    lease_values = [keyword.value for keyword in acquire.keywords if keyword.arg == "lease_seconds"]
    if len(lease_values) != 1 or _dotted_name(lease_values[0]) != "session_service.session_operation_lease_seconds":
        violations.append(f"{contract.name}: acquire must use exact session-service lease duration")

    assignment_index = endpoint.body.index(assignment)
    if contract.name == "delete_blob":
        prefix = endpoint.body[:assignment_index]
        if (
            prefix
            and isinstance(prefix[0], ast.Expr)
            and isinstance(prefix[0].value, ast.Constant)
            and isinstance(prefix[0].value.value, str)
        ):
            prefix = prefix[1:]
        exact_initializers = (
            len(prefix) == 2
            and sum(_approved_service_initialization(statement, "blob_service") for statement in prefix) == 1
            and sum(_approved_service_initialization(statement, "session_service") for statement in prefix) == 1
        )
        if not exact_initializers:
            violations.append("delete_blob: only exact authenticated service initializers may precede ARCHIVE acquire")
    post_acquire_nodes = _nodes_in_statements(endpoint.body[assignment_index + 1 :])
    lease_rebindings = [
        candidate
        for candidate in post_acquire_nodes
        if isinstance(candidate, ast.Name) and candidate.id == lease_name and isinstance(candidate.ctx, (ast.Store, ast.Del))
    ]
    if lease_rebindings:
        violations.append(f"{contract.name}: acquired lease binding {lease_name!r} must not be rebound or deleted")
    for protected_binding in ("blob_service", "session_service"):
        violations.extend(
            _service_binding_violations(
                endpoint,
                binding=protected_binding,
                acquisition_index=assignment_index,
                allow_parameter=False,
            )
        )
    if assignment_index + 1 >= len(endpoint.body) or not isinstance(endpoint.body[assignment_index + 1], ast.Try):
        violations.append(f"{contract.name}: protecting try must immediately follow lease acquisition")
        return violations
    protecting_try = endpoint.body[assignment_index + 1]
    protected_nodes = _nodes_in_statements(protecting_try.body)
    directly_awaited = _directly_awaited_calls(protected_nodes)
    allowed_prior_controls = {
        statement
        for statement in protecting_try.body
        if contract.name in _STATE_GUARD_AST_SHA256
        and hashlib.sha256(ast.dump(statement, include_attributes=False).encode()).hexdigest() == _STATE_GUARD_AST_SHA256[contract.name]
    }

    expected_effects: list[ast.Call] = []
    approved_lease_loads: set[ast.Name] = set()
    for receiver, method_name in contract.effects:
        calls = _effect_calls(endpoint, method_name)
        if len(calls) != 1:
            violations.append(f"{contract.name}: expected exactly one {method_name} effect; found {len(calls)}")
            continue
        call = calls[0]
        expected_effects.append(call)
        actual_receiver = _dotted_name(call.func.value) if isinstance(call.func, ast.Attribute) else None
        if actual_receiver != receiver:
            violations.append(f"{contract.name}: {method_name} must use exact receiver {receiver!r}")
        callable_references = _effect_callable_references(
            endpoint,
            receiver=receiver,
            method_name=method_name,
        )
        if len(callable_references) != 1 or callable_references[0] is not call.func:
            callable_name = f"{receiver}.{method_name}" if receiver is not None else method_name
            violations.append(f"{contract.name}: {callable_name} callable must not escape its exact awaited call")
        if call not in protected_nodes:
            violations.append(f"{contract.name}: {method_name} is outside the protecting try")
        if call not in directly_awaited:
            violations.append(f"{contract.name}: {method_name} must itself be directly awaited")
        expected_positional, expected_keywords = _EFFECT_CALL_SHAPES[(contract.name, method_name)]
        if not _call_matches_shape(
            call,
            positional=expected_positional,
            keywords=expected_keywords,
        ):
            violations.append(f"{contract.name}: {method_name} must receive only its exact contract arguments")
        if _is_statically_unreachable(
            call,
            protecting_try,
            allowed_prior_controls=allowed_prior_controls,
        ):
            violations.append(f"{contract.name}: {method_name} effect is statically unreachable")
        if not _is_exact_context_keyword(call, lease_name):
            violations.append(f"{contract.name}: {method_name} must receive exact {lease_name}.context")
        else:
            context_value = next(keyword.value for keyword in call.keywords if keyword.arg == _CONTEXT_PARAMETER)
            if isinstance(context_value, ast.Attribute) and isinstance(context_value.value, ast.Name):
                approved_lease_loads.add(context_value.value)

    if len(expected_effects) == len(contract.effects):
        effect_statement_indices = [
            next(
                index
                for index, statement in enumerate(protecting_try.body)
                if any(candidate is effect for candidate in ast.walk(statement))
            )
            for effect in expected_effects
        ]
        if effect_statement_indices[0] != 0:
            violations.append(f"{contract.name}: first required effect must occur in the first protecting-try statement")
        if len(effect_statement_indices) > 1:
            first_index, second_index = effect_statement_indices[:2]
            intervening = protecting_try.body[first_index + 1 : second_index]
            guard_is_exact = not intervening or (
                len(intervening) == 1
                and contract.name in _STATE_GUARD_AST_SHA256
                and hashlib.sha256(ast.dump(intervening[0], include_attributes=False).encode()).hexdigest()
                == _STATE_GUARD_AST_SHA256[contract.name]
            )
            if second_index < first_index or not guard_is_exact:
                violations.append(f"{contract.name}: only the exact optional state guard may occur between required blob-read effects")

    if contract.name == "delete_blob":
        direct_delete_body = (
            len(protecting_try.body) == 1
            and isinstance(protecting_try.body[0], ast.Expr)
            and isinstance(protecting_try.body[0].value, ast.Await)
            and isinstance(protecting_try.body[0].value.value, ast.Call)
            and len(expected_effects) == 1
            and protecting_try.body[0].value.value is expected_effects[0]
        )
        if not direct_delete_body or assignment_index + 2 != len(endpoint.body):
            violations.append("delete_blob: protecting try must contain only direct delete and be the final endpoint statement")

    total_closes = [
        candidate
        for candidate in _runtime_nodes(endpoint, root_is_scope=True)
        if isinstance(candidate, ast.Call) and _dotted_name(candidate.func) == f"{lease_name}.close"
    ]
    close_references = [
        candidate
        for candidate in _runtime_nodes(endpoint, root_is_scope=True)
        if isinstance(candidate, ast.Attribute) and _dotted_name(candidate) == f"{lease_name}.close"
    ]
    finally_nodes = _nodes_in_statements(protecting_try.finalbody)
    directly_awaited_finally = _directly_awaited_calls(finally_nodes)
    canonical_finally = (
        len(protecting_try.finalbody) == 1
        and isinstance(protecting_try.finalbody[0], ast.Expr)
        and isinstance(protecting_try.finalbody[0].value, ast.Await)
        and isinstance(protecting_try.finalbody[0].value.value, ast.Call)
        and _dotted_name(protecting_try.finalbody[0].value.value.func) == f"{lease_name}.close"
        and not protecting_try.finalbody[0].value.value.args
        and not protecting_try.finalbody[0].value.value.keywords
    )
    if len(total_closes) != 1:
        violations.append(f"{contract.name}: expected exactly one total exact {lease_name}.close() call; found {len(total_closes)}")
    elif total_closes[0].args or total_closes[0].keywords:
        violations.append(f"{contract.name}: exact {lease_name}.close() accepts no arguments")
    elif total_closes[0] not in directly_awaited_finally:
        violations.append(f"{contract.name}: finally must directly await exact {lease_name}.close() once")
    if not canonical_finally:
        violations.append(f"{contract.name}: finally body must solely be exact await {lease_name}.close()")
    canonical_close_call = (
        protecting_try.finalbody[0].value.value
        if canonical_finally
        and isinstance(protecting_try.finalbody[0], ast.Expr)
        and isinstance(protecting_try.finalbody[0].value, ast.Await)
        and isinstance(protecting_try.finalbody[0].value.value, ast.Call)
        else None
    )
    if len(close_references) != 1 or canonical_close_call is None or close_references[0] is not canonical_close_call.func:
        violations.append(f"{contract.name}: {lease_name}.close reference must occur only as the canonical finally callable")
    elif isinstance(canonical_close_call.func, ast.Attribute) and isinstance(canonical_close_call.func.value, ast.Name):
        approved_lease_loads.add(canonical_close_call.func.value)

    directly_awaited_endpoint_calls = _directly_awaited_calls(set(_runtime_nodes(endpoint, root_is_scope=True)))
    approved_auxiliary_awaits: set[ast.Call] = set()
    for statement in endpoint.body:
        if not _approved_service_initialization(statement, "blob_service"):
            continue
        value = _direct_assignment_value(statement, "blob_service")
        if isinstance(value, ast.Await) and isinstance(value.value, ast.Call):
            approved_auxiliary_awaits.add(value.value)
    if contract.name == "create_blob_upload":
        approved_auxiliary_awaits.update(
            call
            for call in directly_awaited_endpoint_calls
            if _dotted_name(call.func) == "file.read"
            and tuple(ast.unparse(argument) for argument in call.args) == ("8192",)
            and not call.keywords
        )
    approved_awaits = {acquire, *expected_effects, *total_closes, *approved_auxiliary_awaits}
    unapproved_awaits = directly_awaited_endpoint_calls - approved_awaits
    if unapproved_awaits:
        violations.append(
            f"{contract.name}: directly awaited calls must be exact verifier, upload-read, lease, effect, or close operations"
        )

    lease_loads = [
        candidate
        for candidate in post_acquire_nodes
        if isinstance(candidate, ast.Name) and candidate.id == lease_name and isinstance(candidate.ctx, ast.Load)
    ]
    unapproved_lease_loads = [candidate for candidate in lease_loads if candidate not in approved_lease_loads]
    if unapproved_lease_loads:
        violations.append(f"{contract.name}: acquired lease may only load for exact effect contexts and canonical close")
    if not protecting_try.finalbody:
        violations.append(f"{contract.name}: protecting try has no finally")
    if any(call.lineno <= assignment.lineno for call in expected_effects):
        violations.append(f"{contract.name}: a service effect precedes lease acquisition")
    return violations


def test_standalone_blob_routes_bind_the_exact_renewable_lease() -> None:
    tree = ast.parse(_BLOB_ROUTES.read_text(encoding="utf-8"), filename=str(_BLOB_ROUTES))
    violations = [violation for contract in _ENDPOINTS for violation in _route_violations(tree, contract)]
    assert not violations, "\n".join(violations)


def test_production_gate_is_fillable_by_canonical_functional_delete_tail() -> None:
    tree = ast.parse(_BLOB_ROUTES.read_text(encoding="utf-8"))
    endpoint = _endpoint_definitions(tree, "delete_blob")[0]
    acquisition_index = next(
        index
        for index, statement in enumerate(endpoint.body)
        if any(isinstance(candidate, ast.Call) and _is_acquisition_like_call(candidate) for candidate in ast.walk(statement))
    )
    reference_tree = ast.parse(_delete_endpoint_source(preliminary_get=False))
    reference_endpoint = _endpoint_definitions(reference_tree, "delete_blob")[0]
    reference_acquisition_index = next(
        index
        for index, statement in enumerate(reference_endpoint.body)
        if any(isinstance(candidate, ast.Call) and _is_acquisition_like_call(candidate) for candidate in ast.walk(statement))
    )
    reference_tail = reference_endpoint.body[reference_acquisition_index + 1 :]
    line_delta = endpoint.body[acquisition_index + 1].lineno - reference_tail[0].lineno
    for statement in reference_tail:
        ast.increment_lineno(statement, line_delta)
    endpoint.body[acquisition_index + 1 :] = reference_tail
    violations = [violation for contract in _ENDPOINTS for violation in _route_violations(tree, contract)]
    assert violations == []


def test_route_gate_rejects_top_level_shadow_of_canonical_import() -> None:
    source = _BLOB_ROUTES.read_text(encoding="utf-8").replace(
        "def _blob_response(record: BlobRecord) -> BlobMetadataResponse:",
        "def BlobQuotaExceededError():\n    return None\n\n\ndef _blob_response(record: BlobRecord) -> BlobMetadataResponse:",
        1,
    )
    tree = ast.parse(source)
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    assert any("exact closed top-level definition inventory" in item for item in _route_violations(tree, contract))


@pytest.mark.parametrize(
    ("mutation", "expected_violation"),
    (
        ("async_factory_redefinition", "exact closed top-level definition inventory"),
        ("verifier_inner_import", "trusted route helper '_verify_session_and_get_blob_service' does not match"),
        ("owner_nested_lambda", "trusted route helper '_get_owned_blob' does not match"),
        ("missing_verifier", "exact closed top-level definition inventory"),
        ("duplicate_owner_helper", "exact closed top-level definition inventory"),
        ("factory_type_parameter", "create_blobs_router: standalone blob route definitions must not declare type parameters"),
        ("endpoint_type_parameter", "download_blob_content: standalone blob route definitions must not declare type parameters"),
        ("endpoint_return_expression", "endpoint return annotation must match the exact canonical definition surface"),
    ),
)
def test_route_gate_rejects_helper_and_factory_definition_drift(mutation: str, expected_violation: str) -> None:
    source = _BLOB_ROUTES.read_text(encoding="utf-8")
    if mutation == "async_factory_redefinition":
        source += "\nasync def create_blobs_router():\n    return attacker_router\n"
    elif mutation == "verifier_inner_import":
        source = source.replace(
            ') -> BlobServiceImpl:\n    """Verify session ownership and return the blob service.',
            ') -> BlobServiceImpl:\n    from attacker import *\n\n    """Verify session ownership and return the blob service.',
            1,
        )
    elif mutation == "owner_nested_lambda":
        source = source.replace(
            ') -> BlobRecord:\n    """Fetch a blob and verify it belongs to the given session.',
            ') -> BlobRecord:\n    decoy = lambda: attacker_callback()\n\n    """Fetch a blob and verify it belongs to the given session.',
            1,
        )
    elif mutation == "missing_verifier":
        source = source.replace(
            "async def _verify_session_and_get_blob_service(",
            "async def missing_verifier(",
            1,
        )
    elif mutation == "factory_type_parameter":
        source = source.replace(
            "def create_blobs_router() -> APIRouter:",
            "def create_blobs_router[T]() -> APIRouter:",
            1,
        )
    elif mutation == "endpoint_type_parameter":
        source = source.replace(
            "    async def download_blob_content(",
            "    async def download_blob_content[T: attacker[0]](",
            1,
        )
    elif mutation == "endpoint_return_expression":
        source = source.replace(
            "    ) -> Response:",
            "    ) -> Query():",
            1,
        )
    else:
        source += "\nasync def _get_owned_blob():\n    return attacker_blob\n"
    tree = ast.parse(source)
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    assert any(expected_violation in item for item in _route_violations(tree, contract))


def test_route_gate_rejects_rebound_factory_router() -> None:
    source = _BLOB_ROUTES.read_text(encoding="utf-8").replace(
        '    @router.post("", status_code=201, response_model=BlobMetadataResponse)',
        '    router = attacker_router\n\n    @router.post("", status_code=201, response_model=BlobMetadataResponse)',
        1,
    )
    tree = ast.parse(source)
    for endpoint_name in ("create_blob_upload", "download_blob_content"):
        contract = next(item for item in _ENDPOINTS if item.name == endpoint_name)
        violations = _route_violations(tree, contract)
        assert any("router requires exactly one immutable canonical APIRouter binding" in item for item in violations)


@pytest.mark.parametrize(
    ("mutation", "expected_violation"),
    (
        ("wrong_factory_return", "factory must end with the sole exact return router statement"),
        ("module_helper_decorator", "module-scope definition surface contains executable expressions"),
        ("list_endpoint_decorator", "list_blobs decorator must match exact canonical registration"),
        ("extra_untracked_endpoint", "nested endpoint inventory must match the exact closed canonical set"),
        ("factory_outer_implicit_expression", "outer statement inventory must be exact"),
        ("list_endpoint_body_mutation", "list_blobs definition must match exact canonical implementation"),
    ),
)
def test_route_gate_rejects_open_factory_and_definition_surfaces(mutation: str, expected_violation: str) -> None:
    source = _BLOB_ROUTES.read_text(encoding="utf-8")
    if mutation == "wrong_factory_return":
        source = source.replace("    return router\n", "    return None\n", 1)
    elif mutation == "module_helper_decorator":
        source = source.replace(
            "def _blob_response(record: BlobRecord) -> BlobMetadataResponse:",
            "@attacker_decorator\ndef _blob_response(record: BlobRecord) -> BlobMetadataResponse:",
            1,
        )
    elif mutation == "list_endpoint_decorator":
        source = source.replace(
            '    @router.get("", response_model=list[BlobMetadataResponse])',
            '    @attacker_decorator\n    @router.get("", response_model=list[BlobMetadataResponse])',
            1,
        )
    elif mutation == "factory_outer_implicit_expression":
        source = source.replace(
            "    return router\n",
            "    if attacker_condition:\n        pass\n\n    return router\n",
            1,
        )
    elif mutation == "list_endpoint_body_mutation":
        source = source.replace(
            "        records = await blob_service.list_blobs(session_id, limit=limit, offset=offset)",
            "        assert attacker_condition\n        records = await blob_service.list_blobs(session_id, limit=limit, offset=offset)",
            1,
        )
    else:
        source = source.replace(
            "    return router\n",
            '    @router.delete("/attacker")\n'
            "    async def attacker_delete():\n"
            "        await blob_service.delete_blob(attacker_blob_id)\n\n"
            "    return router\n",
            1,
        )
    tree = ast.parse(source)
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    assert any(expected_violation in item for item in _route_violations(tree, contract))


@pytest.mark.parametrize(
    ("mutation", "expected_violation"),
    (
        ("wildcard_import", "must not use wildcard imports"),
        ("side_effect_import", "exact closed canonical import inventory"),
        ("import_factory_rebind", "binding 'create_blobs_router' must not be imported"),
        ("import_auth_dependency_rebind", "binding 'get_current_user' must not be imported"),
        ("renamed_factory", "require exactly one top-level create_blobs_router factory"),
        ("changed_factory_signature", "signature must match exact canonical factory identity"),
    ),
)
def test_route_gate_rejects_open_import_and_factory_identity_surfaces(mutation: str, expected_violation: str) -> None:
    source = _BLOB_ROUTES.read_text(encoding="utf-8")
    if mutation == "wildcard_import":
        source += "\nfrom attacker import *\n"
    elif mutation == "side_effect_import":
        source += "\nimport attacker\n"
    elif mutation == "import_factory_rebind":
        source += "\nfrom attacker import create_blobs_router\n"
    elif mutation == "import_auth_dependency_rebind":
        source += "\nfrom attacker import get_current_user\n"
    elif mutation == "renamed_factory":
        source = source.replace("def create_blobs_router() -> APIRouter:", "def attacker_factory() -> APIRouter:", 1)
    else:
        source = source.replace(
            "def create_blobs_router() -> APIRouter:",
            "def create_blobs_router(trap: object = None) -> APIRouter:",
            1,
        )
    tree = ast.parse(source)
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    assert any(expected_violation in item for item in _route_violations(tree, contract))


def _delete_endpoint_source(*, preliminary_get: bool) -> str:
    preget = (
        "        await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)\n"
        if preliminary_get
        else ""
    )
    return f"""{_TRUSTED_IMPORT_SOURCE}from fastapi import HTTPException
@router.delete("/{{blob_id}}", status_code=204)
async def delete_blob(session_id, blob_id, request, user):
    session_service = request.app.state.session_service
    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
    lease = await SessionOperationLease.acquire(
        session_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.ARCHIVE,
        owner_instance_id=session_service.session_operation_owner_instance_id,
        lease_seconds=session_service.session_operation_lease_seconds,
    )
    try:
{preget}        await blob_service.delete_blob(blob_id, session_operation_context=lease.context)
    except BlobNotFoundError:
        return
    except (BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    finally:
        await lease.close()
"""


def test_delete_gate_accepts_direct_cleanup_capable_shape() -> None:
    contract = next(item for item in _ENDPOINTS if item.name == "delete_blob")
    assert _route_violations(ast.parse(_delete_endpoint_source(preliminary_get=False)), contract) == []


def test_delete_gate_rejects_preget_that_blocks_cleanup_only_retry() -> None:
    contract = next(item for item in _ENDPOINTS if item.name == "delete_blob")
    violations = _route_violations(ast.parse(_delete_endpoint_source(preliminary_get=True)), contract)
    assert any("cleanup-only retry unreachable" in violation for violation in violations)


@pytest.mark.parametrize(
    "mutation",
    ("injected_services", "qualified_preget", "wrapper_precheck", "wrong_blob_id", "unreachable_delete"),
)
def test_delete_gate_rejects_cleanup_blocking_or_forged_shapes(mutation: str) -> None:
    source = _delete_endpoint_source(preliminary_get=False)
    if mutation == "injected_services":
        source = source.replace(
            "async def delete_blob(session_id, blob_id, request, user):\n",
            "async def delete_blob(session_id, blob_id, request, user, session_service, blob_service):\n",
        ).replace(
            "    session_service = request.app.state.session_service\n"
            "    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)\n",
            "",
        )
    elif mutation == "qualified_preget":
        source = source.replace(
            "    try:\n",
            "    try:\n"
            "        await blob_routes._get_owned_blob("
            "blob_service, session_id, blob_id, session_operation_context=lease.context)\n",
        )
    elif mutation == "wrapper_precheck":
        source = source.replace(
            "    lease = await SessionOperationLease.acquire(\n",
            "    await ownership_precheck(blob_service, blob_id)\n    lease = await SessionOperationLease.acquire(\n",
        )
    elif mutation == "wrong_blob_id":
        source = source.replace("delete_blob(blob_id,", "delete_blob(other_blob_id,")
    elif mutation == "unreachable_delete":
        source = source.replace(
            "        await blob_service.delete_blob(",
            "        if False:\n            await blob_service.delete_blob(",
        )
    contract = next(item for item in _ENDPOINTS if item.name == "delete_blob")
    assert _route_violations(ast.parse(source), contract), f"DELETE gate admitted {mutation}"


def _inline_create_endpoint_source(*, unreachable: bool) -> str:
    effect_indent = "        if False:\n            " if unreachable else "        "
    return f"""{_TRUSTED_IMPORT_SOURCE}
@router.post("/inline", status_code=201, response_model=BlobMetadataResponse)
async def create_blob_inline(session_id, body, request, user):
    session_service = request.app.state.session_service
    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)
    lease = await SessionOperationLease.acquire(
        session_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.CREATE,
        owner_instance_id=session_service.session_operation_owner_instance_id,
        lease_seconds=session_service.session_operation_lease_seconds,
    )
    try:
{effect_indent}record = await blob_service.create_blob(
            session_id=session_id,
            filename=body.filename,
            content=content_bytes,
            mime_type=body.mime_type,
            created_by="user",
            source_description="created inline",
            session_operation_context=lease.context,
        )
        return record
    finally:
        await lease.close()
"""


@pytest.mark.parametrize(
    "mutation",
    (
        "constant_false",
        "after_unconditional_return",
        "under_not_true",
        "false_comparison",
        "arithmetic_false",
        "multiply_false",
        "after_break",
        "after_continue",
        "after_if_true_return",
        "under_empty_for",
        "nested_try_assert_false",
        "nested_try_exhaustive_match",
    ),
)
def test_create_gate_rejects_statically_unreachable_effect(mutation: str) -> None:
    contract = next(item for item in _ENDPOINTS if item.name == "create_blob_inline")
    valid_source = _inline_create_endpoint_source(unreachable=False)
    assert _route_violations(ast.parse(valid_source), contract) == []
    if mutation == "constant_false":
        source = _inline_create_endpoint_source(unreachable=True)
    elif mutation == "after_unconditional_return":
        source = valid_source.replace("    try:\n        record", "    try:\n        return\n        record")
    elif mutation == "under_not_true":
        source = valid_source.replace("    try:\n        record", "    try:\n        if not True:\n            record")
    elif mutation == "false_comparison":
        source = valid_source.replace("    try:\n        record", "    try:\n        if 1 == 2:\n            record")
    elif mutation == "arithmetic_false":
        source = valid_source.replace("    try:\n        record", "    try:\n        if 1 - 1:\n            record")
    elif mutation == "multiply_false":
        source = valid_source.replace("    try:\n        record", "    try:\n        if 1 * 0:\n            record")
    elif mutation == "after_break":
        source = valid_source.replace("    try:\n        record", "    try:\n        while True:\n            break\n            record")
    elif mutation == "after_continue":
        source = valid_source.replace("    try:\n        record", "    try:\n        while True:\n            continue\n            record")
    elif mutation == "after_if_true_return":
        source = valid_source.replace("    try:\n        record", "    try:\n        if True:\n            return\n        record")
    elif mutation == "under_empty_for":
        source = valid_source.replace("    try:\n        record", "    try:\n        for _ in ():\n            record")
    elif mutation == "nested_try_assert_false":
        source = valid_source.replace(
            "    try:\n        record",
            "    try:\n        try:\n            assert False\n            record",
        ).replace(
            "        )\n        return record",
            "        )\n        finally:\n            pass\n        return record",
        )
    else:
        source = valid_source.replace(
            "    try:\n        record",
            "    try:\n        try:\n            match 0:\n                case _:\n                    return\n            record",
        ).replace(
            "        )\n        return record",
            "        )\n        finally:\n            pass\n        return record",
        )
    violations = _route_violations(ast.parse(source), contract)
    assert any("statically unreachable" in violation for violation in violations)


@pytest.mark.parametrize(
    "mutation",
    (
        "other_context",
        "wrong_receiver",
        "unawaited_forward",
        "multiple_get_blob",
        "context_reassignment",
        "aliased_get_blob",
        "receiver_reassignment",
        "nested_nonlocal_context_replacement",
        "nested_nonlocal_receiver_replacement",
        "other_blob_id",
        "unreachable_get_blob",
        "get_blob_after_unconditional_return",
        "get_blob_under_not_true",
        "get_blob_under_false_comparison",
    ),
)
def test_owned_blob_helper_gate_rejects_adversarial_forwarding(mutation: str) -> None:
    receiver = "blob_service"
    context = "session_operation_context"
    await_prefix = "await "
    pre_call = ""
    call_indent = "    "
    blob_argument = "blob_id"
    extra_call = ""
    if mutation == "other_context":
        context = "other_context"
    elif mutation == "wrong_receiver":
        receiver = "other_service"
    elif mutation == "unawaited_forward":
        await_prefix = ""
    elif mutation == "multiple_get_blob":
        extra_call = "\n    await blob_service.get_blob(blob_id, session_operation_context=session_operation_context)"
    elif mutation == "context_reassignment":
        pre_call = "    session_operation_context = other_context\n"
    elif mutation == "aliased_get_blob":
        pre_call = "    alias = blob_service.get_blob\n"
    elif mutation == "receiver_reassignment":
        pre_call = "    blob_service = other_service\n"
    elif mutation == "nested_nonlocal_context_replacement":
        pre_call = (
            "    async def replace_context():\n"
            "        nonlocal session_operation_context\n"
            "        session_operation_context = other_context\n"
            "    await replace_context()\n"
        )
    elif mutation == "nested_nonlocal_receiver_replacement":
        pre_call = (
            "    async def replace_receiver():\n"
            "        nonlocal blob_service\n"
            "        blob_service = other_service\n"
            "    await replace_receiver()\n"
        )
    elif mutation == "other_blob_id":
        blob_argument = "other_blob_id"
    elif mutation == "unreachable_get_blob":
        pre_call = "    if False:\n"
        call_indent = "        "
    elif mutation == "get_blob_after_unconditional_return":
        pre_call = "    return None\n"
    elif mutation == "get_blob_under_not_true":
        pre_call = "    if not True:\n"
        call_indent = "        "
    elif mutation == "get_blob_under_false_comparison":
        pre_call = "    if 1 == 2:\n"
        call_indent = "        "
    source = f"""
async def _get_owned_blob(blob_service, session_id, blob_id, *, session_operation_context):
{pre_call}{call_indent}blob = {await_prefix}{receiver}.get_blob({blob_argument}, session_operation_context={context}){extra_call}
    return blob
"""
    violations = _owned_blob_helper_violations(ast.parse(source))
    assert violations, f"helper gate admitted adversarial forwarding: {mutation}"
    if mutation in {
        "unreachable_get_blob",
        "get_blob_after_unconditional_return",
        "get_blob_under_not_true",
        "get_blob_under_false_comparison",
    }:
        assert any("statically unreachable" in violation for violation in violations)


@pytest.mark.parametrize(
    "mutation",
    (
        "decoy_and_unfenced",
        "double_acquire",
        "unawaited_close",
        "wrong_receiver_and_context",
        "other_session_id",
        "uncalled_nested_only_effect",
        "duplicate_same_name_endpoint",
        "awaited_and_unawaited_close",
        "pre_try_raising_gap",
        "unawaited_owned_blob",
        "unawaited_content_read",
        "invalid_arg_close_before_valid_close",
        "pre_close_raise",
        "pre_close_return",
        "lease_reassignment_inside_try",
        "decoy_direct_and_aliased_post_close",
        "decoy_helper_and_aliased_post_close",
        "receiver_reassignment_inside_try",
        "session_service_reassignment_inside_try",
        "aliased_early_close",
        "pre_acquire_blob_service_reassignment",
        "pre_acquire_session_service_reassignment",
        "lease_alias_early_close",
        "called_nested_lease_close",
        "nested_receiver_rebinding",
        "wrong_owner_identity",
        "wrong_lease_duration",
        "unexpected_acquire_keyword",
        "unexpected_acquire_positional",
        "wrong_read_blob_id",
        "unreachable_content_read",
        "unreachable_owned_blob",
        "module_forged_lease",
        "local_forged_lease",
        "module_forged_owner_helper",
        "module_forged_verifier",
        "async_function_forged_verifier",
        "class_forged_lease",
        "import_forged_verifier",
        "aliased_second_acquire",
        "aliased_acquire_callable",
        "imported_lease_alias_second_acquire",
        "nested_second_acquire",
        "qualified_module_second_acquire",
        "qualified_getattr_second_acquire",
        "qualified_dynamic_getattr_second_acquire",
        "aliased_getattr_second_acquire",
        "module_helper_second_acquire",
        "module_helper_captures_acquire",
        "module_helper_escapes_authority_acquire",
        "module_helper_fully_aliased_getattr",
        "module_helper_fully_aliased_authority_getattr",
        "module_helper_builtins_getattr_alias",
        "module_helper_type_member_alias",
        "module_helper_vars_lookup",
        "module_helper_dict_lookup",
        "module_helper_eval_lookup",
        "setattr_blob_service_effect",
        "setattr_session_authority",
        "direct_session_authority_mutation",
        "direct_blob_effect_mutation",
        "unknown_sync_before_acquire",
        "unknown_sync_after_close",
        "nonexact_state_guard",
        "container_list_alias_mutation",
        "container_dict_alias_mutation",
        "container_tuple_unpack_alias_mutation",
        "named_expression_alias_mutation",
        "list_comprehension_alias_mutation",
        "dict_comprehension_alias_mutation",
        "nested_comprehension_alias_mutation",
        "unfenced_endpoint_decorator",
        "rebound_http_exception_callable",
        "mutated_quote_callable_object",
        "cast_laundered_object_mutation",
        "except_handler_callable_rebind",
        "endpoint_default_callback",
        "module_top_level_callback",
        "with_implicit_callback",
        "for_implicit_callback",
        "endpoint_default_implicit_comprehension",
        "module_implicit_comprehension",
        "await_noncall_object",
        "yield_before_acquire",
        "endpoint_default_subscription",
        "endpoint_default_binary_operator",
        "endpoint_annotation_subscription",
        "module_subscription_expression",
        "module_binary_expression",
        "pre_acquire_assert_truthiness",
        "pre_acquire_if_truthiness",
        "pre_acquire_subscription",
        "pre_acquire_binary_operator",
        "post_effect_subscription",
        "post_effect_assert_truthiness",
        "post_effect_binary_operator",
        "post_effect_if_truthiness",
        "post_effect_raise",
        "post_effect_nested_function",
        "post_effect_nested_class",
        "effects_after_exhaustive_match",
        "mutate_lease_acquire",
        "mutate_verifier_code",
        "mutate_owner_helper_code",
        "mutate_operation_kind_member",
        "rebind_user",
        "rebind_session_id",
        "rebind_request",
        "rebind_blob_id",
        "import_rebind_user",
        "from_import_rebind_session_id",
        "function_rebind_blob_id",
        "class_rebind_request",
        "class_rebind_blob_service",
        "import_rebind_blob_service",
        "class_rebind_session_service",
        "match_capture_lease",
        "match_star_capture_lease",
        "match_mapping_capture_user",
        "match_class_capture_user",
        "effects_after_unconditional_return",
        "effects_under_not_true",
        "effects_under_false_comparison",
        "effects_after_break",
        "effects_after_continue",
        "effects_after_if_true_return",
        "effects_under_multiply_false",
        "effects_under_empty_for",
        "effects_after_try_return",
        "effects_after_infinite_while",
        "effects_after_exhaustive_match",
        "effects_after_if_true_return",
        "effects_under_multiply_false",
        "effects_under_empty_for",
        "effects_after_try_return",
        "effects_after_infinite_while",
        "effects_after_if_true_return",
        "effects_under_multiply_false",
        "effects_under_empty_for",
    ),
)
def test_route_gate_rejects_adversarial_decoys(mutation: str) -> None:
    pre_initialization: list[str] = []
    pre_acquire: list[str] = []
    acquire = [
        "lease = await SessionOperationLease.acquire(",
        "    session_service.session_operation_authority,",
        "    session_id=session_id,",
        "    operation_kind=SessionOperationKind.BLOB_READ,",
        "    owner_instance_id=session_service.session_operation_owner_instance_id,",
        "    lease_seconds=session_service.session_operation_lease_seconds,",
        ")",
    ]
    protected_lines = ["await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)"]
    pre_try: list[str] = []
    close_lines = ["await lease.close()"]
    tail: list[str] = []
    if mutation == "double_acquire":
        acquire += [line.replace("lease =", "decoy =") for line in acquire]
    elif mutation == "unawaited_close":
        close_lines = ["lease.close()"]
    elif mutation == "wrong_receiver_and_context":
        protected_lines = ["await decoy_service.read_blob_content(blob_id, session_operation_context=decoy.context)"]
    elif mutation == "decoy_and_unfenced":
        tail = ["await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)"]
    elif mutation == "other_session_id":
        acquire = [line.replace("session_id=session_id", "session_id=other_session_id") for line in acquire]
    elif mutation == "uncalled_nested_only_effect":
        protected_lines = [
            "async def decoy_effect():",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "awaited_and_unawaited_close":
        tail = ["lease.close()"]
    elif mutation == "pre_try_raising_gap":
        pre_try = ["raise RuntimeError('gap before protecting try')"]
    elif mutation == "unawaited_owned_blob":
        protected_lines = [
            "blob = _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "unawaited_content_read":
        protected_lines = ["blob_service.read_blob_content(blob_id, session_operation_context=lease.context)"]
    elif mutation == "invalid_arg_close_before_valid_close":
        close_lines = ["lease.close('invalid')", "await lease.close()"]
    elif mutation == "pre_close_raise":
        close_lines = ["raise RuntimeError('before close')", "await lease.close()"]
    elif mutation == "pre_close_return":
        close_lines = ["return", "await lease.close()"]
    elif mutation == "lease_reassignment_inside_try":
        protected_lines.append("lease = replacement_lease")
    elif mutation == "decoy_direct_and_aliased_post_close":
        tail = [
            "reader = blob_service.read_blob_content",
            "await reader(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "decoy_helper_and_aliased_post_close":
        tail = [
            "owner_lookup = _get_owned_blob",
            "await owner_lookup(blob_service, session_id, blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "receiver_reassignment_inside_try":
        protected_lines.append("blob_service = other_service")
    elif mutation == "session_service_reassignment_inside_try":
        protected_lines.append("session_service = other_session_service")
    elif mutation == "aliased_early_close":
        protected_lines = [
            "closer = lease.close",
            "await closer()",
            *protected_lines,
        ]
    elif mutation == "pre_acquire_blob_service_reassignment":
        pre_acquire = ["blob_service = other_service"]
    elif mutation == "pre_acquire_session_service_reassignment":
        pre_acquire = ["session_service = other_session_service"]
    elif mutation == "setattr_blob_service_effect":
        pre_acquire = ['setattr(blob_service, "read_blob_content", forged_read)']
    elif mutation == "setattr_session_authority":
        pre_acquire = ['setattr(session_service, "session_operation_authority", attacker_authority)']
    elif mutation == "direct_session_authority_mutation":
        pre_acquire = ["session_service.session_operation_authority = attacker_authority"]
    elif mutation == "direct_blob_effect_mutation":
        pre_acquire = ["blob_service.read_blob_content = forged_read"]
    elif mutation == "container_list_alias_mutation":
        pre_acquire = ["holders = [blob_service]", "holders[0].read_blob_content = forged_read"]
    elif mutation == "container_dict_alias_mutation":
        pre_acquire = ['holders = {"service": blob_service}', 'holders["service"].read_blob_content = forged_read']
    elif mutation == "container_tuple_unpack_alias_mutation":
        pre_acquire = ["holders = (blob_service,)", "(holder,) = holders", "holder.read_blob_content = forged_read"]
    elif mutation == "named_expression_alias_mutation":
        pre_acquire = ["if holders := [blob_service]:", "    holders[0].read_blob_content = forged_read"]
    elif mutation == "list_comprehension_alias_mutation":
        pre_acquire = ["holders = [blob_service for _ in (0,)]", "holders[0].read_blob_content = forged_read"]
    elif mutation == "dict_comprehension_alias_mutation":
        pre_acquire = [
            'holders = {key: blob_service for key in ("service",)}',
            'holders["service"].read_blob_content = forged_read',
        ]
    elif mutation == "nested_comprehension_alias_mutation":
        pre_acquire = [
            "holders = [[blob_service for _ in (0,)] for _ in (0,)]",
            "holders[0][0].read_blob_content = forged_read",
        ]
    elif mutation == "rebound_http_exception_callable":
        pre_acquire = ["HTTPException = attacker_callback", "HTTPException()"]
    elif mutation == "mutated_quote_callable_object":
        pre_acquire = ["quote.__code__ = forged_quote.__code__"]
    elif mutation == "cast_laundered_object_mutation":
        pre_acquire = ["escaped = cast(BlobServiceImpl, blob_service)", "escaped.read_blob_content = forged_read"]
    elif mutation == "except_handler_callable_rebind":
        pre_acquire = [
            "try:",
            "    raise BaseException",
            "except BaseException as HTTPException:",
            "    pass",
            "HTTPException()",
        ]
    elif mutation == "with_implicit_callback":
        pre_acquire = ["with attacker_context:", "    pass"]
    elif mutation == "for_implicit_callback":
        pre_acquire = ["for ignored in attacker_iterable:", "    pass"]
    elif mutation == "await_noncall_object":
        pre_acquire = ["await attacker_awaitable"]
    elif mutation == "yield_before_acquire":
        pre_acquire = ["yield attacker_value"]
    elif mutation == "pre_acquire_assert_truthiness":
        pre_acquire = ["assert attacker_condition"]
    elif mutation == "pre_acquire_if_truthiness":
        pre_acquire = ["if attacker_condition:", "    pass"]
    elif mutation == "pre_acquire_subscription":
        pre_acquire = ["escaped = attacker_container[0]"]
    elif mutation == "pre_acquire_binary_operator":
        pre_acquire = ["escaped = attacker_value + 1"]
    elif mutation == "unknown_sync_before_acquire":
        pre_acquire = ["unknown_callback()"]
    elif mutation == "unknown_sync_after_close":
        tail = ["unknown_callback()"]
    elif mutation == "post_effect_subscription":
        tail = ["attacker_container[0]"]
    elif mutation == "post_effect_assert_truthiness":
        tail = ["assert attacker_condition"]
    elif mutation == "post_effect_binary_operator":
        tail = ["attacker_value + 1"]
    elif mutation == "post_effect_if_truthiness":
        tail = ["if attacker_condition:", "    pass"]
    elif mutation == "post_effect_raise":
        tail = ["raise attacker_exception"]
    elif mutation == "post_effect_nested_function":
        tail = ["@attacker_decorator", "def nested():", "    pass"]
    elif mutation == "post_effect_nested_class":
        tail = ["@attacker_decorator", "class Nested:", "    pass"]
    elif mutation == "lease_alias_early_close":
        protected_lines = [
            "same_lease = lease",
            "await same_lease.close()",
            *protected_lines,
        ]
    elif mutation == "called_nested_lease_close":
        protected_lines = [
            "async def close_early():",
            "    await lease.close()",
            "await close_early()",
            *protected_lines,
        ]
    elif mutation == "nested_receiver_rebinding":
        protected_lines = [
            "async def redirect_receiver():",
            "    nonlocal blob_service",
            "    blob_service = other_service",
            "await redirect_receiver()",
            *protected_lines,
        ]
    elif mutation == "wrong_owner_identity":
        acquire = [line.replace("session_service.session_operation_owner_instance_id", "attacker_owner") for line in acquire]
    elif mutation == "wrong_lease_duration":
        acquire = [line.replace("session_service.session_operation_lease_seconds", "0") for line in acquire]
    elif mutation == "unexpected_acquire_keyword":
        acquire.insert(-1, "    unexpected=True,")
    elif mutation == "unexpected_acquire_positional":
        acquire.insert(2, "    attacker_authority,")
    elif mutation == "wrong_read_blob_id":
        protected_lines = ["await blob_service.read_blob_content(other_blob_id, session_operation_context=lease.context)"]
    elif mutation == "unreachable_content_read":
        protected_lines = [
            "if False:",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "unreachable_owned_blob":
        protected_lines = [
            "if False:",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "local_forged_lease":
        pre_acquire = ["SessionOperationLease = ForgedLease"]
    elif mutation == "aliased_second_acquire":
        pre_acquire = [
            "LeaseAlias = SessionOperationLease",
            "decoy = await LeaseAlias.acquire(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "aliased_acquire_callable":
        pre_acquire = [
            "acquirer = SessionOperationLease.acquire",
            "decoy = await acquirer(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "imported_lease_alias_second_acquire":
        pre_acquire = [
            "decoy = await LeaseAlias.acquire(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "nested_second_acquire":
        pre_acquire = [
            "async def acquire_decoy():",
            "    return await SessionOperationLease.acquire(",
            "        attacker_authority,",
            "        session_id=session_id,",
            "        operation_kind=SessionOperationKind.BLOB_READ,",
            "        owner_instance_id=attacker_owner,",
            "        lease_seconds=30,",
            "    )",
            "await acquire_decoy()",
        ]
    elif mutation == "qualified_module_second_acquire":
        pre_acquire = [
            "decoy = await lifecycle.SessionOperationLease.acquire(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "qualified_getattr_second_acquire":
        pre_acquire = [
            'decoy = await getattr(lifecycle.SessionOperationLease, "acquire")(',
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "qualified_dynamic_getattr_second_acquire":
        pre_acquire = [
            'method_name = "acquire"',
            "decoy = await getattr(lifecycle.SessionOperationLease, method_name)(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "aliased_getattr_second_acquire":
        pre_acquire = [
            "lookup = getattr",
            'method_name = "acquire"',
            "decoy = await lookup(lifecycle.SessionOperationLease, method_name)(",
            "    session_service.session_operation_authority,",
            "    session_id=session_id,",
            "    operation_kind=SessionOperationKind.BLOB_READ,",
            "    owner_instance_id=session_service.session_operation_owner_instance_id,",
            "    lease_seconds=session_service.session_operation_lease_seconds,",
            ")",
        ]
    elif mutation == "module_helper_second_acquire":
        pre_acquire = ["await acquire_extra()"]
    elif mutation in {
        "module_helper_captures_acquire",
        "module_helper_escapes_authority_acquire",
        "module_helper_fully_aliased_getattr",
        "module_helper_fully_aliased_authority_getattr",
        "module_helper_builtins_getattr_alias",
        "module_helper_type_member_alias",
        "module_helper_vars_lookup",
        "module_helper_dict_lookup",
        "module_helper_eval_lookup",
    }:
        pre_acquire = ["await acquire_extra(session_service.session_operation_authority)"]
    elif mutation == "rebind_user":
        pre_initialization = ["user = request.app.state.attacker_identity"]
    elif mutation == "rebind_session_id":
        pre_initialization = ["session_id = attacker_session_id"]
    elif mutation == "rebind_request":
        pre_initialization = ["request = attacker_request"]
    elif mutation == "rebind_blob_id":
        pre_initialization = ["blob_id = attacker_blob_id"]
    elif mutation == "import_rebind_user":
        pre_initialization = ["import attacker_identity as user"]
    elif mutation == "from_import_rebind_session_id":
        pre_initialization = ["from attacker import chosen_session as session_id"]
    elif mutation == "function_rebind_blob_id":
        pre_initialization = ["def blob_id():", "    return attacker_blob_id"]
    elif mutation == "class_rebind_request":
        pre_initialization = ["class request:", "    app = attacker_app"]
    elif mutation == "class_rebind_blob_service":
        pre_acquire = ["class blob_service:", "    read_blob_content = forged_read"]
    elif mutation == "import_rebind_blob_service":
        pre_acquire = ["import attacker_blob_service as blob_service"]
    elif mutation == "class_rebind_session_service":
        pre_acquire = ["class session_service:", "    session_operation_authority = attacker_authority"]
    elif mutation == "match_capture_lease":
        pre_initialization = ["match forged:", "    case SessionOperationLease:", "        pass"]
    elif mutation == "match_star_capture_lease":
        pre_initialization = ["match forged:", "    case [*SessionOperationLease]:", "        pass"]
    elif mutation == "match_mapping_capture_user":
        pre_initialization = ["match forged:", "    case {**user}:", "        pass"]
    elif mutation == "match_class_capture_user":
        pre_initialization = ["match forged:", "    case Identity(user_id=user):", "        pass"]
    elif mutation == "effects_after_unconditional_return":
        protected_lines = [
            "return",
            "await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_under_not_true":
        protected_lines = [
            "if not True:",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_under_false_comparison":
        protected_lines = [
            "if (1 == 2) or not True:",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_break":
        protected_lines = [
            "while True:",
            "    break",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_continue":
        protected_lines = [
            "while True:",
            "    continue",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_if_true_return":
        protected_lines = [
            "if True:",
            "    return",
            "await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_under_multiply_false":
        protected_lines = [
            "if 1 * 0:",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_under_empty_for":
        protected_lines = [
            "for _ in ():",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_try_return":
        protected_lines = [
            "try:",
            "    return",
            "finally:",
            "    pass",
            "await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_infinite_while":
        protected_lines = [
            "while True:",
            "    pass",
            "await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "nonexact_state_guard":
        protected_lines = [
            "if blob:",
            "    return",
            "await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
        ]
    elif mutation == "effects_after_exhaustive_match":
        protected_lines = [
            "try:",
            "    match 0:",
            "        case _:",
            "            return",
            "    await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)",
            "    await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)",
            "finally:",
            "    pass",
        ]

    source_lines = [
        "from elspeth.web.coordination.contracts import SessionOperationKind",
        "from elspeth.web.coordination.lifecycle import SessionOperationLease",
        '@router.get("/{blob_id}/content")',
        "async def download_blob_content(session_id, blob_id, request, user):",
        *(f"    {line}" for line in pre_initialization),
        "    session_service = request.app.state.session_service",
        "    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)",
        *(f"    {line}" for line in pre_acquire),
        *(f"    {line}" for line in acquire),
        *(f"    {line}" for line in pre_try),
        "    try:",
        *(
            []
            if mutation
            in {
                "unawaited_owned_blob",
                "unreachable_owned_blob",
                "effects_after_unconditional_return",
                "effects_under_not_true",
                "effects_under_false_comparison",
                "effects_after_break",
                "effects_after_continue",
                "effects_after_if_true_return",
                "effects_under_multiply_false",
                "effects_under_empty_for",
                "effects_after_try_return",
                "effects_after_infinite_while",
                "effects_after_exhaustive_match",
            }
            else ["        await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)"]
        ),
        *(f"        {line}" for line in protected_lines),
        "    finally:",
        *(f"        {line}" for line in close_lines),
    ]
    source_lines.extend(f"    {line}" for line in tail)
    source = "\n".join(source_lines)
    if mutation == "unfenced_endpoint_decorator":
        source = source.replace(
            '@router.get("/{blob_id}/content")',
            '@unfenced_wrapper\n@router.get("/{blob_id}/content")',
        )
    elif mutation == "duplicate_same_name_endpoint":
        source = f"{source}\n{source}"
    elif mutation == "module_forged_lease":
        source = f"SessionOperationLease = ForgedLease\n{source}"
    elif mutation == "module_forged_owner_helper":
        source = f"_get_owned_blob = forged_owner_lookup\n{source}"
    elif mutation == "module_forged_verifier":
        source = f"_verify_session_and_get_blob_service = forged_verifier\n{source}"
    elif mutation == "async_function_forged_verifier":
        source = (
            "async def _verify_session_and_get_blob_service(session_id, user, request):\n"
            "    return request.app.state.blob_service\n"
            f"{source}"
        )
    elif mutation == "class_forged_lease":
        source = f"class SessionOperationLease:\n    acquire = ForgedLease.acquire\n{source}"
    elif mutation == "import_forged_verifier":
        source = f"from attacker import x as _verify_session_and_get_blob_service\n{source}"
    elif mutation == "imported_lease_alias_second_acquire":
        source = f"from elspeth.web.coordination.lifecycle import SessionOperationLease as LeaseAlias\n{source}"
    elif mutation in {
        "qualified_module_second_acquire",
        "qualified_getattr_second_acquire",
        "qualified_dynamic_getattr_second_acquire",
        "aliased_getattr_second_acquire",
    }:
        source = f"import elspeth.web.coordination.lifecycle as lifecycle\n{source}"
    elif mutation == "module_helper_second_acquire":
        source = (
            "async def acquire_extra():\n"
            "    return await SessionOperationLease.acquire(\n"
            "        attacker_authority,\n"
            "        session_id=attacker_session_id,\n"
            "        operation_kind=SessionOperationKind.BLOB_READ,\n"
            "        owner_instance_id=attacker_owner,\n"
            "        lease_seconds=30,\n"
            "    )\n"
            f"{source}"
        )
    elif mutation == "module_helper_captures_acquire":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "async def acquire_extra(authority):\n"
            "    acquire = lifecycle.SessionOperationLease.acquire\n"
            "    return await acquire(\n"
            "        authority,\n"
            "        session_id=attacker_session_id,\n"
            "        operation_kind=SessionOperationKind.BLOB_READ,\n"
            "        owner_instance_id=attacker_owner,\n"
            "        lease_seconds=30,\n"
            "    )\n"
            f"{source}"
        )
    elif mutation == "module_helper_escapes_authority_acquire":
        source = f"async def acquire_extra(authority):\n    return await run_sync_in_worker(authority.acquire)\n{source}"
    elif mutation == "module_helper_fully_aliased_getattr":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "async def acquire_extra(authority):\n"
            "    lookup = getattr\n"
            '    lease_class = lookup(lifecycle, "SessionOperationLease")\n'
            '    acquire = lookup(lease_class, "acquire")\n'
            "    return await acquire(\n"
            "        authority,\n"
            "        session_id=attacker_session_id,\n"
            "        operation_kind=SessionOperationKind.BLOB_READ,\n"
            "        owner_instance_id=attacker_owner,\n"
            "        lease_seconds=30,\n"
            "    )\n"
            f"{source}"
        )
    elif mutation == "module_helper_fully_aliased_authority_getattr":
        source = (
            "async def acquire_extra(authority):\n"
            "    lookup = getattr\n"
            '    acquire = lookup(authority, "acquire")\n'
            "    return await run_sync_in_worker(acquire)\n"
            f"{source}"
        )
    elif mutation == "module_helper_builtins_getattr_alias":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "from builtins import getattr as lookup\n"
            "async def acquire_extra(authority):\n"
            '    lease_class = lookup(lifecycle, "SessionOperationLease")\n'
            '    acquire = lookup(lease_class, "acquire")\n'
            "    return await run_sync_in_worker(acquire, authority)\n"
            f"{source}"
        )
    elif mutation == "module_helper_type_member_alias":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "async def acquire_extra(authority):\n"
            "    lookup = type(lifecycle).__getattribute__\n"
            '    lease_class = lookup(lifecycle, "SessionOperationLease")\n'
            "    acquire = type(lease_class).__getattribute__\n"
            '    member = acquire(lease_class, "acquire")\n'
            "    return await run_sync_in_worker(member, authority)\n"
            f"{source}"
        )
    elif mutation == "module_helper_vars_lookup":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "async def acquire_extra(authority):\n"
            "    namespace = vars(lifecycle)\n"
            '    lease_type = namespace["SessionOperation" + "Lease"]\n'
            '    acquirer = vars(lease_type)["acquire"]\n'
            "    return await acquirer(\n"
            "        authority,\n"
            "        session_id=attacker_session_id,\n"
            "        operation_kind=SessionOperationKind.BLOB_READ,\n"
            "        owner_instance_id=attacker_owner,\n"
            "        lease_seconds=30,\n"
            "    )\n"
            f"{source}"
        )
    elif mutation == "module_helper_dict_lookup":
        source = (
            "import elspeth.web.coordination.lifecycle as lifecycle\n"
            "async def acquire_extra(authority):\n"
            '    lease_type = lifecycle.__dict__["SessionOperationLease"]\n'
            '    acquirer = lease_type.__dict__["acquire"]\n'
            "    return await run_sync_in_worker(acquirer, authority)\n"
            f"{source}"
        )
    elif mutation == "module_helper_eval_lookup":
        source = (
            "async def acquire_extra(authority):\n"
            '    acquirer = eval("authority.acquire")\n'
            "    return await run_sync_in_worker(acquirer)\n"
            f"{source}"
        )
    elif mutation == "mutate_lease_acquire":
        source = f"SessionOperationLease.acquire = ForgedLease.acquire\n{source}"
    elif mutation == "mutate_verifier_code":
        source = f"_verify_session_and_get_blob_service.__code__ = forged_verifier.__code__\n{source}"
    elif mutation == "mutate_owner_helper_code":
        source = f"_get_owned_blob.__code__ = forged_owner_lookup.__code__\n{source}"
    elif mutation == "mutate_operation_kind_member":
        source = f"SessionOperationKind.BLOB_READ = forged_kind\n{source}"
    elif mutation == "mutated_quote_callable_object":
        source = f"from urllib.parse import quote\ndef forged_quote(value):\n    return value\n{source}"
    elif mutation == "cast_laundered_object_mutation":
        source = f"from typing import cast\n{source}"
    elif mutation == "endpoint_default_callback":
        source = source.replace(
            "async def download_blob_content(session_id, blob_id, request, user):",
            "async def download_blob_content(session_id, blob_id, request, user, trap=attacker_callback()):",
        )
    elif mutation == "module_top_level_callback":
        source = f"attacker_callback()\n{source}"
    elif mutation == "endpoint_default_implicit_comprehension":
        source = source.replace(
            "async def download_blob_content(session_id, blob_id, request, user):",
            "async def download_blob_content(session_id, blob_id, request, user, trap=[item for item in attacker_iterable]):",
        )
    elif mutation == "module_implicit_comprehension":
        source = f"trap = [item for item in attacker_iterable]\n{source}"
    elif mutation == "endpoint_default_subscription":
        source = source.replace(
            "async def download_blob_content(session_id, blob_id, request, user):",
            "async def download_blob_content(session_id, blob_id, request, user, trap=attacker_container[0]):",
        )
    elif mutation == "endpoint_default_binary_operator":
        source = source.replace(
            "async def download_blob_content(session_id, blob_id, request, user):",
            "async def download_blob_content(session_id, blob_id, request, user, trap=attacker_value + 1):",
        )
    elif mutation == "endpoint_annotation_subscription":
        source = source.replace(
            "async def download_blob_content(session_id, blob_id, request, user):",
            "async def download_blob_content(session_id: attacker_container[0], blob_id, request, user):",
        )
    elif mutation == "module_subscription_expression":
        source = f"trap = attacker_container[0]\n{source}"
    elif mutation == "module_binary_expression":
        source = f"trap = attacker_value + 1\n{source}"
    tree = ast.parse(source)
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    violations = _route_violations(tree, contract)
    assert violations, f"gate admitted adversarial route: {mutation}"
    if mutation in {
        "effects_after_unconditional_return",
        "effects_under_not_true",
        "effects_under_false_comparison",
        "effects_after_break",
        "effects_after_continue",
    }:
        assert sum("statically unreachable" in violation for violation in violations) == 2
    elif mutation == "aliased_second_acquire":
        assert any("must not escape or be mutated" in violation for violation in violations)
    elif mutation == "aliased_acquire_callable":
        assert any("reference must occur only as the canonical acquisition callable" in violation for violation in violations)
    elif mutation == "imported_lease_alias_second_acquire":
        assert any("forged import provenance" in violation for violation in violations)
    elif mutation in {
        "nested_second_acquire",
        "qualified_module_second_acquire",
        "qualified_getattr_second_acquire",
        "qualified_dynamic_getattr_second_acquire",
        "aliased_getattr_second_acquire",
    }:
        assert any("acquisition-like calls" in violation or "reference must occur only" in violation for violation in violations)
    elif mutation in {
        "module_helper_second_acquire",
        "module_helper_captures_acquire",
        "module_helper_escapes_authority_acquire",
    }:
        assert any("must be owned directly by a standalone blob endpoint" in violation for violation in violations)
    elif mutation in {
        "module_helper_fully_aliased_getattr",
        "module_helper_fully_aliased_authority_getattr",
        "module_helper_builtins_getattr_alias",
        "module_helper_type_member_alias",
        "module_helper_vars_lookup",
        "module_helper_dict_lookup",
        "module_helper_eval_lookup",
        "setattr_blob_service_effect",
        "setattr_session_authority",
    }:
        assert any("dynamic member lookup" in violation for violation in violations)
    elif mutation in {
        "direct_session_authority_mutation",
        "direct_blob_effect_mutation",
        "container_list_alias_mutation",
        "container_dict_alias_mutation",
        "container_tuple_unpack_alias_mutation",
        "named_expression_alias_mutation",
        "list_comprehension_alias_mutation",
        "dict_comprehension_alias_mutation",
        "nested_comprehension_alias_mutation",
    }:
        assert any("protected endpoint objects" in violation for violation in violations)
    elif mutation == "unfenced_endpoint_decorator":
        assert any("exact canonical route registration" in violation for violation in violations)
    elif mutation == "rebound_http_exception_callable":
        assert any("protected callable 'HTTPException'" in violation for violation in violations)
    elif mutation == "mutated_quote_callable_object":
        assert any("protected callable 'quote'" in violation for violation in violations)
    elif mutation == "cast_laundered_object_mutation":
        assert any("must not mutate attributes or items" in violation for violation in violations)
    elif mutation == "except_handler_callable_rebind":
        assert any("protected callable 'HTTPException' must not be structurally rebound" in violation for violation in violations)
    elif mutation == "endpoint_default_callback":
        assert any("defaults and annotations contain unapproved executable calls" in violation for violation in violations)
    elif mutation == "module_top_level_callback":
        assert any("must not execute calls at module top level" in violation for violation in violations)
    elif mutation in {"with_implicit_callback", "for_implicit_callback"}:
        assert any("implicit protocol execution" in violation for violation in violations)
    elif mutation in {"endpoint_default_implicit_comprehension", "module_implicit_comprehension"}:
        assert any("implicit-execution comprehension" in violation for violation in violations)
    elif mutation == "await_noncall_object":
        assert any("must not await non-call" in violation for violation in violations)
    elif mutation == "yield_before_acquire":
        assert any("implicit protocol execution" in violation for violation in violations)
    elif mutation in {
        "endpoint_default_subscription",
        "endpoint_default_binary_operator",
        "endpoint_annotation_subscription",
    }:
        assert any("endpoint definition surface" in violation for violation in violations)
    elif mutation in {"module_subscription_expression", "module_binary_expression"}:
        assert any("unapproved executable module-scope statements" in violation for violation in violations)
    elif mutation in {
        "pre_acquire_assert_truthiness",
        "pre_acquire_if_truthiness",
        "pre_acquire_subscription",
        "pre_acquire_binary_operator",
    }:
        assert any("pre-acquisition statements must match the exact approved preamble" in violation for violation in violations)
    elif mutation in {
        "post_effect_subscription",
        "post_effect_assert_truthiness",
        "post_effect_binary_operator",
        "post_effect_if_truthiness",
        "post_effect_raise",
        "post_effect_nested_function",
        "post_effect_nested_class",
    }:
        assert any("post-acquisition statements must match the exact approved fenced tail" in violation for violation in violations)
    elif mutation in {"unknown_sync_before_acquire", "unknown_sync_after_close"}:
        assert any("exact callable allowlist" in violation for violation in violations)
    elif mutation == "nonexact_state_guard":
        assert any("exact optional state guard" in violation for violation in violations)
    elif mutation in {"rebind_user", "rebind_session_id", "rebind_request", "rebind_blob_id"}:
        assert any("trusted endpoint input" in violation and "must not be rebound" in violation for violation in violations)
    elif mutation in {
        "import_rebind_user",
        "from_import_rebind_session_id",
        "function_rebind_blob_id",
        "class_rebind_request",
    }:
        assert any("trusted endpoint input" in violation and "must not be defined or imported" in violation for violation in violations)
    elif mutation in {"class_rebind_blob_service", "import_rebind_blob_service", "class_rebind_session_service"}:
        assert any("must not be defined, imported, or structurally rebound" in violation for violation in violations)
    elif mutation in {"match_capture_lease", "match_star_capture_lease"}:
        assert any("must not be structurally bound" in violation for violation in violations)
    elif mutation in {"match_mapping_capture_user", "match_class_capture_user"}:
        assert any("trusted endpoint input 'user' must not be structurally bound" in violation for violation in violations)
    elif mutation in {
        "mutate_lease_acquire",
        "mutate_verifier_code",
        "mutate_owner_helper_code",
        "mutate_operation_kind_member",
    }:
        assert any("must not escape or be mutated" in violation for violation in violations)
    elif mutation == "async_function_forged_verifier":
        assert any("does not match its canonical definition" in violation for violation in violations)
    elif mutation == "class_forged_lease":
        assert any("must not be locally defined" in violation for violation in violations)
    elif mutation == "import_forged_verifier":
        assert any("forged import provenance" in violation for violation in violations)


def _blob_read_endpoint_source(*, annotated: bool = False, injected_services: bool = False) -> str:
    parameters = "session_id, blob_id, session_service, blob_service" if injected_services else "session_id, blob_id, request, user"
    initializers = (
        ""
        if injected_services
        else (
            "    session_service = request.app.state.session_service\n"
            "    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)\n"
        )
    )
    annotation = ": SessionOperationLease" if annotated else ""
    return f"""{_TRUSTED_IMPORT_SOURCE}
@router.get("/{{blob_id}}/content")
async def download_blob_content({parameters}):
{initializers}    lease{annotation} = await SessionOperationLease.acquire(
        session_service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id=session_service.session_operation_owner_instance_id,
        lease_seconds=session_service.session_operation_lease_seconds,
    )
    try:
        await _get_owned_blob(blob_service, session_id, blob_id, session_operation_context=lease.context)
        await blob_service.read_blob_content(blob_id, session_operation_context=lease.context)
    finally:
        await lease.close()
"""


@pytest.mark.parametrize("contract_name", ("create_blob_inline", "download_blob_content"))
def test_route_gate_rejects_injected_service_bindings(contract_name: str) -> None:
    contract = next(item for item in _ENDPOINTS if item.name == contract_name)
    if contract_name == "create_blob_inline":
        valid_source = _inline_create_endpoint_source(unreachable=False)
        injected_source = valid_source.replace(
            "async def create_blob_inline(session_id, body, request, user):\n",
            "async def create_blob_inline(session_id, body, session_service, blob_service):\n",
        ).replace(
            "    session_service = request.app.state.session_service\n"
            "    blob_service = await _verify_session_and_get_blob_service(session_id, user, request)\n",
            "",
        )
    else:
        valid_source = _blob_read_endpoint_source()
        injected_source = _blob_read_endpoint_source(injected_services=True)
    assert _route_violations(ast.parse(valid_source), contract) == []
    violations = _route_violations(ast.parse(injected_source), contract)
    assert sum("must use exact approved local initialization" in violation for violation in violations) == 2


def test_route_gate_requires_canonical_trusted_imports() -> None:
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    valid_source = _blob_read_endpoint_source()
    assert _route_violations(ast.parse(valid_source), contract) == []
    source_without_imports = valid_source.replace(_TRUSTED_IMPORT_SOURCE, "")
    violations = _route_violations(ast.parse(source_without_imports), contract)
    assert sum("requires exactly one canonical top-level import; found 0" in violation for violation in violations) == 2


def test_route_gate_accepts_annotated_direct_binding() -> None:
    contract = next(item for item in _ENDPOINTS if item.name == "download_blob_content")
    assert _route_violations(ast.parse(_blob_read_endpoint_source(annotated=True)), contract) == []


def test_blob_read_vocabulary_is_present_in_epoch_44_without_protocol_bump() -> None:
    assert SessionOperationKind.BLOB_READ.value == "blob_read"
    assert SESSION_SCHEMA_EPOCH == 44
    assert WEB_COORDINATION_PROTOCOL_VERSION == 1
    kind_check = next(
        constraint for constraint in session_operation_fences_table.constraints if constraint.name == "ck_session_operation_fences_kind"
    )
    assert "'blob_read'" in str(kind_check.sqltext)


@pytest.mark.asyncio
async def test_stale_blob_read_context_changes_neither_row_nor_canonical_bytes(tmp_path: Path) -> None:
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    authority = SQLiteLocalSessionOperationAuthority(engine)
    created = authority.create_session_with_initial_fence(
        user_id="alice",
        title="blob fencing",
        auth_provider_type="local",
        owner_instance_id="owner-a",
        lease_seconds=30,
    )
    content = b"canonical blob bytes"
    blob_id = uuid4()
    storage = tmp_path / "blobs" / str(created.id) / f"{blob_id}_evidence.txt"
    storage.parent.mkdir(parents=True)
    storage.write_bytes(content)
    with engine.begin() as conn:
        conn.execute(
            blobs_table.insert().values(
                id=str(blob_id),
                session_id=str(created.id),
                filename="evidence.txt",
                mime_type="text/plain",
                size_bytes=len(content),
                content_hash=hashlib.sha256(content).hexdigest(),
                storage_path=str(storage),
                created_at=datetime.now(UTC),
                created_by="user",
                status="ready",
                creation_modality="verbatim",
            )
        )
    stale = authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="owner-a",
        lease_seconds=30,
    )
    authority.release(stale)
    authority.acquire(
        session_id=created.id,
        operation_kind=SessionOperationKind.BLOB_READ,
        owner_instance_id="owner-b",
        lease_seconds=30,
    )
    with engine.connect() as conn:
        before = dict(conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).mappings().one())

    service = BlobServiceImpl(engine, tmp_path)
    with pytest.raises(SessionOperationFenceLost):
        await service.read_blob_content(blob_id, session_operation_context=stale)  # type: ignore[call-arg]

    with engine.connect() as conn:
        after = dict(conn.execute(select(blobs_table).where(blobs_table.c.id == str(blob_id))).mappings().one())
    assert after == before
    assert storage.read_bytes() == content
    engine.dispose()
