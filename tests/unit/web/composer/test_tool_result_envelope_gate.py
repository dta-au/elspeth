"""Whole-tree gate: every key ``ToolResult.to_dict`` ships to the planner is registered, admitted, and taught or fenced.

The freeform tool loop hands the model ``json.dumps(result.to_dict())`` verbatim
(``discovery_cache.serialize_tool_result``); redaction runs only when the audit
row is persisted. So an untaught key misleads the planner, and an unadmitted key
breaks (type-driven tools raise at persist) or blinds (declarative tools
sentinel) the audit row. Three sides, all DERIVED at test time, never
hand-listed:

  shipped  — the AST of ``ToolResult.to_dict`` and the helpers it calls, the
             owned TypedDicts / pydantic models behind each sub-envelope, and
             the ``data=`` payload at every tool's result-constructor site
  admitted — ``tool_result_envelope`` (the registry) versus the live redaction
             manifest objects and the planner's closed discovery twin
  taught   — the rendered system prompt (both skills) plus every
             ``ToolDeclaration.description``, in house-style quoted form

The only curated inputs are the attribution maps (which owned payload type a
``data=`` helper or local carries — an unattributed one is a walker REFUSAL,
never a silent skip) and the fence fixture (``tool_result_envelope_fence.json``):
keys deliberately left untaught, each with a reason a reviewer can check. A
fence that has since become taught, or whose key no longer ships, is itself a
failure. Method: docs/agents/explore-and-pin-methodology.md (elspeth-e405ad7cd2).
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import typing
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import NamedTuple, NotRequired, TypedDict

import pytest
from pydantic import BaseModel

from elspeth.web.catalog.schemas import PluginSchemaInfo
from elspeth.web.composer import pipeline_planner, redaction, state
from elspeth.web.composer import tool_result_envelope as env
from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.tools import _common as common
from elspeth.web.composer.tools import _dispatch, _registry, blobs, secrets
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.execution.schemas import ValidationResult
from tests.unit.web.composer._teaching_gate_support import (
    REPO_ROOT,
    WEB_SRC,
    _call_name,
    _display,
    _enclosing_function,
    _is_cast,
    _literal_str,
    _typed_keys,
    is_quoted_leaf,
)

FENCE_PATH = Path(__file__).with_name("tool_result_envelope_fence.json")
COMPOSER = WEB_SRC / "composer"
COMMON = COMPOSER / "tools" / "_common.py"
TOOL_BATCH = COMPOSER / "tool_batch.py"
TOOLS_DIR = COMPOSER / "tools"
SHARED = "*"  # the ``tool`` column for surfaces every tool ships

# Files whose result-constructor calls define a TOOL's ``data`` payload. The
# shared helpers (_common.py, tool_batch.py) and the planner's own rejection
# builders are walked by the shared-surface generators instead.
_TOOL_DATA_FILES: tuple[str, ...] = ("blobs.py", "generation.py", "outputs.py", "secrets.py", "sessions.py", "sources.py", "transforms.py")
_RESULT_CONSTRUCTORS = frozenset({"_discovery_result", "_mutation_result", "ToolResult", "replace"})


class ShippedKey(NamedTuple):
    surface: str  # envelope | validation | delta | guidance | echo | preflight | plugin-schema | failure-data | tool-data
    tool: str  # SHARED or the tool name
    key: str  # dotted path from the envelope root, e.g. "validation.errors[].error_code"
    site: str


class FenceEntry(NamedTuple):
    surface: str
    tool: str
    key: str
    reason: str


# --- attribution maps (the census fills these; an unattributed site is a refusal) -----------------


class _Literal(NamedTuple):
    """Attribute a helper by READING the dict literal(s) it returns, for helpers that build plain dicts."""

    path: Path
    function: str


# helper called as ``data=<helper>(...)`` -> the owned TypedDict / pydantic model it returns, a
# ``_Literal`` pointing at the function whose return literal to read, or ``None`` for a helper
# that returns scalars only (nothing to teach). Every helper reached from a ``data=`` site MUST
# appear here; the walker refuses an unattributed one.
_DATA_HELPER_PAYLOADS: dict[str, type | _Literal | None] = {
    "_blob_create_payload": blobs.BlobCreatePayload,
    "_serialize_full_pipeline_state": common._FullPipelineStatePayload,
    "_serialize_set_pipeline_arguments": _Literal(COMMON, "_serialize_set_pipeline_arguments"),
    "_inventory_item_payload": secrets._SecretInventoryItemPayload,
    "get_schema": PluginSchemaInfo,  # context.catalog.get_schema returns the pydantic model instance
    "get_expression_grammar": None,  # a str
    "_sync_list_blobs": _Literal(TOOLS_DIR / "blobs.py", "_sync_list_blobs"),
    "_sync_list_ready_blob_inline_descriptors": _Literal(TOOLS_DIR / "blobs.py", "_sync_list_ready_blob_inline_descriptors"),
    "facts_to_dict": _Literal(COMPOSER / "source_inspection.py", "facts_to_dict"),
    "diff_states": _Literal(COMMON, "diff_states"),
    "_vf_destination_note": _Literal(COMMON, "_vf_destination_note"),
}
# Wrappers that return their first argument's shape unchanged.
_PASSTHROUGH_HELPERS = frozenset({"redact_source_storage_path"})
# (file name, enclosing function, local name) -> payload type, ONLY for a local the resolver
# cannot follow (it follows assignments, subscript stores, .update, if/else, ``or``, and tuple
# unpacking from a _Literal helper on its own).
_LOCAL_DATA_PAYLOADS: dict[tuple[str, str, str], type | None] = {}
# ``{**<name>, ...}`` inside a ``data=`` literal: the keys the splatted local carries when the
# splat is not a result's own ``.data``.
_SPLAT_KEYS: dict[str, tuple[str, ...]] = {}
# enclosing function -> tool name, where the ``_handle_`` / ``_execute_`` prefix rule does not apply.
_FUNCTION_TOOL_OVERRIDES: dict[str, str] = {
    "build_set_pipeline_candidate": "set_pipeline",
    "_execute_upsert_queue_node": "upsert_node",
    "_execute_delete_blob_locked": "delete_blob",
    "_finalize_journaled_blob_deletion": "delete_blob",
    "_check_duplicate_interpretation": "request_interpretation_review",
}


# --- walker primitives ----------------------------------------------------------------------------


def _module_str_constants(tree: ast.Module, path: Path | None = None) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` / ``NAME: Final[str] = "literal"`` bindings, plus — when
    ``path`` is given — every ``from m import NAME`` whose live value in that module is a str
    (``_DATA_ERROR_KEY`` is imported into the tool modules from ``_common``)."""
    out: dict[str, str] = {}
    if path is not None:
        module = _module_of(path)
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    bound = alias.asname or alias.name
                    # The module namespace is a dict; the name comes from the import
                    # statement just parsed, so this is a lookup, not a presence probe.
                    value = vars(module).get(bound)
                    if type(value) is str:
                        out[bound] = value
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        literal = _literal_str(value)
        if literal is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = literal
    return out


