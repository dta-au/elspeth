"""Runtime-VAL manifest builder (ADR-010 §Decision 3 M3).

Extended under ADR-010 §H2 landing scope N1: the serialized manifest now carries
per-contract dispatch-site claims so the runs-row records not just *which*
contracts were active during run X but *which dispatch sites* each
contract implemented.

At orchestrator bootstrap the declaration-contract registry
(``EXPECTED_CONTRACT_SITES`` / ``registered_declaration_contracts``) and
the Tier-1 error registry (``TIER_1_ERRORS``) are both frozen. The M3
finding requires these to be serialized into the Landscape run-header so
an auditor can answer:

- "Which VAL contracts were in force during run X?"
- "Which dispatch sites did contract Y implement during run X?" (N1)
- "Was the ``can_drop_rows`` contract active during run X?" (time-series audit)
- "Are the TIER_1_ERRORS the same across runs X and Y?" (regression detection)

Shape:

    {
      "declaration_contracts": [
        {"name": "passes_through_input",
         "class_name": "PassThroughDeclarationContract",
         "class_module": "elspeth.engine.executors.pass_through",
         "dispatch_sites": ["batch_flush_check", "post_emission_check"],
         "implementation_hash": "sha256:..."},
        ...
      ],
      "expected_contract_sites": {
         "passes_through_input": ["batch_flush_check", "post_emission_check"],
         ...
      },
      "tier_1_errors": [...],
    }

Ordering is deterministic (name / class_name / site sort) so the
serialised form is stable and hashable for cross-run regression comparisons.
"""

from __future__ import annotations

import ast
import dataclasses
import dis
import hashlib
import inspect
import json
import re
import textwrap
from types import (
    BuiltinFunctionType,
    CodeType,
    FunctionType,
    GetSetDescriptorType,
    MemberDescriptorType,
    MethodDescriptorType,
    ModuleType,
    WrapperDescriptorType,
)
from typing import Any, NotRequired, Required, get_args, get_origin, get_type_hints, is_typeddict

from elspeth.contracts.declaration_contracts import (
    EXPECTED_CONTRACT_SITES,
    DeclarationContract,
    contract_sites,
    declaration_registry_is_frozen,
    registered_declaration_contracts,
)
from elspeth.contracts.tier_registry import (
    _TIER_1_ERRORS_VIEW,
    FrameworkBugError,
    tier_1_reason,
    tier_registry_is_frozen,
)


def _json_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


class _UnsupportedManifestValue:
    pass


_UNSUPPORTED_MANIFEST_VALUE = _UnsupportedManifestValue()
_MISSING_CLASS_ATTRIBUTE = object()


def _type_identity(cls: type[object]) -> str:
    module_name: object = cls.__module__
    qualname: object = cls.__qualname__
    if type(module_name) is not str or type(qualname) is not str:
        raise FrameworkBugError(
            "Runtime-VAL type identity requires string module and qualname; "
            f"got module={type(module_name).__name__!r}, qualname={type(qualname).__name__!r}"
        )
    return f"{module_name}:{qualname}"


def _function_module(func: FunctionType) -> str:
    module_name: object = func.__module__
    if type(module_name) is not str:
        raise FrameworkBugError(f"Runtime-VAL callable {func.__qualname__!r} has invalid module identity {type(module_name).__name__!r}")
    return module_name


def _callable_dependency_key(func: FunctionType) -> str:
    return f"{_function_module(func)}:{func.__qualname__}"


def _module_is_owned(module_name: str, *, owner_module: str) -> bool:
    if module_name.startswith("elspeth."):
        return True
    return module_name == owner_module and (owner_module.startswith("tests.") or owner_module == "__main__")


def _normalized_sort_key(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _try_normalize_code_constant(value: object) -> object:
    if isinstance(value, CodeType):
        return {"code": _normalize_code_object(value)}
    if isinstance(value, tuple):
        tuple_items = [_try_normalize_code_constant(item) for item in value]
        if any(item is _UNSUPPORTED_MANIFEST_VALUE for item in tuple_items):
            return _UNSUPPORTED_MANIFEST_VALUE
        return {"tuple": tuple_items}
    if isinstance(value, list):
        list_items = [_try_normalize_code_constant(item) for item in value]
        if any(item is _UNSUPPORTED_MANIFEST_VALUE for item in list_items):
            return _UNSUPPORTED_MANIFEST_VALUE
        return {"list": list_items}
    if isinstance(value, (set, frozenset)):
        collection_items = [_try_normalize_code_constant(item) for item in value]
        if any(item is _UNSUPPORTED_MANIFEST_VALUE for item in collection_items):
            return _UNSUPPORTED_MANIFEST_VALUE
        key = "set" if isinstance(value, set) else "frozenset"
        return {key: sorted(collection_items, key=_normalized_sort_key)}
    if isinstance(value, dict):
        dict_items: list[dict[str, object]] = []
        for item_key, item_value in value.items():
            normalized_key = _try_normalize_code_constant(item_key)
            normalized_value = _try_normalize_code_constant(item_value)
            if normalized_key is _UNSUPPORTED_MANIFEST_VALUE or normalized_value is _UNSUPPORTED_MANIFEST_VALUE:
                return _UNSUPPORTED_MANIFEST_VALUE
            dict_items.append({"key": normalized_key, "value": normalized_value})
        return {"dict": sorted(dict_items, key=_normalized_sort_key)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, re.Pattern):
        normalized_pattern = _try_normalize_code_constant(value.pattern)
        if normalized_pattern is _UNSUPPORTED_MANIFEST_VALUE:
            return _UNSUPPORTED_MANIFEST_VALUE
        return {"regex": {"pattern": normalized_pattern, "flags": value.flags}}
    if isinstance(value, complex):
        return {"complex": [value.real, value.imag]}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if value is Ellipsis:
        return {"ellipsis": True}
    if value is Required:
        return {"typing_special_form": "Required"}
    if value is NotRequired:
        return {"typing_special_form": "NotRequired"}
    if value is dataclasses._HAS_DEFAULT_FACTORY:  # type: ignore[attr-defined]
        return {"dataclasses_default_factory_sentinel": True}
    annotation_origin = get_origin(value)
    if annotation_origin is not None:
        normalized_origin = _try_normalize_code_constant(annotation_origin)
        normalized_args = [_try_normalize_code_constant(arg) for arg in get_args(value)]
        if normalized_origin is _UNSUPPORTED_MANIFEST_VALUE or any(arg is _UNSUPPORTED_MANIFEST_VALUE for arg in normalized_args):
            return _UNSUPPORTED_MANIFEST_VALUE
        return {"annotation": {"origin": normalized_origin, "args": normalized_args}}
    if isinstance(value, type):
        return {"type": _type_identity(value)}
    if isinstance(value, BuiltinFunctionType):
        module_name = value.__module__
        if type(module_name) is str:
            return {"builtin": f"{module_name}:{value.__qualname__}"}
        bound_owner = value.__self__
        if isinstance(bound_owner, type):
            return {"builtin": f"{_type_identity(bound_owner)}:{value.__name__}"}
        return _UNSUPPORTED_MANIFEST_VALUE
    return _UNSUPPORTED_MANIFEST_VALUE


def _normalize_code_constant(value: object) -> object:
    normalized = _try_normalize_code_constant(value)
    if normalized is _UNSUPPORTED_MANIFEST_VALUE:
        raise FrameworkBugError(
            "Runtime-VAL manifest cannot deterministically normalize "
            f"owned value of type {type(value).__module__}.{type(value).__qualname__}"
        )
    return normalized


def _normalize_code_object(code: CodeType) -> dict[str, object]:
    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "instructions": [_normalize_instruction(instruction) for instruction in dis.get_instructions(code)],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "exceptiontable": code.co_exceptiontable.hex(),
    }


