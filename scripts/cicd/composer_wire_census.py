"""Measure shipped input names against models reached by registered handlers.

This is a deliberately bounded source census, not a control-flow proof. It
follows direct forwarding of the original input to module-bound Python
functions, and recognises BaseModel.model_validate and the Composer mutation
validator. Unsupported validation/forwarding raises CensusError. A missing
model is reported as absent, never supplied from the redaction manifest.

No repository walk is needed: live callable identities select source files.
The advisor interception has no registered callable and remains explicitly
unresolved. This census makes no claim that validation runs on every branch.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import json
import runpy
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType, ModuleType
from typing import cast

from pydantic import BaseModel

from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.redaction import MANIFEST
from elspeth.web.composer.tools._common import _validate_mutation_arguments
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
from elspeth.web.composer.tools.sessions import _SESSION_AWARE_TOOL_HANDLERS

# Explicitly emitted by _dispatch and intercepted by ComposerServiceImpl;
# absence from the callable registry must never silently admit another tool.
_INTERCEPTED_TOOL_NAMES = frozenset({"request_advisor_hint"})


class CensusError(RuntimeError):
    """The supported source shapes cannot establish unambiguous ownership."""


@dataclass(frozen=True)
class ModelWireRow:
    tool: str
    shipped: frozenset[str]
    model_class: str | None
    model_fields: frozenset[str]
    site: str


def _body_nodes(node: ast.AST) -> list[ast.AST]:
    """Walk executable syntax without attributing deferred nested bodies."""
    result: list[ast.AST] = []
    for child in ast.iter_child_nodes(node):
        result.append(child)
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            result.extend(_body_nodes(child))
    return result


def _resolve(node: ast.expr, function: FunctionType, local_names: set[str]) -> object:
    if isinstance(node, ast.Name):
        if node.id in local_names:
            raise CensusError(f"{function.__qualname__}: local binding {node.id} cannot establish validation ownership")
        if node.id not in function.__globals__:
            if node.id in vars(builtins):
                return vars(builtins)[node.id]
            raise CensusError(f"{function.__qualname__}: unresolved binding {node.id}")
        return function.__globals__[node.id]
    if isinstance(node, ast.Attribute):
        owner = _resolve(node.value, function, local_names)
        if isinstance(owner, ModuleType) and node.attr in vars(owner):
            return vars(owner)[node.attr]
    raise CensusError(f"{function.__qualname__}: unsupported binding {ast.unparse(node)}")


def _input_expression(node: ast.expr, parameter: str) -> bool:
    return isinstance(node, ast.Name) and node.id == parameter


def _mentions_input(node: ast.AST, parameter: str) -> bool:
    return any(isinstance(part, ast.Name) and part.id == parameter for part in ast.walk(node))


def _model_for_handler(handler: FunctionType, *, input_parameter: str | None = None) -> tuple[type[BaseModel] | None, tuple[str, ...]]:
    """Resolve one original-input model, refusing ambiguity and opaque shapes.

    Parameter renaming across direct function forwarding is supported. Local
    aliases/rebinding, closures, computed model receivers, validation aliases,
    and alternate Pydantic validation methods are deliberately unsupported.
    """
    models: dict[type[BaseModel], set[str]] = {}
    visited: set[tuple[FunctionType, str]] = set()

    def scan(function: FunctionType, parameter: str) -> None:
        function = inspect.unwrap(function)
        key = (function, parameter)
        if key in visited:
            raise CensusError(f"{function.__qualname__}: recursive argument forwarding")
        visited.add(key)
        lines, start = inspect.getsourcelines(function)
        tree = ast.parse(textwrap.dedent("".join(lines)))
        definition = tree.body[0]
        if not isinstance(definition, ast.FunctionDef | ast.AsyncFunctionDef):
            raise CensusError(f"{function.__qualname__}: unsupported handler syntax")
        nodes = _body_nodes(definition)
        nested_functions = {node.name: node for node in nodes if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        local_names = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        local_names.update(node.name for node in nodes if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef))
        for node in nodes:
            if isinstance(node, ast.Import):
                local_names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                local_names.update(alias.asname or alias.name for alias in node.names)
        local_names.update(inspect.signature(function).parameters)
        # Candidate preparation owns a complete shallow copy for presence
        # evidence. Prove the copy and refuse mutation before treating its
        # loads as original-input provenance; selected-key copies never qualify.
        copies: dict[str, ast.Assign] = {}
        for node in nodes:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and len(node.value.args) == 1
                and not node.value.keywords
                and _input_expression(node.value.args[0], parameter)
                and _resolve(node.value.func, function, local_names) is dict
            ):
                copies[node.targets[0].id] = node
        for alias, assignment in copies.items():
            stores = [node for node in nodes if isinstance(node, ast.Name) and node.id == alias and isinstance(node.ctx, ast.Store)]
            if len(stores) != 1:
                raise CensusError(f"{function.__qualname__}: input copy rebound")
            for node in nodes:
                if isinstance(node, ast.Subscript) and _input_expression(node.value, alias) and isinstance(node.ctx, ast.Store | ast.Del):
                    raise CensusError(f"{function.__qualname__}: input copy mutated")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and _input_expression(node.func.value, alias)
                    and node.func.attr not in {"get", "keys", "items", "values"}
                ):
                    raise CensusError(f"{function.__qualname__}: unsupported input copy method")
            excluded = {id(node) for node in ast.walk(assignment)}
            nodes = [node for node in nodes if id(node) not in excluded]
            for node in nodes:
                if isinstance(node, ast.Name) and node.id == alias:
                    node.id = parameter
        if any(isinstance(node, ast.Name) and node.id == parameter and isinstance(node.ctx, ast.Store) for node in nodes):
            raise CensusError(f"{function.__qualname__}: original input rebound")
        for node in nodes:
            if isinstance(node, ast.Subscript) and _mentions_input(node.value, parameter) and isinstance(node.ctx, ast.Store | ast.Del):
                raise CensusError(f"{function.__qualname__}: original input mutated")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and _input_expression(node.func.value, parameter)
                and node.func.attr not in {"get", "keys", "items", "values"}
            ):
                raise CensusError(f"{function.__qualname__}: unsupported original input method")
            if (
                isinstance(node, ast.Assign | ast.AnnAssign)
                and isinstance(node.value, ast.Dict | ast.DictComp)
                and _mentions_input(node.value, parameter)
            ):
                raise CensusError(f"{function.__qualname__}: selected-key reconstruction is not original input")
            if isinstance(node, ast.Dict) and any(
                key is None and _mentions_input(value, parameter) for key, value in zip(node.keys, node.values, strict=True)
            ):
                raise CensusError(f"{function.__qualname__}: unsupported input dictionary unpacking")
            if (
                isinstance(node, ast.Assign | ast.AnnAssign | ast.NamedExpr)
                and node.value is not None
                and _input_expression(node.value, parameter)
            ):
                raise CensusError(f"{function.__qualname__}: unsupported input alias")
        for call in (node for node in nodes if isinstance(node, ast.Call)):
            site = f"{function.__module__}.{function.__qualname__}:{start + call.lineno - 1}"
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in nested_functions
                and _mentions_input(nested_functions[call.func.id], parameter)
            ):
                raise CensusError(f"{site}: invoked closure captures original input")
            receiver: ast.expr | None = None
            value: ast.expr | None = None
            is_validation = isinstance(call.func, ast.Attribute) and call.func.attr.startswith("model_validate")
            if is_validation:
                assert isinstance(call.func, ast.Attribute)
                values = [*call.args, *(keyword.value for keyword in call.keywords if keyword.arg == "obj")]
                if len(values) != 1:
                    raise CensusError(f"{site}: unsupported validation input")
                value = values[0]
                if not _input_expression(value, parameter):
                    # Literal independent input is demonstrably unrelated. Anything
                    # transformed from the original input is not full-input proof.
                    if _mentions_input(value, parameter) or isinstance(value, ast.Name | ast.Call):
                        raise CensusError(f"{site}: unsupported validation input {ast.unparse(value)}")
                    continue
                if call.func.attr != "model_validate" or call.keywords:
                    raise CensusError(f"{site}: unsupported validation construct")
                receiver = call.func.value
            else:
                if any(
                    not _input_expression(argument, parameter) and _mentions_input(argument, parameter)
                    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]
                ):
                    if _resolve(call.func, function, local_names) is cast:
                        # typing.cast returns its value unchanged; executable
                        # child calls remain in nodes and are scanned normally.
                        continue
                    raise CensusError(f"{site}: unsupported transformed input forwarding")
                forwarded = [(index, arg) for index, arg in enumerate(call.args) if _input_expression(arg, parameter)]
                forwarded_keywords = [keyword for keyword in call.keywords if _input_expression(keyword.value, parameter)]
                if not forwarded and not forwarded_keywords:
                    continue
                target = _resolve(call.func, function, local_names)
                if target is dict:
                    raise CensusError(f"{site}: unsupported input copy")
                if target in (isinstance, len, bool, str, type, set, frozenset, sorted):
                    continue
                if target is _validate_mutation_arguments:
                    if len(call.args) != 3 or call.keywords or not _input_expression(call.args[1], parameter):
                        raise CensusError(f"{site}: unsupported mutation validation construct")
                    receiver = call.args[0]
                elif isinstance(target, FunctionType):
                    parameters = list(inspect.signature(target).parameters)
                    for index, _ in forwarded:
                        if index >= len(parameters):
                            raise CensusError(f"{site}: unsupported argument forwarding")
                        scan(target, parameters[index])
                    for keyword in forwarded_keywords:
                        if keyword.arg is None or keyword.arg not in parameters:
                            raise CensusError(f"{site}: unsupported keyword forwarding")
                        scan(target, keyword.arg)
                    continue
                else:
                    raise CensusError(f"{site}: unsupported callable receiving original input")
            assert receiver is not None
            model = _resolve(receiver, function, local_names)
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise CensusError(f"{site}: validation receiver is not a BaseModel class")
            if any(field.alias is not None or field.validation_alias is not None for field in model.model_fields.values()):
                raise CensusError(f"{site}: validation aliases are unsupported")
            models.setdefault(model, set()).add(site)
        visited.remove(key)

    parameters = list(inspect.signature(handler).parameters)
    if not parameters:
        raise CensusError(f"{handler.__qualname__}: handler has no input parameter")
    scan(handler, input_parameter if input_parameter is not None else parameters[0])
    if len(models) > 1:
        names = sorted(f"{model.__module__}.{model.__qualname__}" for model in models)
        raise CensusError(f"{handler.__qualname__}: ambiguous input models {names}")
    if not models:
        return None, ()
    model, sites = next(iter(models.items()))
    return model, tuple(sorted(sites))


def census_model_wire() -> dict[str, ModelWireRow]:
    handlers: dict[str, object] = {declaration.name: declaration.handler for declaration in _REGISTERED_TOOLS}
    if len(handlers) != len(_REGISTERED_TOOLS) or handlers.keys() & _SESSION_AWARE_TOOL_HANDLERS.keys():
        raise CensusError("duplicate registered handler names")
    handlers.update(_SESSION_AWARE_TOOL_HANDLERS)
    definitions = get_tool_definitions()
    shipped_names = [definition["name"] for definition in definitions]
    if len(set(shipped_names)) != len(shipped_names):
        raise CensusError("duplicate shipped tool names")
    expected = set(handlers) | _INTERCEPTED_TOOL_NAMES
    if set(shipped_names) != expected:
        raise CensusError(
            f"shipped/handler universe mismatch: missing={sorted(expected - set(shipped_names))}, "
            f"unexpected={sorted(set(shipped_names) - expected)}"
        )
    rows: dict[str, ModelWireRow] = {}
    handler: object
    for definition in definitions:
        name = definition["name"]
        shipped = frozenset(definition["parameters"]["properties"])
        input_parameter = None
        if name in _INTERCEPTED_TOOL_NAMES:
            try:
                handler = _advisor_admission_handler()
            except CensusError as exc:
                rows[name] = ModelWireRow(name, shipped, None, frozenset(), f"unresolved:{exc}")
                continue
            input_parameter = "arguments"
        else:
            handler = handlers[name]
        if not isinstance(handler, FunctionType):
            raise CensusError(f"{name}: registered handler is not a Python function")
        try:
            model, sites = _model_for_handler(handler, input_parameter=input_parameter)
        except CensusError as exc:
            rows[name] = ModelWireRow(name, shipped, None, frozenset(), f"unresolved:{exc}")
            continue
        handler_site = f"handler:{handler.__module__}.{handler.__qualname__}"
        rows[name] = ModelWireRow(
            name,
            shipped,
            None if model is None else f"{model.__module__}.{model.__qualname__}",
            frozenset() if model is None else frozenset(model.model_fields),
            handler_site + (" -> " + ", ".join(sites) if sites else " (no input model found)"),
        )
    return rows


def _advisor_admission_handler() -> FunctionType:
    """Bind the public interception to its called service admission method.

    This deliberately supports the one owned batch adapter, not arbitrary
    method dispatch. A changed receiver, copied subset, or missing call fails
    closed and requires an explicit provenance extension.
    """
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.tool_batch import run_tool_batch

    tree = ast.parse(textwrap.dedent(inspect.getsource(run_tool_batch)))
    branches = [
        node for node in ast.walk(tree) if isinstance(node, ast.If) and ast.unparse(node.test) == "tool_name == 'request_advisor_hint'"
    ]
    calls = [
        node
        for branch in branches
        for statement in branch.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_validate_advisor_arguments"
    ]
    if len(calls) != 1:
        raise CensusError("advisor interception must call exactly one public admission")
    call = calls[0]
    assert isinstance(call.func, ast.Attribute)
    if (
        ast.unparse(call.func.value) != "ctx.service"
        or call.keywords
        or len(call.args) != 1
        or not _input_expression(call.args[0], "arguments")
    ):
        raise CensusError("advisor interception does not forward complete original arguments")
    handler = vars(ComposerServiceImpl)[call.func.attr]
    if not isinstance(handler, FunctionType):
        raise CensusError("advisor admission is not an owned service method")
    return handler


def census_redaction_models() -> dict[str, type[BaseModel] | None]:
    """Report manifest ownership separately; this does not prove validation."""
    return {name: entry.argument_model for name, entry in MANIFEST.items()}


@dataclass(frozen=True)
class TaughtWireRow:
    tool: str
    shipped: frozenset[str]
    taught: frozenset[str]
    declared: frozenset[str]


def census_taught_wire() -> dict[str, TaughtWireRow]:
    """Measure own-context teaching using the same authority as the response gate.

    Load the small helper by its explicit repository path. Direct script
    execution has only the two source roots on PYTHONPATH; importing ``tests``
    would depend on pytest path injection or an unrelated installed package.
    run_path gives the helper a bounded namespace and imports no test gate.
    """
    helper_path = Path(__file__).resolve().parents[2] / "tests/unit/web/composer/_teaching_gate_support.py"
    helper = runpy.run_path(str(helper_path))
    reader = cast(
        Callable[..., dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]]],
        helper["argument_teaching"],
    )
    return {
        name: TaughtWireRow(name, shipped, taught, declared)
        for name, (shipped, taught, declared) in reader(get_tool_definitions(), build_system_prompt(None)).items()
    }


if __name__ == "__main__":
    redaction_models = census_redaction_models()
    taught_rows = census_taught_wire()
    print(
        json.dumps(
            [
                {
                    "tool": row.tool,
                    "shipped": sorted(row.shipped),
                    "model_class": row.model_class,
                    "redaction_model_class": (
                        None
                        if (redaction_model := redaction_models[row.tool]) is None
                        else f"{redaction_model.__module__}.{redaction_model.__qualname__}"
                    ),
                    "model_fields": sorted(row.model_fields),
                    "site": row.site,
                    "shipped_not_model": sorted(row.shipped - row.model_fields),
                    "model_not_shipped": sorted(row.model_fields - row.shipped),
                    "taught": sorted(taught_rows[row.tool].taught),
                    "untaught": sorted(row.shipped - taught_rows[row.tool].taught),
                    "stale_argument_declarations": sorted(taught_rows[row.tool].declared - row.shipped),
                }
                for row in census_model_wire().values()
            ],
            indent=2,
        )
    )