def _key_of(node: ast.AST, constants: dict[str, str], site: str) -> str:
    key = _literal_str(node)
    if key is None and isinstance(node, ast.Name) and node.id in constants:
        key = constants[node.id]
    assert key is not None, f"{site}: non-literal key in a shipped dict literal ({ast.dump(node)[:60]})"
    return key


def _function(tree: ast.Module, name: str, *, in_class: str | None = None) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if in_class is not None:
            if isinstance(node, ast.ClassDef) and node.name == in_class:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                        return item
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found{' in ' + in_class if in_class else ''}")


_NameResolver = Callable[[ast.Name], Iterator[str]]


def _is_own_data(node: ast.AST) -> bool:
    """``result.data`` or ``cast(T, result.data)``: a result's own payload, counted at its producer."""
    if isinstance(node, ast.Attribute) and node.attr == "data":
        return True
    return isinstance(node, ast.Call) and _is_cast(node) and len(node.args) == 2 and _is_own_data(node.args[1])


def _splat_keys(node: ast.AST, site: str, resolve_name: _NameResolver | None) -> Iterator[str]:
    """Keys a ``**`` splat re-carries: nothing for a result's own ``.data`` (already counted at its
    producer), the attributed keys for a name in ``_SPLAT_KEYS``, or — inside a handler — whatever
    the local resolves to. Anything else is a refusal."""
    if _is_own_data(node):
        return iter(())
    if isinstance(node, ast.Name) and node.id in _SPLAT_KEYS:
        return iter(_SPLAT_KEYS[node.id])
    if isinstance(node, ast.Name) and resolve_name is not None:
        return resolve_name(node)
    raise AssertionError(f"{site}: ** splat of {ast.dump(node)[:60]} inside a shipped dict literal — attribute it in _SPLAT_KEYS")


def _dict_literal_keys(
    node: ast.AST,
    prefix: str,
    site: str,
    constants: dict[str, str],
    resolve_name: _NameResolver | None = None,
) -> Iterator[str]:
    """Dotted keys of a dict literal, recursing through nested dict literals and list/tuple-of-dict literals.

    Refuses (AssertionError) a key that is neither a string literal nor a module
    string constant, an unattributed ``**`` splat, and a ``cast(...)`` value:
    each would let a producer ship a key the walker cannot read.
    """
    assert isinstance(node, ast.Dict), f"{site}: expected a dict literal, got {type(node).__name__}"
    for key_node, value in zip(node.keys, node.values, strict=True):
        if key_node is None:
            for key in _splat_keys(value, site, resolve_name):
                yield f"{prefix}{key}"
            continue
        key = _key_of(key_node, constants, site)
        assert not _is_cast(value), f"{site}: cast(...) hides the shape of {prefix}{key}"
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, ast.Dict):
            yield from _dict_literal_keys(value, path + ".", site, constants)
        elif isinstance(value, (ast.List, ast.Tuple)) and value.elts and isinstance(value.elts[0], ast.Dict):
            yield from _dict_literal_keys(value.elts[0], path + "[].", site, constants)


def _subscript_assign_keys(fn: ast.AST, target: str, site: str, constants: dict[str, str]) -> Iterator[tuple[str, int]]:
    """``target["key"] = ...`` and ``target.update({...})`` statements inside ``fn``: (key, lineno), in source order.

    ``ast.walk`` is breadth-first, so nested assignments would otherwise come
    out after their later siblings and the emission-order pins would lie.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name) and tgt.value.id == target:
                key = _key_of(tgt.slice, constants, f"{site}:{node.lineno}")
                assert not _is_cast(node.value), f"{site}:{node.lineno}: cast(...) hides the shape of {key}"
                found.append((key, node.lineno))
        is_update = isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
        if is_update and isinstance(node.func.value, ast.Name) and node.func.value.id == target and node.args:
            found.extend((key, node.lineno) for key in _dict_literal_keys(node.args[0], "", f"{site}:{node.lineno}", constants))
    yield from sorted(found, key=lambda item: item[1])


def _initial_dict_assign(fn: ast.AST, target: str, site: str) -> ast.Dict:
    for node in ast.walk(fn):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgt = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(tgt, ast.Name) and tgt.id == target and isinstance(node.value, ast.Dict):
                return node.value
    raise AssertionError(f"{site}: {target} is not initialised from a dict literal")


def _payload_keys(payload: type | None, prefix: str) -> list[str]:
    if payload is None:
        return []
    if typing.is_typeddict(payload):
        return _typed_keys(payload, prefix)
    assert issubclass(payload, BaseModel), f"{payload!r} is neither a TypedDict nor a pydantic model"
    return [f"{prefix}{name}" for name in payload.model_fields]


def _owned_payload_type(value: object) -> type | None:
    """``value`` when it is one of the two owned payload shapes ``_payload_keys`` reads — a
    TypedDict class or a pydantic model class — else None. Nominal (ADR-032): a class is a
    payload type by what it IS, never by whether it happens to carry ``model_fields``."""
    if isinstance(value, type) and (typing.is_typeddict(value) or issubclass(value, BaseModel)):
        return value
    return None


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# --- probes (module level: postponed annotations break function-local TypedDicts) ----------------


class _ProbeEntry(TypedDict):
    code: str
    detail: NotRequired[str]


class _ProbeEnvelope(TypedDict):
    items: list[_ProbeEntry]
    nested: _ProbeEntry


_PROBE_TO_DICT = """
_K: Final[str] = "const_key"
class ToolResult:
    def to_dict(self):
        result: dict[str, Any] = {"success": True, "validation": {"is_valid": True, "errors": []}, "version": 1}
        if self.data is not None:
            result["data"] = self.data
        result[_K] = 1
        result.update({"late": 2})
        return result
