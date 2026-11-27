"""Long-lived web owner loops review worker admission alongside database retries."""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.tree_gate import iter_gate_files

_ROOT = Path(__file__).resolve().parents[3]
_WEB = _ROOT / "src" / "elspeth" / "web"
_DB_RETRY_NAMES = frozenset({"OperationalError", "_LOSS_WATCHER_TRANSIENT_DB_ERRORS"})
_ADMISSION_NAME = "WORKER_ADMISSION_REFUSED"

_REVIEWED: dict[tuple[str, str], str] = {
    (
        "src/elspeth/web/sessions/routes/_helpers.py",
        "composer_request_lifecycle.renew",
    ): "Per-request renewal cancels only its own operation; this is not a process owner loop.",
}

# Reviewed per-operation paths that hold no async while retry handler. Each entry
# must name a function that exists and scans with no hit (see the test below).
_OUT_OF_SCOPE: dict[tuple[str, str], str] = {
    ("src/elspeth/web/sessions/composer_async_worker.py", "ComposerAsyncWorker._job"): "One operation with separate custody handling.",
    ("src/elspeth/web/execution/recovery.py", "RunRecoveryCoordinator._recover_candidate"): "One recovery candidate.",
    ("src/elspeth/web/execution/service.py", "ExecutionServiceImpl._settle_admission_refusal"): "One execution lease settlement.",
    ("src/elspeth/web/sessions/composer_async_worker.py", "ComposerAsyncWorker.reap_once"): "Custody wrapper outside the long-lived loop.",
}

_REQUIRED = frozenset(
    {
        ("src/elspeth/web/execution/service.py", "ExecutionServiceImpl._signal_shutdown_on_operation_loss"),
        ("src/elspeth/web/app.py", "_periodic_orphan_cleanup"),
        ("src/elspeth/web/sessions/composer_async_worker.py", "ComposerAsyncWorker._loop"),
        ("src/elspeth/web/coordination/membership_lifecycle.py", "RegisteredWebInstanceMembership._heartbeat_loop"),
    }
)


def _names(expression: ast.expr | None) -> frozenset[str]:
    if expression is None:
        return frozenset()
    return frozenset(node.id for node in ast.walk(expression) if isinstance(node, ast.Name))


class _LoopRetryScanner(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.async_owner: str | None = None
        self.while_depth = 0
        self.hits: list[tuple[str, str, int, bool]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, *, is_async: bool) -> None:
        prior_owner, prior_depth = self.async_owner, self.while_depth
        self.scope.append(node.name)
        self.async_owner = ".".join(self.scope) if is_async else None
        self.while_depth = 0
        self.generic_visit(node)
        self.scope.pop()
        self.async_owner, self.while_depth = prior_owner, prior_depth

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node, is_async=True)

    def visit_While(self, node: ast.While) -> None:
        self.while_depth += 1
        self.generic_visit(node)
        self.while_depth -= 1

    def visit_Try(self, node: ast.Try) -> None:
        if self.async_owner is not None and self.while_depth:
            guarded = any(_ADMISSION_NAME in _names(handler.type) for handler in node.handlers)
            for handler in node.handlers:
                if _names(handler.type) & _DB_RETRY_NAMES:
                    self.hits.append((self.path, self.async_owner, handler.lineno, guarded))
        self.generic_visit(node)

    visit_TryStar = visit_Try


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.names: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.names.add(".".join(self.scope))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def _function_names(path: str, source: str) -> frozenset[str]:
    collector = _FunctionCollector()
    collector.visit(ast.parse(source, filename=path))
    return frozenset(collector.names)


def _scan(path: str, source: str) -> list[tuple[str, str, int, bool]]:
    scanner = _LoopRetryScanner(path)
    scanner.visit(ast.parse(source, filename=path))
    return scanner.hits


def _current_hits() -> list[tuple[str, str, int, bool]]:
    hits: list[tuple[str, str, int, bool]] = []
    for path in iter_gate_files(_WEB):
        relative = str(path.relative_to(_ROOT))
        hits.extend(_scan(relative, path.read_text(encoding="utf-8")))
    return sorted(hits)


def _gate_errors(hits: list[tuple[str, str, int, bool]]) -> list[str]:
    keys = {(path, function) for path, function, _line, _guarded in hits}
    unguarded = {(path, function) for path, function, _line, guarded in hits if not guarded}
    errors: list[str] = []
    if not keys >= _REQUIRED:
        errors.append(f"missing required owner loops: {sorted(_REQUIRED - keys)}")
    if not keys >= set(_REVIEWED):
        errors.append(f"reviewed handlers without hits: {sorted(set(_REVIEWED) - keys)}")
    if unguarded != set(_REVIEWED):
        errors.append(f"unguarded handlers: {sorted(unguarded - set(_REVIEWED))}")
    if not all(reason.strip() for reason in _REVIEWED.values()):
        errors.append("reviewed handler lacks reason")
    return errors


