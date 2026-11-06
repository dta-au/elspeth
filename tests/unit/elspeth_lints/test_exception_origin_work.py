"""Origin work is shared without changing conservative mutation evidence."""

from __future__ import annotations

import ast
import hashlib
from textwrap import dedent

import pytest

from elspeth_lints.rules.trust_tier.tier_model.exception_taxonomy import ExceptionTaxonomy, _Module
from elspeth_lints.rules.trust_tier.tier_model.rule import TierModelVisitor


class _CountingModule(_Module):
    """Count actual assignment transfers, including repeated native work."""

    def __init__(self, source: str) -> None:
        super().__init__("subject.py", "subject", ast.parse(dedent(source)))
        self.assignment_reads = 0

    def _assigned_value(self, node: ast.AST) -> ast.expr | None:
        self.assignment_reads += 1
        return super()._assigned_value(node)


def _last_expression(module: _Module) -> ast.expr:
    statement = module.tree.body[-1]
    assert isinstance(statement, ast.Expr)
    return statement.value


@pytest.mark.parametrize("depth", [0, 4, 8, 12])
def test_repeated_origin_diamond_has_linear_assignment_work(depth: int) -> None:
    module = _CountingModule(
        "\n".join(
            [
                "from types import SimpleNamespace as Imported",
                "v0 = Imported",
                *(f"v{index} = (v{index - 1}, v{index - 1})" for index in range(1, depth + 1)),
            ]
        )
    )
    # Negative control: an import has no assignment transfer to count.
    assert module.possible_mutation_roots(module.root, "Imported") == ({"types.SimpleNamespace"}, False)
    assert module.assignment_reads == 0

    assert module.possible_mutation_roots(module.root, f"v{depth}") == ({"types.SimpleNamespace"}, depth > 0)
    # The instrument must observe the positive chain, while duplicate edges
    # must not multiply the amount of native assignment-transfer work.
    assert depth + 1 <= module.assignment_reads <= 4 * (depth + 1)


def test_cycle_guard_context_keeps_both_suffixed_origin_paths() -> None:
    module = _CountingModule(
        """
        from types import SimpleNamespace as Imported
        a = (b.left, Imported)
        b = a
        result = (a.first, b.second)
        """
    )
    assert module.possible_mutation_roots(module.root, "result") == (
        {"types.SimpleNamespace.first", "types.SimpleNamespace.second"},
        True,
    )


def test_ambiguous_assignments_keep_known_and_unknown_origin_evidence() -> None:
    module = _CountingModule(
        """
        from types import SimpleNamespace as Imported
        alias = Imported
        alias = unknown
        result = (alias, alias)
        """
    )
    assert module.possible_mutation_roots(module.root, "result") == ({"types.SimpleNamespace"}, True)


@pytest.mark.parametrize("blocked", [False, True])
def test_explicit_seen_set_keeps_its_original_unknown_result(blocked: bool) -> None:
    module = _CountingModule("from types import SimpleNamespace as Imported\nImported")
    seen = frozenset({(id(module.root.node), "Imported")}) if blocked else frozenset()
    expected = (set(), True) if blocked else ({"types.SimpleNamespace"}, False)
    assert module.possible_value_origins(module.root, _last_expression(module), seen) == expected


def test_existing_depth_exhaustion_is_still_unknown() -> None:
    module = _CountingModule(
        "\n".join(
            [
                "from types import SimpleNamespace as Imported",
                *(f"v{index} = v{index + 1}" for index in range(80)),
                "v80 = Imported",
            ]
        )
    )
    assert module.possible_mutation_roots(module.root, "v0") == (set(), True)


def test_assignment_origins_keep_the_binding_lexical_scope() -> None:
    module = _CountingModule(
        """
        from types import SimpleNamespace as shared
        global_alias = shared
        def local():
            from builtins import Exception as shared
            result = (shared, global_alias)
        """
    )
    function = module.tree.body[-1]
    assert isinstance(function, ast.FunctionDef)
    assert module.possible_mutation_roots(module.scopes[id(function)], "result") == (
        {"builtins.Exception", "types.SimpleNamespace"},
        True,
    )