"""

_PROBE_SPLAT = """
class ToolResult:
    def to_dict(self):
        result = {"success": True, **self.extra}
        return result
"""

_PROBE_CAST = """
class ToolResult:
    def to_dict(self):
        result = {"success": True}
        result["data"] = cast(JsonValue, self.data)
        return result
"""

_PROBE_DYNAMIC_KEY = """
class ToolResult:
    def to_dict(self):
        result = {"success": True}
        result[self.key_name] = 1
        return result
"""

_PROBE_NESTED_TUPLE = """
def f():
    return {"components": ({"component_id": "x", "fields": ("a",)},), "repair": {"inline_form": {"instruction": "i"}}}
"""


def test_walker_reads_literal_constant_subscript_and_update_keys_in_emission_order() -> None:
    tree = ast.parse(_PROBE_TO_DICT)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    constants = _module_str_constants(tree)
    initial = list(_dict_literal_keys(_initial_dict_assign(fn, "result", "probe"), "", "probe", constants))
    later = [k for k, _ in _subscript_assign_keys(fn, "result", "probe", constants)]
    assert initial == ["success", "validation", "validation.is_valid", "validation.errors", "version"]
    assert later == ["data", "const_key", "late"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_PROBE_SPLAT, "** splat"),
        (_PROBE_CAST, "cast(...) hides"),
        (_PROBE_DYNAMIC_KEY, "non-literal key"),
    ],
    ids=["splat", "cast", "dynamic-key"],
)
def test_walker_refuses_shapes_it_cannot_read(source: str, message: str) -> None:
    tree = ast.parse(source)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    constants = _module_str_constants(tree)
    with pytest.raises(AssertionError, match=re.escape(message)):
        list(_dict_literal_keys(_initial_dict_assign(fn, "result", "probe"), "", "probe", constants))
        list(_subscript_assign_keys(fn, "result", "probe", constants))


def test_walker_recurses_through_tuple_of_dicts_and_nested_dicts() -> None:
    tree = ast.parse(_PROBE_NESTED_TUPLE)
    fn = _function(tree, "f")
    ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
    assert ret.value is not None
    assert list(_dict_literal_keys(ret.value, "data.", "probe", {})) == [
        "data.components",
        "data.components[].component_id",
        "data.components[].fields",
        "data.repair",
        "data.repair.inline_form",
        "data.repair.inline_form.instruction",
    ]


def test_typed_keys_recurse_through_nested_and_list_of_typed_dicts() -> None:
    assert _typed_keys(_ProbeEnvelope, "x.") == [
        "x.items",
        "x.items[].code",
        "x.items[].detail",
        "x.nested",
        "x.nested.code",
        "x.nested.detail",
    ]


# --- shipped side: shared surfaces -----------------------------------------------------------------


def _envelope_sites() -> Iterator[ShippedKey]:
    path = COMMON
    tree = _parse(path)
    constants = _module_str_constants(tree, path)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    site = f"{_display(COMMON)}:{fn.lineno}"
    for key in _dict_literal_keys(_initial_dict_assign(fn, "result", site), "", site, constants):
        surface = "validation" if key.startswith("validation.") else "envelope"
        yield ShippedKey(surface, SHARED, key, site)
    for key, lineno in _subscript_assign_keys(fn, "result", site, constants):
        yield ShippedKey("envelope", SHARED, key, f"{_display(COMMON)}:{lineno}")


def _entry_keys(prefix: str) -> list[str]:
    """The entry-level vocabulary of a ValidationEntryDict, one level deep.

    The sub-keys of the three detail payloads (``contract.*``, ``row_union_schema.*``,
    ``coalesce_union_type.*``) are pinned per ``error_code`` by the sibling gate
    ``test_planner_teaching_gate.py`` against the catalogue text that accompanies
    that code, which is where a model reads them; this gate owns the envelope
    vocabulary around them.
    """
    return [key for key in _typed_keys(state.ValidationEntryDict, prefix) if "." not in key.removeprefix(prefix)]


def _validation_typed_sites() -> Iterator[ShippedKey]:
    entry_site = f"{_display(COMPOSER / 'state.py')}:ValidationEntryDict"
    for list_key in ("errors", "warnings", "suggestions"):
        for key in _entry_keys(f"validation.{list_key}[]."):
            yield ShippedKey("validation", SHARED, key, entry_site)
    for key in _typed_keys(common._SemanticEdgeContractPayload, "validation.semantic_contracts[]."):
        yield ShippedKey("validation", SHARED, key, f"{_display(COMMON)}:_SemanticEdgeContractPayload")
    for key in _typed_keys(common._GraphRepairSuggestion, "validation.graph_repair_suggestions[]."):
        yield ShippedKey("validation", SHARED, key, f"{_display(COMMON)}:_GraphRepairSuggestion")


def _delta_sites() -> Iterator[ShippedKey]:
    tree = _parse(COMMON)
    fn = _function(tree, "_compute_validation_delta")
    site = f"{_display(COMMON)}:{fn.lineno}"
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert len(returns) == 1, f"{site}: expected exactly one return"
    for key in _dict_literal_keys(returns[0].value, "validation_delta.", site, _module_str_constants(tree, COMMON)):
        yield ShippedKey("delta", SHARED, key, site)
        for entry_key in _entry_keys(key + "[]."):
            yield ShippedKey("delta", SHARED, entry_key, site)


def _guidance_sites() -> Iterator[ShippedKey]:
    site = f"{_display(COMPOSER / 'tool_result_envelope.py')}:ValidationGuidance"
    for key in _typed_keys(env.ValidationGuidance, "validation_guidance."):
        yield ShippedKey("guidance", SHARED, key, site)
    # ``codes`` is keyed by the closed error_code; each value is the entry TypedDict.
    for key in _typed_keys(env.ValidationCodeGuidance, "validation_guidance.codes.<code>."):
        yield ShippedKey("guidance", SHARED, key, site)


def _echo_sites() -> Iterator[ShippedKey]:
    tree = _parse(COMMON)
    fn = _function(tree, "_applied_component_echo")
    for key, lineno in _subscript_assign_keys(fn, "echo", f"{_display(COMMON)}:{fn.lineno}", _module_str_constants(tree, COMMON)):
        yield ShippedKey("echo", SHARED, f"applied_component.{key}", f"{_display(COMMON)}:{lineno}")


def _preflight_sites() -> Iterator[ShippedKey]:
    site = f"{_display(WEB_SRC / 'execution' / 'schemas.py')}:ValidationResult"
    for key in _payload_keys(ValidationResult, "runtime_preflight."):
        yield ShippedKey("preflight", SHARED, key, site)


def _plugin_schema_sites() -> Iterator[ShippedKey]:
    site = f"{_display(WEB_SRC / 'catalog' / 'schemas.py')}:PluginSchemaInfo"
    for key in _payload_keys(PluginSchemaInfo, "plugin_schemas.<kind/plugin>."):
        yield ShippedKey("plugin-schema", SHARED, key, site)


# (file, function, the local dict the payload is assembled in) for the shared failure helpers.
_FAILURE_DATA_HELPERS: tuple[tuple[Path, str, str | None], ...] = (
    (COMMON, "_failure_result", "data"),
    (COMMON, "_credential_wiring_contract_failure", None),  # passed inline as data={...}
    (TOOL_BATCH, "run_tool_batch", "proposal_payload"),
    (TOOL_BATCH, "run_tool_batch", "feedback_data"),
)


def _failure_data_sites() -> Iterator[ShippedKey]:
    for path, fn_name, owner in _FAILURE_DATA_HELPERS:
        tree = _parse(path)
        constants = _module_str_constants(tree, path)
        fn = _function(tree, fn_name)
        site = f"{_display(path)}:{fn.lineno}"
        found = False
        if owner is not None:
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == owner for t in node.targets):
                    if isinstance(node.value, ast.Dict):
                        found = True
                        for key in _dict_literal_keys(node.value, "data.", f"{site}:{node.lineno}", constants):
                            yield ShippedKey("failure-data", SHARED, key, f"{_display(path)}:{node.lineno}")
                    elif isinstance(node.value, ast.Call) and _call_name(node.value) == "dict":
                        # ``dict(<seed>)`` — the seed is a result's own data, counted at its producer.
                        found = True
            for key, lineno in _subscript_assign_keys(fn, owner, site, constants):
                found = True
                yield ShippedKey("failure-data", SHARED, f"data.{key}", f"{_display(path)}:{lineno}")
        else:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and _call_name(node) == "ToolResult":
                    for kw in node.keywords:
                        if kw.arg == "data" and isinstance(kw.value, ast.Dict):
                            found = True
                            for key in _dict_literal_keys(kw.value, "data.", f"{site}:{node.lineno}", constants):
                                yield ShippedKey("failure-data", SHARED, key, f"{_display(path)}:{node.lineno}")
        assert found, f"{site}: no readable data payload for {fn_name} — walker out of date"
    yield ShippedKey(
        "failure-data", SHARED, f"data.{common.COMPONENTS_WITHHELD_KEY}", f"{_display(COMMON)}:_merged_component_rejection_result"
    )


# --- shipped side: per-tool data --------------------------------------------------------------------


def _registered_tool_names() -> frozenset[str]:
    return frozenset(d["name"] for d in get_tool_definitions()) | frozenset(redaction.MANIFEST)


def _tool_name_for(fn_name: str | None, site: str) -> str:
    assert fn_name is not None, f"{site}: result constructed outside any function"
    if fn_name in _FUNCTION_TOOL_OVERRIDES:
        return _FUNCTION_TOOL_OVERRIDES[fn_name]
    name = fn_name
    for prefix in ("_handle_", "_execute_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    assert name in _registered_tool_names(), f"{site}: cannot attribute function {fn_name!r} to a tool — add it to _FUNCTION_TOOL_OVERRIDES"
    return name


def _data_expr(node: ast.Call) -> ast.AST | None:
    for kw in node.keywords:
        if kw.arg == "data":
            return kw.value
    if _call_name(node) == "_discovery_result" and len(node.args) >= 2:
        return node.args[1]
    return None


def _module_of(path: Path) -> ModuleType:
    return importlib.import_module(".".join(path.relative_to(REPO_ROOT / "src").with_suffix("").parts))


def _function_node(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _assignments_to(fn: ast.AST, name: str) -> Iterator[tuple[ast.AST, int | None, ast.AST | None]]:
    """Every ``name = <rhs>`` / ``name: T = <rhs>`` / ``(.., name, ..) = <rhs>`` inside ``fn``:
    (rhs, tuple index or None, annotation or None)."""
    for node in ast.walk(fn):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
            yield node.value, None, node.annotation
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    yield node.value, None, None
                elif isinstance(target, ast.Tuple):
                    for index, elt in enumerate(target.elts):
                        if isinstance(elt, ast.Name) and elt.id == name:
                            yield node.value, index, None


def _expr_keys(
    expr: ast.AST,
    *,
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    tree: ast.Module,
    path: Path,
    prefix: str,
    site: str,
    depth: int = 0,
    visiting: frozenset[tuple[str, str]] = frozenset(),
) -> Iterator[str]:
    """Dotted keys the expression ships, following the code rather than a hand-list.

    Dict literals, attributed helper calls, passthrough wrappers, locals (all
    of their assignments, subscript stores, and ``.update`` calls), ``x if c
    else y``, ``x or {}``, list comprehensions of dicts, and a helper's return
    tuple are readable. Anything else is a REFUSAL naming the site.
    """
    assert depth < 12, f"{site}: resolution too deep ({prefix})"
    constants = _module_str_constants(tree, path)
    if isinstance(expr, ast.Constant) and expr.value is None:
        return
    if isinstance(expr, ast.Dict):

        def resolve_local(name: ast.Name) -> Iterator[str]:
            return _expr_keys(name, fn=fn, tree=tree, path=path, prefix="", site=site, depth=depth + 1, visiting=visiting)

        yield from _dict_literal_keys(expr, prefix, site, constants, resolve_local)
        return
    if isinstance(expr, ast.IfExp):
        yield from _expr_keys(expr.body, fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
        yield from _expr_keys(expr.orelse, fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
        return
    if isinstance(expr, ast.BoolOp):
        for value in expr.values:
            yield from _expr_keys(value, fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
        return
    if isinstance(expr, ast.ListComp) and isinstance(expr.elt, (ast.Dict, ast.Call, ast.Name)):
        yield from _expr_keys(expr.elt, fn=fn, tree=tree, path=path, prefix=prefix + "[].", site=site, depth=depth + 1, visiting=visiting)
        return
    if isinstance(expr, (ast.List, ast.Tuple)) and expr.elts and isinstance(expr.elts[0], ast.Dict):
        yield from _expr_keys(
            expr.elts[0], fn=fn, tree=tree, path=path, prefix=prefix + "[].", site=site, depth=depth + 1, visiting=visiting
        )
        return
    if isinstance(expr, ast.Call):
        helper = _call_name(expr)
        if helper in _PASSTHROUGH_HELPERS and expr.args:
            yield from _expr_keys(expr.args[0], fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
            return
        if helper == "dict" and expr.args:
            yield from _expr_keys(expr.args[0], fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
            return
        assert helper in _DATA_HELPER_PAYLOADS, f"{site}: data built by unattributed helper {helper!r} — add it to _DATA_HELPER_PAYLOADS"
        attribution = _DATA_HELPER_PAYLOADS[helper]
        if isinstance(attribution, _Literal):
            yield from _helper_return_keys(attribution, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
        else:
            yield from _payload_keys(attribution, prefix)
        return
    if isinstance(expr, ast.Name):
        fn_name = fn.name
        ident = (path.name, fn_name, expr.id)
        if ident in _LOCAL_DATA_PAYLOADS:
            yield from _payload_keys(_LOCAL_DATA_PAYLOADS[ident], prefix)
            return
        if (fn_name, expr.id) in visiting:
            # ``data = wrapper(data)``: the local is already being resolved one level up.
            return
        visiting = visiting | {(fn_name, expr.id)}
        seen = False
        for rhs, index, annotation in _assignments_to(fn, expr.id):
            seen = True
            if isinstance(annotation, ast.Name):
                typed = _owned_payload_type(vars(_module_of(path)).get(annotation.id))
                if typed is not None:
                    yield from _payload_keys(typed, prefix)
                    continue
            if index is not None:
                assert isinstance(rhs, ast.Call), f"{site}: tuple-unpacked local {expr.id!r} from a non-call"
                helper = _call_name(rhs)
                attribution = _DATA_HELPER_PAYLOADS.get(helper)
                assert isinstance(attribution, _Literal), f"{site}: tuple-returning helper {helper!r} needs a _Literal attribution"
                yield from _helper_return_keys(attribution, prefix=prefix, site=site, depth=depth + 1, index=index, visiting=visiting)
                continue
            yield from _expr_keys(rhs, fn=fn, tree=tree, path=path, prefix=prefix, site=site, depth=depth + 1, visiting=visiting)
        for key, _ in _subscript_assign_keys(fn, expr.id, site, constants):
            seen = True
            yield f"{prefix}{key}"
        assert seen, f"{site}: local {expr.id!r} is never assigned in its function — attribute it in _LOCAL_DATA_PAYLOADS"
        return
    raise AssertionError(f"{site}: data expression {type(expr).__name__} is not readable ({ast.dump(expr)[:80]})")


def _helper_return_keys(
    lit: _Literal,
    *,
    prefix: str,
    site: str,
    depth: int,
    index: int | None = None,
    visiting: frozenset[tuple[str, str]] = frozenset(),
) -> Iterator[str]:
    del visiting  # a helper's locals are its own namespace; the guard restarts inside it
    tree = _parse(lit.path)
    fn = _function_node(tree, lit.function)
    assert fn is not None, f"{site}: helper {lit.function!r} not found in {_display(lit.path)}"
    returns = [n.value for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert returns, f"{site}: helper {lit.function!r} has no return value"
    for value in returns:
        target = value
        if index is not None:
            assert isinstance(value, ast.Tuple), f"{site}: {lit.function!r} return is not a tuple"
            target = value.elts[index]
        yield from _expr_keys(target, fn=fn, tree=tree, path=lit.path, prefix=prefix, site=f"{site} via {lit.function}", depth=depth)


def _tool_data_sites(files: Iterable[Path] | None = None) -> Iterator[ShippedKey]:
    paths = [TOOLS_DIR / name for name in _TOOL_DATA_FILES] if files is None else list(files)
    for path in paths:
        tree = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in _RESULT_CONSTRUCTORS:
                continue
            data = _data_expr(node)
            if data is None or (isinstance(data, ast.Constant) and data.value is None):
                continue
            site = f"{_display(path)}:{node.lineno}"
            fn_name = _enclosing_function(tree, node.lineno)
            fn = _function_node(tree, fn_name) if fn_name is not None else None
            assert fn is not None, f"{site}: result constructed outside any function"
            tool = _tool_name_for(fn_name, site)
            seen: dict[str, None] = {}
            for key in _expr_keys(data, fn=fn, tree=tree, path=path, prefix="data.", site=site):
                seen.setdefault(key, None)
            for key in seen:
                yield ShippedKey("tool-data", tool, key, site)


def _tools_calling(helper: str) -> frozenset[str]:
    """Tools whose handler file calls ``helper`` (e.g. ``_attach_post_call_hints``), by enclosing function."""
    tools: set[str] = set()
    for name in _TOOL_DATA_FILES:
        path = TOOLS_DIR / name
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) == helper:
                fn_name = _enclosing_function(tree, node.lineno)
                if fn_name is not None:
                    tools.add(_tool_name_for(fn_name, f"{_display(path)}:{node.lineno}"))
    return frozenset(tools)


def _tools_setting(field: str) -> frozenset[str]:
    """Tools whose handler constructs a ToolResult with ``field=`` set explicitly."""
    tools: set[str] = set()
    for name in _TOOL_DATA_FILES:
        path = TOOLS_DIR / name
        tree = _parse(path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) in {"ToolResult", "replace"}
                and any(kw.arg == field for kw in node.keywords)
            ):
                fn_name = _enclosing_function(tree, node.lineno)
                if fn_name is not None:
                    tools.add(_tool_name_for(fn_name, f"{_display(path)}:{node.lineno}"))
    return frozenset(tools)


def can_ship(tool: str) -> frozenset[str]:
    """Top-level keys ``tool``'s results can carry, derived from the dispatcher's own registries."""
    keys = set(env.TOOL_RESULT_REQUIRED_KEYS) | {"data"}
    if tool in _dispatch._ALL_MUTATION_TOOL_NAMES:
        keys |= {"validation_delta", "validation_guidance", "applied_component"}
    if tool in _tools_calling("_attach_post_call_hints"):
        keys |= {"post_call_hints"}
    if _registry.should_augment_with_plugin_schemas(tool):
        keys |= {"plugin_schemas"}
    if tool in _tools_setting("runtime_preflight"):
        keys |= {"runtime_preflight"}
    return frozenset(keys)


def shipped_keys() -> list[ShippedKey]:
    return [
        *_envelope_sites(),
        *_validation_typed_sites(),
        *_delta_sites(),
        *_guidance_sites(),
        *_echo_sites(),
        *_preflight_sites(),
        *_plugin_schema_sites(),
        *_failure_data_sites(),
        *_tool_data_sites(),
    ]


# --- admitted side ---------------------------------------------------------------------------------


def admitted_keys() -> dict[str, frozenset[str]]:
    """tool -> top-level keys the persisted audit row keeps (neither sentinelled nor raised on)."""
    out: dict[str, frozenset[str]] = {}
    for name, entry in redaction.MANIFEST.items():
        if entry.response_model is not None:
            out[name] = frozenset(entry.response_model.model_fields)
        else:
            assert entry.policy is not None
            out[name] = frozenset(entry.policy.known_response_keys) | redaction._TOOL_RESULT_ENVELOPE_KEYS
    return out


# --- taught side -----------------------------------------------------------------------------------


def _all_descriptions() -> dict[str, str]:
    return {d["name"]: d["description"] for d in get_tool_definitions()}


def _authoring_vocabulary() -> frozenset[str]:
    """Property names anywhere in the set_pipeline argument schema: keys the model itself authors."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(props)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(canonical_set_pipeline_schema())
    return frozenset(names)


