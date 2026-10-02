"""Only ``SandboxedTemplate`` renders a row, and only projected to its node's declaration (elspeth-5887fb7928 S3 W10, ADR-051).

A row value in a template failure is withheld by exactly one renderer,
``SandboxedTemplate``, which renders in the bounded worker with the value-free
undefined type. A Jinja environment built anywhere else, or a
``create_sandboxed_environment()`` without ``value_free=True`` that is handed a
row, would render row data with Jinja's own messages, which quote it. This
whole-tree scan pins the inventory: a new site fails here and must be judged.
"""

from __future__ import annotations

import ast
from collections import Counter
from functools import cache
from pathlib import Path

_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"
_JINJA_MODULES = frozenset({"jinja2", "jinja2.environment", "jinja2.sandbox", "jinja2.nativetypes"})
_JINJA_CONSTRUCTORS = frozenset({"Environment", "SandboxedEnvironment", "ImmutableSandboxedEnvironment", "NativeEnvironment", "Template"})

# Every module that can build a Jinja environment or template itself, and why that is not a row renderer.
_EXPECTED_JINJA_CONSTRUCTOR_IMPORTS = {
    # The bounded environment, the worker and SandboxedTemplate themselves.
    "plugins/infrastructure/templates.py": frozenset({"ImmutableSandboxedEnvironment", "Template"}),
    # Field extraction parses templates; it never renders one.
    "core/templates.py": frozenset({"Environment"}),
    # The S3 key template admits only literal text, run_id and timestamp (AST-checked at compile).
    "plugins/sinks/aws_s3_sink.py": frozenset({"SandboxedEnvironment"}),
}
# Where a SandboxedTemplate is built: the two row renderers, and RAG's config-time compile check.
_EXPECTED_SANDBOXED_TEMPLATE_SITES = Counter(
    {
        "plugins/transforms/llm/templates.py": 1,
        "plugins/transforms/rag/query.py": 1,
        "plugins/transforms/rag/core.py": 1,
    }
)
# create_sandboxed_environment() without value_free=True: config parsing, globals and run-level paths only.
_EXPECTED_MESSAGE_QUOTING_ENVIRONMENTS = Counter(
    {
        "plugins/sinks/azure_blob_sink.py": 2,
        "plugins/transforms/llm/base.py": 2,
        # RetrievalOutputConfig parses the query template for its unbound names: config text only.
        "plugins/transforms/rag/core.py": 1,
        "plugins/sources/llm/config.py": 2,
        "web/composer/state.py": 2,
    }
)
# The only context a path template outside SandboxedTemplate is rendered with.
_RUN_LEVEL_RENDER_NAMES = frozenset({"run_id", "timestamp"})


@cache
def _modules() -> dict[str, ast.Module]:
    from tests.helpers.tree_gate import iter_gate_sources

    return {parsed.path.relative_to(_SRC).as_posix(): parsed.tree for parsed in iter_gate_sources(_SRC)}


def _called_name(call: ast.Call) -> str | None:
    if type(call.func) is ast.Name:
        return call.func.id
    if type(call.func) is ast.Attribute:
        return call.func.attr
    return None


def test_only_the_known_modules_import_a_jinja_environment_or_template_class() -> None:
    found: dict[str, frozenset[str]] = {}
    for name, module in _modules().items():
        imported: set[str] = set()
        for node in ast.walk(module):
            if type(node) is ast.ImportFrom and node.module in _JINJA_MODULES:
                imported.update(alias.name for alias in node.names if alias.name in _JINJA_CONSTRUCTORS)
            elif (
                type(node) is ast.Attribute
                and type(node.value) is ast.Name
                and node.value.id == "jinja2"
                and node.attr in _JINJA_CONSTRUCTORS
            ):
                imported.add(node.attr)
        if imported:
            found[name] = frozenset(imported)
    assert found == _EXPECTED_JINJA_CONSTRUCTOR_IMPORTS


def test_sandboxed_templates_are_built_only_by_the_row_renderers() -> None:
    sites = Counter(
        name
        for name, module in _modules().items()
        if name != "plugins/infrastructure/templates.py"
        for node in ast.walk(module)
        if type(node) is ast.Call and _called_name(node) == "SandboxedTemplate"
    )
    assert sites == _EXPECTED_SANDBOXED_TEMPLATE_SITES