def _iter_code_objects(code: CodeType, *, path: str = "<root>") -> list[tuple[str, CodeType]]:
    code_objects = [(path, code)]
    for index, constant in enumerate(code.co_consts):
        if type(constant) is not CodeType:
            continue
        child_path = f"{path}/{constant.co_name}[{index}]"
        code_objects.extend(_iter_code_objects(constant, path=child_path))
    return code_objects


def _normalize_instruction(instruction: dis.Instruction) -> dict[str, object]:
    normalized: dict[str, object] = {"opname": instruction.opname}
    if instruction.opcode in dis.hasconst:
        normalized["const"] = _normalize_code_constant(instruction.argval)
    elif instruction.arg is not None:
        normalized["arg"] = instruction.arg
        normalized["argval"] = _normalize_code_constant(instruction.argval)
    return normalized


def _normalize_bound_value(
    value: object,
    *,
    binding_name: str,
    code: CodeType,
    load_opnames: frozenset[str],
    owner_module: str,
    seen_function_ids: frozenset[int],
    seen_dependency_ids: frozenset[int],
) -> object:
    qualified_values = _qualified_bound_values(
        code,
        value,
        binding_name=binding_name,
        load_opnames=load_opnames,
    )
    if inspect.isfunction(value):
        callable_key = _callable_dependency_key(value)
        if not _module_is_owned(_function_module(value), owner_module=owner_module):
            normalized_binding: dict[str, object] = {"external_callable": callable_key}
        elif id(value) in seen_function_ids:
            normalized_binding = {"callable_reference": callable_key}
        else:
            normalized_binding = {
                "callable": callable_key,
                "implementation": _callable_implementation_payload(
                    value,
                    seen_function_ids=seen_function_ids,
                    seen_dependency_ids=seen_dependency_ids,
                ),
                "dependencies": _callable_dependency_hashes(
                    value,
                    seen=seen_dependency_ids | frozenset({id(value)}),
                ),
            }
    elif isinstance(value, type):
        normalized_class = _normalize_class_binding(
            value,
            owner_module=owner_module,
            binding_name=binding_name,
            attribute_names=_loaded_binding_attribute_names(
                code,
                load_opnames=load_opnames,
                binding_name=binding_name,
            ),
            directly_called=_binding_is_directly_called(
                code,
                load_opnames=load_opnames,
                binding_name=binding_name,
            ),
            seen=seen_dependency_ids,
        )
        if not isinstance(normalized_class, dict):
            raise FrameworkBugError(f"Runtime-VAL class binding {binding_name!r} has invalid normalized payload")
        normalized_binding = normalized_class
    elif isinstance(value, ModuleType) and qualified_values:
        module_name: object = value.__name__
        if type(module_name) is not str:
            raise FrameworkBugError(f"Runtime-VAL module binding {binding_name!r} has invalid name identity")
        normalized_binding = {"module": module_name}
    else:
        normalized_value = _normalize_dependency_value(
            value,
            owner_module=owner_module,
            name=binding_name,
            seen=seen_dependency_ids,
        )
        if not isinstance(normalized_value, dict):
            normalized_binding = {"value": normalized_value}
        else:
            normalized_binding = normalized_value

    if qualified_values:
        bound_dependencies: dict[str, object] = {}
        for qualified_name, candidate, candidate_owner, directly_called in qualified_values:
            _add_resolved_dependency(
                bound_dependencies,
                owner_module=owner_module,
                dependency_scope=f"<bound:{binding_name}>",
                binding_name=qualified_name,
                candidate=candidate,
                candidate_owner=candidate_owner,
                directly_called=directly_called,
                seen=seen_dependency_ids,
            )
        normalized_binding["bound_dependencies"] = bound_dependencies
    return normalized_binding


def _normalize_callable_closure(
    func: FunctionType,
    *,
    seen_function_ids: frozenset[int],
    seen_dependency_ids: frozenset[int],
) -> list[dict[str, object]]:
    closure = func.__closure__
    if closure is None:
        if func.__code__.co_freevars:
            raise FrameworkBugError(f"Runtime-VAL callable {_callable_dependency_key(func)!r} has freevars but no closure")
        return []
    if len(closure) != len(func.__code__.co_freevars):
        raise FrameworkBugError(f"Runtime-VAL callable {_callable_dependency_key(func)!r} has inconsistent closure metadata")

    normalized_cells: list[dict[str, object]] = []
    for freevar_name, cell in zip(func.__code__.co_freevars, closure, strict=True):
        try:
            cell_value = cell.cell_contents
        except ValueError as exc:
            raise FrameworkBugError(
                f"Runtime-VAL callable {_callable_dependency_key(func)!r} has empty closure cell {freevar_name!r}"
            ) from exc
        normalized_cells.append(
            {
                "name": freevar_name,
                "value": _normalize_bound_value(
                    cell_value,
                    binding_name=freevar_name,
                    code=func.__code__,
                    load_opnames=frozenset({"LOAD_DEREF"}),
                    owner_module=_function_module(func),
                    seen_function_ids=seen_function_ids,
                    seen_dependency_ids=seen_dependency_ids,
                ),
            }
        )
    return normalized_cells


