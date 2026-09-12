"""Composer wire census: derive, per tool, what each wire carries.

Every side is read from the live registry, the live manifest, or the AST of the
handler modules. Nothing here is a regex over source text (AGENTS.md).
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from elspeth.web.composer import redaction
from elspeth.web.composer.tools._dispatch import get_tool_definitions

TOOLS_ROOT = Path(__file__).resolve().parents[2] / "src" / "elspeth" / "web" / "composer" / "tools"
VALIDATOR_NAMES = frozenset({"_validate_mutation_arguments", "_validate_arguments"})
# Both handler spellings the dispatch registry binds. ``_handle_*`` is not a
# second-class form: ``validate_secret_ref`` is validated only there, and a
# census blind to it reports a validated tool as having no model at all.
HANDLER_PREFIXES: Final[tuple[str, ...]] = ("_execute_", "_handle_")


@dataclass(frozen=True)
class ModelWireRow:
    tool: str
    shipped: frozenset[str]
    model_class: str | None
    model_fields: frozenset[str]
    site: str


def _shipped() -> dict[str, frozenset[str]]:
    return {d["name"]: frozenset((d.get("parameters") or {}).get("properties", {})) for d in get_tool_definitions()}


def _validated_model_name(call: ast.Call, *, path: Path) -> str | None:
    """The arguments-model class name this call validates against, or None.

    Two call shapes admit a model, and both must name it as a bare ``Name`` so
    the census can import the class it reports:

    * ``_validate_mutation_arguments(<Model>, ...)`` / ``_validate_arguments(<Model>, ...)``
    * ``<Model>.model_validate(...)``

    Either shape written any other way (a subscript, an attribute chain, a
    call result) is a finding, not a row: raise, because the alternative is
    reporting a validated tool as unvalidated, and a gate reading this census
    would then pass over the very gap it exists to catch.
    """
    func = call.func
    if isinstance(func, ast.Name) and func.id in VALIDATOR_NAMES:
        if not call.args or not isinstance(call.args[0], ast.Name):
            raise RuntimeError(f"{path.name}:{call.lineno}: {func.id} called without a bare model Name")
        return call.args[0].id
    if isinstance(func, ast.Attribute) and func.attr == "model_validate":
        if not isinstance(func.value, ast.Name):
            raise RuntimeError(f"{path.name}:{call.lineno}: model_validate called on a receiver that is not a bare model Name")
        return func.value.id
    return None


def _handler_models(root: Path = TOOLS_ROOT) -> dict[str, tuple[str, str]]:
    """tool -> (model class name, 'handler:<module>.<function>') from the AST.

    A handler is a module-level ``def``/``async def`` named ``_execute_<tool>``
    or ``_handle_<tool>``; its model is whatever :func:`_validated_model_name`
    reads out of the calls in its body. One tool may be validated from both
    spellings (``_handle_upsert_node`` and ``_execute_upsert_node``); the two
    naming DIFFERENT models is refused rather than resolved by file order,
    which would make the census's answer depend on where a function sits.

    ``root`` is a parameter so the refusals above can be exercised against a
    planted module: a refusal no test can fire is not a refusal.
    """
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(root.glob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module.body:
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            prefixes = [prefix for prefix in HANDLER_PREFIXES if node.name.startswith(prefix)]
            if not prefixes:
                continue
            tool = node.name.removeprefix(prefixes[0])
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                class_name = _validated_model_name(call, path=path)
                if class_name is None:
                    continue
                site = f"handler:{path.stem}.{node.name}"
                previous = found.get(tool)
                if previous is not None and previous[0] != class_name:
                    raise RuntimeError(
                        f"{path.name}:{call.lineno}: {tool} is validated against two models "
                        f"({previous[0]} at {previous[1]}, {class_name} at {site}); the census cannot pick one"
                    )
                found[tool] = (class_name, site)
    return found


def census_model_wire() -> dict[str, ModelWireRow]:
    shipped = _shipped()
    handler_models = _handler_models()
    rows: dict[str, ModelWireRow] = {}
    for tool, props in shipped.items():
        entry = redaction.MANIFEST[tool]
        if entry.argument_model is not None:
            model = entry.argument_model
            rows[tool] = ModelWireRow(tool, props, model.__name__, frozenset(model.model_fields), "manifest")
            continue
        if tool in handler_models:
            class_name, site = handler_models[tool]
            module_name = site.removeprefix("handler:").rsplit(".", 1)[0]
            model = getattr(importlib.import_module(f"elspeth.web.composer.tools.{module_name}"), class_name)
            rows[tool] = ModelWireRow(tool, props, class_name, frozenset(model.model_fields), site)
            continue
        rows[tool] = ModelWireRow(tool, props, None, frozenset(), "none")
    return rows


if __name__ == "__main__":
    for row in sorted(census_model_wire().values(), key=lambda r: r.tool):
        gap_out = sorted(row.shipped - row.model_fields)
        gap_in = sorted(row.model_fields - row.shipped)
        print(f"{row.tool:32} {row.site:48} shipped-not-model={gap_out} model-not-shipped={gap_in}")
