"""Regression coverage for explicit attribute contracts in Sessions/Composer."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest
from starlette.requests import Request

from elspeth.web.composer import discovery_cache, llm_response_parsing
from elspeth.web.composer.implicit_decisions import _is_auto_wired_control
from elspeth.web.composer.state import NodeSpec
from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
from elspeth.web.sessions.routes._helpers import _failure_log_request_id, _is_client_disconnect_cancel


@dataclass(frozen=True, order=True)
class _AttributeProbe:
    file_path: str
    symbol: str
    operation: str
    field_name: str | None


class _ProbeCollector(ast.NodeVisitor):
    def __init__(self, *, file_path: str) -> None:
        self._file_path = file_path
        self._symbols: list[str] = []
        self.probes: list[_AttributeProbe] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "__getattr__":
            self.probes.append(_AttributeProbe(self._file_path, self._symbol, "__getattr__", None))
        self._symbols.append(node.name)
        self.generic_visit(node)
        self._symbols.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        operation: str | None = None
        if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
            operation = node.func.id
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "getattr_static":
            operation = "getattr_static"
        if operation is not None:
            field_name = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else None
            self.probes.append(_AttributeProbe(self._file_path, self._symbol, operation, field_name))
        self.generic_visit(node)

    @property
    def _symbol(self) -> str:
        return ".".join(self._symbols) if self._symbols else "<module>"


def _attribute_probes(repo_root: Path) -> set[_AttributeProbe]:
    probes: set[_AttributeProbe] = set()
    for relative_root in ("src/elspeth/web/sessions", "src/elspeth/web/composer"):
        for path in sorted((repo_root / relative_root).rglob("*.py")):
            relative_path = path.relative_to(repo_root).as_posix()
            collector = _ProbeCollector(file_path=relative_path)
            collector.visit(ast.parse(path.read_text(), filename=relative_path))
            probes.update(collector.probes)
    return probes


def test_sessions_and_composer_use_explicit_attribute_contracts() -> None:
    """Only the two ADR-032 LiteLLM admission boundaries may use ``getattr``."""

    repo_root = Path(__file__).resolve().parents[3]
    expected = {
        _AttributeProbe(
            "src/elspeth/web/sessions/_auto_title.py",
            "_admit_auto_title_completion",
            "getattr",
            "choices",
        ),
        _AttributeProbe(
            "src/elspeth/web/sessions/_auto_title.py",
            "_admit_auto_title_completion",
            "getattr",
            "message",
        ),
        _AttributeProbe(
            "src/elspeth/web/sessions/_auto_title.py",
            "_admit_auto_title_completion",
            "getattr",
            "content",
        ),
        _AttributeProbe(
            "src/elspeth/web/sessions/_auto_title.py",
            "_admit_auto_title_completion",
            "getattr",
            "finish_reason",
        ),
        _AttributeProbe(
            "src/elspeth/web/composer/guided/chat_solver.py",
            "_admit_deferred_intent_repair_thread",
            "getattr",
            "content",
        ),
        _AttributeProbe(
            "src/elspeth/web/composer/guided/chat_solver.py",
            "_admit_deferred_intent_repair_thread",
            "getattr",
            "id",
        ),
        _AttributeProbe(
            "src/elspeth/web/composer/guided/chat_solver.py",
            "_admit_deferred_intent_repair_thread",
            "getattr",
            "function",
        ),
        _AttributeProbe(
            "src/elspeth/web/composer/guided/chat_solver.py",
            "_admit_deferred_intent_repair_thread",
            "getattr",
            "name",
        ),
        _AttributeProbe(
            "src/elspeth/web/composer/guided/chat_solver.py",
            "_admit_deferred_intent_repair_thread",
            "getattr",
            "arguments",
        ),
        _AttributeProbe("src/elspeth/web/composer/tool_batch.py", "_admit_tool_batch", "getattr", "id"),
        _AttributeProbe("src/elspeth/web/composer/tool_batch.py", "_admit_tool_batch", "getattr", "function"),
        _AttributeProbe("src/elspeth/web/composer/tool_batch.py", "_admit_tool_batch", "getattr", "name"),
        _AttributeProbe("src/elspeth/web/composer/tool_batch.py", "_admit_tool_batch", "getattr", "arguments"),
    }

    assert _attribute_probes(repo_root) == expected


def test_provider_artifact_normalizer_does_not_hide_internal_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a provider-shape rejection may become the unavailable sentinel."""

    def fail_normalizer(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("internal normalizer defect")

    monkeypatch.setattr(llm_response_parsing, "_normalize_provider_artifact", fail_normalizer)

    with pytest.raises(RuntimeError, match="internal normalizer defect"):
        llm_response_parsing._json_safe_provider_artifact({"safe": "value"})


def test_pydantic_json_fallback_rejects_masqueraders_without_invoking_them() -> None:
    """The JSON fallback accepts nominal Pydantic models, not a method name."""

    class Masquerader:
        called = False

        def model_dump(self) -> dict[str, object]:
            self.called = True
            return {"forged": True}

    value = Masquerader()
    with pytest.raises(TypeError, match="Masquerader is not JSON serializable"):
        discovery_cache.pydantic_default(value)
    assert value.called is False


def test_auto_wired_disclosure_rejects_malformed_owned_requirements() -> None:
    """A matching user_term cannot masquerade as a validated disclosure row."""

    node = NodeSpec(
        id="shield",
        node_type="transform",
        plugin="aws_bedrock_prompt_shield",
        input="rows",
        on_success="shielded",
        on_error="discard",
        options={"interpretation_requirements": [{"user_term": REQUIRED_CONTROL_AUTO_WIRED_USER_TERM}]},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )

    with pytest.raises(KeyError, match="id"):
        _is_auto_wired_control(node)


def test_failure_log_request_id_parses_only_the_middleware_owned_state_dict() -> None:
    assert _failure_log_request_id(Request({"type": "http"})) is None
    assert _failure_log_request_id(Request({"type": "http", "state": MappingProxyType({"request_id": "forged"})})) is None
    assert _failure_log_request_id(Request({"type": "http", "state": {"request_id": 7}})) is None
    assert _failure_log_request_id(Request({"type": "http", "state": {"request_id": "request-1"}})) == "request-1"


def test_disconnect_marker_requires_the_private_token_by_identity() -> None:
    class MasqueradingMessage:
        def __eq__(self, _other: object) -> bool:
            return True

    assert _is_client_disconnect_cancel(asyncio.CancelledError(MasqueradingMessage())) is False