def _normalize_callable_defaults(
    func: FunctionType,
    *,
    seen_function_ids: frozenset[int],
    seen_dependency_ids: frozenset[int],
) -> dict[str, object]:
    code = func.__code__
    defaults = func.__defaults__
    normalized_positional: object = None
    if defaults is not None:
        first_default_index = code.co_argcount - len(defaults)
        normalized_positional = [
            {
                "name": code.co_varnames[first_default_index + index],
                "value": _normalize_bound_value(
                    value,
                    binding_name=code.co_varnames[first_default_index + index],
                    code=code,
                    load_opnames=frozenset({"LOAD_FAST", "LOAD_DEREF"}),
                    owner_module=_function_module(func),
                    seen_function_ids=seen_function_ids,
                    seen_dependency_ids=seen_dependency_ids,
                ),
            }
            for index, value in enumerate(defaults)
        ]

    kwdefaults = func.__kwdefaults__
    normalized_keyword_only: object = None
    if kwdefaults is not None:
        normalized_keyword_only = [
            {
                "name": name,
                "value": _normalize_bound_value(
                    kwdefaults[name],
                    binding_name=name,
                    code=code,
                    load_opnames=frozenset({"LOAD_FAST", "LOAD_DEREF"}),
                    owner_module=_function_module(func),
                    seen_function_ids=seen_function_ids,
                    seen_dependency_ids=seen_dependency_ids,
                ),
            }
            for name in sorted(kwdefaults)
        ]
    return {"positional": normalized_positional, "keyword_only": normalized_keyword_only}


def _callable_implementation_payload(
    func: FunctionType,
    *,
    seen_function_ids: frozenset[int],
    seen_dependency_ids: frozenset[int],
) -> dict[str, object]:
    next_seen_function_ids = seen_function_ids | frozenset({id(func)})
    next_seen_dependency_ids = seen_dependency_ids | frozenset({id(func)})
    return {
        "identity": _callable_dependency_key(func),
        "code": _normalize_code_object(func.__code__),
        "defaults": _normalize_callable_defaults(
            func,
            seen_function_ids=next_seen_function_ids,
            seen_dependency_ids=next_seen_dependency_ids,
        ),
        "annotations": _normalize_code_constant(func.__annotations__),
        "closure": _normalize_callable_closure(
            func,
            seen_function_ids=next_seen_function_ids,
            seen_dependency_ids=next_seen_dependency_ids,
        ),
    }


def _callable_implementation_hash(func: FunctionType, *, seen: frozenset[int] = frozenset()) -> str:
    return _json_hash(
        _callable_implementation_payload(
            func,
            seen_function_ids=seen,
            seen_dependency_ids=seen,
        )
    )


def _strip_docstrings(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not child.body:
            continue
        first = child.body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            child.body.pop(0)


def _class_source_hash(cls: type[object]) -> str:
    try:
        source = textwrap.dedent(inspect.getsource(cls))
    except (OSError, TypeError) as exc:
        raise FrameworkBugError(
            "Runtime-VAL manifest cannot hash source for "
            f"{cls.__module__}.{cls.__qualname__}: source unavailable. "
            "Classes recorded in the resume-trust manifest must be source-available."
        ) from exc
    tree = ast.parse(source)
    _strip_docstrings(tree)
    return _json_hash(ast.dump(tree, include_attributes=False))


def _unwrap_callable(attribute: object) -> FunctionType | None:
    candidate: object
    if isinstance(attribute, (staticmethod, classmethod)):
        candidate = attribute.__func__
    elif isinstance(attribute, property):
        candidate = attribute.fget
    else:
        candidate = attribute
    if inspect.isfunction(candidate):
        return candidate
    return None


def _runtime_val_helper_dependency(candidate: object, *, owner_module: str) -> FunctionType | None:
    if not inspect.isfunction(candidate):
        return None
    module_name = _function_module(candidate)
    if module_name == "builtins":
        return None
    if _module_is_owned(module_name, owner_module=owner_module):
        return candidate
    return None


def _static_class_attribute(cls: type[object], name: str) -> object:
    for base in cls.__mro__:
        if name in base.__dict__:
            attribute: object = base.__dict__[name]
            return attribute
    return _MISSING_CLASS_ATTRIBUTE


def _loaded_binding_attribute_names(
    code: CodeType,
    *,
    load_opnames: frozenset[str],
    binding_name: str,
) -> list[str]:
    names: set[str] = set()
    for _, nested_code in _iter_code_objects(code):
        instructions = list(dis.get_instructions(nested_code))
        for index, instruction in enumerate(instructions[:-1]):
            if instruction.opname not in load_opnames or instruction.argval != binding_name:
                continue
            attribute_instruction = instructions[index + 1]
            if attribute_instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
                continue
            attribute_name: str = attribute_instruction.argval
            names.add(attribute_name)
    return sorted(names)


def _binding_is_directly_called(
    code: CodeType,
    *,
    load_opnames: frozenset[str],
    binding_name: str,
) -> bool:
    for _, nested_code in _iter_code_objects(code):
        instructions = list(dis.get_instructions(nested_code))
        for index, instruction in enumerate(instructions):
            if instruction.opname not in load_opnames or instruction.argval != binding_name:
                continue
            if instruction.opname == "LOAD_GLOBAL" and instruction.arg is not None and instruction.arg & 1:
                return True
            if index > 0 and instructions[index - 1].opname == "PUSH_NULL":
                return True
            if index + 1 < len(instructions) and instructions[index + 1].opname == "PUSH_NULL":
                return True
    return False


def _callable_dependency_payload(
    candidate: FunctionType,
    *,
    seen: frozenset[int],
    owner_cls: type[object] | None,
) -> object:
    callable_key = _callable_dependency_key(candidate)
    if id(candidate) in seen:
        return {"callable_reference": callable_key}
    return {
        "implementation_hash": _callable_implementation_hash(candidate, seen=seen),
        "dependencies": _callable_dependency_hashes(candidate, seen=seen, owner_cls=owner_cls),
    }


def _normalize_class_binding(
    cls: type[object],
    *,
    owner_module: str,
    binding_name: str,
    attribute_names: list[str],
    directly_called: bool,
    seen: frozenset[int],
) -> object:
    identity = _type_identity(cls)
    cls_module = cls.__module__
    if not _module_is_owned(cls_module, owner_module=owner_module):
        return {"type": identity}

    attributes: dict[str, object] = {}
    names = set(attribute_names)
    if directly_called:
        names.update({"__init__", "__new__"})
        metaclass_call = _static_class_attribute(type(cls), "__call__")
        metaclass_callable = _unwrap_callable(metaclass_call)
        if metaclass_callable is not None:
            attributes["<metaclass>.__call__"] = _callable_dependency_payload(
                metaclass_callable,
                seen=seen,
                owner_cls=type(cls),
            )
    for name in sorted(names):
        attribute = _static_class_attribute(cls, name)
        if attribute is _MISSING_CLASS_ATTRIBUTE:
            continue
        callable_obj = _unwrap_callable(attribute)
        if callable_obj is not None:
            if _function_module(callable_obj) == "builtins":
                continue
            nested_owner_cls = None if isinstance(attribute, staticmethod) else cls
            attributes[name] = _callable_dependency_payload(
                callable_obj,
                seen=seen,
                owner_cls=nested_owner_cls,
            )
            continue
        if isinstance(attribute, (MemberDescriptorType, GetSetDescriptorType)):
            continue
        if isinstance(attribute, (MethodDescriptorType, WrapperDescriptorType)):
            attributes[name] = {"builtin_descriptor": f"{_type_identity(attribute.__objclass__)}:{attribute.__name__}"}
            continue
        attributes[name] = _normalize_dependency_value(
            attribute,
            owner_module=owner_module,
            name=f"{binding_name}.{name}",
            seen=seen,
        )
    normalized: dict[str, object] = {
        "type": identity,
        "mro": [_type_identity(base) for base in cls.__mro__],
        "bound_attributes": attributes,
    }
    if not attribute_names and not directly_called:
        fallback_method_names = _owned_callable_method_names(cls, owner_module=owner_module)
        normalized["source_hash"] = _class_source_hash(cls)
        normalized["methods"] = _iter_relevant_method_hashes(cls, method_names=fallback_method_names, seen=seen)
        normalized["method_dependencies"] = _iter_relevant_method_dependency_hashes(
            cls,
            method_names=fallback_method_names,
            seen=seen,
        )
    return normalized


def _add_callable_dependency(
    dependencies: dict[str, object],
    candidate: FunctionType,
    *,
    binding_key: str,
    seen: frozenset[int],
    owner_cls: type[object] | None,
) -> None:
    dependency_key = _callable_dependency_key(candidate)
    if id(candidate) in seen:
        return
    dependencies[f"{binding_key}->{dependency_key}"] = _callable_dependency_payload(
        candidate,
        seen=seen,
        owner_cls=owner_cls,
    )


def _loaded_global_names(code: CodeType) -> list[str]:
    names: set[str] = set()
    for instruction in dis.get_instructions(code):
        if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"}:
            continue
        name: str = instruction.argval
        names.add(name)
    return sorted(names)


def _loaded_owner_attribute_names(code: CodeType) -> list[str]:
    names: set[str] = set()
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions[1:], start=1):
        if instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
            continue
        receiver_instruction = instructions[index - 1]
        if receiver_instruction.opname not in {"LOAD_FAST", "LOAD_DEREF"} or receiver_instruction.argval not in {"self", "cls"}:
            continue
        name: str = instruction.argval
        names.add(name)
    return sorted(names)