def taught_text(tool: str) -> str:
    skill = build_system_prompt(None)
    descriptions = _all_descriptions()
    if tool == SHARED:
        return skill + "\n" + "\n".join(descriptions.values())
    return skill + "\n" + descriptions.get(tool, "")


def is_taught(shipped: ShippedKey) -> bool:
    if is_quoted_leaf(shipped.key, taught_text(shipped.tool)):
        return True
    # The echo and the state read restate the authoring payload the model itself wrote via
    # set_pipeline, so its property names are taught by the argument schema. No other surface
    # gets this: ``id`` on a blob list is not the node ``id`` the model authored.
    restates_authoring = shipped.surface == "echo" or (shipped.surface == "tool-data" and shipped.tool == "get_pipeline_state")
    if restates_authoring and "[]" in shipped.key:
        return shipped.key.split(".")[-1] in _authoring_vocabulary()
    return False


def untaught_keys() -> dict[tuple[str, str, str], list[str]]:
    out: dict[tuple[str, str, str], list[str]] = {}
    for shipped in shipped_keys():
        if is_taught(shipped):
            continue
        out.setdefault((shipped.surface, shipped.tool, shipped.key), []).append(shipped.site)
    return out


def load_fence(path: Path = FENCE_PATH) -> list[FenceEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [FenceEntry(e["surface"], e["tool"], e["key"], e["reason"]) for e in raw["fenced"]]


def matrix_rows() -> list[dict[str, object]]:
    """One row per shipped key: the census artefact for the ticket."""
    admitted = admitted_keys()
    rows: list[dict[str, object]] = []
    for shipped in shipped_keys():
        top = shipped.key.split(".")[0]
        admitted_on: object
        if shipped.tool == SHARED:
            admitted_on = sorted(tool for tool, keys in admitted.items() if top in keys)
        else:
            admitted_on = top in admitted.get(shipped.tool, frozenset())
        rows.append(
            {
                "surface": shipped.surface,
                "tool": shipped.tool,
                "key": shipped.key,
                "site": shipped.site,
                "taught": is_taught(shipped),
                "admitted_on": admitted_on,
            }
        )
    return rows


# --- the gate --------------------------------------------------------------------------------------


def test_every_shipped_envelope_key_is_taught_or_fenced() -> None:
    fenced = {(e.surface, e.tool, e.key) for e in load_fence()}
    unexplained = {k: v for k, v in untaught_keys().items() if k not in fenced}
    lines = [f"{s} {t} {k}  <- {', '.join(sites)}" for (s, t, k), sites in sorted(unexplained.items())]
    assert not unexplained, (
        f"{len(unexplained)} tool-result key(s) reach the planner with no prose that names them. "
        "Teach each in skills/pipeline_composer.md ('Reading a tool result') or the tool's declaration "
        "description, or fence it with a checkable reason in tool_result_envelope_fence.json:\n" + "\n".join(lines)
    )


def test_fence_entries_are_live_untaught_keys() -> None:
    """A fence must not outlive what it fences: a taught or no-longer-shipped key leaves the fixture."""
    untaught = untaught_keys()
    shipped = {(s.surface, s.tool, s.key) for s in shipped_keys()}
    stale = []
    for entry in load_fence():
        ident = (entry.surface, entry.tool, entry.key)
        if ident not in shipped:
            stale.append(f"{ident}: no producer ships this key any more")
        elif ident not in untaught:
            stale.append(f"{ident}: now taught — remove the fence")
    assert not stale, "stale fence entries:\n" + "\n".join(stale)


def test_fence_entries_carry_a_checkable_reason() -> None:
    """A fence is an adjudicated decision, not a parking spot (elspeth-e405ad7cd2)."""
    placeholder = re.compile(r"^\s*(pending|todo|tbd|fixme|wip)\b", re.IGNORECASE)
    pending = [e for e in load_fence() if len(e.reason.split()) < 12 or placeholder.match(e.reason)]
    assert not pending, "fence entries await adjudication:\n" + "\n".join(f"{e.surface} {e.tool} {e.key}: {e.reason!r}" for e in pending)


def test_fence_fixture_has_no_duplicates() -> None:
    entries = load_fence()
    assert len({(e.surface, e.tool, e.key) for e in entries}) == len(entries)


# --- admitted-side pins ----------------------------------------------------------------------------


def test_envelope_sites_equal_the_registry_in_emission_order() -> None:
    """to_dict's literal keys ARE the registry: a key added on one side turns this red."""
    top = [k.key for k in _envelope_sites() if k.surface == "envelope"]
    assert tuple(top) == (*env.TOOL_RESULT_REQUIRED_KEYS, *env.TOOL_RESULT_OPTIONAL_KEYS)


def test_validation_literal_keys_equal_the_registry() -> None:
    nested = [k.key.removeprefix("validation.") for k in _envelope_sites() if k.surface == "validation"]
    assert tuple(nested) == env.VALIDATION_KEYS


def test_delta_and_echo_literal_keys_equal_the_registry() -> None:
    delta = [k.key.removeprefix("validation_delta.") for k in _delta_sites() if "[]" not in k.key]
    echo = [k.key.removeprefix("applied_component.") for k in _echo_sites()]
    assert tuple(delta) == env.VALIDATION_DELTA_KEYS
    assert tuple(echo) == env.APPLIED_COMPONENT_KEYS


def test_type_driven_response_model_fields_equal_the_registry() -> None:
    fields = tuple(redaction._ToolResultResponseModel.model_fields)
    assert fields == (*env.tool_result_keys(data=True), *env.TOOL_RESULT_POST_DISPATCH_KEYS)


def test_declarative_key_tables_equal_the_registry() -> None:
    assert redaction._tool_result_response_keys(data=True) == env.tool_result_keys(data=True)
    assert redaction._tool_result_response_keys(data=False) == env.tool_result_keys(data=False)


def test_implicit_declarative_envelope_covers_every_required_key() -> None:
    """D1 (ratified 2026-09-02): a required producer key that is not implicitly known fires the drift
    counter on every call of every declarative tool, which is what made the counter useless."""
    assert set(env.TOOL_RESULT_REQUIRED_KEYS) <= redaction._TOOL_RESULT_ENVELOPE_KEYS


def test_closed_provider_discovery_payload_is_a_subset_of_the_registry() -> None:
    keys = set(typing.get_type_hints(pipeline_planner._ClosedProviderDiscoveryPayload))
    assert keys <= set(env.tool_result_keys(data=True))
    nested = set(typing.get_type_hints(pipeline_planner._ClosedProviderValidationEnvelope))
    assert nested <= set(env.VALIDATION_KEYS)


def test_no_shared_envelope_key_is_unadmitted_on_a_mutation_tool() -> None:
    """A key a tool CAN ship that its audit row would raise on (type-driven) or sentinel (declarative with
    declared keys) is a producer/manifest split. ``can_ship`` is derived from the dispatcher's registries.
    The 26 declarative entries with no known keys are D4 (ratified: no change); ``request_advisor_hint``
    never returns a ToolResult (its envelope is built outside execute_tool)."""
    for tool, keys in admitted_keys().items():
        if tool == "request_advisor_hint":
            continue
        entry = redaction.MANIFEST[tool]
        declares = entry.response_model is not None or (entry.policy is not None and bool(entry.policy.known_response_keys))
        if not declares:
            continue
        missing = can_ship(tool) - keys - {"data"}  # data admission is per tool by design
        assert not missing, f"{tool}: can ship {sorted(missing)} but the audit row does not admit them"


def test_every_result_constructor_site_is_attributed() -> None:
    """The tool-data walker must not skip a site silently: it raises on the first unattributed one."""
    list(_tool_data_sites())


# The APPROVAL_REQUIRED proposal payload, in the order run_tool_batch authors it. ``success`` is
# deliberately absent: the envelope's own ``success`` already says it and ``status`` is the
# discriminator a reader keys on (F1 on elspeth-e405ad7cd2).
_PROPOSAL_PAYLOAD_KEYS: tuple[str, ...] = ("status", "proposal_id", "tool_name", "summary", "message")


def test_approval_required_proposal_payload_ships_exactly_its_keys() -> None:
    """Exact-key pin on the proposal payload (red-team R5, mutation RM5).

    The teaching gate cannot catch ``"success": True`` creeping back into this
    payload: the leaf ``success`` is quoted everywhere the envelope is taught,
    so ``data.success`` would count as taught. Only an exact pin on the literal
    the walker reads can refuse it. The pin also refuses a second assignment or
    a later ``proposal_payload[...] =`` store, so the literal is the whole shape,
    and checks that the census actually reads this site (the
    ``_FAILURE_DATA_HELPERS`` row for it), so the pin and the matrix cannot drift
    apart.
    """
    tree = _parse(TOOL_BATCH)
    constants = _module_str_constants(tree, TOOL_BATCH)
    fn = _function(tree, "run_tool_batch")
    site = f"{_display(TOOL_BATCH)}:{fn.lineno}"
    assignments = list(_assignments_to(fn, "proposal_payload"))
    assert len(assignments) == 1, f"{site}: proposal_payload must be assigned exactly once, got {len(assignments)}"
    literal, index, _annotation = assignments[0]
    assert index is None and isinstance(literal, ast.Dict), f"{site}: proposal_payload is not a plain dict literal"
    keys = list(_dict_literal_keys(literal, "", f"{site}:{literal.lineno}", constants))
    assert tuple(keys) == _PROPOSAL_PAYLOAD_KEYS
    assert "success" not in keys
    assert list(_subscript_assign_keys(fn, "proposal_payload", site, constants)) == []
    carried = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and _call_name(node) == "ToolResult"
        and any(kw.arg == "data" and isinstance(kw.value, ast.Name) and kw.value.id == "proposal_payload" for kw in node.keywords)
    ]
    assert len(carried) == 1, f"{site}: expected exactly one ToolResult(data=proposal_payload), got {len(carried)}"
    census = [row.key for row in _failure_data_sites() if row.site == f"{_display(TOOL_BATCH)}:{literal.lineno}"]
    assert census == [f"data.{key}" for key in _PROPOSAL_PAYLOAD_KEYS]


