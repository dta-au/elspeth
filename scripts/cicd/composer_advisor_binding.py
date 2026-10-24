"""Finite source proof of the owned B2 batch's complete Advisor input.

This receipt joins the live adapter, once-only initialization, decoded input,
retained carriers and explicit nominal manual-Advisor admission. The actual
runtime guard rejects foreign/subclass receivers; the unbound owned method
uses the same instance and its settings, bypassing per-instance overrides.

Complete executable source contracts close eager expressions, lexical binding,
retained aliases and finally paths. Inspected suppliers establish the separate
input-observer premise. This is a bounded source census, not service lifecycle,
provider execution, durable replay, arbitrary runtime patching or release
qualification. No business stage, decoder, provider or service runs here; the
inspected pure thawer only compares actual sent and validator schemas.
"""

from __future__ import annotations

import ast
import builtins
import inspect
import textwrap
from types import CodeType, FunctionType, ModuleType

if __package__:
    from .composer_advisor_binding_templates import TEMPLATES
else:
    from composer_advisor_binding_templates import TEMPLATES


class AdvisorBindingError(RuntimeError):
    """The owned source chain no longer proves complete Advisor admission."""


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise AdvisorBindingError(f"advisor continuation provenance: {detail}")


def _source_contract(subject: object, key: str, namespace: dict[str, object]) -> None:
    """Compare all syntax, not a selected walk of names or writes."""
    _require(isinstance(subject, (FunctionType, ModuleType, type)), f"{key} is not an owned source object")
    function = subject
    if isinstance(subject, FunctionType):
        function = _owned_function(subject, key)
        _require(function.__globals__ is namespace, f"{key} has a foreign global namespace")
    else:
        _require("__wrapped__" not in vars(subject), f"{key} has unsupported wrapper indirection")
    try:
        actual = ast.parse(textwrap.dedent(inspect.getsource(subject)))
        expected = ast.parse(textwrap.dedent(TEMPLATES[key]))
    except (OSError, TypeError, SyntaxError) as exc:
        raise AdvisorBindingError(f"advisor continuation provenance: {key} source unavailable or malformed") from exc
    if not isinstance(subject, ModuleType):
        _require(len(actual.body) == 1 and len(expected.body) == 1, f"{key} is not one complete definition")
    if key == "batch._execute_continuation_call_live":
        admissions = [
            node
            for node in ast.walk(actual)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_validate_advisor_arguments"
        ]
        _require(
            len(admissions) == 1
            and ast.dump(admissions[0], include_attributes=False)
            == ast.dump(
                ast.parse("AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)", mode="eval").body,
                include_attributes=False,
            ),
            "advisor interception does not forward the complete decoded arguments",
        )
    _require(
        ast.dump(actual, include_attributes=False) == ast.dump(expected, include_attributes=False),
        f"{key} changed its complete executable source contract",
    )
    # A module may legitimately omit builtin globals. Any explicit shadow must
    # still be the real builtin; spelling alone cannot authorize a constructor.
    builtin_values = vars(builtins)
    for node in ast.walk(actual):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in builtin_values:
            if node.id in namespace:
                _require(namespace[node.id] is builtin_values[node.id], f"{key} shadows builtin {node.id}")
            if isinstance(function, FunctionType):
                _require(
                    node.id in function.__builtins__ and function.__builtins__[node.id] is builtin_values[node.id],
                    f"{key} has foreign builtin {node.id}",
                )


def _owned_function(subject: FunctionType, key: str) -> FunctionType:
    """Close the one actual owned decorator; never trust inspect.unwrap alone."""
    from elspeth.contracts.trust_boundary import TrustBoundaryMetadata, trust_boundary

    if "__wrapped__" not in vars(subject):
        return subject
    # The wrapper is the exact code object emitted by this inspected factory,
    # not another function borrowing a name, signature, annotation or digest.
    decorators = [value for value in trust_boundary.__code__.co_consts if isinstance(value, CodeType) and value.co_name == "decorator"]
    _require(len(decorators) == 1, f"{key} has unsupported wrapper factory")
    wrappers = [value for value in decorators[0].co_consts if isinstance(value, CodeType) and value.co_name == "wrapper"]
    _require(
        len(wrappers) == 1
        and subject.__code__ is wrappers[0]
        and subject.__globals__ is trust_boundary.__globals__
        and subject.__code__.co_freevars == ("func",)
        and subject.__closure__ is not None
        and len(subject.__closure__) == 1,
        f"{key} has unsupported wrapper indirection",
    )
    assert subject.__closure__ is not None
    original = subject.__closure__[0].cell_contents
    metadata = vars(subject)["__trust_boundary__"] if "__trust_boundary__" in vars(subject) else None
    _require(
        isinstance(original, FunctionType)
        and type(metadata) is TrustBoundaryMetadata
        and metadata.func is original
        and vars(subject)["__wrapped__"] is original
        and "__wrapped__" not in vars(original),
        f"{key} lost its actual boundary wrapper input",
    )
    assert isinstance(original, FunctionType)
    return original