def _resolve_static_attribute(owner: object, name: str) -> tuple[object, object | None]:
    if isinstance(owner, ModuleType) or inspect.isfunction(owner):
        namespace = vars(owner)
        if name in namespace:
            return namespace[name], owner
        return _MISSING_CLASS_ATTRIBUTE, None
    if isinstance(owner, type):
        return _static_class_attribute(owner, name), owner
    try:
        namespace = vars(owner)
    except TypeError:
        namespace = {}
    if name in namespace:
        return namespace[name], owner
    return _static_class_attribute(type(owner), name), owner


def _reject_truncated_descriptor_chain(
    instructions: list[dis.Instruction],
    *,
    terminal_index: int,
    candidate: object,
    path: str,
) -> None:
    if not isinstance(candidate, (MemberDescriptorType, GetSetDescriptorType, property)):
        return
    if terminal_index + 1 >= len(instructions):
        return
    if instructions[terminal_index + 1].opname in {"LOAD_ATTR", "LOAD_METHOD"}:
        raise FrameworkBugError(f"Runtime-VAL cannot statically resolve descriptor chain {path}")


def _qualified_global_values(
    code: CodeType,
    globals_table: dict[str, object],
    name: str,
) -> list[tuple[str, object, object | None, bool]]:
    qualified_values: list[tuple[str, object, object | None, bool]] = []
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions[:-1]):
        if instruction.opname not in {"LOAD_GLOBAL", "LOAD_NAME"} or instruction.argval != name:
            continue
        candidate = globals_table[name]
        candidate_owner: object | None = None
        path_parts = [name]
        terminal_index = index
        while terminal_index + 1 < len(instructions):
            attribute_instruction = instructions[terminal_index + 1]
            if attribute_instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
                break
            attribute_name: str = attribute_instruction.argval
            candidate, candidate_owner = _resolve_static_attribute(candidate, attribute_name)
            path_parts.append(attribute_name)
            if candidate is _MISSING_CLASS_ATTRIBUTE:
                raise FrameworkBugError(f"Runtime-VAL cannot statically resolve dependency {'.'.join(path_parts)}")
            terminal_index += 1
            if isinstance(candidate, (MemberDescriptorType, GetSetDescriptorType, property)):
                break
        if terminal_index == index:
            continue
        _reject_truncated_descriptor_chain(
            instructions,
            terminal_index=terminal_index,
            candidate=candidate,
            path=".".join(path_parts),
        )
        terminal_instruction = instructions[terminal_index]
        directly_called = terminal_instruction.arg is not None and bool(terminal_instruction.arg & 1)
        if terminal_index + 1 < len(instructions) and instructions[terminal_index + 1].opname == "PUSH_NULL":
            directly_called = True
        qualified_values.append((".".join(path_parts), candidate, candidate_owner, directly_called))
    return _merge_qualified_values(qualified_values)


