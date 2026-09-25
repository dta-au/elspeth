"""Only ``SandboxedTemplate`` renders a row (elspeth-5887fb7928 S3, W10).

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
        "plugins/transforms/llm/base.py": 3,
        "plugins/sources/llm/config.py": 2,
        "web/composer/state.py": 2,
    }
)
# The only context a path template outside SandboxedTemplate is rendered with.
_RUN_LEVEL_RENDER_NAMES = frozenset({"run_id", "timestamp"})


@cache
def _modules() -> dict[str, ast.Module]:
    return {path.relative_to(_SRC).as_posix(): ast.parse(path.read_text(encoding="utf-8")) for path in sorted(_SRC.rglob("*.py"))}


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