def _bindings(namespace: dict[str, object], expected: dict[str, object], label: str) -> None:
    for name, value in expected.items():
        _require(name in namespace and namespace[name] is value, f"{label}.{name} no longer resolves to its owned callable/value")


def continuation_advisor_execution_owner(module: ModuleType) -> FunctionType:
    """Join the nominal receiver guard, once-only batch and exact input."""
    import copy
    import dataclasses
    import typing
    from types import MappingProxyType

    from jsonschema import Draft202012Validator

    from elspeth.contracts import freeze, hashing
    from elspeth.contracts.composer_llm_audit import ToolContractDialect
    from elspeth.contracts.trust_boundary import trust_boundary
    from elspeth.core import canonical
    from elspeth.web.composer import (
        _required_paths_validator as required,
    )
    from elspeth.web.composer import (
        advisor_checkpoint as advisor,
    )
    from elspeth.web.composer import (
        audit,
    )
    from elspeth.web.composer import (
        batch_dependencies as resource,
    )
    from elspeth.web.composer import (
        bounded_json as bounded,
    )
    from elspeth.web.composer.redaction import MANIFEST
    from elspeth.web.composer.tools import _dispatch as dispatch
    from elspeth.web.composer.tools import _registry as registry
    from elspeth.web.composer.tools import discovery
    from elspeth.web.composer.tools import wire_projection as wire
    from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel

    namespace = vars(module)
    modules = {
        "batch": module,
        "canonical": canonical,
        "required": required,
        "resource": resource,
        "registry": registry,
        "freeze": freeze,
        "audit": audit,
        "bounded": bounded,
        "wire": wire,
        "discovery": discovery,
        "hashing": hashing,
        "dispatch": dispatch,
    }
    # These imported owners are joined by identity, then their complete source
    # below supplies the separate effect premise. In particular cast is actual
    # typing.cast, not an unproved name, and audit_arguments remains an alias.
    _bindings(
        namespace,
        {
            "cast": typing.cast,
            "dataclass": dataclasses.dataclass,
            "field": dataclasses.field,
            "replace": dataclasses.replace,
            "bounded_json_loads": bounded.bounded_json_loads,
            "decode_wire_arguments": wire.decode_wire_arguments,
            "deep_thaw": freeze.deep_thaw,
            "_WIRE_TOOL_DEFS": wire._WIRE_TOOL_DEFS,
            "ToolContractDialect": ToolContractDialect,
            "_TOOL_REQUIRED_PATHS": required._TOOL_REQUIRED_PATHS,
            "_find_missing_required_paths": required._find_missing_required_paths,
            "is_mutation_tool": discovery.is_mutation_tool,
            "is_cacheable_discovery_tool": discovery.is_cacheable_discovery_tool,
            "canonical_json": canonical.canonical_json,
            "build_canonicalization_sentinel": audit.build_canonicalization_sentinel,
            "begin_dispatch_or_arg_error": audit.begin_dispatch_or_arg_error,
            "promised_resources": resource.promised_resources,
            "resolve_tool_effects": registry.resolve_tool_effects,
            "RequestAdvisorHintArgumentsModel": RequestAdvisorHintArgumentsModel,
            "AdvisorCheckpointOwner": advisor.AdvisorCheckpointOwner,
        },
        "batch",
    )
    _bindings(
        vars(audit),
        {
            "canonical_json": canonical.canonical_json,
            "stable_hash": canonical.stable_hash,
            "dataclass": dataclasses.dataclass,
        },
        "audit",
    )
    _bindings(
        vars(canonical),
        {
            "_primitive_canonical_json": hashing.canonical_json,
            "_primitive_stable_hash": hashing.stable_hash,
        },
        "canonical",
    )
    _bindings(
        vars(wire),
        {
            "deepcopy": copy.deepcopy,
            "dataclass": dataclasses.dataclass,
            "freeze_fields": freeze.freeze_fields,
            "ToolContractDialect": ToolContractDialect,
            "Draft202012Validator": Draft202012Validator,
            "MappingProxyType": MappingProxyType,
            "_TOOL_SCHEMA_BY_NAME": dispatch._TOOL_SCHEMA_BY_NAME,
        },
        "wire",
    )
    _bindings(vars(bounded), {"dataclass": dataclasses.dataclass}, "bounded")
    _bindings(
        vars(advisor),
        {
            "RequestAdvisorHintArgumentsModel": RequestAdvisorHintArgumentsModel,
            "require_schema_valid_arguments": dispatch.require_schema_valid_arguments,
        },
        "advisor",
    )
    _bindings(
        vars(dispatch),
        {
            "Draft202012Validator": Draft202012Validator,
            "cast": typing.cast,
            "deep_thaw": freeze.deep_thaw,
        },
        "dispatch",
    )
    _require(type(dispatch._TOOL_SCHEMA_BY_NAME) is MappingProxyType, "flat schemas lost their immutable owned table")

    for key in TEMPLATES:
        group, name = key.split(".", 1)
        if group == "boundary":
            owner_namespace = trust_boundary.__globals__
            members = owner_namespace
        elif group == "wire_module":
            _source_contract(wire, key, vars(wire))
            continue
        elif group == "advisor":
            owner_namespace = vars(advisor)
            members = vars(advisor.AdvisorCheckpointOwner)
        else:
            owner_namespace = vars(modules[group])
            members = owner_namespace
        _require(name in members, f"{name} is unavailable")
        _source_contract(members[name], key, owner_namespace)

    # Owned dataclass source + actual dataclass/field suppliers establish direct
    # retained references; no replacement __post_init__, descriptor or factory
    # is inferred merely from a type annotation.
    for name in ("ContinuationBatchState", "ContinuationCallState", "_CallWireFacts", "ToolBatchContext", "BatchAccumulator"):
        carrier = namespace[name]
        _require(type(carrier) is type and carrier.__bases__ == (object,), f"{name} changed its nominal constructor owner")
    # The runtime guard proves the actual receiver's exact nominal identity.
    # Ordinary class lookup supplies the unbound method; instance overrides,
    # subclass hooks and foreign context receivers cannot become admission.
    owner_class = advisor.AdvisorCheckpointOwner
    _require(
        type(owner_class) is type and owner_class.__bases__ == (object,) and owner_class.__getattribute__ is object.__getattribute__,
        "Advisor admission changed its nominal class or instance settings lookup",
    )

    # Read actual closed tables instead of executing arbitrary classifiers or
    # observers. Their source contracts above show membership-only classifiers,
    # no Advisor resource branch, and the early async effect-table return.
    _bindings(
        vars(discovery),
        {
            "_MUTATION_TOOL_NAMES": registry._MUTATION_TOOL_NAMES,
            "_BLOB_MUTATION_TOOL_NAMES": registry._BLOB_MUTATION_TOOL_NAMES,
            "_SECRET_MUTATION_TOOL_NAMES": registry._SECRET_MUTATION_TOOL_NAMES,
            "_CACHEABLE_DISCOVERY_TOOL_NAMES": registry._CACHEABLE_DISCOVERY_TOOL_NAMES,
        },
        "discovery",
    )
    advisor_name = "request_advisor_hint"
    _require(
        advisor_name not in registry._MUTATION_TOOL_NAMES
        and advisor_name not in registry._BLOB_MUTATION_TOOL_NAMES
        and advisor_name not in registry._SECRET_MUTATION_TOOL_NAMES
        and advisor_name not in registry._CACHEABLE_DISCOVERY_TOOL_NAMES,
        "Advisor no longer follows the non-mutation, non-cache path",
    )
    _require(advisor_name in MANIFEST, "Advisor is absent from the actual redaction manifest")
    _require(
        bool(wire._WIRE_TOOL_DEFS) and all(advisor_name in tools for tools in wire._WIRE_TOOL_DEFS.values()),
        "Advisor is absent from an actual sent-wire dialect",
    )
    _require(
        advisor_name in registry.ASYNC_TOOL_EFFECTS and not registry.ASYNC_TOOL_EFFECTS[advisor_name].domains,
        "Advisor no longer has the owned empty async effect declaration",
    )
    # The classifier runs before deepcopy(raw). Close its actual constructor
    # source, nominal receiver and sent schema, not just the iter_errors name.
    _require(type(wire._WIRE_VALIDATORS) is MappingProxyType, "wire validators lost their owned immutable table")
    _require(set(wire._WIRE_VALIDATORS) == set(wire._WIRE_TOOL_DEFS), "wire validator dialects differ from sent definitions")
    for dialect, tools in wire._WIRE_TOOL_DEFS.items():
        validators = wire._WIRE_VALIDATORS[dialect]
        _require(
            type(validators) is MappingProxyType and set(validators) == set(tools), "wire validator names differ from sent definitions"
        )
        for name, tool in tools.items():
            _require(
                type(tool) is wire.WireTool and tool.name == name and tool.dialect is dialect, "sent wire entry lost its owned identity"
            )
            _require(
                all(type(node) in (wire.EnvelopeUnwrap, wire.StripNull, wire.EmptyArgumentsMarker) for node in tool.decode_plan),
                "sent wire decode plan has a foreign node",
            )
            validator = validators[name]
            _require(type(validator) is Draft202012Validator, "wire classifier is not the actual Draft202012Validator")
            _require(
                validator.schema == freeze.deep_thaw(tool.function)["parameters"],
                "wire classifier schema differs from the actual sent schema",
            )
    owner = namespace["_execute_continuation_call_live"]
    _require(isinstance(owner, FunctionType), "execution owner is not a Python function")
    assert isinstance(owner, FunctionType)
    return owner
