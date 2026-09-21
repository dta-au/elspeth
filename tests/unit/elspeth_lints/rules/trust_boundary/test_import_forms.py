"""Import-form regressions for all three independent honesty gates."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from elspeth.contracts import __all__ as contracts_exports
from elspeth.contracts.trust_boundary import __all__ as boundary_exports
from elspeth_lints.core.protocols import Rule, RuleContext
from elspeth_lints.rules.trust_boundary.scope import RULE as SCOPE_RULE
from elspeth_lints.rules.trust_boundary.shared import _canonical_star_exports
from elspeth_lints.rules.trust_boundary.tests import RULE as TESTS_RULE
from elspeth_lints.rules.trust_boundary.tier import RULE as TIER_RULE
from elspeth_lints.rules.trust_tier.tier_model import RULE as TIER_MODEL_RULE


def _package_file(root: Path, package: str, filename: str = "handler.py") -> Path:
    directory = root
    for part in package.split("."):
        directory /= part
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").touch()
    return directory / filename


@pytest.mark.parametrize("rule, expected", [(TIER_RULE, "TBT2"), (SCOPE_RULE, "TBS3"), (TESTS_RULE, "TBE4")])
@pytest.mark.parametrize("scan", [False, True], ids=["direct", "scan"])
@pytest.mark.parametrize(
    "package, filename, import_statement, decorator",
    [
        ("elspeth.contracts", "handler.py", "from elspeth.contracts.trust_boundary import trust_boundary", "trust_boundary"),
        ("elspeth.contracts", "handler.py", "from .trust_boundary import trust_boundary", "trust_boundary"),
        ("elspeth.contracts", "__init__.py", "from .trust_boundary import trust_boundary as boundary", "boundary"),
        ("elspeth.plugins", "handler.py", "from ..contracts import trust_boundary as boundary", "boundary"),
        ("elspeth.plugins", "handler.py", "from ..contracts import trust_boundary as boundary_module", "boundary_module.trust_boundary"),
        ("elspeth.contracts", "handler.py", "from elspeth.contracts.trust_boundary import *", "trust_boundary"),
        ("elspeth.contracts", "handler.py", "from elspeth.contracts import *", "trust_boundary"),
        ("elspeth.contracts", "handler.py", "from .trust_boundary import *", "trust_boundary"),
        ("elspeth.contracts", "handler.py", "from .trust_boundary import *", "observation_boundary"),
    ],
)
def test_import_forms_cannot_bypass_honesty(
    rule: Rule,
    expected: str,
    scan: bool,
    package: str,
    filename: str,
    import_statement: str,
    decorator: str,
    tmp_path: Path,
) -> None:
    file_path = _package_file(tmp_path, package, filename)
    source = f"{import_statement}\n@{decorator}(tier=unverifiable)\ndef handler(data):\n    return data\n"
    file_path.write_text(source)
    tree = ast.Module(body=[], type_ignores=[]) if scan else ast.parse(source)
    findings = rule.analyze(tree, tmp_path if scan else file_path, RuleContext(root=tmp_path))
    assert expected in {finding.rule_id for finding in findings}


@pytest.mark.parametrize("rule", [TIER_RULE, SCOPE_RULE, TESTS_RULE])
@pytest.mark.parametrize(
    "import_statement",
    [
        "from .trust_boundary import trust_boundary",
        "from .trust_boundary import *",
        "from elspeth.contracts.trust_boundary import trust_boundary\nfrom foreign import *",
        "from elspeth.contracts.trust_boundary import *\ntrust_boundary = foreign",
    ],
)
def test_unrelated_or_shadowed_imports_are_not_elspeth_boundaries(rule: Rule, import_statement: str, tmp_path: Path) -> None:
    file_path = _package_file(tmp_path, "foreign.contracts")
    source = f"{import_statement}\n@trust_boundary(tier=unverifiable)\ndef handler(data):\n    return data\n"
    file_path.write_text(source)
    assert rule.analyze(ast.parse(source), file_path, RuleContext(root=tmp_path)) == []


@pytest.mark.parametrize("rule, expected", [(TIER_RULE, "TBT2"), (SCOPE_RULE, "TBS3"), (TESTS_RULE, "TBE4")])
@pytest.mark.parametrize(
    "imports",
    [
        "if condition:\n    from .trust_boundary import trust_boundary\nelse:\n    from elspeth.contracts.trust_boundary import *",
        "for item in items:\n    pass\nelse:\n    from .trust_boundary import trust_boundary",
        "try:\n    pass\nfinally:\n    from .trust_boundary import *",
    ],
)
def test_honesty_imports_survive_control_flow_joins(rule: Rule, expected: str, imports: str, tmp_path: Path) -> None:
    file_path = _package_file(tmp_path, "elspeth.contracts")
    source = f"{imports}\n@trust_boundary(tier=unverifiable)\ndef handler(data):\n    return data\n"
    findings = rule.analyze(ast.parse(source), file_path, RuleContext(root=tmp_path))
    assert expected in {finding.rule_id for finding in findings}


@pytest.mark.parametrize("rule, expected", [(TIER_RULE, "TBT2"), (SCOPE_RULE, "TBS3"), (TESTS_RULE, "TBE4")])
def test_honesty_finally_entry_uses_package_aware_import_effects(rule: Rule, expected: str, tmp_path: Path) -> None:
    file_path = _package_file(tmp_path, "elspeth.contracts")
    source = textwrap.dedent("""
        from .trust_boundary import trust_boundary
        try:
            for item in items:
                pass
            else:
                from .trust_boundary import trust_boundary
        finally:
            @trust_boundary(tier=unverifiable)
            def handler(data):
                return data
    """)
    findings = rule.analyze(ast.parse(source), file_path, RuleContext(root=tmp_path))
    # Import failure inside try invalidates the alias on an exception path;
    # the finalbody cannot assume every reachable path preserves its identity.
    assert expected not in {finding.rule_id for finding in findings}


@pytest.mark.parametrize(
    "import_statement",
    ["from .trust_boundary import trust_boundary", "from elspeth.contracts.trust_boundary import *"],
)
def test_honesty_recognition_does_not_grant_tier_model_suppression(import_statement: str, tmp_path: Path) -> None:
    file_path = _package_file(tmp_path, "elspeth.contracts")
    source = textwrap.dedent(f"""
        {import_statement}
        @trust_boundary(tier=3, source="external", source_param="data", suppresses=("R1",), invariant="ValueError on invalid input")
        def handler(data):
            return data.get("x")
    """)
    findings = TIER_MODEL_RULE.analyze(ast.parse(source), file_path, RuleContext(root=tmp_path))
    assert "R1" in {finding.rule_id for finding in findings}
    assert "R_TB_SUPPRESSED" not in {finding.rule_id for finding in findings}


@pytest.mark.parametrize("rule, expected", [(TIER_RULE, "TBT2"), (SCOPE_RULE, "TBS3"), (TESTS_RULE, "TBE4")])
@pytest.mark.parametrize("module", ["elspeth.contracts", "elspeth.contracts.trust_boundary"])
@pytest.mark.parametrize("alias, recognized", [("tb", True), ("BoundaryRule", False)])
def test_known_star_rebinds_only_exported_names(
    rule: Rule, expected: str, module: str, alias: str, recognized: bool, tmp_path: Path
) -> None:
    source = textwrap.dedent(f"""
        from elspeth.contracts.trust_boundary import trust_boundary as {alias}
        from {module} import *
        @{alias}(tier=unverifiable)
        def handler(data):
            return data
    """)
    findings = rule.analyze(ast.parse(source), tmp_path / "handler.py", RuleContext(root=tmp_path))
    assert (expected in {finding.rule_id for finding in findings}) is recognized


@pytest.mark.parametrize(
    "module, exports", [("elspeth.contracts", contracts_exports), ("elspeth.contracts.trust_boundary", boundary_exports)]
)
def test_canonical_star_exports_match_runtime_contract(module: str, exports: list[str]) -> None:
    assert _canonical_star_exports(module) == tuple(exports)


@pytest.mark.parametrize("source", ["", "__all__ = names", "__all__ = [name]", "__all__ = ['trust_boundary']\n__all__ = []"])
def test_canonical_star_export_drift_fails_closed(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def read_export_source(self: Path, encoding: str) -> str:
        return source

    monkeypatch.setattr(Path, "read_text", read_export_source)
    with pytest.raises(ValueError, match="Canonical boundary"):
        _canonical_star_exports("elspeth.contracts.trust_boundary")
