"""Walker helpers shared by the two teaching gates.

``test_planner_teaching_gate.py`` (repair-feedback fact keys, elspeth-68721c71d7)
and ``test_tool_result_envelope_gate.py`` (the ToolResult wire envelope,
elspeth-e405ad7cd2) derive their shipped and taught sides with the same
primitives. They live here so a walker fix lands in both gates at once instead
of drifting between two copies (prior-art rule: one authority per helper).
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path

from elspeth_lints.core.ast_walker import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[4]
# The whole web package, not only the composer: ``ValidationEntry`` is also
# constructed in web/plugin_policy and web/sessions (detail-free today), and a
# detail added there would otherwise be outside the walk (final red-team).
WEB_SRC = REPO_ROOT / "src" / "elspeth" / "web"


def _typed_keys(payload: type, prefix: str) -> list[str]:
    """Flatten a TypedDict to dotted key paths, recursing through nested and list-of TypedDicts."""
    keys: list[str] = []
    for name, hint in typing.get_type_hints(payload, include_extras=False).items():
        path = f"{prefix}{name}"
        keys.append(path)
        args = typing.get_args(hint)
        if typing.is_typeddict(hint):
            keys.extend(_typed_keys(hint, path + "."))
        elif typing.get_origin(hint) is list and args and typing.is_typeddict(args[0]):
            keys.extend(_typed_keys(args[0], path + "[]."))
        else:
            # ``X | None`` around a record: recurse through the record member.
            for arg in args:
                if typing.is_typeddict(arg):
                    keys.extend(_typed_keys(arg, path + "."))
    return keys


def _is_cast(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _call_name(node) == "cast"


def _is_dict_literal(node: ast.AST) -> bool:
    """A dict built inline: a literal, a comprehension, or a ``dict(...)`` call."""
    if isinstance(node, ast.Dict | ast.DictComp):
        return True
    return isinstance(node, ast.Call) and _call_name(node) == "dict"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and type(node.value) is str else None


def _enclosing_function(tree: ast.Module, lineno: int) -> str | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= lineno <= (node.end_lineno or lineno) and (best is None or node.lineno > best.lineno):
            best = node
    return None if best is None else best.name


def composer_python_files() -> list[Path]:
    return list(iter_python_files(WEB_SRC))


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def is_quoted_leaf(key: str, text: str) -> bool:
    """The key's leaf appears in house-style quoted form (``'leaf'`` or ```leaf```) in ``text``.

    Quoted or backticked only: a bare-word match let ordinary prose ("the
    consumer node", "a field carried by") count as teaching ``consumer`` or
    ``field``, so deleting the deliberate teaching of a common-word key left
    the gate green (red-team finding on bc8b9e237).
    """
    leaf = key.split(".")[-1].replace("[]", "")
    return re.search(rf"['`]{re.escape(leaf)}['`]", text) is not None
