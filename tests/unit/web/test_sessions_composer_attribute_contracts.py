"""Regression coverage for explicit attribute contracts in Sessions/Composer."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer import discovery_cache, llm_response_parsing
from elspeth.web.composer.implicit_decisions import _is_auto_wired_control
from elspeth.web.composer.state import NodeSpec
from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
from elspeth.web.required_work import (
    ComposerFailureReceipt,
    OwnedCompletionWitness,
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkSource,
    make_required_work_key,
    reduce_composer_failures,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED
from tests.helpers.tree_gate import iter_gate_sources


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
        for parsed in iter_gate_sources(repo_root / relative_root):
            relative_path = parsed.path.relative_to(repo_root).as_posix()
            collector = _ProbeCollector(file_path=relative_path)
            collector.visit(parsed.tree)
            probes.update(collector.probes)
    return probes


def test_sessions_and_composer_use_explicit_attribute_contracts() -> None:
    """Only the ADR-032 LiteLLM admission boundaries may use ``getattr``."""

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
        _AttributeProbe("src/elspeth/web/composer/provider_gateway.py", "_capture_composer_llm_completion_fields", "getattr", "choices"),
        _AttributeProbe("src/elspeth/web/composer/provider_gateway.py", "_capture_composer_llm_completion_fields", "getattr", "message"),
        _AttributeProbe("src/elspeth/web/composer/provider_gateway.py", "_capture_composer_llm_completion_fields", "getattr", "content"),
        _AttributeProbe("src/elspeth/web/composer/provider_gateway.py", "_capture_composer_llm_completion_fields", "getattr", "tool_calls"),
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


def test_required_work_cancel_marker_requires_the_owned_nominal_type() -> None:
    class MasqueradingMessage:
        def __eq__(self, _other: object) -> bool:
            return True

    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "private-token", 1), SessionOperationKind.COMPOSE)
    authority = RequiredWorkAuthority(RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4()))
    masquerader = asyncio.CancelledError(MasqueradingMessage())
    actual = asyncio.CancelledError(COMPOSER_CANCEL_REQUESTED)

    def receipt(error: asyncio.CancelledError, *, recurrence_ordinal: int) -> ComposerFailureReceipt:
        key = make_required_work_key(authority, RequiredWorkSource.LOCAL_CANCELLATION_SIGNAL, recurrence_ordinal=recurrence_ordinal)
        return ComposerFailureReceipt(key, error, required_failure_leaves(error), OwnedCompletionWitness(key))

    fake_first = receipt(masquerader, recurrence_ordinal=0)
    actual_second = receipt(actual, recurrence_ordinal=1)
    reduced = reduce_composer_failures((fake_first, actual_second))
    assert reduced.winner is actual_second and reduced.winner.original_root is actual
    assert reduced.category_rank == 60
    assert reduced.original_roots == (masquerader, actual)