def _merge_qualified_values(
    qualified_values: list[tuple[str, object, object | None, bool]],
) -> list[tuple[str, object, object | None, bool]]:
    merged: dict[str, tuple[object, object | None, bool]] = {}
    for qualified_name, candidate, candidate_owner, directly_called in qualified_values:
        existing = merged.get(qualified_name)
        if existing is None:
            merged[qualified_name] = (candidate, candidate_owner, directly_called)
            continue
        existing_candidate, existing_owner, existing_directly_called = existing
        if candidate is not existing_candidate or candidate_owner is not existing_owner:
            raise FrameworkBugError(f"Runtime-VAL dependency {qualified_name} did not resolve to one stable binding")
        merged[qualified_name] = (candidate, candidate_owner, existing_directly_called or directly_called)
    return [
        (qualified_name, candidate, candidate_owner, directly_called)
        for qualified_name, (candidate, candidate_owner, directly_called) in sorted(merged.items())
    ]


def _qualified_bound_values(
    code: CodeType,
    value: object,
    *,
    binding_name: str,
    load_opnames: frozenset[str],
) -> list[tuple[str, object, object | None, bool]]:
    qualified_values: list[tuple[str, object, object | None, bool]] = []
    for _, nested_code in _iter_code_objects(code):
        instructions = list(dis.get_instructions(nested_code))
        for index, instruction in enumerate(instructions[:-1]):
            if instruction.opname not in load_opnames or instruction.argval != binding_name:
                continue
            candidate = value
            candidate_owner: object | None = None
            path_parts = [binding_name]
            terminal_index = index
            while terminal_index + 1 < len(instructions):
                attribute_instruction = instructions[terminal_index + 1]
                if attribute_instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
                    break
                attribute_name: str = attribute_instruction.argval
                candidate, candidate_owner = _resolve_static_attribute(candidate, attribute_name)
                path_parts.append(attribute_name)
                if candidate is _MISSING_CLASS_ATTRIBUTE:
                    raise FrameworkBugError(f"Runtime-VAL cannot statically resolve bound dependency {'.'.join(path_parts)}")
                terminal_index += 1
                if isinstance(candidate, (MemberDescriptorType, GetSetDescriptorType, property)):
                    break
            if terminal_index == index:
                continue
            _reject_truncated_descriptor_chain(
                instructions,
                terminal_index=terminal_index,
                candidate=candidate,
                path=".".join(path_parts),
            )
            terminal_instruction = instructions[terminal_index]
            directly_called = terminal_instruction.arg is not None and bool(terminal_instruction.arg & 1)
            if terminal_index > 0 and instructions[terminal_index - 1].opname == "PUSH_NULL":
                directly_called = True
            if terminal_index + 1 < len(instructions) and instructions[terminal_index + 1].opname == "PUSH_NULL":
                directly_called = True
            qualified_values.append((".".join(path_parts), candidate, candidate_owner, directly_called))
    return _merge_qualified_values(qualified_values)


def _qualified_owner_values(
    code: CodeType,
    owner_cls: type[object],
) -> list[tuple[str, object, object | None, bool]]:
    qualified_values: list[tuple[str, object, object | None, bool]] = []
    instructions = list(dis.get_instructions(code))
    for index, instruction in enumerate(instructions[:-1]):
        if instruction.opname not in {"LOAD_FAST", "LOAD_DEREF"} or instruction.argval not in {"self", "cls"}:
            continue
        candidate: object = owner_cls
        candidate_owner: object | None = None
        path_parts = [owner_cls.__qualname__]
        terminal_index = index
        attribute_count = 0
        while terminal_index + 1 < len(instructions):
            attribute_instruction = instructions[terminal_index + 1]
            if attribute_instruction.opname not in {"LOAD_ATTR", "LOAD_METHOD"}:
                break
            attribute_name: str = attribute_instruction.argval
            candidate, candidate_owner = _resolve_static_attribute(candidate, attribute_name)
            path_parts.append(attribute_name)
            attribute_count += 1
            if candidate is _MISSING_CLASS_ATTRIBUTE:
                break
            terminal_index += 1
            if isinstance(candidate, (MemberDescriptorType, GetSetDescriptorType, property)):
                break
        if attribute_count < 2 or candidate is _MISSING_CLASS_ATTRIBUTE:
            continue
        _reject_truncated_descriptor_chain(
            instructions,
            terminal_index=terminal_index,
            candidate=candidate,
            path=".".join(path_parts),
        )
        terminal_instruction = instructions[terminal_index]
        directly_called = terminal_instruction.arg is not None and bool(terminal_instruction.arg & 1)
        if terminal_index + 1 < len(instructions) and instructions[terminal_index + 1].opname == "PUSH_NULL":
            directly_called = True
        qualified_values.append((".".join(path_parts), candidate, candidate_owner, directly_called))
    return _merge_qualified_values(qualified_values)


def _add_closed_global_dependency(
    dependencies: dict[str, object],
    *,
    owner_module: str,
    name: str,
    candidate: object,
    seen: frozenset[int] = frozenset(),
    dependency_key: str | None = None,
) -> None:
    normalized = _normalize_dependency_value(candidate, owner_module=owner_module, name=name, seen=seen)
    key = f"{owner_module}:<global>:{name}" if dependency_key is None else dependency_key
    dependencies[key] = {"value": normalized}


def _owned_instance_state(candidate: object, *, owner_module: str) -> dict[str, object]:
    state: dict[str, object] = {}
    try:
        state["dict"] = vars(candidate)
    except TypeError:
        state["dict_unavailable"] = True

    slot_state: dict[str, object] = {}
    candidate_cls = type(candidate)
    for base in candidate_cls.__mro__:
        if not _module_is_owned(base.__module__, owner_module=owner_module):
            continue
        for attribute_name, descriptor in base.__dict__.items():
            if not isinstance(descriptor, MemberDescriptorType):
                continue
            slot_name = f"{_type_identity(base)}:{attribute_name}"
            try:
                slot_state[slot_name] = descriptor.__get__(candidate, candidate_cls)
            except AttributeError:
                slot_state[slot_name] = {"uninitialized_slot": True}
    state["slots"] = slot_state
    return state


