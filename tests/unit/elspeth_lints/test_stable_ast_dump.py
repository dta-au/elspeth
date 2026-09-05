"""``stable_ast_dump`` renders Python 3.13's default ``ast.dump`` shape on every interpreter.

Every AST fingerprint the gates pin was generated on the 3.13 development
runtime, whose :func:`ast.dump` omits empty-list fields and ``None`` optional
fields. On 3.12 the same source dumps with ``posonlyargs=[]``, ``defaults=[]``,
``decorator_list=[]`` and so on, so a gate calling :func:`ast.dump` directly
reports every pinned inventory as drift there (elspeth-b4f1be3f80). Two pins:

* the exact 3.13-shape string for a sample that exercises each omission rule,
  asserted on EVERY interpreter — removing either omission rule from the port
  changes this string;
* on 3.13 itself, byte-equality with :func:`ast.dump` over every Python file
  in the tree, in both annotate modes — the proof that no pinned fingerprint
  moves when a gate switches to the helper.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from elspeth_lints.core.ast_dump import stable_ast_dump

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_ROOTS = ("src", "elspeth-lints/src", "tests")

SAMPLE = 'def f(a, /, b, *args, c=None, **kw) -> None:\n    """doc"""\n    return g(a, key=b, **kw)\n\n\nclass C:\n    x: int\n\n\nmatch v:\n    case None:\n        pass\n    case [1, *rest]:\n        pass\n'

# ``ast.dump(ast.parse(SAMPLE), annotate_fields=True, include_attributes=False)`` on CPython 3.13.
ANNOTATED_3_13_SHAPE = "Module(body=[FunctionDef(name='f', args=arguments(posonlyargs=[arg(arg='a')], args=[arg(arg='b')], vararg=arg(arg='args'), kwonlyargs=[arg(arg='c')], kw_defaults=[Constant(value=None)], kwarg=arg(arg='kw')), body=[Expr(value=Constant(value='doc')), Return(value=Call(func=Name(id='g', ctx=Load()), args=[Name(id='a', ctx=Load())], keywords=[keyword(arg='key', value=Name(id='b', ctx=Load())), keyword(value=Name(id='kw', ctx=Load()))]))], returns=Constant(value=None)), ClassDef(name='C', body=[AnnAssign(target=Name(id='x', ctx=Store()), annotation=Name(id='int', ctx=Load()), simple=1)]), Match(subject=Name(id='v', ctx=Load()), cases=[match_case(pattern=MatchSingleton(value=None), body=[Pass()]), match_case(pattern=MatchSequence(patterns=[MatchValue(value=Constant(value=1)), MatchStar(name='rest')]), body=[Pass()])])])"

# ``ast.dump(ast.parse(SAMPLE), annotate_fields=False, include_attributes=False)`` on CPython 3.13.
POSITIONAL_3_13_SHAPE = "Module([FunctionDef('f', arguments([arg('a')], [arg('b')], arg('args'), [arg('c')], [Constant(None)], arg('kw')), [Expr(Constant('doc')), Return(Call(Name('g', Load()), [Name('a', Load())], [keyword('key', Name('b', Load())), keyword(value=Name('kw', Load()))]))], [], Constant(None)), ClassDef('C', [], [], [AnnAssign(Name('x', Store()), Name('int', Load()), simple=1)]), Match(Name('v', Load()), [match_case(MatchSingleton(None), body=[Pass()]), match_case(MatchSequence([MatchValue(Constant(1)), MatchStar('rest')]), body=[Pass()])])])"


def test_sample_dumps_in_the_3_13_shape_on_this_interpreter() -> None:
    tree = ast.parse(SAMPLE)
    assert stable_ast_dump(tree) == ANNOTATED_3_13_SHAPE
    assert stable_ast_dump(tree, annotate_fields=False) == POSITIONAL_3_13_SHAPE


def test_sample_shape_omits_empty_lists_and_optional_nones_but_keeps_none_values() -> None:
    assert "posonlyargs=[]" not in ANNOTATED_3_13_SHAPE
    assert "decorator_list=[]" not in ANNOTATED_3_13_SHAPE
    assert "type_comment=None" not in ANNOTATED_3_13_SHAPE
    assert "keyword(value=Name(id='kw', ctx=Load()))" in ANNOTATED_3_13_SHAPE  # ``**kw``: arg=None omitted
    assert "kw_defaults=[Constant(value=None)]" in ANNOTATED_3_13_SHAPE  # a None VALUE stays
    assert "MatchSingleton(value=None)" in ANNOTATED_3_13_SHAPE


def test_rejects_non_ast_input() -> None:
    with pytest.raises(TypeError, match="expected AST, got str"):
        stable_ast_dump("not a tree")  # type: ignore[arg-type]


@pytest.mark.skipif(sys.version_info[:2] != (3, 13), reason="the pinned shape IS CPython 3.13's ast.dump; equality is the proof only there")
def test_matches_ast_dump_over_the_whole_tree_on_3_13() -> None:
    mismatches: list[str] = []
    checked = 0
    for root in CORPUS_ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except SyntaxError:
                continue
            checked += 1
            for annotate_fields in (True, False):
                if stable_ast_dump(tree, annotate_fields=annotate_fields) != ast.dump(
                    tree, annotate_fields=annotate_fields, include_attributes=False
                ):
                    mismatches.append(f"{path.relative_to(REPO_ROOT)} annotate_fields={annotate_fields}")
    assert checked > 1000, checked
    assert mismatches == []
