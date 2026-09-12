"""Walker helpers shared by the teaching gates and standalone argument census.

``test_planner_teaching_gate.py`` (repair-feedback fact keys, elspeth-68721c71d7)
and ``test_tool_result_envelope_gate.py`` (the ToolResult wire envelope,
elspeth-e405ad7cd2) derive their shipped and taught sides with the same
primitives. Argument teaching also uses the ownership reader below. They live here so a walker fix lands in the gates at once instead
of drifting between two copies (prior-art rule: one authority per helper).
"""

from __future__ import annotations

import ast
import re
import typing
from pathlib import Path
from typing import NamedTuple

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


def leaf_of(key: str) -> str:
    """The last segment of a dotted key, without its list marker: what a reader sees quoted.

    One authority, because two checks depend on the SAME derivation: whether a
    key is taught (``is_quoted_leaf``) and whether it is a homonym of another
    surface's key (the envelope gate's homonym refusal). A second copy of this
    line would let the refusal and the admission disagree about which word the
    corpus was matched on.
    """
    return key.split(".")[-1].replace("[]", "")


def is_quoted_leaf(key: str, text: str) -> bool:
    """The key's leaf appears in house-style quoted form (``'leaf'`` or ```leaf```) in ``text``.

    Quoted or backticked only: a bare-word match let ordinary prose ("the
    consumer node", "a field carried by") count as teaching ``consumer`` or
    ``field``, so deleting the deliberate teaching of a common-word key left
    the gate green (red-team finding on bc8b9e237).
    """
    return re.search(rf"['`]{re.escape(leaf_of(key))}['`]", text) is not None


class TeachingScope(NamedTuple):
    surface: str
    tool: str
    path: str
    row: str = ""


class TeachingBlock(NamedTuple):
    text: str
    site: str
    scopes: tuple[TeachingScope, ...]
    implicit_tool: str | None


def teaching_blocks(text: str, tools: frozenset[str], *, site: str, description_owner: str | None = None) -> list[TeachingBlock]:
    """Read ownership before quoted leaves, without heading or previous-tool inheritance.

    Balanced ``<!-- taught:begin surface tool path; ... -->`` / ``taught:end``
    comments scope precise passages. Paths are exact, or ``path.*`` for one
    record's descendants. Markers have only gate meaning: this reader never
    rewrites the rendered prompt. Unmarked fenced code has no implicit owner.
    An explicit response scope never teaches an argument, even in its own tool
    description. Argument scopes also declare names for the stale-name census.
    """
    blocks: list[TeachingBlock] = []
    scopes: tuple[TeachingScope, ...] = ()
    pending: list[str] = []
    start = 1
    fence: str | None = None
    scope_start = 0

    def flush(*, code: bool = False) -> None:
        if not pending:
            return
        content = "\n".join(pending)
        mentioned = {tool for tool in tools if is_quoted_leaf(tool, content)}
        owner = description_owner if description_owner is not None else next(iter(mentioned)) if len(mentioned) == 1 else None
        blocks.append(TeachingBlock(content, f"{site}:{start}", scopes, None if code else owner))
        pending.clear()

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if fence is not None:
            pending.append(line)
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + ",}\\s*", stripped):
                flush(code=True)
                fence = None
            continue
        if "taught:" in line:
            flush()
            if stripped == "<!-- taught:end -->":
                if not scopes:
                    raise ValueError(f"{site}:{lineno}: unmatched teaching end marker")
                for scope in scopes:
                    if scope.row and not any(_scope_row_matches(scope, block.text) for block in blocks[scope_start:]):
                        raise ValueError(f"{site}:{lineno}: stale teaching table-row selector {scope.row}")
                scopes = ()
                continue
            match = re.fullmatch(r"<!-- taught:begin (.+) -->", stripped)
            if match is None or scopes:
                raise ValueError(f"{site}:{lineno}: malformed or nested teaching scope")
            parsed: list[TeachingScope] = []
            for declaration in match[1].split(";"):
                fields = declaration.split()
                if len(fields) not in (3, 4):
                    raise ValueError(f"{site}:{lineno}: expected surface tool path")
                surface, tool, path = fields[:3]
                row = ""
                if len(fields) == 4:
                    if not re.fullmatch(r"row=[a-z_]+", fields[3]):
                        raise ValueError(f"{site}:{lineno}: malformed teaching table-row selector")
                    row = fields[3].removeprefix("row=")
                if tool != "*" and tool not in tools:
                    raise ValueError(f"{site}:{lineno}: stale teaching owner {tool}")
                if surface in ("argument", "tool-data") and tool == "*":
                    raise ValueError(f"{site}:{lineno}: tool-specific surface requires a named owner")
                if surface == "tool-data" and path == "data.*":
                    raise ValueError(f"{site}:{lineno}: tool-data requires a specific field or record")
                if not re.fullmatch(r"[a-z][a-z-]*", surface) or not re.fullmatch(
                    r"[\w<>/.-]+(?:\[\])?(?:\.[\w<>/-]+(?:\[\])?)*(?:\.\*)?", path
                ):
                    raise ValueError(f"{site}:{lineno}: malformed teaching surface/path")
                if surface == "argument" and not path.isidentifier():
                    raise ValueError(f"{site}:{lineno}: argument declarations require an exact top-level name")
                parsed.append(TeachingScope(surface, tool, path, row))
            if len(set(parsed)) != len(parsed):
                raise ValueError(f"{site}:{lineno}: duplicate teaching scope")
            scopes = tuple(parsed)
            scope_start = len(blocks)
            # Retain even an empty declaration so staleness cannot hide behind
            # a missing prose block. Empty text teaches nothing.
            blocks.append(TeachingBlock("", f"{site}:{lineno}", scopes, None))
            continue
        opening = re.match(r"^(`{3,}|~{3,})", stripped)
        if opening:
            flush()
            start = lineno
            fence = opening[1]
            pending.append(line)
        elif not stripped or stripped.startswith("#") or stripped.startswith("<!--"):
            flush()
        elif stripped.startswith("|"):
            flush()
            start = lineno
            pending.append(line)
            flush()
        else:
            if not pending:
                start = lineno
            pending.append(line)
    if fence is not None:
        raise ValueError(f"{site}: unterminated code fence")
    flush()
    if scopes:
        raise ValueError(f"{site}: unclosed teaching scope")
    return blocks


