"""Tests for ``@trust_boundary`` decorator-aware suppression in tier_model.

The rule under test (``trust_tier.tier_model``) drops findings inside a
``@trust_boundary``-decorated function when:

1. the finding's ``rule_id`` is listed in the decorator's ``suppresses``
   tuple, and
2. the finding's AST subject is rooted at the decorator's ``source_param``
   parameter (or at a name derived from it through subscript, attribute
   access, ``.get(...)``, iteration, unpacking, or walrus).

Findings that don't satisfy both conditions remain visible — the decorator
is not a whole-function exemption cloak. Malformed decorators (non-literal
kwargs, wrong-shaped values) emit their own ``R_TB_NONLITERAL`` /
``R_TB_MALFORMED`` finding and are treated as inert.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from textwrap import dedent, indent

import pytest

from elspeth_lints.core.cli import main
from elspeth_lints.rules.trust_tier.tier_model.rule import Finding, TierModelVisitor
from elspeth_lints.rules.trust_tier.tier_model.trust_boundary_suppress import (
    extract_boundary_metadata,
)


def _findings(source: str, filename: str = "test_module.py") -> list[Finding]:
    """Run the tier-model visitor on ``source`` and return findings."""
    return _visitor(source, filename=filename).findings


def _visitor(source: str, filename: str = "test_module.py") -> TierModelVisitor:
    """Run the tier-model visitor on ``source`` and return the visitor."""
    tree = ast.parse(source, filename=filename)
    source_lines = source.splitlines()
    file_fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()
    visitor = TierModelVisitor(filename, source_lines, file_fingerprint)
    visitor.visit(tree)
    return visitor


def _findings_by_rule(findings: list[Finding], rule_id: str) -> list[Finding]:
    return [f for f in findings if f.rule_id == rule_id]


def _first_function(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(dedent(source))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise AssertionError("fixture must contain a top-level function")


_NESTED_OBSERVATION_HANDLER = """
@observation_boundary(
    tier=3,
    source="x",
    source_param="data",
    suppresses=("R1",),
    invariant="returns None on absence",
)
def handler(data):
    return data.get("x")
