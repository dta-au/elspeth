"""Measure shipped input names against models reached by registered handlers.

This is a deliberately bounded source census, not a control-flow proof. It
follows direct forwarding of the original input to module-bound Python
functions, and recognises BaseModel.model_validate and the Composer mutation
validator. Unsupported validation/forwarding raises CensusError. A missing
model is reported as absent, never supplied from the redaction manifest.

No repository walk is needed: live callable identities select source files.
The advisor interception is bound by a source-checked adapter. READ records
input-field extractions, not causal use or execution of every branch.
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


def _unwrap_forwarder(function: FunctionType) -> FunctionType:
    """Only cross wrappers whose actual body forwards all input unchanged."""
    visited: set[FunctionType] = set()
    while "__wrapped__" in vars(function):
        if function in visited:
            raise CensusError(f"{function.__qualname__}: cyclic wrapper identity")
        visited.add(function)
        wrapped = vars(function)["__wrapped__"]
        lines, _ = inspect.getsourcelines(function.__code__)
        definition = ast.parse(textwrap.dedent("".join(lines))).body[0]
        if not isinstance(definition, ast.FunctionDef):
            raise CensusError(f"{function.__qualname__}: unsupported wrapper syntax")
        arguments = definition.args
        statement = definition.body[0] if len(definition.body) == 1 else None
        call = statement.value if isinstance(statement, ast.Return) else None
        if (
            not isinstance(wrapped, FunctionType)
            or arguments.args
            or arguments.posonlyargs
            or arguments.kwonlyargs
            or arguments.vararg is None
            or arguments.kwarg is None
            or not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Name)
            or inspect.getclosurevars(function).nonlocals.get(call.func.id) is not wrapped
            or len(call.args) != 1
            or not isinstance(call.args[0], ast.Starred)
            or not _input_expression(call.args[0].value, arguments.vararg.arg)
            or len(call.keywords) != 1
            or call.keywords[0].arg is not None
            or not _input_expression(call.keywords[0].value, arguments.kwarg.arg)
        ):
            raise CensusError(f"{function.__qualname__}: unsupported wrapper forwarding identity")
        if not isinstance(wrapped, FunctionType):
            raise CensusError(f"{function.__qualname__}: unsupported wrapped callable identity")
        function = wrapped
    return function


def _model_for_handler(handler: FunctionType, *, input_parameter: str | None = None) -> tuple[type[BaseModel] | None, tuple[str, ...]]:
    """Resolve one original-input model, refusing ambiguity and opaque shapes.

    Parameter renaming across direct function forwarding is supported. Local
    aliases/rebinding, closures, computed model receivers, validation aliases,
    and alternate Pydantic validation methods are deliberately unsupported.
    """
    models: dict[type[BaseModel], set[str]] = {}
    visited: set[tuple[FunctionType, str]] = set()

    def scan(function: FunctionType, parameter: str) -> None:
        function = _unwrap_forwarder(function)
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
        awaited_calls = {id(node.value) for node in nodes if isinstance(node, ast.Await)}
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
                    target = _unwrap_forwarder(target)
                    if (
                        inspect.isgeneratorfunction(target)
                        or inspect.isasyncgenfunction(target)
                        or (inspect.iscoroutinefunction(target) and id(call) not in awaited_calls)
                    ):
                        raise CensusError(f"{site}: deferred helper body has no admission proof")
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
            validation_method = next(vars(base)["model_validate"] for base in model.__mro__ if "model_validate" in vars(base))
            if validation_method is not vars(BaseModel)["model_validate"]:
                raise CensusError(f"{site}: overridden model validation identity")
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


@dataclass(frozen=True)
class _ToolInput:
    name: str
    shipped: frozenset[str]
    handler: FunctionType | None
    parameter: str | None
    unresolved: str | None = None


def _tool_inputs() -> tuple[_ToolInput, ...]:
    """One live callable universe shared by MODEL and READ."""
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
    inputs: list[_ToolInput] = []
    handler: object
    for definition in definitions:
        name = definition["name"]
        shipped = frozenset(definition["parameters"]["properties"])
        input_parameter = None
        if name in _INTERCEPTED_TOOL_NAMES:
            try:
                handler = _advisor_admission_handler()
            except CensusError as exc:
                inputs.append(_ToolInput(name, shipped, None, None, str(exc)))
                continue
            input_parameter = "arguments"
        else:
            handler = handlers[name]
        if not isinstance(handler, FunctionType):
            raise CensusError(f"{name}: registered handler is not a Python function")
        inputs.append(_ToolInput(name, shipped, handler, input_parameter))
    return tuple(inputs)


def census_model_wire() -> dict[str, ModelWireRow]:
    rows: dict[str, ModelWireRow] = {}
    for tool in _tool_inputs():
        name, shipped, handler = tool.name, tool.shipped, tool.handler
        if handler is None:
            rows[name] = ModelWireRow(name, shipped, None, frozenset(), f"unresolved:{tool.unresolved}")
            continue
        try:
            model, sites = _model_for_handler(handler, input_parameter=tool.parameter)
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
class ReadSite:
    field: str
    site: str
    callers: tuple[str, ...]


@dataclass(frozen=True)
class ReadWireRow:
    tool: str
    shipped: frozenset[str]
    read: frozenset[str]
    extractions: tuple[ReadSite, ...]
    presence: tuple[ReadSite, ...]
    unresolved: tuple[str, ...]


@dataclass(frozen=True)
class _ReadOrigin:
    model: type[BaseModel] | None = None
    fields: frozenset[str] | None = None


def _reads_for_handler(
    handler: FunctionType, *, input_parameter: str | None = None
) -> tuple[tuple[ReadSite, ...], tuple[ReadSite, ...], tuple[str, ...]]:
    """Observe original-input extraction, retaining unsupported provenance.

    This is intentionally not use analysis. Once a field is extracted, its
    subsequent transformation is outside this pass. Whole-input and admitted
    instance forwarding remain provenance obligations; dumps confer no fields.
    """
    extractions: set[ReadSite] = set()
    presence: set[ReadSite] = set()
    unresolved: set[str] = set()
    active: set[tuple[FunctionType, str, _ReadOrigin, str]] = set()

    def scan(
        function: FunctionType,
        parameter: str,
        origin: _ReadOrigin,
        callers: tuple[str, ...],
        *,
        closure: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
        captures: dict[str, _ReadOrigin] | None = None,
        source_start: int | None = None,
        enclosing_names: frozenset[str] = frozenset(),
    ) -> _ReadOrigin | None:
        try:
            function = _unwrap_forwarder(function)
        except CensusError as exc:
            unresolved.add(str(exc))
            return None
        identity = f"{function.__module__}.{function.__qualname__}"
        if closure is not None:
            identity += f".<locals>.{closure.name}"
        key = (function, parameter, origin, identity)
        if key in active:
            unresolved.add(f"{identity}: recursive input forwarding")
            return None
        active.add(key)
        if closure is None:
            lines, start = inspect.getsourcelines(function)
            definition = ast.parse(textwrap.dedent("".join(lines))).body[0]
        else:
            assert source_start is not None
            start = source_start
            definition = closure
        nodes = _body_nodes(definition)
        awaited_calls = {id(node.value) for node in nodes if isinstance(node, ast.Await)}
        stores: dict[str, int] = {}
        for node in nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                stores[node.id] = stores.get(node.id, 0) + 1
        local_names = set(stores) | set(enclosing_names)
        if closure is None:
            local_names.update(inspect.signature(function).parameters)
        nested = {node.name: node for node in nodes if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
        nested_definitions = [node.name for node in nodes if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)]
        lambdas = {
            node.targets[0].id: node.value
            for node in nodes
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Lambda)
        }
        declared_classes = {node.name for node in nodes if isinstance(node, ast.ClassDef)}
        local_names.update(nested)
        local_names.update(declared_classes)
        imported_names: set[str] = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                imported_names.update(alias.asname or alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_names.update(alias.asname or alias.name for alias in node.names)
        local_names.update(imported_names)
        mutated = {
            node.value.id
            for node in nodes
            if isinstance(node, ast.Subscript | ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and isinstance(node.value, ast.Name)
        }
        mutated.update(
            node.func.value.id
            for node in nodes
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in {"clear", "pop", "popitem", "update", "setdefault", "__setitem__", "__delitem__"}
        )
        bindings: dict[str, _ReadOrigin] = {parameter: origin} if captures is None else dict(captures)
        local_names.update(bindings)
        for name in tuple(bindings):
            if name in stores or name in mutated or name in imported_names or name in nested or name in declared_classes:
                unresolved.add(f"{identity}:{start}: original input rebound or mutated")
                del bindings[name]
        cache: dict[int, _ReadOrigin | None] = {}
        call_chain = (*callers, identity)

        def site(node: ast.expr | ast.stmt) -> str:
            return f"{identity}:{start + node.lineno - 1}"

        def resolve(node: ast.expr) -> object | None:
            try:
                return _resolve(node, function, local_names)
            except CensusError:
                return None

        def record(field: ast.expr, node: ast.expr, *, membership: bool = False) -> None:
            if not isinstance(field, ast.Constant) or not isinstance(field.value, str):
                unresolved.add(f"{site(node)}: dynamic input key")
                return
            target = presence if membership else extractions
            target.add(ReadSite(field.value, site(node), call_chain))

        def admitted(model_node: ast.expr, value: _ReadOrigin | None, node: ast.Call) -> _ReadOrigin | None:
            if value is None:
                return None
            model = resolve(model_node)
            if value.model is not None or not isinstance(model, type) or not issubclass(model, BaseModel):
                unresolved.add(f"{site(node)}: unsupported model admission identity")
                return None
            validation_method = next(vars(base)["model_validate"] for base in model.__mro__ if "model_validate" in vars(base))
            if validation_method is not vars(BaseModel)["model_validate"]:
                unresolved.add(f"{site(node)}: overridden model validation identity")
                return None
            if any(field.alias is not None or field.validation_alias is not None for field in model.model_fields.values()):
                unresolved.add(f"{site(node)}: model validation aliases")
                return None
            return _ReadOrigin(model)

        def expression(node: ast.expr) -> _ReadOrigin | None:
            if id(node) in cache:
                return cache[id(node)]
            cache[id(node)] = None
            result = evaluate(node)
            cache[id(node)] = result
            return result

        def evaluate(node: ast.expr) -> _ReadOrigin | None:
            if isinstance(node, ast.Name):
                return bindings.get(node.id)
            if isinstance(node, ast.Await):
                return expression(node.value)
            if isinstance(node, ast.Lambda):
                return None
            if isinstance(node, ast.Attribute):
                receiver = expression(node.value)
                if (
                    receiver is not None
                    and receiver.model is not None
                    and node.attr in receiver.model.model_fields
                    and (receiver.fields is None or node.attr in receiver.fields)
                ):
                    extractions.add(ReadSite(node.attr, site(node), call_chain))
                return None
            if isinstance(node, ast.Subscript):
                receiver = expression(node.value)
                if receiver is not None:
                    if receiver.model is None and isinstance(node.ctx, ast.Load):
                        record(node.slice, node)
                    elif receiver.model is not None:
                        unresolved.add(f"{site(node)}: subscript on admitted model")
                expression(node.slice)
                return None
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                for index, operator in enumerate(node.ops):
                    if isinstance(operator, ast.In | ast.NotIn) and expression(operands[index + 1]) is not None:
                        record(operands[index], node, membership=True)
                for operand in operands:
                    expression(operand)
                return None
            if not isinstance(node, ast.Call):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.expr) and expression(child) is not None:
                        unresolved.add(f"{site(node)}: unsupported whole-input construction")
                return None

            args = [expression(arg) for arg in node.args]
            kwargs = {keyword.arg: expression(keyword.value) for keyword in node.keywords}
            lambda_body = (
                node.func if isinstance(node.func, ast.Lambda) else lambdas.get(node.func.id) if isinstance(node.func, ast.Name) else None
            )
            if lambda_body is not None and any(_mentions_input(lambda_body, name) for name in bindings):
                unresolved.add(f"{site(node)}: unsupported invoked lambda closure")
                return None
            target = resolve(node.func)
            if target is cast:
                return args[1] if len(args) == 2 and not node.keywords else None
            if target is _validate_mutation_arguments:
                if len(args) == 3 and not node.keywords:
                    return admitted(node.args[0], args[1], node)
                unresolved.add(f"{site(node)}: unsupported mutation admission")
                return None
            if isinstance(node.func, ast.Attribute):
                receiver = expression(node.func.value)
                if node.func.attr.startswith("model_validate") and any(value is not None for value in args):
                    if node.func.attr == "model_validate" and len(args) == 1 and not node.keywords:
                        return admitted(node.func.value, args[0], node)
                    unresolved.add(f"{site(node)}: unsupported model validation form")
                    return None
                if receiver is not None:
                    if receiver.model is None and node.func.attr == "get":
                        if node.args:
                            record(node.args[0], node)
                        else:
                            unresolved.add(f"{site(node)}: missing literal input key")
                        return None
                    if node.func.attr == "model_dump":
                        unresolved.add(f"{site(node)}: bulk model dump has no per-field extraction proof")
                        return None
                    if node.func.attr == "model_copy":
                        copy_method = (
                            next((vars(base)["model_copy"] for base in receiver.model.__mro__ if "model_copy" in vars(base)), None)
                            if receiver.model is not None
                            else None
                        )
                        if (
                            receiver.model is not None
                            and copy_method is BaseModel.model_copy
                            and not node.args
                            and len(node.keywords) == 1
                            and node.keywords[0].arg == "update"
                            and isinstance(node.keywords[0].value, ast.Dict)
                        ):
                            updates = node.keywords[0].value.keys
                            if all(
                                isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in receiver.model.model_fields
                                for key in updates
                            ):
                                replaced = frozenset(key.value for key in updates if isinstance(key, ast.Constant))
                                available = frozenset(receiver.model.model_fields) if receiver.fields is None else receiver.fields
                                return _ReadOrigin(receiver.model, available - replaced)
                        unresolved.add(f"{site(node)}: unsupported admitted model copy")
                        return None
                    if receiver.model is None and node.func.attr in {"keys", "items", "values"}:
                        return None
                    method = None if receiver.model is None else vars(receiver.model).get(node.func.attr)
                    if isinstance(method, FunctionType) and not node.args and not node.keywords:
                        if deferred(method, node):
                            return None
                        return scan(method, next(iter(inspect.signature(method).parameters)), receiver, call_chain)
                    unresolved.add(f"{site(node)}: unsupported input method {node.func.attr}")
                    return None
            if isinstance(node.func, ast.Name) and node.func.id in nested:
                captures = any(_mentions_input(nested[node.func.id], name) for name in bindings)
                if captures or any(value is not None for value in [*args, *kwargs.values()]):
                    child = nested[node.func.id]
                    if (
                        node.args
                        or node.keywords
                        or child.decorator_list
                        or isinstance(child, ast.AsyncFunctionDef)
                        or any(isinstance(part, ast.Yield | ast.YieldFrom) for part in _body_nodes(child))
                        or child.args.args
                        or child.args.posonlyargs
                        or child.args.kwonlyargs
                        or child.args.vararg is not None
                        or child.args.kwarg is not None
                        or node.func.id in stores
                        or node.func.id in imported_names
                        or nested_definitions.count(node.func.id) != 1
                        or child.lineno >= node.lineno
                    ):
                        unresolved.add(f"{site(node)}: unsupported invoked closure identity")
                    else:
                        return scan(
                            function,
                            parameter,
                            origin,
                            call_chain,
                            closure=child,
                            captures={name: value for name, value in bindings.items() if _mentions_input(child, name)},
                            source_start=start,
                            enclosing_names=frozenset(local_names),
                        )
                return None
            forwarded = any(value is not None for value in [*args, *kwargs.values()])
            if not forwarded:
                return None
            if target is dict and len(args) == 1 and not node.keywords:
                if args[0] is not None and args[0].model is None:
                    return args[0]
                unresolved.add(f"{site(node)}: bulk model-to-dict transformation")
                return None
            if target in (isinstance, len, bool, str, type, set, frozenset, sorted):
                return None
            if not isinstance(target, FunctionType) or None in kwargs:
                unresolved.add(f"{site(node)}: unsupported callable receiving input provenance")
                return None
            if deferred(target, node):
                return None
            # Unknown ** forwarding was refused above; bind only named keys.
            named_kwargs = {name: value for name, value in kwargs.items() if name is not None}
            try:
                bound = inspect.signature(target).bind_partial(*args, **named_kwargs)
            except TypeError:
                unresolved.add(f"{site(node)}: unsupported helper argument binding")
                return None
            returned = []
            for name, value in bound.arguments.items():
                if isinstance(value, _ReadOrigin):
                    returned.append(scan(target, name, value, call_chain))
                elif isinstance(value, tuple | dict):
                    unresolved.add(f"{site(node)}: variadic helper input forwarding")
            return returned[0] if returned and all(value == returned[0] for value in returned) else None

        def deferred(target: FunctionType, node: ast.Call) -> bool:
            try:
                target = _unwrap_forwarder(target)
            except CensusError as exc:
                unresolved.add(str(exc))
                return True
            if (
                inspect.isgeneratorfunction(target)
                or inspect.isasyncgenfunction(target)
                or (inspect.iscoroutinefunction(target) and id(node) not in awaited_calls)
            ):
                unresolved.add(f"{site(node)}: deferred helper body has no extraction proof")
                return True
            return False

        returns: list[_ReadOrigin | None] = []
        for node in nodes:
            if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id in nested
                    and any(_mentions_input(nested[node.value.id], name) for name in bindings)
                ):
                    unresolved.add(f"{site(node)}: captured closure alias has unresolved provenance")
                value = expression(node.value)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is not None:
                    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                        unresolved.add(f"{site(node)}: unsupported input assignment")
                    else:
                        name = targets[0].id
                        if stores[name] != 1 or name in mutated or name in imported_names or name in nested or name in declared_classes:
                            unresolved.add(f"{site(node)}: input binding rebound or mutated: {name}")
                        elif isinstance(node.value, ast.Name):
                            unresolved.add(f"{site(node)}: input alias: {name}")
                        else:
                            bindings[name] = value
            elif isinstance(node, ast.Return) and node.value is not None:
                returns.append(expression(node.value))
            elif isinstance(node, ast.expr):
                expression(node)
        active.remove(key)
        return returns[0] if returns and all(value == returns[0] for value in returns) else None

    parameter = input_parameter or next(iter(inspect.signature(handler).parameters))
    scan(handler, parameter, _ReadOrigin(), ())

    def sort_key(item: ReadSite) -> tuple[str, str, tuple[str, ...]]:
        return item.field, item.site, item.callers

    return tuple(sorted(extractions, key=sort_key)), tuple(sorted(presence, key=sort_key)), tuple(sorted(unresolved))


def census_read_wire() -> dict[str, ReadWireRow]:
    """Read the shared live catalog; unresolved rows retain partial fields."""
    rows: dict[str, ReadWireRow] = {}
    for tool in _tool_inputs():
        if tool.handler is None:
            rows[tool.name] = ReadWireRow(tool.name, tool.shipped, frozenset(), (), (), (str(tool.unresolved),))
            continue
        evidence, presence, unresolved = _reads_for_handler(tool.handler, input_parameter=tool.parameter)
        rows[tool.name] = ReadWireRow(tool.name, tool.shipped, frozenset(item.field for item in evidence), evidence, presence, unresolved)
    return rows


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
    read_rows = census_read_wire()
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
                    "read": sorted(read_rows[row.tool].read),
                    "shipped_not_read": sorted(row.shipped - read_rows[row.tool].read),
                    "read_not_shipped": sorted(read_rows[row.tool].read - row.shipped),
                    "read_unresolved": list(read_rows[row.tool].unresolved),
                    "read_sites": [
                        {"field": item.field, "site": item.site, "callers": list(item.callers)} for item in read_rows[row.tool].extractions
                    ],
                    "presence_sites": [
                        {"field": item.field, "site": item.site, "callers": list(item.callers)} for item in read_rows[row.tool].presence
                    ],
                    "taught": sorted(taught_rows[row.tool].taught),
                    "untaught": sorted(row.shipped - taught_rows[row.tool].taught),
                    "stale_argument_declarations": sorted(taught_rows[row.tool].declared - row.shipped),
                }
                for row in census_model_wire().values()
            ],
            indent=2,
        )
    )