def test_web_owner_loops_review_admission_with_database_retries() -> None:
    assert not _gate_errors(_current_hits())


def test_out_of_scope_paths_exist_and_hold_no_loop_retry_handler() -> None:
    keys = {(path, function) for path, function, _line, _guarded in _current_hits()}
    for (path, function), reason in _OUT_OF_SCOPE.items():
        assert reason.strip(), f"{path}:{function} lacks a reason"
        source = (_ROOT / path).read_text(encoding="utf-8")
        assert function in _function_names(path, source), f"{path}:{function} no longer exists"
        assert (path, function) not in keys, f"{path}:{function} now holds an async while retry handler"


def test_scanner_detects_missing_same_try_admission_handler() -> None:
    protected = "async def owner():\n    while True:\n        try:\n            await poll()\n        except (OperationalError, WORKER_ADMISSION_REFUSED):\n            continue\n"
    missing = protected.replace("(OperationalError, WORKER_ADMISSION_REFUSED)", "OperationalError")
    assert _scan("owner.py", protected) == [("owner.py", "owner", 5, True)]
    assert _scan("owner.py", missing) == [("owner.py", "owner", 5, False)]
    assert _scan("owner.py", "async def owner():\n    try:\n        await poll()\n    except OperationalError:\n        pass\n") == []


def _reject_mutant(path: str, function: str, mutant_source: str) -> None:
    live = _current_hits()
    assert not _gate_errors(live), "the live tree must pass the gate before a mutant is scored"
    hits = [hit for hit in live if (hit[0], hit[1]) != (path, function)]
    hits.extend(hit for hit in _scan(path, mutant_source) if hit[1] == function)
    errors = _gate_errors(hits)
    assert any(function in error for error in errors), f"gate accepted mutant in {function}: {errors}"


def test_watcher_separate_inner_try_mutant_is_rejected() -> None:
    path = "src/elspeth/web/execution/service.py"
    source = (
        "class ExecutionServiceImpl:\n"
        "    async def _signal_shutdown_on_operation_loss(self):\n"
        "        while True:\n"
        "            try:\n"
        "                await poll()\n"
        "            except _LOSS_WATCHER_TRANSIENT_DB_ERRORS:\n"
        "                continue\n"
        "            try:\n"
        "                await admission()\n"
        "            except WORKER_ADMISSION_REFUSED:\n"
        "                continue\n"
    )
    _reject_mutant(path, "ExecutionServiceImpl._signal_shutdown_on_operation_loss", source)


def test_heartbeat_bare_name_mutant_is_rejected() -> None:
    path = "src/elspeth/web/coordination/membership_lifecycle.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    original = "except WORKER_ADMISSION_REFUSED as exc:"
    assert source.count(original) == 1
    _reject_mutant(path, "RegisteredWebInstanceMembership._heartbeat_loop", source.replace(original, "except ValueError as exc:"))


def test_sweep_tuple_mutant_is_rejected() -> None:
    path = "src/elspeth/web/app.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    original = "except (OperationalError, WORKER_ADMISSION_REFUSED, ExceptionGroup) as cleanup_exc:"
    assert source.count(original) == 1
    _reject_mutant(path, "_periodic_orphan_cleanup", source.replace(original, "except (OperationalError, ExceptionGroup) as cleanup_exc:"))


def test_composer_tuple_mutant_is_rejected() -> None:
    path = "src/elspeth/web/sessions/composer_async_worker.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    original = "except (OperationalError, WORKER_ADMISSION_REFUSED) as exc:"
    assert source.count(original) == 1
    _reject_mutant(path, "ComposerAsyncWorker._loop", source.replace(original, "except OperationalError as exc:"))


def test_watcher_handler_mutant_is_rejected() -> None:
    path = "src/elspeth/web/execution/service.py"
    source = (_ROOT / path).read_text(encoding="utf-8")
    original = "except WORKER_ADMISSION_REFUSED as exc:"
    assert source.count(original) == 1
    _reject_mutant(path, "ExecutionServiceImpl._signal_shutdown_on_operation_loss", source.replace(original, "except ValueError as exc:"))


def test_outer_try_guard_mutant_is_rejected() -> None:
    path = "src/elspeth/web/sessions/composer_async_worker.py"
    source = (
        "class ComposerAsyncWorker:\n"
        "    async def _loop(self):\n"
        "        while True:\n"
        "            try:\n"
        "                try:\n"
        "                    await poll()\n"
        "                except OperationalError:\n"
        "                    continue\n"
        "            except WORKER_ADMISSION_REFUSED:\n"
        "                continue\n"
    )
    _reject_mutant(path, "ComposerAsyncWorker._loop", source)