def _normalize_dependency_value(
    candidate: object,
    *,
    owner_module: str,
    name: str,
    seen: frozenset[int] = frozenset(),
) -> object:
    if isinstance(candidate, tuple):
        if id(candidate) in seen:
            raise FrameworkBugError(f"Runtime-VAL dependency {owner_module}:{name} contains a cyclic tuple")
        nested_seen = seen | frozenset({id(candidate)})
        return {
            "tuple": [
                _normalize_dependency_value(item, owner_module=owner_module, name=f"{name}[{index}]", seen=nested_seen)
                for index, item in enumerate(candidate)
            ]
        }
    if isinstance(candidate, list):
        if id(candidate) in seen:
            raise FrameworkBugError(f"Runtime-VAL dependency {owner_module}:{name} contains a cyclic list")
        nested_seen = seen | frozenset({id(candidate)})
        return {
            "list": [
                _normalize_dependency_value(item, owner_module=owner_module, name=f"{name}[{index}]", seen=nested_seen)
                for index, item in enumerate(candidate)
            ]
        }
    if isinstance(candidate, (set, frozenset)):
        if id(candidate) in seen:
            raise FrameworkBugError(f"Runtime-VAL dependency {owner_module}:{name} contains a cyclic set")
        nested_seen = seen | frozenset({id(candidate)})
        normalized_items = [
            _normalize_dependency_value(item, owner_module=owner_module, name=f"{name}[]", seen=nested_seen) for item in candidate
        ]
        key = "set" if isinstance(candidate, set) else "frozenset"
        return {key: sorted(normalized_items, key=_normalized_sort_key)}
    if isinstance(candidate, dict):
        if id(candidate) in seen:
            raise FrameworkBugError(f"Runtime-VAL dependency {owner_module}:{name} contains a cyclic mapping")
        nested_seen = seen | frozenset({id(candidate)})
        normalized_items = [
            {
                "key": _normalize_dependency_value(key, owner_module=owner_module, name=f"{name}.<key>", seen=nested_seen),
                "value": _normalize_dependency_value(value, owner_module=owner_module, name=f"{name}[value]", seen=nested_seen),
            }
            for key, value in candidate.items()
        ]
        return {"dict": sorted(normalized_items, key=_normalized_sort_key)}
    if isinstance(candidate, type) and is_typeddict(candidate):
        return {"typed_dict_hash": _payload_schema_hash(candidate)}
    if isinstance(candidate, type):
        identity = _type_identity(candidate)
        candidate_module = candidate.__module__
        if _module_is_owned(candidate_module, owner_module=owner_module):
            return _normalize_class_binding(
                candidate,
                owner_module=owner_module,
                binding_name=name,
                attribute_names=[],
                directly_called=False,
                seen=seen,
            )
        return {"type": identity}

    normalized = _try_normalize_code_constant(candidate)
    if normalized is not _UNSUPPORTED_MANIFEST_VALUE:
        return normalized
    callable_candidate = _unwrap_callable(candidate)
    if callable_candidate is not None:
        candidate_module = _function_module(callable_candidate)
        if _module_is_owned(candidate_module, owner_module=owner_module):
            if id(callable_candidate) in seen:
                return {"callable_reference": _callable_dependency_key(callable_candidate)}
            return {
                "callable": _callable_dependency_key(callable_candidate),
                "implementation_hash": _callable_implementation_hash(callable_candidate, seen=seen),
                "dependencies": _callable_dependency_hashes(callable_candidate, seen=seen),
            }
        return {"external_callable": _callable_dependency_key(callable_candidate)}
    if isinstance(candidate, ModuleType):
        module_name: object = candidate.__name__
        if type(module_name) is not str:
            raise FrameworkBugError(f"Runtime-VAL module dependency {name!r} has invalid name identity")
        raise FrameworkBugError(f"Runtime-VAL cannot statically bind module dependency {owner_module}:{name} ({module_name})")

    candidate_cls = type(candidate)
    candidate_type_identity = _type_identity(candidate_cls)
    candidate_type_module = candidate_cls.__module__
    if candidate_cls is object and owner_module == "elspeth.contracts.declaration_contracts" and name == "_DISPATCHER_ATTACHMENT_TOKEN":
        return {"opaque_identity_token": f"{owner_module}:{name}", "type": candidate_type_identity}
    if _module_is_owned(candidate_type_module, owner_module=owner_module):
        if id(candidate) in seen:
            raise FrameworkBugError(f"Runtime-VAL dependency {owner_module}:{name} contains cyclic owned instance state")
        nested_seen = seen | frozenset({id(candidate)})
        candidate_state = _owned_instance_state(candidate, owner_module=owner_module)
        return {
            "owned_instance_type": candidate_type_identity,
            "source_hash": _class_source_hash(candidate_cls),
            "methods": _iter_relevant_method_hashes(
                candidate_cls,
                method_names=_owned_callable_method_names(candidate_cls, owner_module=owner_module),
                seen=nested_seen,
            ),
            "method_dependencies": _iter_relevant_method_dependency_hashes(
                candidate_cls,
                method_names=_owned_callable_method_names(candidate_cls, owner_module=owner_module),
                seen=nested_seen,
            ),
            "state": _normalize_dependency_value(
                candidate_state,
                owner_module=owner_module,
                name=f"{name}.<state>",
                seen=nested_seen,
            ),
        }
    if (
        owner_module == "elspeth.engine.executors.pass_through"
        and name == "_VIOLATIONS_COUNTER"
        and candidate_type_module.startswith("opentelemetry.")
    ):
        return {"telemetry_handle": f"{owner_module}:{name}", "type": candidate_type_identity}
    raise FrameworkBugError(
        f"Runtime-VAL cannot deterministically normalize global dependency {owner_module}:{name} of type {candidate_type_identity}"
    )