"""


def _boundary_after_loop_source(
    *,
    loop_kind: str,
    initial_import: str,
    body: str,
    orelse: str | None = None,
) -> str:
    header = {
        "for": "for item in items:",
        "async for": "async for item in items:",
        "while": "while enabled:",
    }[loop_kind]
    loop = f"{header}\n{indent(dedent(body).strip(), '    ')}"
    if orelse is not None:
        loop += f"\nelse:\n{indent(dedent(orelse).strip(), '    ')}"
    outer_body = f"{initial_import}\n\n{loop}\n\n{dedent(_NESTED_OBSERVATION_HANDLER).strip()}"
    return f"async def outer(items, enabled, stop):\n{indent(outer_body, '    ')}\n"


def _with_suite(with_kind: str, body: str, *, items: str = "suppressing_context()") -> str:
    return f"{with_kind} {items}:\n{indent(dedent(body).strip(), '    ')}"


def _boundary_in_finally_source(transfer: str) -> str:
    try_body = f"""
        from foreign import observation_boundary
        {transfer}
        from elspeth.contracts.trust_boundary import observation_boundary
    """
    try_suite = f"try:\n{indent(dedent(try_body).strip(), '    ')}\nfinally:\n{indent(dedent(_NESTED_OBSERVATION_HANDLER).strip(), '    ')}"
    if transfer in {"break", "continue"}:
        return f"async def outer(items):\n    for item in items:\n{indent(try_suite, '        ')}\n"
    return f"def outer():\n{indent(try_suite, '    ')}\n"


# =============================================================================
# Positive cases: decorator suppresses qualifying findings
# =============================================================================


class TestSuppressionPositive:
    """The decorator suppresses R1 / R5 inside the function body when rooted."""

    def test_suppresses_isinstance_on_source_param(self) -> None:
        """``isinstance(arguments.get("x"), list)`` should be suppressed."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="LLM tool args",
                source_param="arguments",
                suppresses=("R1", "R5"),
                invariant="raises on shape mismatch",
            )
            def handler(arguments):
                if not isinstance(arguments.get("nodes"), list):
                    raise ValueError("nodes must be a list")
                return None
        """)
        findings = _findings(source)
        # R1 (arguments.get) and R5 (isinstance on arguments.get) should both
        # be suppressed.
        assert _findings_by_rule(findings, "R1") == []
        assert _findings_by_rule(findings, "R5") == []

    def test_observation_boundary_alias_suppresses_and_records_nonraising_metadata(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary as observes

            @observes(
                tier=3,
                source="optional LLM field",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("value")
        """)

        visitor = _visitor(source)
        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(visitor.suppressed_findings) == 1

        tree = ast.parse(source)
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef))
        metadata, diagnostics = extract_boundary_metadata(
            function,
            import_aliases={"observes": "elspeth.contracts.trust_boundary.observation_boundary"},
        )
        assert diagnostics == []
        assert metadata is not None
        assert metadata.non_raising is True

    def test_suppression_records_non_failing_observation(self) -> None:
        """Suppressed R1/R5 findings remain auditable as observation records."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="LLM tool args",
                source_param="arguments",
                suppresses=("R1", "R5"),
                invariant="raises on shape mismatch",
                test_ref="tests/test_handler.py::test_rejects_bad_args",
                test_fingerprint="abc123",
            )
            def handler(arguments):
                if not isinstance(arguments.get("nodes"), list):
                    raise ValueError("nodes must be a list")
                return None
        """)
        visitor = _visitor(source)
        expected_file_fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert _findings_by_rule(visitor.findings, "R5") == []
        suppressed = _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")
        assert sorted(item.message for item in suppressed) == [
            (
                "@trust_boundary suppressed R1 under source_param='arguments'; "
                "source='LLM tool args'; test_ref='tests/test_handler.py::test_rejects_bad_args'; "
                "suppresses=('R1', 'R5')"
            ),
            (
                "@trust_boundary suppressed R5 under source_param='arguments'; "
                "source='LLM tool args'; test_ref='tests/test_handler.py::test_rejects_bad_args'; "
                "suppresses=('R1', 'R5')"
            ),
        ]
        assert {item.file_fingerprint for item in suppressed} == {expected_file_fingerprint}

    def test_suppresses_with_fully_qualified_decorator_after_sibling_import(self) -> None:
        """A later ``elspeth.*`` import must not hide the FQ decorator spelling."""
        source = dedent("""
            import elspeth.contracts.trust_boundary
            import elspeth.web

            @elspeth.contracts.trust_boundary(
                tier=3,
                source="LLM tool args",
                source_param="arguments",
                suppresses=("R1",),
                invariant="raises on shape mismatch",
            )
            def handler(arguments):
                return arguments.get("nodes")
        """)

        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_suppresses_with_fully_qualified_observation_boundary_after_sibling_import(self) -> None:
        source = dedent("""
            import elspeth.contracts.trust_boundary
            import elspeth.web

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="optional LLM field",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("nodes")
        """)

        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_core_cli_emits_suppression_observation_without_failing(self, tmp_path: Path, capsys) -> None:
        """The CI-facing CLI surfaces suppression observations at note severity."""
        allowlist_dir = tmp_path / "config" / "cicd" / "enforce_tier_model"
        allowlist_dir.mkdir(parents=True)
        (tmp_path / "handler.py").write_text(
            dedent("""
                from elspeth.contracts import trust_boundary

                @trust_boundary(
                    tier=3,
                    source="LLM tool args",
                    source_param="arguments",
                    suppresses=("R1",),
                    invariant="raises on shape mismatch",
                    test_ref="tests/test_handler.py::test_rejects_bad_args",
                    test_fingerprint="abc123",
                )
                def handler(arguments):
                    return arguments.get("nodes")
            """),
            encoding="utf-8",
        )

        exit_code = main(
            [
                "check",
                "--rules",
                "trust_tier.tier_model",
                "--root",
                str(tmp_path),
                "--format",
                "json",
            ]
        )

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert [(item["rule_id"], item["severity"]) for item in payload] == [
            ("R_TB_SUPPRESSED", "note"),
        ]
        assert "suppressed R1" in payload[0]["message"]

    def test_suppresses_get_on_loop_variable(self) -> None:
        """``for raw in arguments["nodes"]: raw.get("id")`` — both suppressed."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                for raw in arguments["nodes"]:
                    raw.get("id")
                return None
        """)
        findings = _findings(source)
        # Both ``raw.get`` and any other arguments-rooted ``.get`` should be
        # suppressed.
        assert _findings_by_rule(findings, "R1") == []

    def test_suppresses_dataflow_propagation_through_assignment(self) -> None:
        """``raw = arguments["x"]; raw.get("y")`` — propagation through assign."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                raw = arguments["x"]
                raw.get("y")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_suppresses_inside_comprehension(self) -> None:
        """``[x.get("y") for x in arguments]`` — comprehension target derived."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                return [x.get("y") for x in arguments]
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_suppresses_walrus_then_method_call(self) -> None:
        """``if (x := arguments.get("k")): x.get("v")`` — both suppressed."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                if (x := arguments.get("k")):
                    x.get("v")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []


class TestLiveDerivedNameSuppression:
    """Derived-name coverage through the production visitor path."""

    def test_annotation_augassign_with_and_namedexpr_suppress_live_findings(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="arguments",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(arguments, total):
                annotated: object = arguments["annotated"]
                total += arguments["delta"]
                with arguments["ctx"] as ctx:
                    pass
                if (chosen := arguments["maybe"]):
                    pass
                annotated.get("a")
                total.get("b")
                ctx.get("c")
                chosen.get("d")
        """)

        assert _findings_by_rule(_findings(source), "R1") == []

    def test_async_for_async_with_and_comprehension_suppress_live_findings(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="arguments",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            async def handler(arguments):
                async for item in arguments["items"]:
                    item.get("id")
                async with arguments["ctx"] as ctx:
                    ctx.get("id")
                [row.get("id") for row in arguments["rows"]]
                {tag.get("id") for tag in arguments["tags"]}
                {key.get("id"): value.get("id") for key, value in arguments["pairs"]}
                (part.get("id") for part in arguments["parts"])
        """)

        assert _findings_by_rule(_findings(source), "R1") == []


class TestTryHandlersInsideBoundary:
    """Exception handlers inside a boundary still run exception rules."""

    def test_broad_except_still_fires_inside_trust_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="LLM tool args",
                source_param="arguments",
                suppresses=("R1", "R5"),
                invariant="raises on shape mismatch",
            )
            def handler(arguments):
                try:
                    return arguments["value"]
                except Exception:
                    pass
                return None
        """)

        findings = _findings(source)

        r4 = _findings_by_rule(findings, "R4")
        assert len(r4) == 1
        assert "Broad exception caught without re-raise" in r4[0].message

    def test_silent_specific_except_still_fires_inside_trust_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="LLM tool args",
                source_param="arguments",
                suppresses=("R1", "R5"),
                invariant="raises on shape mismatch",
            )
            def handler(arguments):
                try:
                    return arguments["value"]
                except ValueError:
                    pass
                return None
        """)

        findings = _findings(source)

        r6 = _findings_by_rule(findings, "R6")
        assert len(r6) == 1
        assert "Exception swallowed without re-raise" in r6[0].message


# =============================================================================
# Negative cases: decorator does NOT suppress
# =============================================================================


class TestSuppressionNegative:
    """Findings outside the decorator's scope remain visible."""

    def test_non_rooted_access_still_reported(self) -> None:
        """``self._cache.get(node_id)`` is NOT rooted at ``arguments``."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            class Foo:
                @trust_boundary(
                    tier=3,
                    source="x",
                    source_param="arguments",
                    suppresses=("R1",),
                    invariant="x",
                )
                def handler(self, arguments):
                    arguments.get("ok")  # suppressed
                    self._cache.get("not-ok")  # NOT suppressed
                    return None
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        # Exactly one R1: the ``self._cache.get`` call.
        assert len(r1) == 1
        assert "self._cache" in r1[0].code_snippet

    def test_rule_outside_suppresses_still_reported(self) -> None:
        """``suppresses=("R1",)`` does NOT cover R5."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")  # R1, suppressed
                isinstance(arguments, dict)  # R5, NOT suppressed
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []
        r5 = _findings_by_rule(findings, "R5")
        assert len(r5) == 1

    def test_no_decorator_no_suppression(self) -> None:
        """Plain function with the boundary pattern is fully reported."""
        source = dedent("""
            def handler(arguments):
                for raw in arguments["nodes"]:
                    raw.get("id")
                return None
        """)
        findings = _findings(source)
        # arguments["nodes"] subscript: no R1 (not a .get). raw.get is R1.
        # No isinstance, so no R5 either.
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) >= 1

    def test_later_source_assignment_does_not_suppress_earlier_safe_local(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload):
                raw = {}
                raw.get("safe")
                raw = payload["raw"]
                return raw["id"]
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1
        assert 'raw.get("safe")' in r1[0].code_snippet

    def test_safe_reassignment_clears_previously_derived_local(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload):
                raw = payload["raw"]
                raw.get("external")
                raw = {}
                raw.get("safe")
                return raw
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1
        assert 'raw.get("safe")' in r1[0].code_snippet

    def test_if_branch_derived_name_only_on_one_path_does_not_suppress_after_join(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload, use_external):
                if use_external:
                    raw = {}
                else:
                    raw = payload["raw"]
                return raw.get("id")
        """)

        r1 = _findings_by_rule(_findings(source), "R1")
        assert len(r1) == 1
        assert 'raw.get("id")' in r1[0].code_snippet

    def test_if_branch_derived_name_on_every_path_suppresses_after_join(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload, choose_a):
                if choose_a:
                    raw = payload["a"]
                else:
                    raw = payload["b"]
                return raw.get("id")
        """)

        assert _findings_by_rule(_findings(source), "R1") == []

    def test_try_branch_derived_name_only_on_handler_path_does_not_suppress_after_join(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload):
                try:
                    raw = {}
                except ValueError:
                    raw = payload["raw"]
                return raw.get("id")
        """)

        r1 = _findings_by_rule(_findings(source), "R1")
        assert len(r1) == 1
        assert 'raw.get("id")' in r1[0].code_snippet

    def test_while_body_derived_name_does_not_suppress_after_zero_iteration_join(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="external payload",
                source_param="payload",
                suppresses=("R1",),
                invariant="raises ValueError on malformed payload",
            )
            def handler(payload, keep_going):
                raw = {}
                while keep_going:
                    raw = payload["raw"]
                    break
                return raw.get("id")
        """)

        r1 = _findings_by_rule(_findings(source), "R1")
        assert len(r1) == 1
        assert 'raw.get("id")' in r1[0].code_snippet


