from __future__ import annotations

import ast
import hashlib
from textwrap import dedent

from elspeth_lints.rules.trust_tier.tier_model.rule import Finding, TierModelVisitor


def _r2_findings(source: str) -> list[Finding]:
    tree = ast.parse(source, filename="test.py")
    visitor = TierModelVisitor(
        "test.py",
        source.splitlines(),
        hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )
    visitor.visit(tree)
    return [finding for finding in visitor.findings if finding.rule_id == "R2"]


def test_detects_inspect_getattr_static_keyword_default() -> None:
    source = dedent(
        """
        import inspect

        value = inspect.getattr_static(obj, "attr", default=None)
        """
    )

    findings = _r2_findings(source)

    assert len(findings) == 1
    assert "inspect.getattr_static" in findings[0].message


def test_detects_assignment_alias_of_inspect_getattr_static() -> None:
    source = dedent(
        """
        import inspect

        lookup = inspect.getattr_static
        value = lookup(obj, "attr", None)
        """
    )

    findings = _r2_findings(source)

    assert len(findings) == 1
    assert "inspect.getattr_static" in findings[0].message


def test_comprehension_target_shadows_imported_getattr_static_alias() -> None:
    source = dedent(
        """
        from inspect import getattr_static as lookup

        values = [lookup(obj, "attr", None) for lookup in accessors]
        value = lookup(obj, "attr", None)
        """
    )

    findings = _r2_findings(source)

    assert len(findings) == 1
    assert findings[0].line == 5
