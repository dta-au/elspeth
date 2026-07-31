"""Regression tests for shared trust-boundary honesty-rule helpers."""

from __future__ import annotations

import ast
import hashlib
import textwrap
from pathlib import Path
from typing import get_args

import pytest

from elspeth.contracts.trust_boundary import BoundaryRule
from elspeth_lints.core.boundary_aliases import evaluate_alias_flow
from elspeth_lints.rules.trust_boundary.shared import iter_trust_boundary_decorators, make_decorator_finding
from elspeth_lints.rules.trust_boundary.tier.metadata import RULE_METADATA
from elspeth_lints.rules.trust_tier.tier_model.trust_boundary_suppress import _ALLOWED_BOUNDARY_RULES


def _recognized_names(source: str) -> list[str]:
    tree = ast.parse(textwrap.dedent(source))
    return [func.name for func, _call in iter_trust_boundary_decorators(tree)]


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
    loop = f"{header}\n{textwrap.indent(textwrap.dedent(body).strip(), '    ')}"
    if orelse is not None:
        loop += f"\nelse:\n{textwrap.indent(textwrap.dedent(orelse).strip(), '    ')}"
    outer_body = f"{initial_import}\n\n{loop}\n\n{textwrap.dedent(_NESTED_OBSERVATION_HANDLER).strip()}"
    return f"async def outer(items, enabled, stop):\n{textwrap.indent(outer_body, '    ')}\n"


def _with_suite(with_kind: str, body: str, *, items: str = "suppressing_context()") -> str:
    return f"{with_kind} {items}:\n{textwrap.indent(textwrap.dedent(body).strip(), '    ')}"


def _boundary_in_finally_source(transfer: str) -> str:
    try_body = f"""
        from foreign import observation_boundary
        {transfer}
        from elspeth.contracts.trust_boundary import observation_boundary
    """
    try_suite = f"try:\n{textwrap.indent(textwrap.dedent(try_body).strip(), '    ')}\nfinally:\n{textwrap.indent(textwrap.dedent(_NESTED_OBSERVATION_HANDLER).strip(), '    ')}"
    if transfer in {"break", "continue"}:
        return f"async def outer(items):\n    for item in items:\n{textwrap.indent(try_suite, '        ')}\n"
    return f"def outer():\n{textwrap.indent(try_suite, '    ')}\n"


def test_make_decorator_finding_uses_single_fingerprint_shape() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from elspeth.contracts.trust_boundary import trust_boundary

            @trust_boundary(
                tier=2,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def handler(data):
                return data
            """
        )
    )
    _func, call = next(iter_trust_boundary_decorators(tree))

    finding = make_decorator_finding(
        metadata=RULE_METADATA,
        rule_id="TBT1",
        file_path="example.py",
        call=call,
        message="tier must be 3",
        suggestion="use tier=3",
    )

    expected = hashlib.sha256(f"TBT1|example.py|{call.lineno}|{call.col_offset}".encode()).hexdigest()[:16]
    assert finding.fingerprint == expected
    assert finding.line == call.lineno
    assert finding.column == call.col_offset
    assert finding.severity == RULE_METADATA.severity


def test_honesty_rules_do_not_reintroduce_private_make_finding_helpers() -> None:
    repo_root = Path(__file__).resolve().parents[5]
    rule_root = repo_root / "elspeth-lints/src/elspeth_lints/rules/trust_boundary"

    for relative in ("scope/rule.py", "tier/rule.py", "tests/rule.py"):
        text = (rule_root / relative).read_text(encoding="utf-8")
        assert "def _make_finding(" not in text, relative
        assert "make_decorator_finding(" in text, relative


def test_runtime_boundary_rule_literal_matches_analyzer_allowlist() -> None:
    """The runtime decorator and analyzer must agree on suppressible rule IDs."""
    runtime_rules = frozenset(get_args(BoundaryRule.__value__))
    assert runtime_rules == _ALLOWED_BOUNDARY_RULES


def test_iter_trust_boundary_decorators_ignores_unrelated_attribute_decorator() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            import other

            @other.trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def handler(data):
                return data
            """
        )
    )

    assert list(iter_trust_boundary_decorators(tree)) == []


def test_iter_trust_boundary_decorators_ignores_unrelated_bare_import() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from other import trust_boundary

            @trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def handler(data):
                return data
            """
        )
    )

    assert list(iter_trust_boundary_decorators(tree)) == []


def test_iter_trust_boundary_decorators_accepts_elspeth_import_surfaces() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from elspeth.contracts.trust_boundary import trust_boundary
            import elspeth.contracts as contracts
            import elspeth.contracts.trust_boundary as tb_mod

            @trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def bare(data):
                return data

            @contracts.trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def contracts_alias(data):
                return data

            @tb_mod.trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def module_alias(data):
                return data
            """
        )
    )

    names = [func.name for func, _call in iter_trust_boundary_decorators(tree)]
    assert names == ["bare", "contracts_alias", "module_alias"]


