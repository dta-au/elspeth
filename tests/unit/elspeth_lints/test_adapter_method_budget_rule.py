"""Tests for the contract_invariants.adapter_method_budget rule.

Port of scripts/cicd/enforce_adapter_budget.py (elspeth-3575a7f15d): the
ratchet must fail on growth AND on slack, and the AST port must fail closed
where the legacy inspect-based script crashed (missing file/class, syntax
error).
"""

from __future__ import annotations

import ast
from pathlib import Path

from elspeth_lints.core.protocols import RuleContext
from elspeth_lints.rules.contract_invariants.adapter_method_budget.rule import (
    ADAPTER_RELATIVE_PATH,
    RATCHET,
    RULE,
)

_EMPTY_TREE = ast.Module(body=[], type_ignores=[])
_DISPLAY_PATH = "src/elspeth/core/landscape/plugin_audit_writer.py"


def _write_adapter(root: Path, source: str) -> None:
    adapter_file = root / ADAPTER_RELATIVE_PATH
    adapter_file.parent.mkdir(parents=True, exist_ok=True)
    adapter_file.write_text(source, encoding="utf-8")


def _adapter_source(public_method_count: int) -> str:
    methods = "\n".join(f"    def method_{index:02d}(self) -> None:\n        return None\n" for index in range(public_method_count))
    return f"class PluginAuditWriterAdapter:\n    def __init__(self) -> None:\n        pass\n\n{methods}"


def _analyze(root: Path) -> list:
    return list(RULE.analyze(_EMPTY_TREE, root, RuleContext(root=root)))


def test_exact_ratchet_is_clean(tmp_path: Path) -> None:
    _write_adapter(tmp_path, _adapter_source(RATCHET))

    assert _analyze(tmp_path) == []


def test_growth_fails_and_lists_methods(tmp_path: Path) -> None:
    _write_adapter(tmp_path, _adapter_source(RATCHET + 1))

    findings = _analyze(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.file_path == _DISPLAY_PATH
    assert finding.fingerprint == f"budget-growth:{RATCHET + 1}"
    assert f"has {RATCHET + 1} public methods (ratchet: {RATCHET})" in finding.message
    assert "method_00" in finding.message
    assert finding.line == 1
    assert finding.symbol_context == ("PluginAuditWriterAdapter",)


def test_slack_fails_with_lower_the_ratchet_guidance(tmp_path: Path) -> None:
    _write_adapter(tmp_path, _adapter_source(RATCHET - 1))

    findings = _analyze(tmp_path)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.fingerprint == f"ratchet-slack:{RATCHET - 1}"
    assert "Ratchet has slack" in finding.message
    assert finding.suggestion is not None
    assert "Lower RATCHET" in finding.suggestion


def test_missing_adapter_file_fails_closed(tmp_path: Path) -> None:
    findings = _analyze(tmp_path)

    assert len(findings) == 1
    assert findings[0].fingerprint == "adapter-file-missing"
    assert findings[0].file_path == _DISPLAY_PATH


def test_missing_adapter_class_fails_closed(tmp_path: Path) -> None:
    _write_adapter(tmp_path, "class SomethingElse:\n    def method(self) -> None:\n        return None\n")

    findings = _analyze(tmp_path)

    assert len(findings) == 1
    assert findings[0].fingerprint == "adapter-class-missing"


def test_syntax_error_fails_closed(tmp_path: Path) -> None:
    _write_adapter(tmp_path, "class PluginAuditWriterAdapter(:\n")

    findings = _analyze(tmp_path)

    assert len(findings) == 1
    assert findings[0].fingerprint == "adapter-unparseable"


def test_private_nested_and_duplicate_defs_do_not_inflate_the_count(tmp_path: Path) -> None:
    """Only distinct public defs directly in the class body count.

    ``__init__``/private helpers are excluded, nested-class defs belong to the
    nested class, redefinitions dedupe to one (runtime semantics), and a
    decorated def still counts as public surface.
    """
    methods = "\n".join(f"    def method_{index:02d}(self) -> None:\n        return None\n" for index in range(RATCHET - 2))
    source = (
        "class PluginAuditWriterAdapter:\n"
        "    def __init__(self) -> None:\n"
        "        pass\n\n"
        "    def _private_helper(self) -> None:\n"
        "        return None\n\n"
        "    class _Nested:\n"
        "        def not_counted(self) -> None:\n"
        "            return None\n\n"
        "    def duplicated(self) -> None:\n"
        "        return None\n\n"
        "    def duplicated(self) -> None:  # noqa: F811\n"
        "        return None\n\n"
        "    @staticmethod\n"
        "    def decorated_still_counts() -> None:\n"
        "        return None\n\n"
        f"{methods}"
    )
    _write_adapter(tmp_path, source)

    assert _analyze(tmp_path) == []


def test_src_elspeth_scan_root_resolves_to_repository_root(tmp_path: Path) -> None:
    """CI runs contract_invariants/* with --root src/elspeth; the rule must still find the adapter."""
    _write_adapter(tmp_path, _adapter_source(RATCHET))

    scan_root = tmp_path / "src" / "elspeth"
    assert list(RULE.analyze(_EMPTY_TREE, scan_root, RuleContext(root=scan_root))) == []


def test_explicit_repo_root_wins(tmp_path: Path) -> None:
    _write_adapter(tmp_path, _adapter_source(RATCHET))

    elsewhere = tmp_path / "unrelated-scan-root"
    elsewhere.mkdir()
    context = RuleContext(root=elsewhere, repo_root=tmp_path)

    assert list(RULE.analyze(_EMPTY_TREE, elsewhere, context)) == []


def test_ratchet_is_tight_against_the_live_adapter() -> None:
    """RATCHET must track the real PluginAuditWriterAdapter exactly.

    This anchors the constant to the tree so `pytest tests/` catches adapter
    drift even without the elspeth-lints CI gate.
    """
    repo_root = Path(__file__).resolve().parents[3]

    assert list(RULE.analyze(_EMPTY_TREE, repo_root, RuleContext(root=repo_root))) == []