def test_later_namespace_and_callable_facts_are_fresh_for_each_query() -> None:
    module = _CountingModule("from types import SimpleNamespace as Imported\nImported")
    value = _last_expression(module)
    assert module.possible_value_origins(module.root, value) == ({"types.SimpleNamespace"}, False)

    module.record_namespace_read(module.root, value)
    assert module.possible_value_origins(module.root, value) == ({f"@namespace:{id(value)}"}, True)

    module.record_namespace_read(module.root, value, callable_kind="globals")
    assert module.possible_value_origins(module.root, value) == ({f"@namespace:{id(value)}.globals"}, True)


def test_later_read_default_and_factory_facts_are_fresh_for_each_query() -> None:
    module = _CountingModule("from types import SimpleNamespace as Imported\nunknown()\nImported")
    statement = module.tree.body[-2]
    assert isinstance(statement, ast.Expr)
    value = statement.value
    module.record_unknown_read(module.root, value)
    assert module.possible_value_origins(module.root, value) == ({f"@unknown-read:{id(value)}"}, True)

    module.record_unknown_read(module.root, value, default=_last_expression(module))
    assert module.possible_value_origins(module.root, value) == (
        {f"@unknown-read:{id(value)}", "types.SimpleNamespace"},
        True,
    )

    module.factory_values[id(value)] = "@factory:selected"
    assert module.possible_value_origins(module.root, value) == ({"@factory:selected"}, True)


def test_later_declared_mutable_fact_reopens_the_parent_binding() -> None:
    module = _CountingModule(
        """
        from types import SimpleNamespace as Imported
        alias = Imported
        def local(alias):
            alias
        """
    )
    function = module.tree.body[-1]
    assert isinstance(function, ast.FunctionDef)
    scope = module.scopes[id(function)]
    assert module.possible_mutation_roots(scope, "alias") == (set(), True)
    module.declared_mutable.add("alias")
    assert module.possible_mutation_roots(scope, "alias") == ({"types.SimpleNamespace"}, True)


@pytest.mark.parametrize("member_first", [False, True])
def test_unknown_subscript_retains_container_origins_only_for_member_writes(member_first: bool) -> None:
    module = _CountingModule("from types import SimpleNamespace as Imported\ndata = [Imported]\ndata[0]")
    value = _last_expression(module)
    module.record_unknown_read(module.root, value)
    marker = f"@unknown-read:{id(value)}"
    for member_write in (member_first, not member_first):
        expected = {marker, "types.SimpleNamespace"} if member_write else {marker}
        assert module.possible_value_origins(module.root, value, member_write=member_write) == (expected, True)


def test_returned_origin_sets_cannot_poison_a_later_query() -> None:
    module = _CountingModule("from types import SimpleNamespace as Imported\na = Imported\nb = (a, a)")
    roots, unknown = module.possible_mutation_roots(module.root, "b")
    assert roots == {"types.SimpleNamespace"}
    assert unknown is True
    roots.clear()
    assert module.possible_mutation_roots(module.root, "b") == ({"types.SimpleNamespace"}, True)


@pytest.mark.parametrize(
    ("body", "refused"),
    [
        ("data = {'error': Refusal}\ndata['note'] = context", False),
        ("data = {'error': Refusal}\ndata['error'].__bases__ = (str,)", True),
        ("data = globals()\ndata['Refusal'] = str", True),
    ],
    ids=["ordinary-element-write", "contained-member-write", "namespace-element-write"],
)
def test_mutation_oppositions_keep_the_original_exception_verdict(body: str, refused: bool) -> None:
    source = "class Refusal(Exception): pass\ndef update(context):\n" + "\n".join(f"    {line}" for line in body.splitlines())
    source += "\ndef check(exc): return isinstance(exc, Refusal)"
    tree = ast.parse(source, filename="subject.py")
    before = ast.dump(tree, include_attributes=True)
    visitor = TierModelVisitor(
        "subject.py",
        source.splitlines(),
        hashlib.sha256(source.encode()).hexdigest(),
        exception_taxonomy=ExceptionTaxonomy({"subject.py": tree}),
    )
    visitor.visit(tree)
    assert bool([finding for finding in visitor.findings if finding.rule_id == "R5"]) is refused
    assert ast.dump(tree, include_attributes=True) == before