def _scope_row_matches(scope: TeachingScope, text: str) -> bool:
    return not scope.row or re.match(rf"^\|\s*{re.escape(scope.row)}\s*\|", text) is not None


def scope_matches(scope: TeachingScope, surface: str, tool: str, key: str) -> bool:
    if (scope.surface, scope.tool) != (surface, tool):
        return False
    return key.startswith(scope.path[:-1]) if scope.path.endswith(".*") else key == scope.path


def validate_teaching_scopes(blocks: list[TeachingBlock], shipped: frozenset[tuple[str, str, str]]) -> None:
    """Refuse stale response declarations against the actual shipped census.

    Arguments are checked separately as stale declarations, not parser errors.
    A descendant scope must identify a prefix with a live descendant. Some
    censused nested records have no separate container row.
    """
    for block in blocks:
        for scope in block.scopes:
            if scope.surface == "argument":
                continue
            if not any(scope_matches(scope, *row) for row in shipped):
                raise ValueError(f"{block.site}: stale teaching scope {scope}")


def owned_teaching_text(blocks: list[TeachingBlock], surface: str, tool: str, key: str) -> str:
    """Select only the requested context; shared and canonical prose cannot leak."""
    return "\n".join(
        block.text
        for block in blocks
        if (
            any(scope_matches(scope, surface, tool, key) and _scope_row_matches(scope, block.text) for scope in block.scopes)
            if block.scopes
            else tool != "*" and surface in ("argument", "tool-data") and block.implicit_tool == tool
        )
    )


def argument_teaching(
    definitions: list[dict[str, typing.Any]], prompt: str
) -> dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]]:
    """Return shipped, taught and explicitly declared argument names per live tool.

    General quoted words never produce stale arguments. Property descriptions
    teach only their exact top-level argument, not a sibling or nested homonym.
    """
    tools = frozenset(definition["name"] for definition in definitions)
    if len(tools) != len(definitions):
        raise ValueError("duplicate teaching tool names")
    blocks = teaching_blocks(prompt, tools, site="system-prompt")
    for definition in definitions:
        blocks.extend(
            teaching_blocks(
                definition["description"], tools, site=f"description:{definition['name']}", description_owner=definition["name"]
            )
        )
    result: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {}
    for definition in definitions:
        tool = definition["name"]
        properties = definition["parameters"]["properties"]
        taught: set[str] = set()
        for key, schema in properties.items():
            description = schema.get("description")
            if (isinstance(description, str) and description.strip()) or is_quoted_leaf(
                key, owned_teaching_text(blocks, "argument", tool, key)
            ):
                taught.add(key)
        declared = frozenset(scope.path for block in blocks for scope in block.scopes if (scope.surface, scope.tool) == ("argument", tool))
        result[tool] = frozenset(properties), frozenset(taught), declared
    return result
