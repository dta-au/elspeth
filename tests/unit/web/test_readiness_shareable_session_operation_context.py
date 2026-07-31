"""Contract tests for caller-owned blob-read authority propagation."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.audit_readiness.service import ReadinessService
from elspeth.web.shareable_reviews.service import ShareableReviewService


def _async_function(path: Path, name: str) -> ast.AsyncFunctionDef:
    module = ast.parse(path.read_text(encoding="utf-8"))
    matches = [node for node in ast.walk(module) if isinstance(node, ast.AsyncFunctionDef) and node.name == name]
    assert len(matches) == 1
    return matches[0]


def _assert_required_exact_context(method: object) -> None:
    signature = inspect.signature(method)
    parameter = signature.parameters["session_operation_context"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert get_type_hints(method)["session_operation_context"] is SessionOperationContext


def test_services_require_caller_owned_session_operation_context() -> None:
    _assert_required_exact_context(ReadinessService.compute_snapshot)
    _assert_required_exact_context(ShareableReviewService.mark_ready_for_review)


def test_readiness_forwards_its_context_to_validation() -> None:
    node = _async_function(
        Path("src/elspeth/web/audit_readiness/service.py"),
        "compute_snapshot",
    )
    calls = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "validate_state"
    ]
    assert len(calls) == 1
    forwarded = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "session_operation_context")
    assert isinstance(forwarded, ast.Name)
    assert forwarded.id == "session_operation_context"


def test_shareable_reuses_one_context_for_direct_and_nested_validation() -> None:
    node = _async_function(
        Path("src/elspeth/web/shareable_reviews/service.py"),
        "mark_ready_for_review",
    )
    calls = [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr in {"validate_state", "compute_snapshot"}
    ]
    assert len(calls) == 2
    for call in calls:
        forwarded = next(keyword.value for keyword in call.keywords if keyword.arg == "session_operation_context")
        assert isinstance(forwarded, ast.Name)
        assert forwarded.id == "session_operation_context"


def test_http_entrypoints_own_one_blob_read_lease() -> None:
    targets = (
        (Path("src/elspeth/web/audit_readiness/routes.py"), "snapshot"),
        (Path("src/elspeth/web/shareable_reviews/routes.py"), "mark_ready_for_review"),
    )
    for path, function_name in targets:
        node = _async_function(path, function_name)
        acquires = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "acquire"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "SessionOperationLease"
        ]
        closes = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "close"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "lease"
        ]
        assert len(acquires) == 1
        assert len(closes) == 1
        operation_kind = next(keyword.value for keyword in acquires[0].keywords if keyword.arg == "operation_kind")
        assert ast.unparse(operation_kind) == "SessionOperationKind.BLOB_READ"