def _add_resolved_dependency(
    dependencies: dict[str, object],
    *,
    owner_module: str,
    dependency_scope: str,
    binding_name: str,
    candidate: object,
    candidate_owner: object | None,
    directly_called: bool,
    seen: frozenset[int],
) -> None:
    dependency_key = f"{owner_module}:<code>:{dependency_scope}:<global>:{binding_name}"
    callable_obj = _unwrap_callable(candidate)
    if callable_obj is not None:
        helper_candidate = _runtime_val_helper_dependency(callable_obj, owner_module=owner_module)
        if helper_candidate is not None:
            nested_owner_cls: type[object] | None = None
            if not isinstance(candidate, staticmethod) and candidate_owner is not None:
                nested_owner_cls = candidate_owner if isinstance(candidate_owner, type) else type(candidate_owner)
            callable_binding_key = f"{dependency_key}->{_callable_dependency_key(helper_candidate)}"
            _add_callable_dependency(
                dependencies,
                helper_candidate,
                binding_key=dependency_key,
                seen=seen,
                owner_cls=nested_owner_cls,
            )
            if (
                candidate_owner is not None
                and not isinstance(candidate_owner, (type, ModuleType, FunctionType))
                and callable_binding_key in dependencies
            ):
                dependency_payload = dependencies[callable_binding_key]
                if not isinstance(dependency_payload, dict):
                    raise FrameworkBugError(f"Runtime-VAL callable dependency {callable_binding_key} has invalid payload")
                dependency_payload["receiver"] = _normalize_dependency_value(
                    candidate_owner,
                    owner_module=owner_module,
                    name=binding_name.rpartition(".")[0],
                    seen=seen,
                )
            return
        candidate = callable_obj
    if isinstance(candidate, type):
        dependencies[dependency_key] = {
            "value": _normalize_class_binding(
                candidate,
                owner_module=owner_module,
                binding_name=binding_name,
                attribute_names=[],
                directly_called=directly_called,
                seen=seen,
            )
        }
        return
    if isinstance(candidate, (MethodDescriptorType, WrapperDescriptorType)):
        descriptor_payload: dict[str, object] = {"builtin_descriptor": f"{_type_identity(candidate.__objclass__)}:{candidate.__name__}"}
        if candidate_owner is not None and not isinstance(candidate_owner, type):
            receiver_name = binding_name.rpartition(".")[0]
            descriptor_payload["receiver"] = _normalize_dependency_value(
                candidate_owner,
                owner_module=owner_module,
                name=receiver_name,
                seen=seen,
            )
        dependencies[dependency_key] = {"value": descriptor_payload}
        return
    _add_closed_global_dependency(
        dependencies,
        owner_module=owner_module,
        name=binding_name,
        candidate=candidate,
        seen=seen,
        dependency_key=dependency_key,
    )


def _callable_dependency_hashes(
    func: FunctionType,
    *,
    seen: frozenset[int] = frozenset(),
    owner_cls: type[object] | None = None,
) -> dict[str, object]:
    globals_table = func.__globals__
    next_seen = seen | frozenset({id(func)})
    owner_module = _function_module(func)
    dependencies: dict[str, object] = {}
    for code_path, code in _iter_code_objects(func.__code__):
        dependency_scope = f"{_callable_dependency_key(func)}:{code_path}"
        for name in _loaded_global_names(code):
            if name not in globals_table:
                continue
            raw_candidate = globals_table[name]
            qualified_values = _qualified_global_values(code, globals_table, name)
            candidate = _runtime_val_helper_dependency(raw_candidate, owner_module=owner_module)
            if candidate is not None and (
                not qualified_values
                or _binding_is_directly_called(
                    code,
                    load_opnames=frozenset({"LOAD_GLOBAL", "LOAD_NAME"}),
                    binding_name=name,
                )
            ):
                _add_callable_dependency(
                    dependencies,
                    candidate,
                    binding_key=f"{owner_module}:<code>:{dependency_scope}:<global>:{name}",
                    seen=next_seen,
                    owner_cls=None,
                )
            handled_raw_candidate = candidate is not None
            if isinstance(raw_candidate, type):
                dependencies[f"{owner_module}:<code>:{dependency_scope}:<global>:{name}"] = {
                    "value": _normalize_class_binding(
                        raw_candidate,
                        owner_module=owner_module,
                        binding_name=name,
                        attribute_names=_loaded_binding_attribute_names(
                            code,
                            load_opnames=frozenset({"LOAD_GLOBAL", "LOAD_NAME"}),
                            binding_name=name,
                        ),
                        directly_called=_binding_is_directly_called(
                            code,
                            load_opnames=frozenset({"LOAD_GLOBAL", "LOAD_NAME"}),
                            binding_name=name,
                        ),
                        seen=next_seen,
                    )
                }
                handled_raw_candidate = True
            if qualified_values:
                for qualified_name, qualified_candidate, qualified_owner, directly_called in qualified_values:
                    _add_resolved_dependency(
                        dependencies,
                        owner_module=owner_module,
                        dependency_scope=dependency_scope,
                        binding_name=qualified_name,
                        candidate=qualified_candidate,
                        candidate_owner=qualified_owner,
                        directly_called=directly_called,
                        seen=next_seen,
                    )
                handled_raw_candidate = True
            if not handled_raw_candidate:
                _add_closed_global_dependency(
                    dependencies,
                    owner_module=owner_module,
                    name=name,
                    candidate=raw_candidate,
                    seen=next_seen,
                    dependency_key=f"{owner_module}:<code>:{dependency_scope}:<global>:{name}",
                )
        if owner_cls is not None:
            for name in _loaded_owner_attribute_names(code):
                attribute = _static_class_attribute(owner_cls, name)
                if attribute is _MISSING_CLASS_ATTRIBUTE:
                    continue
                callable_obj = _unwrap_callable(attribute)
                if callable_obj is not None:
                    if _function_module(callable_obj) == "builtins":
                        continue
                    nested_owner_cls = None if isinstance(attribute, staticmethod) else owner_cls
                    _add_callable_dependency(
                        dependencies,
                        callable_obj,
                        binding_key=f"{_type_identity(owner_cls)}:<code>:{code_path}:<attribute>:{name}",
                        seen=next_seen,
                        owner_cls=nested_owner_cls,
                    )
                    continue
                if isinstance(attribute, (MemberDescriptorType, GetSetDescriptorType)):
                    continue
                _add_closed_global_dependency(
                    dependencies,
                    owner_module=_type_identity(owner_cls).partition(":")[0],
                    name=f"{owner_cls.__qualname__}.{name}",
                    candidate=attribute,
                    seen=next_seen,
                    dependency_key=f"{_type_identity(owner_cls)}:<code>:{code_path}:<attribute>:{name}",
                )
            for qualified_name, qualified_candidate, qualified_owner, directly_called in _qualified_owner_values(code, owner_cls):
                _add_resolved_dependency(
                    dependencies,
                    owner_module=_type_identity(owner_cls).partition(":")[0],
                    dependency_scope=dependency_scope,
                    binding_name=qualified_name,
                    candidate=qualified_candidate,
                    candidate_owner=qualified_owner,
                    directly_called=directly_called,
                    seen=next_seen,
                )
    return dependencies


def _static_method_names(cls: type[object]) -> list[str]:
    return sorted({name for base in cls.__mro__ for name in base.__dict__})