def test_is_taught_requires_the_quoted_form() -> None:
    assert not is_quoted_leaf("affected_nodes", "the affected nodes are listed")
    assert is_quoted_leaf("affected_nodes", "read `affected_nodes` first")
    assert is_quoted_leaf("data.error", "carries 'error' text")


# --- self-tests: the derivation reacts to the tree ------------------------------------------------


def test_gate_derives_a_new_envelope_key_from_the_ast(tmp_path: Path) -> None:
    """A key appended to to_dict must appear in the envelope surface without any list being edited."""
    source = COMMON.read_text(encoding="utf-8")
    marker = "        if self.post_call_hints:\n"
    assert marker in source
    patched = source.replace(marker, '        result["probe_key"] = 1\n' + marker, 1)
    probe = tmp_path / "_common.py"
    probe.write_text(patched, encoding="utf-8")
    tree = ast.parse(patched)
    fn = _function(tree, "to_dict", in_class="ToolResult")
    keys = [k for k, _ in _subscript_assign_keys(fn, "result", "probe", _module_str_constants(tree, COMMON))]
    assert "probe_key" in keys


# --- operator reference ----------------------------------------------------------------------------


def test_operator_reference_names_every_registry_key() -> None:
    """docs/reference/composer-tools.md 'Tool Result Format' is regenerated from the registry, not remembered."""
    text = (REPO_ROOT / "docs" / "reference" / "composer-tools.md").read_text(encoding="utf-8")
    start = text.index("## Tool Result Format")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    missing = [
        key
        for key in (
            *env.tool_result_keys(data=True),
            *env.TOOL_RESULT_POST_DISPATCH_KEYS,
            *env.VALIDATION_KEYS,
            *env.VALIDATION_DELTA_KEYS,
            *env.APPLIED_COMPONENT_KEYS,
        )
        if f"`{key}`" not in section
    ]
    assert missing == [], f"operator reference does not name: {missing}"


def test_applied_component_echo_typed_dict_keys_equal_the_registry() -> None:
    """The echo's TypedDict, the registry, and the ``echo[...] =`` sites are one vocabulary (mutation M8)."""
    assert tuple(typing.get_type_hints(common.AppliedComponentEcho)) == env.APPLIED_COMPONENT_KEYS
