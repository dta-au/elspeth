"""Tests for BaseTransform / BaseSink / BaseSource default semantic and assistance hooks."""

from __future__ import annotations

from elspeth.contracts import Determinism
from elspeth.contracts.plugin_semantics import (
    InputSemanticRequirements,
    OutputSemanticDeclaration,
)
from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform


class _StubTransform(BaseTransform):
    name = "stub"
    determinism = Determinism.DETERMINISTIC

    def process(self, row, ctx):  # pragma: no cover — not exercised
        raise NotImplementedError


class _StubSink(BaseSink):
    name = "stub-sink"
    determinism = Determinism.IO_WRITE

    def write(self, rows, ctx):  # pragma: no cover — not exercised
        raise NotImplementedError

    def flush(self) -> None:  # pragma: no cover — not exercised
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover — not exercised
        raise NotImplementedError


class _StubSource(BaseSource):
    name = "stub-source"
    determinism = Determinism.DETERMINISTIC

    def load(self, ctx):  # pragma: no cover — not exercised
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover — not exercised
        raise NotImplementedError


def test_default_output_semantics_is_empty():
    instance = _StubTransform.__new__(_StubTransform)
    decl = instance.output_semantics()
    assert isinstance(decl, OutputSemanticDeclaration)
    assert decl.fields == ()


def test_default_input_semantic_requirements_is_empty():
    instance = _StubTransform.__new__(_StubTransform)
    reqs = instance.input_semantic_requirements()
    assert isinstance(reqs, InputSemanticRequirements)
    assert reqs.fields == ()


def test_default_get_agent_assistance_returns_none():
    result = _StubTransform.get_agent_assistance(issue_code=None)
    assert result is None
    result_with_code = _StubTransform.get_agent_assistance(issue_code="any.code")
    assert result_with_code is None


def test_default_get_post_call_hints_returns_empty_tuple():
    result = _StubTransform.get_post_call_hints(tool_name="upsert_node", config_snapshot={})
    assert result == ()


def test_basesink_default_get_agent_assistance_returns_none():
    """Confirm BaseSink exposes the assistance hook (dual-use with issue_code)."""
    assert BaseSink.get_agent_assistance(issue_code=None) is None
    assert BaseSink.get_agent_assistance(issue_code="any.code") is None


def test_basesink_default_get_post_call_hints_returns_empty_tuple():
    """Confirm BaseSink exposes the postscript hook with the two-param contract."""
    result = BaseSink.get_post_call_hints(tool_name="set_output", config_snapshot={})
    assert result == ()


def test_basesource_default_get_agent_assistance_returns_none():
    """Confirm BaseSource exposes the assistance hook (dual-use with issue_code)."""
    assert BaseSource.get_agent_assistance(issue_code=None) is None
    assert BaseSource.get_agent_assistance(issue_code="any.code") is None


def test_basesource_default_get_post_call_hints_returns_empty_tuple():
    """Confirm BaseSource exposes the postscript hook with the two-param contract."""
    result = BaseSource.get_post_call_hints(tool_name="set_source", config_snapshot={})
    assert result == ()


def test_basesink_declares_the_input_semantic_requirements_hook() -> None:
    """The hook must live on BaseSink itself, not be duck-typed at the call site.

    ``validate_semantic_contracts`` reaches a sink through plain attribute
    access on a declared base method. Bridging the gap with hasattr/getattr
    would be structural typing, which ADR-032 rules out as a control — and
    before this hook existed, every sink requirement was unreadable.
    """
    assert "input_semantic_requirements" in BaseSink.__dict__, (
        "BaseSink must DECLARE the hook; inheriting nothing is what made a sink requirement unreadable"
    )
    reqs = _StubSink.__new__(_StubSink).input_semantic_requirements()
    assert isinstance(reqs, InputSemanticRequirements)
    assert reqs.fields == (), "default is no requirements — a sink opts in by overriding"


def test_basesource_declares_the_output_semantics_hook() -> None:
    """Same for the producer side: LLMSource's declaration was dead code without it."""
    assert "output_semantics" in BaseSource.__dict__
    decl = _StubSource.__new__(_StubSource).output_semantics()
    assert isinstance(decl, OutputSemanticDeclaration)
    assert decl.fields == ()


def test_semantic_hooks_stay_on_the_side_that_can_honestly_declare_them() -> None:
    """A sink consumes and never produces; a source produces and never consumes.

    Adding the mirror hook to either would invite a declaration nothing reads.
    """
    assert not hasattr(BaseSink, "output_semantics"), "nothing downstream of a sink could read its facts"
    assert not hasattr(BaseSource, "input_semantic_requirements"), "a source is the root; it consumes no upstream field"


def test_basesink_probe_config_refuses_rather_than_returning_a_fake_config() -> None:
    """A declarer that cannot be probed must fail loudly, not be skipped.

    ``tests/invariants/test_semantic_requirements_are_satisfiable`` asserts on
    a declarer it cannot instantiate; a silent default would let a requirement
    go unchecked by its own gate.
    """
    import pytest

    with pytest.raises(NotImplementedError, match="probe_config"):
        BaseSink.probe_config()