def _owned_method_names(cls: type[object], *, owner_module: str) -> list[str]:
    return sorted({name for base in cls.__mro__ if _module_is_owned(base.__module__, owner_module=owner_module) for name in base.__dict__})


def _owned_callable_method_names(cls: type[object], *, owner_module: str) -> list[str]:
    owned_names: list[str] = []
    for name in _owned_method_names(cls, owner_module=owner_module):
        callable_obj = _unwrap_callable(_static_class_attribute(cls, name))
        if callable_obj is None:
            continue
        callable_module = _function_module(callable_obj)
        if _module_is_owned(callable_module, owner_module=owner_module):
            owned_names.append(name)
    return owned_names


def _iter_relevant_method_hashes(
    cls: type[object],
    *,
    method_names: list[str] | None = None,
    seen: frozenset[int] = frozenset(),
) -> dict[str, str]:
    if method_names is None:
        names = _static_method_names(cls)
    else:
        names = sorted(set(method_names))

    method_hashes: dict[str, str] = {}
    for name in names:
        attribute = _static_class_attribute(cls, name)
        callable_obj = _unwrap_callable(attribute)
        if callable_obj is None:
            continue
        if _function_module(callable_obj) == "builtins":
            continue
        if id(callable_obj) in seen:
            method_hashes[name] = f"callable-reference:{_callable_dependency_key(callable_obj)}"
            continue
        method_hashes[name] = _callable_implementation_hash(callable_obj, seen=seen)
    return method_hashes


def _iter_relevant_method_dependency_hashes(
    cls: type[object],
    *,
    method_names: list[str],
    seen: frozenset[int] = frozenset(),
) -> dict[str, object]:
    dependency_hashes: dict[str, object] = {}
    for name in sorted(set(method_names)):
        attribute = _static_class_attribute(cls, name)
        callable_obj = _unwrap_callable(attribute)
        if callable_obj is None:
            continue
        if id(callable_obj) in seen:
            dependency_hashes[name] = {"callable_reference": _callable_dependency_key(callable_obj)}
            continue
        owner_cls = None if isinstance(attribute, staticmethod) else cls
        dependencies = _callable_dependency_hashes(callable_obj, seen=seen, owner_cls=owner_cls)
        if dependencies:
            dependency_hashes[name] = dependencies
    return dependency_hashes


def _payload_schema_hash(payload_schema: type) -> str:
    if not is_typeddict(payload_schema):
        raise FrameworkBugError(
            "Runtime-VAL declaration payload_schema must be a TypedDict; "
            f"got {type(payload_schema).__module__}.{type(payload_schema).__qualname__}"
        )
    try:
        annotations = get_type_hints(payload_schema, include_extras=True)
    except (NameError, TypeError) as exc:
        raise FrameworkBugError(f"Runtime-VAL cannot resolve payload schema annotations for {_type_identity(payload_schema)}") from exc
    return _json_hash(
        {
            "module": payload_schema.__module__,
            "qualname": payload_schema.__qualname__,
            "annotations": _normalize_code_constant(annotations),
            "required_keys": sorted(payload_schema.__required_keys__),  # type: ignore[attr-defined]
            "optional_keys": sorted(payload_schema.__optional_keys__),  # type: ignore[attr-defined]
            "source_hash": _class_source_hash(payload_schema),
        }
    )


def _class_implementation_hash(
    cls: type[object],
    *,
    method_names: list[str] | None = None,
    extra: dict[str, object] | None = None,
) -> str:
    mro_hashes = [
        {
            "module": base.__module__,
            "qualname": base.__qualname__,
            "source_hash": _class_source_hash(base),
        }
        for base in cls.mro()
        if base.__module__ != "builtins"
    ]
    return _json_hash(
        {
            "module": cls.__module__,
            "qualname": cls.__qualname__,
            "source_hash": _class_source_hash(cls),
            "mro": mro_hashes,
            "methods": _iter_relevant_method_hashes(cls, method_names=method_names),
            "extra": {} if extra is None else extra,
        }
    )


def _declaration_contract_implementation_hash(contract: DeclarationContract) -> str:
    cls = type(contract)
    method_names = ["applies_to", *sorted(contract_sites(contract))]
    return _class_implementation_hash(
        cls,
        method_names=method_names,
        extra={
            "payload_schema_hash": _payload_schema_hash(cls.payload_schema),
            "method_dependency_hashes": _iter_relevant_method_dependency_hashes(cls, method_names=method_names),
        },
    )


def _tier_1_implementation_hash(cls: type[BaseException]) -> str:
    method_names = _static_method_names(cls)
    return _class_implementation_hash(
        cls,
        method_names=method_names,
        extra={"method_dependency_hashes": _iter_relevant_method_dependency_hashes(cls, method_names=method_names)},
    )


def _assert_runtime_val_registries_frozen() -> None:
    unfrozen: list[str] = []
    if not declaration_registry_is_frozen():
        unfrozen.append("declaration-contract registry")
    if not tier_registry_is_frozen():
        unfrozen.append("Tier-1 error registry")
    if unfrozen:
        raise FrameworkBugError(
            "build_runtime_val_manifest() requires frozen runtime-VAL registries. "
            f"Unfrozen: {', '.join(unfrozen)}. Call prepare_for_run() before serializing the run header."
        )


def build_runtime_val_manifest() -> dict[str, Any]:
    """Return a dict describing the runtime-VAL registries at call time."""
    _assert_runtime_val_registries_frozen()
    declarations = [
        {
            "name": contract.name,
            "class_name": type(contract).__name__,
            "class_module": type(contract).__module__,
            "dispatch_sites": sorted(contract_sites(contract)),
            "implementation_hash": _declaration_contract_implementation_hash(contract),
        }
        for contract in sorted(registered_declaration_contracts(), key=lambda c: c.name)
    ]
    tier_1_entries = [
        {
            "class_name": cls.__name__,
            "class_module": cls.__module__,
            "reason": tier_1_reason(cls),
            "implementation_hash": _tier_1_implementation_hash(cls),
        }
        for cls in sorted(_TIER_1_ERRORS_VIEW, key=lambda c: (c.__module__, c.__name__))
    ]
    expected_contract_sites_serialized: dict[str, list[str]] = {name: sorted(sites) for name, sites in EXPECTED_CONTRACT_SITES.items()}
    return {
        "declaration_contracts": declarations,
        "expected_contract_sites": expected_contract_sites_serialized,
        "tier_1_errors": tier_1_entries,
    }