def test_iter_trust_boundary_decorators_preserves_root_binding_for_dotted_import() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            import elspeth.contracts.trust_boundary
            import elspeth.web

            @elspeth.contracts.trust_boundary(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="y",
            )
            def handler(data):
                return data
            """
        )
    )

    names = [func.name for func, _call in iter_trust_boundary_decorators(tree)]
    assert names == ["handler"]


def test_iter_trust_boundary_decorators_accepts_observation_boundary_alias() -> None:
    """The honesty gates recognize the dedicated non-raising marker through imports."""
    tree = ast.parse(
        textwrap.dedent(
            """
            from elspeth.contracts.trust_boundary import observation_boundary as observes

            @observes(
                tier=3,
                source="x",
                source_param="data",
                suppresses=("R1",),
                invariant="returns None on absence",
            )
            def handler(data):
                return data.get("value")
            """
        )
    )

    names = [func.name for func, _call in iter_trust_boundary_decorators(tree)]
    assert names == ["handler"]


def test_class_local_boundary_import_does_not_leak_into_method_body() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            class Container:
                from elspeth.contracts.trust_boundary import observation_boundary

                def method(self, data):
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="payload",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def nested(payload):
                        return payload.get("value")
                    return nested(data)
            """
        )
    )

    assert list(iter_trust_boundary_decorators(tree)) == []


def test_class_local_foreign_import_does_not_hide_module_boundary_inside_method() -> None:
    tree = ast.parse(
        textwrap.dedent(
            """
            from elspeth.contracts.trust_boundary import observation_boundary

            class Container:
                from foreign import observation_boundary

                def method(self, data):
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="payload",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def nested(payload):
                        return payload.get("value")
                    return nested(data)
            """
        )
    )

    names = [func.name for func, _call in iter_trust_boundary_decorators(tree)]
    assert names == ["nested"]


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
        "match subject:\n    case observation_boundary:\n        pass",
        "type observation_boundary = object",
        "del observation_boundary",
        "from foreign import *",
    ],
)
def test_ordinary_module_bindings_invalidate_imported_boundary_marker(binding: str) -> None:
    source = "from elspeth.contracts.trust_boundary import observation_boundary\n\n"
    source += f"{binding}\n\n"
    source += textwrap.dedent("""
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

    assert _recognized_names(source) == []


def test_function_parameter_shadows_imported_fully_qualified_marker() -> None:
    assert (
        _recognized_names("""
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
        """)
        == []
    )


def test_relative_import_does_not_prove_canonical_boundary_marker() -> None:
    assert (
        _recognized_names("""
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
        == []
    )