# =============================================================================
# Decorator-shape diagnostics
# =============================================================================


class TestDecoratorDiagnostics:
    """Malformed decorators emit their own findings and do not suppress."""

    def test_non_literal_suppresses_emits_diagnostic(self) -> None:
        """``suppresses=ALLOWED`` (a Name) is not literal-evaluatable."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            ALLOWED = ("R1", "R5")

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=ALLOWED,
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        nonliteral = _findings_by_rule(findings, "R_TB_NONLITERAL")
        assert len(nonliteral) == 1
        # And the inner R1 should NOT be suppressed (the decorator is inert).
        assert len(_findings_by_rule(findings, "R1")) == 1

    def test_string_suppresses_emits_malformed(self) -> None:
        """``suppresses="R1"`` (a string instead of a tuple) is malformed."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses="R1",
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        malformed = _findings_by_rule(findings, "R_TB_MALFORMED")
        assert len(malformed) == 1
        # And the inner R1 should NOT be suppressed.
        assert len(_findings_by_rule(findings, "R1")) == 1

    def test_source_param_not_a_real_parameter_emits_malformed(self) -> None:
        """``source_param`` that isn't on the signature → R_TB_MALFORMED."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="missing",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert len(_findings_by_rule(findings, "R_TB_MALFORMED")) == 1
        # Inner R1 NOT suppressed.
        assert len(_findings_by_rule(findings, "R1")) == 1

    def test_non_string_source_and_invariant_are_malformed_and_inert(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source=42,
                source_param="arguments",
                suppresses=("R1",),
                invariant=42,
                test_ref="tests/test_handler.py::test_rejects_bad_args",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        findings = _findings(source)
        malformed = _findings_by_rule(findings, "R_TB_MALFORMED")
        assert len(malformed) == 1
        assert "'source' must be a string" in malformed[0].message
        assert "'invariant' must be a string" in malformed[0].message
        assert len(_findings_by_rule(findings, "R1")) == 1

    def test_stacked_trust_boundary_decorators_emit_stacked_and_do_not_suppress(self) -> None:
        """Multiple boundary decorators are ambiguous and must be inert."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="outer feed",
                source_param="arguments",
                suppresses=("R1",),
                invariant="outer invariant",
            )
            @trust_boundary(
                tier=3,
                source="inner feed",
                source_param="arguments",
                suppresses=("R5",),
                invariant="inner invariant",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        findings = _findings(source)
        stacked = _findings_by_rule(findings, "R_TB_STACKED")
        assert len(stacked) == 1
        assert "multiple @trust_boundary" in stacked[0].message
        assert len(_findings_by_rule(findings, "R1")) == 1


class TestExtractBoundaryMetadata:
    """Direct branch coverage for decorator metadata extraction."""

    def test_no_decorator_returns_empty_result(self) -> None:
        func = _first_function(
            """
            def handler(arguments):
                return arguments
            """
        )

        metadata, diagnostics = extract_boundary_metadata(func)

        assert metadata is None
        assert diagnostics == []

    def test_positional_argument_is_malformed(self) -> None:
        func = _first_function(
            """
            @trust_boundary(3, "x", "arguments", ("R1",), "x")
            def handler(arguments):
                return arguments
            """
        )

        metadata, diagnostics = extract_boundary_metadata(func)

        assert metadata is None
        assert [item.rule_id for item in diagnostics] == ["R_TB_MALFORMED"]
        assert "positional arguments" in diagnostics[0].message

    def test_kwargs_unpacking_is_nonliteral(self) -> None:
        func = _first_function(
            """
            @trust_boundary(**metadata)
            def handler(arguments):
                return arguments
            """
        )

        metadata, diagnostics = extract_boundary_metadata(func)

        assert metadata is None
        assert [item.rule_id for item in diagnostics] == ["R_TB_NONLITERAL"]
        assert "**-unpacking" in diagnostics[0].message

    def test_missing_suppresses_is_malformed(self) -> None:
        func = _first_function(
            """
            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                invariant="x",
            )
            def handler(arguments):
                return arguments
            """
        )

        metadata, diagnostics = extract_boundary_metadata(func)

        assert metadata is None
        assert diagnostics[0].rule_id == "R_TB_MALFORMED"
        assert "missing kwarg 'suppresses'" in diagnostics[0].message

    def test_non_string_suppresses_item_is_malformed(self) -> None:
        func = _first_function(
            """
            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1", 5),
                invariant="x",
            )
            def handler(arguments):
                return arguments
            """
        )

        metadata, diagnostics = extract_boundary_metadata(func)

        assert metadata is None
        assert diagnostics[0].rule_id == "R_TB_MALFORMED"
        assert "non-string" in diagnostics[0].message

    def test_source_param_wrong_type_and_empty_string_are_malformed(self) -> None:
        for source_param, expected in (("5", "must be a string"), ("''", "empty string")):
            func = _first_function(
                f"""
                @trust_boundary(
                    tier=3,
                    source="x",
                    source_param={source_param},
                    suppresses=("R1",),
                    invariant="x",
                )
                def handler(arguments):
                    return arguments
                """
            )

            metadata, diagnostics = extract_boundary_metadata(func)

            assert metadata is None
            assert diagnostics[0].rule_id == "R_TB_MALFORMED"
            assert expected in diagnostics[0].message


# =============================================================================
# Decorator stack ordering
# =============================================================================


class TestDecoratorStackOrdering:
    """``@trust_boundary`` is recognised wherever it appears in the stack."""

    def test_recognised_above_other_decorators(self) -> None:
        """``@trust_boundary(...) @other`` — recognised."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            def some_other_decorator(fn):
                return fn

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            @some_other_decorator
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_recognised_below_other_decorators(self) -> None:
        """``@some_other_decorator @trust_boundary(...)`` — recognised."""
        source = dedent("""
            from elspeth.contracts import trust_boundary

            def some_other_decorator(fn):
                return fn

            @some_other_decorator
            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_attribute_access_form_recognised(self) -> None:
        """``@elspeth.contracts.trust_boundary(...)`` form is recognised."""
        source = dedent("""
            import elspeth.contracts

            @elspeth.contracts.trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_elspeth_contracts_alias_attribute_form_recognised(self) -> None:
        """``import elspeth.contracts as contracts`` remains a valid spelling."""
        source = dedent("""
            import elspeth.contracts as contracts

            @contracts.trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []

    def test_non_elspeth_attribute_named_trust_boundary_does_not_suppress(self) -> None:
        """An unrelated ``foo.trust_boundary`` decorator is not Elspeth's boundary."""
        source = dedent("""
            import foo

            @foo.trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_shadowed_elspeth_name_does_not_suppress(self) -> None:
        """An alias named ``elspeth`` is not enough; it must resolve to Elspeth."""
        source = dedent("""
            import foo as elspeth

            @elspeth.contracts.trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_elspeth_prefixed_root_alias_does_not_masquerade_as_observation_boundary(self) -> None:
        """An exact-looking chain remains foreign when its root alias resolves elsewhere."""
        source = dedent("""
            import elspeth.fake as elspeth

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_nested_legitimate_import_cannot_legitimize_forged_module_alias(self) -> None:
        source = dedent("""
            import elspeth.fake as elspeth

            def unrelated():
                import elspeth.contracts.trust_boundary

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_nested_foreign_import_cannot_poison_legitimate_module_alias(self) -> None:
        source = dedent("""
            import elspeth.contracts.trust_boundary

            def unrelated():
                import elspeth.fake as elspeth

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="optional field",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_class_local_legitimate_import_cannot_legitimize_forged_module_alias(self) -> None:
        source = dedent("""
            import elspeth.fake as elspeth

            class Unrelated:
                import elspeth.contracts.trust_boundary

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_class_local_foreign_import_cannot_poison_legitimate_module_alias(self) -> None:
        source = dedent("""
            import elspeth.contracts.trust_boundary

            class Unrelated:
                import elspeth.fake as elspeth

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="optional field",
                source_param="arguments",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_parameter_binding_shadows_imported_trust_boundary_for_whole_function(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            def outer(trust_boundary):
                @trust_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="x",
                )
                def nested(data):
                    return data.get("x")
                return nested
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_module_assignment_shadows_imported_observation_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            observation_boundary = object()

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_method_parameter_shadows_module_observation_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            class Handler:
                def outer(self, observation_boundary):
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="data",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def nested(data):
                        return data.get("x")
                    return nested
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize(
        "binding",
        [
            "observation_boundary = object()",
            "observation_boundary: object",
            "(observation_boundary := object())",
            "for observation_boundary in ():\n    pass",
            "with context() as observation_boundary:\n    pass",
            "try:\n    pass\nexcept ValueError as observation_boundary:\n    pass",
            "import foreign as observation_boundary",
            "from foreign import decorator as observation_boundary",
            "def observation_boundary():\n    pass",
            "class observation_boundary:\n    pass",
        ],
    )
    def test_function_local_bindings_shadow_inherited_boundary_for_whole_scope(self, binding: str) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            def outer():
                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def nested(data):
                    return data.get("x")
        """)
        source += "\n".join(f"    {line}" for line in binding.splitlines())
        source += "\n    return nested\n"

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize(
        "binding",
        [
            "observation_boundary = object()",
            "observation_boundary: object = object()",
            "(observation_boundary := object())",
            "for observation_boundary in ():\n    pass",
            "with context() as observation_boundary:\n    pass",
            "try:\n    pass\nexcept ValueError as observation_boundary:\n    pass",
            "import foreign as observation_boundary",
            "from foreign import decorator as observation_boundary",
            "def observation_boundary():\n    pass",
            "class observation_boundary:\n    pass",
        ],
    )
    def test_module_bindings_replace_imported_boundary_meaning(self, binding: str) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary
        """)
        source += f"\n{binding}\n"
        source += dedent("""
            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_later_local_import_restores_boundary_meaning_without_leaking_outward(self) -> None:
        source = dedent("""
            from foreign import observation_boundary

            def outer():
                observation_boundary = object()
                from elspeth.contracts.trust_boundary import observation_boundary

                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def nested(data):
                    return data.get("x")
                return nested
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_function_match_capture_shadows_boundary_for_whole_scope(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            def outer(subject):
                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def nested(data):
                    return data.get("x")

                match subject:
                    case {"items": [Point(value=observation_boundary), *tail], **rest}:
                        pass
                return nested
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_module_match_or_capture_replaces_imported_boundary_meaning(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            match subject:
                case ("tag", observation_boundary) | ["tag", observation_boundary]:
                    pass

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_class_match_as_capture_replaces_class_local_boundary_meaning(self) -> None:
        source = dedent("""
            class Handler:
                from elspeth.contracts.trust_boundary import observation_boundary

                match subject:
                    case observation_boundary as captured:
                        pass

                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def handler(data):
                    return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_function_type_alias_shadows_boundary_for_whole_scope(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            def outer():
                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def nested(data):
                    return data.get("x")

                type observation_boundary = object
                return nested
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_module_type_alias_replaces_imported_boundary_meaning(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            type observation_boundary = object

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_class_type_alias_replaces_class_local_boundary_meaning(self) -> None:
        source = dedent("""
            class Handler:
                from elspeth.contracts.trust_boundary import observation_boundary

                type observation_boundary = object

                @observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def handler(data):
                    return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_module_star_import_invalidates_all_trusted_aliases(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary
            from foreign import *

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_match_case_real_import_does_not_legitimize_foreign_alias_in_sibling_case(self) -> None:
        source = dedent("""
            from foreign import observation_boundary

            match subject:
                case 1:
                    from elspeth.contracts.trust_boundary import observation_boundary
                case 2:
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="data",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def sibling(data):
                        return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_match_case_foreign_import_does_not_poison_real_alias_in_sibling_case(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            match subject:
                case 1:
                    from foreign import observation_boundary
                case 2:
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="data",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def sibling(data):
                        return data.get("x")
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_match_case_rebinding_invalidates_alias_after_match(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            match subject:
                case 1:
                    from foreign import observation_boundary
                case _:
                    pass

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_match_without_alias_changes_retains_legitimate_alias_after_match(self) -> None:
        source = dedent("""
            from elspeth.contracts.trust_boundary import observation_boundary

            match subject:
                case 1:
                    def helper():
                        from foreign import observation_boundary

                        return observation_boundary
                case _:
                    value = "other"

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_assignment_cannot_forge_fully_qualified_elspeth_marker(self) -> None:
        source = dedent("""
            elspeth = foreign

            @elspeth.contracts.trust_boundary.observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_parameter_cannot_forge_fully_qualified_elspeth_marker(self) -> None:
        source = dedent("""
            import elspeth.contracts.trust_boundary

            def outer(elspeth):
                @elspeth.contracts.trust_boundary.observation_boundary(
                    tier=3,
                    source="x",
                    source_param="data",
                    suppresses=("R1",),
                    invariant="returns None on absence",
                )
                def nested(data):
                    return data.get("x")
                return nested
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_relative_import_cannot_prove_canonical_boundary_marker(self) -> None:
        source = dedent("""
            from .elspeth.contracts.trust_boundary import observation_boundary

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_optional_if_import_does_not_grant_post_branch_trust(self) -> None:
        source = dedent("""
            if enabled:
                from elspeth.contracts.trust_boundary import observation_boundary

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_optional_loop_import_does_not_grant_post_loop_trust(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_optional_loop_foreign_rebinding_invalidates_post_loop_trust(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="from foreign import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_else_can_prove_canonical_marker_on_every_normal_exit(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="from elspeth.contracts.trust_boundary import observation_boundary",
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_else_does_not_hide_foreign_break_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="""
                if stop:
                    break
                from elspeth.contracts.trust_boundary import observation_boundary
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_else_and_break_can_agree_on_canonical_marker(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="""
                from elspeth.contracts.trust_boundary import observation_boundary
                if stop:
                    break
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_continue_path_cannot_hide_foreign_rebinding(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                from foreign import observation_boundary
                if stop:
                    continue
                from elspeth.contracts.trust_boundary import observation_boundary
            """,
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_finally_rebinding_applies_to_break_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="""
                try:
                    from elspeth.contracts.trust_boundary import observation_boundary
                    break
                finally:
                    from foreign import observation_boundary
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_canonical_finally_applies_to_break_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="""
                try:
                    from foreign import observation_boundary
                    break
                finally:
                    from elspeth.contracts.trust_boundary import observation_boundary
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_finally_rebinding_applies_to_continue_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body="""
                try:
                    from elspeth.contracts.trust_boundary import observation_boundary
                    continue
                finally:
                    from foreign import observation_boundary
            """,
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_loop_canonical_finally_applies_to_continue_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                try:
                    from foreign import observation_boundary
                    continue
                finally:
                    from elspeth.contracts.trust_boundary import observation_boundary
            """,
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize(
        ("outer_finally_import", "suppressed"),
        [
            ("from foreign import observation_boundary", False),
            ("from elspeth.contracts.trust_boundary import observation_boundary", True),
        ],
    )
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_nested_loop_finally_blocks_apply_in_execution_order(
        self,
        loop_kind: str,
        outer_finally_import: str,
        *,
        suppressed: bool,
    ) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body=f"""
                try:
                    try:
                        from foreign import observation_boundary
                        break
                    finally:
                        from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    {outer_finally_import}
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert (_findings_by_rule(visitor.findings, "R1") == []) is suppressed
        assert (len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1) is suppressed

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_finally_break_replaces_pending_continue_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                try:
                    from foreign import observation_boundary
                    continue
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    break
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_finally_continue_replaces_pending_break_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                try:
                    from foreign import observation_boundary
                    break
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    continue
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("pending_exit", ["return None", "raise RuntimeError"])
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_finally_break_replaces_non_loop_abrupt_path(self, loop_kind: str, pending_exit: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body=f"""
                try:
                    from foreign import observation_boundary
                    {pending_exit}
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    break
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("final_exit", ["return None", "raise RuntimeError"])
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_finally_non_loop_abrupt_path_replaces_pending_break(self, loop_kind: str, final_exit: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body=f"""
                try:
                    from foreign import observation_boundary
                    break
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    {final_exit}
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_nested_finally_replaces_transfer_kind_inner_to_outer(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                try:
                    try:
                        from foreign import observation_boundary
                        continue
                        from elspeth.contracts.trust_boundary import observation_boundary
                    finally:
                        break
                finally:
                    continue
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_finally_alias_analysis_is_non_emitting_for_lint_expression(self) -> None:
        source = dedent("""
            async def outer(payload, items):
                for item in items:
                    try:
                        break
                    finally:
                        payload.get("x")
        """)

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1

    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_finally_break_preserves_conservative_implicit_exception_path(self, loop_kind: str) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body="""
                try:
                    from foreign import observation_boundary
                    might_raise()
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    break
            """,
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("with_kind", ["with", "async with"])
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_with_suppressed_explicit_exception_reaches_break_with_exception_aliases(
        self,
        loop_kind: str,
        with_kind: str,
    ) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body=_with_suite(
                with_kind,
                """
                    raise RuntimeError
                    from elspeth.contracts.trust_boundary import observation_boundary
                """,
            )
            + "\nbreak",
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("with_kind", ["with", "async with"])
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_with_suppressed_implicit_exception_reaches_break_with_exception_aliases(
        self,
        loop_kind: str,
        with_kind: str,
    ) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from foreign import observation_boundary",
            body=_with_suite(
                with_kind,
                """
                    might_raise()
                    from elspeth.contracts.trust_boundary import observation_boundary
                """,
            )
            + "\nbreak",
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    @pytest.mark.parametrize("with_kind", ["with", "async with"])
    @pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
    def test_with_canonical_exception_path_ignores_unreachable_foreign_rebinding(
        self,
        loop_kind: str,
        with_kind: str,
    ) -> None:
        source = _boundary_after_loop_source(
            loop_kind=loop_kind,
            initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
            body=_with_suite(
                with_kind,
                """
                    raise RuntimeError
                    from foreign import observation_boundary
                """,
            )
            + "\nbreak",
            orelse="from elspeth.contracts.trust_boundary import observation_boundary",
        )

        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    @pytest.mark.parametrize("transfer", ["break", "continue", "return None", "raise RuntimeError"])
    def test_finally_decorator_uses_reachable_pre_finally_aliases(self, transfer: str) -> None:
        visitor = _visitor(_boundary_in_finally_source(transfer))

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_finally_decorator_uses_implicit_exception_aliases_before_unreachable_repair(self) -> None:
        source = f"""
            def outer(exported):
                try:
                    from foreign import observation_boundary
                    might_raise()
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
{indent(dedent(_NESTED_OBSERVATION_HANDLER).strip(), "                    ")}
                    exported.append(handler)
                    return handler
        """

        visitor = _visitor(dedent(source))

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_if_foreign_then_legitimate_else_does_not_grant_post_branch_trust(self) -> None:
        source = dedent("""
            if enabled:
                from foreign import observation_boundary
            else:
                from elspeth.contracts.trust_boundary import observation_boundary

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_try_foreign_then_legitimate_handler_does_not_grant_post_try_trust(self) -> None:
        source = dedent("""
            try:
                from foreign import observation_boundary
            except ImportError:
                from elspeth.contracts.trust_boundary import observation_boundary

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []

    def test_identical_canonical_imports_on_both_if_paths_retain_trust(self) -> None:
        source = dedent("""
            if enabled:
                from elspeth.contracts.trust_boundary import observation_boundary
            else:
                from elspeth.contracts.trust_boundary import observation_boundary

            @observation_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("x")
        """)
        visitor = _visitor(source)

        assert _findings_by_rule(visitor.findings, "R1") == []
        assert len(_findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED")) == 1

    def test_non_elspeth_bare_import_named_trust_boundary_does_not_suppress(self) -> None:
        """``from foo import trust_boundary`` must not masquerade as Elspeth's decorator."""
        source = dedent("""
            from foo import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def handler(arguments):
                arguments.get("k")
                return None
        """)
        visitor = _visitor(source)

        assert len(_findings_by_rule(visitor.findings, "R1")) == 1
        assert _findings_by_rule(visitor.suppressed_findings, "R_TB_SUPPRESSED") == []


# =============================================================================
# Async functions
# =============================================================================


class TestAsyncFunction:
    """Async functions get the same treatment."""

    def test_async_function_suppression(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            async def handler(arguments):
                arguments.get("k")
                return None
        """)
        findings = _findings(source)
        assert _findings_by_rule(findings, "R1") == []


# =============================================================================
# Regression: B1 — nested-function scope leak in live derived-name suppression
# =============================================================================
#
# The visitor must not let nested scopes inherit an outer function's active
# ``@trust_boundary`` suppression state. A finding in a nested function or
# lambda is only suppressible by that nested callable's own decorator.
# =============================================================================


class TestNestedScopeLeakRegression:
    """Pinning the B1 fix: inner-scope assignments must not taint outer names."""

    def test_nested_function_scope_does_not_leak_to_outer(self) -> None:
        """The reviewer's exact snippet. An inner ``raw = arguments['x']``
        must not taint the outer scope's unrelated ``raw`` variable.

        Before the fix: ``ast.walk`` descended into ``inner``'s body,
        saw ``raw = arguments["x"]`` (which contained a derived-name
        reference), and added ``raw`` to the outer function's
        ``derived`` set. The outer-scope ``raw.get("k")`` then matched
        as "rooted at a derived name" and the R1 finding was suppressed.

        After the fix: the inner body is never visited by the outer
        walk, so ``raw`` is bound only as an inner-scope name. The
        outer-scope ``raw = {"k": "v"}`` is an ordinary literal-dict
        assignment; ``raw.get("k")`` on the outer ``raw`` IS NOT rooted
        at ``arguments`` and the R1 finding fires.
        """
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                def inner():
                    raw = arguments["x"]   # taints 'raw' inside inner only
                    return raw
                raw = {"k": "v"}            # outer 'raw' is unrelated
                return raw.get("k")          # FALSELY suppressed before fix
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        # The outer raw.get("k") must fire R1; the inner subscript is not
        # an R1 violation (subscript access != .get with default), so the
        # only expected R1 is the outer call.
        assert len(r1) == 1, (
            f"Expected exactly one R1 finding on the outer ``raw.get('k')``; "
            f"got {len(r1)}: {[(f.rule_id, f.line, f.message[:60]) for f in r1]}"
        )

    def test_lambda_body_does_not_leak_to_outer(self) -> None:
        """Same shape using a lambda instead of a nested def.

        A lambda introduces its own scope. Its body referencing
        ``arguments`` must not taint outer-scope name bindings that
        happen to share a name with the lambda's parameter list or
        with names the lambda's expression mentions.
        """
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                # The lambda's body references arguments but binds nothing
                # in the outer scope. The taint must stay inside the lambda.
                _f = lambda raw: arguments.get(raw)
                raw = {"k": "v"}
                return raw.get("k")
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        # The outer raw.get("k") must fire R1. The lambda's body uses
        # arguments.get(raw) which IS rooted at arguments (suppressed),
        # so the only R1 we expect is the outer call.
        outer_calls = [f for f in r1 if "raw.get" in (f.message or "")]
        assert outer_calls, f"Expected R1 on outer ``raw.get('k')``; got R1 findings: {[(f.line, f.message[:80]) for f in r1]}"


# =============================================================================
# Regression: C5-1 — class-body scope does not inherit boundary suppression
# =============================================================================
#
# Python class bodies execute in a fresh namespace: assignments become class
# attributes, not bindings in the enclosing function's locals. The live visitor
# therefore clears inherited boundary state while visiting nested classes.
# =============================================================================


class TestClassBodyScopeLeakRegression:
    """Pinning the C5-1 fix: class-body assignments must not taint outer names."""

    def test_nested_class_assignment_does_not_leak_to_outer(self) -> None:
        """Outer fn defines a nested class that assigns from ``arguments``;
        the outer ``raw`` must NOT inherit that taint.

        The class-body ``raw`` is a class attribute, not an outer local. The
        outer ``raw.get("k")`` must therefore remain visible as an R1 finding.
        """
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                class Helper:
                    # Class-body assignment. ``raw`` here is a class
                    # attribute on ``Helper``, NOT a binding in
                    # ``outer``'s locals. The outer function's
                    # ``derived`` set must NOT pick this up.
                    raw = arguments["x"]
                raw = {"k": "v"}             # outer 'raw' is unrelated
                return raw.get("k")           # FALSELY suppressed before fix
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        # The outer raw.get("k") must fire R1. The class-body subscript
        # is not an R1 violation (subscript access != .get with
        # default), so the only expected R1 is the outer call.
        assert len(r1) == 1, (
            f"Expected exactly one R1 finding on the outer ``raw.get('k')``; "
            f"got {len(r1)}: {[(f.rule_id, f.line, f.message[:60]) for f in r1]}"
        )


class TestBoundaryDoesNotInheritIntoNestedScopes:
    """Outer trust-boundary metadata must not suppress nested-scope findings."""

    def test_nested_function_free_variable_get_is_not_suppressed(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                def inner():
                    return arguments.get("k")
                return inner()
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1

    def test_nested_function_shadowed_source_param_get_is_not_suppressed(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                def inner(arguments):
                    return arguments.get("k")
                return inner({"k": "v"})
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1

    def test_lambda_body_get_is_not_suppressed_by_outer_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                inner = lambda arguments: arguments.get("k")
                return inner({"k": "v"})
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1

    def test_nested_class_body_get_is_not_suppressed_by_outer_boundary(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
            )
            def outer(arguments):
                class Inner:
                    value = arguments.get("k")
                return Inner.value
        """)
        findings = _findings(source)
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1


# =============================================================================
# Regression: M4 — unauthorised rule IDs in suppresses tuple
# =============================================================================
#
# The decorator's runtime signature constrains ``suppresses`` to
# ``tuple[Literal["R1", "R5"], ...]``. mypy enforces that at the call site,
# but the analyzer's parse path reads kwargs from a static AST and would
# otherwise honour any string the author typed. The fix adds a closed-set
# membership check inside ``extract_boundary_metadata``: unauthorised
# entries fire R_TB_MALFORMED and the decorator becomes inert.
# =============================================================================


class TestUnauthorisedSuppressRules:
    """The closed set ``{R1, R5}`` is enforced; out-of-set entries are malformed."""

    def test_decorator_with_unauthorised_rule_in_suppresses_emits_malformed_and_does_not_suppress(self) -> None:
        """``suppresses=("R2", "R8")`` is unauthorised; treat decorator as inert.

        The function below contains an R1 violation rooted at
        ``arguments``. Before the fix the decorator's ``suppresses``
        tuple was accepted as-is, no R_TB_MALFORMED was emitted, but
        because ``"R1"`` wasn't in the (unauthorised) tuple the R1
        finding fired anyway. The visible behaviour was correct by
        accident. After the fix, the unauthorised tuple fires
        R_TB_MALFORMED on the decorator AND the metadata is inert (so
        any R1/R5 violation rooted at source_param fires too).
        """
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R2", "R8"),
                invariant="x",
            )
            def handler(arguments):
                # R1 rooted at arguments — would be suppressed by a valid
                # ('R1',) decorator. With the unauthorised tuple the
                # decorator is inert, so R1 fires.
                return arguments.get("k")
        """)
        findings = _findings(source)
        malformed = _findings_by_rule(findings, "R_TB_MALFORMED")
        assert len(malformed) == 1, (
            f"Expected exactly one R_TB_MALFORMED on the decorator; got {len(malformed)}: "
            f"{[(f.rule_id, f.line, f.message[:80]) for f in malformed]}"
        )
        # Message should name the offending rule ids so the operator can
        # locate them without re-reading the source.
        assert "R2" in malformed[0].message and "R8" in malformed[0].message, (
            f"R_TB_MALFORMED message must name the unauthorised rule IDs; got: {malformed[0].message}"
        )
        # And the decorator is inert: R1 on arguments.get fires.
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1, f"Expected R1 to fire (decorator is inert under M4); got R1: {[(f.line, f.message[:80]) for f in r1]}"

    def test_decorator_with_mixed_authorised_and_unauthorised_is_inert(self) -> None:
        """``suppresses=("R1", "R3")`` — even a partial match is rejected.

        The closed-set check is all-or-nothing: if ANY entry in the
        tuple is unauthorised, the entire decorator is malformed. We
        cannot silently honour the authorised half because the author's
        intent is structurally unclear (did they mean to write "R5"?
        was "R3" a typo for "R1"?). A blanket malformed-and-inert
        verdict is the conservative call: emit the diagnostic, let the
        author fix the source, and surface every R1/R5 finding the
        valid kwargs would have suppressed.
        """
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1", "R3"),
                invariant="x",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        findings = _findings(source)
        malformed = _findings_by_rule(findings, "R_TB_MALFORMED")
        assert len(malformed) == 1
        assert "R3" in malformed[0].message
        r1 = _findings_by_rule(findings, "R1")
        assert len(r1) == 1, "R1 must fire — decorator is inert"


class TestUnknownTrustBoundaryKwargs:
    """Unknown kwargs are malformed, not ignored documentation."""

    def test_unknown_kwarg_emits_diagnostic_and_makes_decorator_inert(self) -> None:
        source = dedent("""
            from elspeth.contracts import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="arguments",
                suppresses=("R1",),
                invariant="x",
                invarant="typo",
            )
            def handler(arguments):
                return arguments.get("k")
        """)
        findings = _findings(source)
        unknown = _findings_by_rule(findings, "R_TB_UNKNOWN_KWARG")
        assert len(unknown) == 1
        assert "invarant" in unknown[0].message
        assert len(_findings_by_rule(findings, "R1")) == 1