def test_a_message_quoting_environment_never_renders_a_row() -> None:
    """Outside SandboxedTemplate, a template is rendered with run-level names only."""
    modules = _modules()
    quoting: Counter[str] = Counter()
    for name, module in modules.items():
        if name == "plugins/infrastructure/templates.py":
            continue
        for node in ast.walk(module):
            if type(node) is ast.Call and _called_name(node) == "create_sandboxed_environment":
                value_free = [k for k in node.keywords if k.arg == "value_free"]
                if not (len(value_free) == 1 and type(value_free[0].value) is ast.Constant and value_free[0].value.value is True):
                    quoting[name] += 1
    assert quoting == _EXPECTED_MESSAGE_QUOTING_ENVIRONMENTS

    for name in (*_EXPECTED_MESSAGE_QUOTING_ENVIRONMENTS, "plugins/sinks/aws_s3_sink.py"):
        for node in ast.walk(modules[name]):
            if type(node) is ast.Call and _called_name(node) in {"render", "generate", "stream"}:
                assert not node.args, f"{name}:{node.lineno} renders with positional context"
                assert all(k.arg is not None for k in node.keywords), f"{name}:{node.lineno} renders with a ** context"
                keywords = {k.arg for k in node.keywords if k.arg is not None}
                assert keywords <= _RUN_LEVEL_RENDER_NAMES, f"{name}:{node.lineno} renders with {sorted(keywords)}"


# ---------------------------------------------------------------------------
# A row reaches a template only projected to its node's declaration (ADR-051)
# ---------------------------------------------------------------------------

# Where a PipelineRow becomes a TemplateRow, and where a node's declaration is
# read as a projection: the LLM prompt (single and multi-query) and the RAG
# query template, whose ``RetrievalOutputConfig.query_template_row_projection``
# is read by both its configuration checks and ``QueryBuilder``.
_EXPECTED_PROJECTION_SITES = Counter(
    {
        "plugins/transforms/llm/transform.py": 1,
        "plugins/transforms/llm/multi_query.py": 1,
        "plugins/transforms/rag/query.py": 1,
    }
)
_EXPECTED_DECLARATION_READS = Counter(
    {
        "plugins/transforms/llm/transform.py": 1,
        "plugins/transforms/rag/core.py": 1,
    }
)
# The calls that hand a row to a template.
_ROW_RENDER_CALLS = frozenset({"render", "render_with_metadata", "build_template_context"})


def _passes_a_to_dict(call: ast.Call) -> bool:
    """Whether any argument of ``call`` contains a ``<x>.to_dict()`` call: the whole row as a plain dict."""
    arguments: list[ast.AST] = [*call.args, *(keyword.value for keyword in call.keywords)]
    return any(
        type(node) is ast.Call and type(node.func) is ast.Attribute and node.func.attr == "to_dict"
        for argument in arguments
        for node in ast.walk(argument)
    )


def _unprojected_render_sites(modules: dict[str, ast.Module]) -> list[str]:
    return [
        f"{name}:{node.lineno}"
        for name, module in modules.items()
        for node in ast.walk(module)
        if type(node) is ast.Call and _called_name(node) in _ROW_RENDER_CALLS and _passes_a_to_dict(node)
    ]


def test_no_render_is_handed_a_whole_row_as_a_dict() -> None:
    """``to_dict()`` into a render bypasses the projection: the packer refuses a PipelineRow, not a plain dict."""
    assert _unprojected_render_sites(_modules()) == []


def test_the_unprojected_render_scan_finds_a_to_dict_argument() -> None:
    """Positive control for the scan above: each render call shape it must catch."""
    probe = ast.parse(
        "t.render(row=row.to_dict())\n"
        "t.render_with_metadata(row.to_dict(), contract=c)\n"
        "spec.build_template_context(dict(row.to_dict()), p)\n"
        "t.render(row=TemplateRow.project(row, p))\n"
    )
    assert _unprojected_render_sites({"probe.py": probe}) == ["probe.py:1", "probe.py:2", "probe.py:3"]


def test_rows_are_projected_only_where_a_node_declaration_is_read() -> None:
    modules = _modules()
    projections = Counter(
        name
        for name, module in modules.items()
        if name != "plugins/infrastructure/templates.py"
        for node in ast.walk(module)
        if type(node) is ast.Call
        and type(node.func) is ast.Attribute
        and node.func.attr == "project"
        and type(node.func.value) is ast.Name
        and node.func.value.id == "TemplateRow"
    )
    declarations = Counter(
        name
        for name, module in modules.items()
        if name != "plugins/infrastructure/templates.py"
        for node in ast.walk(module)
        if type(node) is ast.Call and _called_name(node) == "declared_row_projection"
    )
    assert projections == _EXPECTED_PROJECTION_SITES
    assert declarations == _EXPECTED_DECLARATION_READS