def test_assignment_cannot_forge_fully_qualified_marker() -> None:
    assert (
        _recognized_names("""
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
        == []
    )


def test_optional_if_import_does_not_grant_post_branch_honesty() -> None:
    assert (
        _recognized_names("""
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
        == []
    )


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_optional_loop_import_does_not_grant_post_loop_honesty(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_optional_loop_foreign_rebinding_invalidates_post_loop_honesty(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="from foreign import observation_boundary",
    )

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_else_can_prove_canonical_marker_on_every_normal_exit(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body="from elspeth.contracts.trust_boundary import observation_boundary",
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_else_does_not_hide_foreign_break_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_else_and_break_can_agree_on_canonical_marker(loop_kind: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_continue_path_cannot_hide_foreign_rebinding(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_finally_rebinding_applies_to_break_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_canonical_finally_applies_to_break_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_finally_rebinding_applies_to_continue_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_loop_canonical_finally_applies_to_continue_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize(
    ("outer_finally_import", "expected"),
    [
        ("from foreign import observation_boundary", []),
        ("from elspeth.contracts.trust_boundary import observation_boundary", ["handler"]),
    ],
)
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_nested_loop_finally_blocks_apply_in_execution_order(
    loop_kind: str,
    outer_finally_import: str,
    expected: list[str],
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

    assert _recognized_names(source) == expected


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_finally_break_replaces_pending_continue_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_finally_continue_replaces_pending_break_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("pending_exit", ["return None", "raise RuntimeError"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_finally_break_replaces_non_loop_abrupt_path(loop_kind: str, pending_exit: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("final_exit", ["return None", "raise RuntimeError"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_finally_non_loop_abrupt_path_replaces_pending_break(loop_kind: str, final_exit: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_nested_finally_replaces_transfer_kind_inner_to_outer(loop_kind: str) -> None:
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

    assert _recognized_names(source) == ["handler"]


def test_finally_alias_analysis_does_not_duplicate_primary_matches() -> None:
    source = """
        from elspeth.contracts.trust_boundary import observation_boundary

        async def outer(items):
            for item in items:
                try:
                    break
                finally:
                    @observation_boundary(
                        tier=3,
                        source="x",
                        source_param="data",
                        suppresses=("R1",),
                        invariant="returns None on absence",
                    )
                    def inside(data):
                        return data.get("x")
    """

    assert _recognized_names(source) == ["inside"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_finally_break_preserves_conservative_implicit_exception_path(loop_kind: str) -> None:
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_with_suppressed_explicit_exception_reaches_break_with_exception_aliases(
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_with_suppressed_implicit_exception_reaches_break_with_exception_aliases(
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

    assert _recognized_names(source) == []


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_with_canonical_exception_path_ignores_unreachable_foreign_rebinding(
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

    assert _recognized_names(source) == ["handler"]


@pytest.mark.parametrize("with_kind", ["with", "async with"])
def test_alias_flow_with_retains_propagation_and_possible_suppression(with_kind: str) -> None:
    source = f"async def outer():\n{textwrap.indent(_with_suite(with_kind, 'raise RuntimeError'), '    ')}\n"
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.AsyncFunctionDef)

    paths = evaluate_alias_flow(function.body, {"observation_boundary": "foreign.observation_boundary"})

    assert {path.transfer for path in paths} == {None, "raise"}
    assert all(path.aliases["observation_boundary"] == "foreign.observation_boundary" for path in paths)


@pytest.mark.parametrize("with_kind", ["with", "async with"])
def test_alias_flow_later_enter_failure_preserves_prior_optional_target_mutation(with_kind: str) -> None:
    source = f"async def outer():\n{textwrap.indent(_with_suite(with_kind, 'pass', items='first() as observation_boundary, second()'), '    ')}\n"
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.AsyncFunctionDef)

    paths = evaluate_alias_flow(
        function.body,
        {"observation_boundary": "elspeth.contracts.trust_boundary.observation_boundary"},
    )

    normal_paths = [path for path in paths if path.transfer is None]
    assert normal_paths
    assert all("observation_boundary" not in path.aliases for path in normal_paths)


@pytest.mark.parametrize("with_kind", ["with", "async with"])
def test_alias_flow_later_enter_failure_keeps_unmodified_canonical_alias(with_kind: str) -> None:
    source = f"async def outer():\n{textwrap.indent(_with_suite(with_kind, 'pass', items='first(), second()'), '    ')}\n"
    function = ast.parse(source).body[0]
    assert isinstance(function, ast.AsyncFunctionDef)
    canonical = "elspeth.contracts.trust_boundary.observation_boundary"

    paths = evaluate_alias_flow(function.body, {"observation_boundary": canonical})

    normal_paths = [path for path in paths if path.transfer is None]
    assert normal_paths
    assert all(path.aliases["observation_boundary"] == canonical for path in normal_paths)


@pytest.mark.parametrize("transfer", ["break", "continue", "return None", "raise RuntimeError"])
def test_finally_decorator_uses_reachable_pre_finally_aliases(transfer: str) -> None:
    assert _recognized_names(_boundary_in_finally_source(transfer)) == []


def test_finally_decorator_uses_implicit_exception_aliases_before_unreachable_repair() -> None:
    source = f"""
        def outer(exported):
            try:
                from foreign import observation_boundary
                might_raise()
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
{textwrap.indent(textwrap.dedent(_NESTED_OBSERVATION_HANDLER).strip(), "                ")}
                exported.append(handler)
                return handler
    """

    assert _recognized_names(source) == []


def test_alias_flow_normalizes_independent_branches_by_transfer_kind() -> None:
    names = [f"alias_{index}" for index in range(18)]
    source = "\n".join(f"if condition_{index}:\n    {name} = replacement" for index, name in enumerate(names))
    statements = ast.parse(source).body

    paths = evaluate_alias_flow(statements, {name: f"canonical.{name}" for name in names})

    assert len(paths) == 1
    assert paths[0].transfer is None
    assert paths[0].aliases == {}


def test_alias_flow_normalization_preserves_transfer_alias_correlation() -> None:
    source = """
        while enabled:
            if break_path:
                from elspeth.contracts.trust_boundary import observation_boundary
                break
            if continue_path:
                from foreign import observation_boundary
                continue
            if return_path:
                from elspeth.contracts.trust_boundary import observation_boundary
                return None
            raise RuntimeError
    """
    loop = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(loop, ast.While)

    paths = evaluate_alias_flow(loop.body, {"observation_boundary": "seed.observation_boundary"})

    assert {path.transfer: path.aliases["observation_boundary"] for path in paths} == {
        "break": "elspeth.contracts.trust_boundary.observation_boundary",
        "continue": "foreign.observation_boundary",
        "return": "elspeth.contracts.trust_boundary.observation_boundary",
        "raise": "seed.observation_boundary",
    }


def test_try_conflicting_imports_do_not_grant_post_flow_honesty() -> None:
    assert (
        _recognized_names("""
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
        == []
    )


def test_identical_canonical_imports_on_all_if_paths_retain_honesty() -> None:
    assert _recognized_names("""
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
    """) == ["handler"]
