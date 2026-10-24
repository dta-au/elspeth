"""Live-source controls for the B2 Advisor argument provenance receipt."""

from __future__ import annotations

import inspect
from functools import wraps
from types import FunctionType, MappingProxyType

import pytest
from scripts.cicd import composer_wire_census as census
from scripts.cicd.composer_advisor_binding import AdvisorBindingError, continuation_advisor_execution_owner

from elspeth.core import canonical
from elspeth.web.composer import (
    _required_paths_validator as required,
)
from elspeth.web.composer import (
    batch_dependencies as resource,
)
from elspeth.web.composer import (
    tool_batch,
)
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.tools import _dispatch as dispatch
from elspeth.web.composer.tools import _registry as registry
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel


def _mutate_source(monkeypatch: pytest.MonkeyPatch, function: object, old: str, new: str) -> None:
    getsource = inspect.getsource
    original = getsource(function)
    assert original.count(old) == 1, (function, old)
    changed = original.replace(old, new)

    def source(subject: object) -> str:
        return changed if subject is function else getsource(subject)

    monkeypatch.setattr(inspect, "getsource", source)


def test_live_b2_chain_reaches_actual_nominal_advisor_model() -> None:
    assert continuation_advisor_execution_owner(tool_batch) is tool_batch._execute_continuation_call_live
    handler = census._advisor_admission_handler()
    assert handler is AdvisorCheckpointOwner._validate_advisor_arguments
    model, sites = census._model_for_handler(handler, input_parameter="arguments")
    assert model is RequestAdvisorHintArgumentsModel
    assert sites
    row = census.census_model_wire()["request_advisor_hint"]
    assert row.model_class is not None
    assert not row.site.startswith("unresolved:")
    assert row.shipped == row.model_fields


def test_source_formatting_preserves_every_model_census_row(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = census.census_model_wire()
    getsource = inspect.getsource
    owners = (
        tool_batch.run_tool_batch,
        tool_batch._initialize_continuation_batch_live,
        tool_batch._prepare_continuation_call_live,
        tool_batch._execute_continuation_call_live,
    )

    def source(subject: object) -> str:
        result = getsource(subject)
        return "\n# formatting-only positive control\n" + result + "\n" if subject in owners else result

    monkeypatch.setattr(inspect, "getsource", source)
    assert census.census_model_wire() == expected


@pytest.mark.parametrize(
    ("function", "old", "new"),
    [
        pytest.param(
            tool_batch.run_tool_batch,
            "await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)",
            "pass",
            id="initializer-bypassed",
        ),
        pytest.param(
            tool_batch.run_tool_batch,
            "await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)",
            "await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)\n    await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)",
            id="initializer-repeated",
        ),
        pytest.param(
            tool_batch.run_tool_batch,
            "calls[batch.next_tool_index]",
            "calls[0]",
            id="wrong-issued-call",
        ),
        pytest.param(
            tool_batch.run_tool_batch,
            'if prepared == "prepared":',
            "if True:",
            id="prepare-result-bypassed",
        ),
        pytest.param(
            tool_batch.run_tool_batch,
            "ctx=ctx, llm_messages=llm_messages, batch=batch, call=call)",
            "ctx=other_ctx, llm_messages=llm_messages, batch=batch, call=call)",
            id="prepare-receiver-changed",
        ),
        pytest.param(
            tool_batch._initialize_continuation_batch_live,
            "completion = call_model.completion",
            "completion = other_model.completion",
            id="initializer-response-replaced",
        ),
        pytest.param(
            tool_batch._initialize_continuation_batch_live,
            'if batch.phase != "uninitialized":',
            "if False:",
            id="once-only-guard-bypassed",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            'bounded_json_loads(tool_call.function.arguments, label=f"{tool_name} arguments")',
            'bounded_json_loads("{}", label=f"{tool_name} arguments")',
            id="provider-wire-replaced",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "decode_wire_arguments(tool_name, ctx.tool_contract_dialect, decoded_arguments)",
            "decode_wire_arguments(tool_name, ctx.tool_contract_dialect, {})",
            id="decoded-input-replaced",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "arguments = cast(dict[str, Any], deep_thaw(decoded.semantic))",
            "arguments = cast(dict[str, Any], decoded_arguments)",
            id="wire-projection-bypassed",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = dict(arguments)",
            id="retained-argument-copy",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "prepared = True",
            "continue\n            prepared = True",
            id="unreachable-retained-handoff",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            arguments.clear()",
            id="retained-input-mutated",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            alias = {'payload': arguments}\n            alias['payload'].clear()",
            id="retained-input-alias-escape",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = dict(call.arguments)",
            id="execution-input-copy",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = call.arguments\n    arguments.clear()",
            id="execution-input-mutated",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            'if tool_name == "request_advisor_hint":',
            'if False and tool_name == "request_advisor_hint":',
            id="unreachable-interception",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "advisor_arg_error = AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)",
            "if False:\n                    advisor_arg_error = AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)",
            id="admission-relocated-into-dead-branch",
        ),
    ],
)
def test_live_chain_changes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    function: object,
    old: str,
    new: str,
) -> None:
    _mutate_source(monkeypatch, function, old, new)
    with pytest.raises(AdvisorBindingError, match="advisor continuation provenance"):
        continuation_advisor_execution_owner(tool_batch)
    with pytest.raises(census.CensusError, match="advisor continuation provenance"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


def test_partial_extraction_cannot_fall_back_to_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(tool_batch, "_prepare_continuation_call_live")
    with pytest.raises(census.CensusError, match="_prepare_continuation_call_live is unavailable"):
        census._advisor_admission_handler()


def test_decoder_binding_must_be_the_actual_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    def unrelated_decoder(*args: object, **kwargs: object) -> object:
        raise AssertionError("source census must not execute this function")

    monkeypatch.setattr(tool_batch, "decode_wire_arguments", unrelated_decoder)
    with pytest.raises(census.CensusError, match="decode_wire_arguments no longer resolves"):
        census._advisor_admission_handler()


@pytest.mark.parametrize(
    "replacement",
    [
        "AdvisorCheckpointOwner._validate_advisor_arguments(ctx.service, arguments)",
        "AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, {'trigger': arguments['trigger']})",
        "ctx.advisor_checkpoint._validate_advisor_arguments(arguments)",
    ],
)
def test_relocated_admission_retains_receiver_and_complete_argument_controls(
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    _mutate_source(
        monkeypatch,
        tool_batch._execute_continuation_call_live,
        "AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)",
        replacement,
    )
    with pytest.raises(census.CensusError, match="advisor interception does not forward the complete decoded arguments"):
        census._advisor_admission_handler()


@pytest.mark.parametrize(
    ("function", "old", "new"),
    [
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            def hidden(_ignored=arguments.clear()):\n                pass",
            id="eager-function-default",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            @ (arguments.clear() or (lambda fn: fn))\n            def hidden():\n                pass",
            id="eager-decorator",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            class Hidden(arguments.clear() or object):\n                pass",
            id="eager-class-base",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            hidden = lambda _ignored=arguments.clear(): None",
            id="eager-lambda-default",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "decoded_args_by_call_id[tool_call.id] = audit_arguments\n            promises_by_call",
            "decoded_args_by_call_id[tool_call.id] = audit_arguments\n            audit_arguments.clear()\n            promises_by_call",
            id="actual-audit-alias-mutation",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            call.arguments.clear()",
            id="retained-field-mutation",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "call.arguments = arguments",
            "call.arguments = arguments\n            decoded_args_by_call_id[tool_call.id].clear()",
            id="retained-audit-map-alias-mutation",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            "prepared = True",
            "arguments.clear()\n            prepared = True",
            id="handoff-tail-mutation",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            '    if prepared:\n        return "prepared"',
            '    if prepared:\n        call.arguments.clear()\n        return "prepared"',
            id="return-after-finally-mutation",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = call.arguments\n    from some_owner import subset as arguments",
            id="import-from-rebinding",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = call.arguments\n    import some_owner as arguments",
            id="import-rebinding",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = call.arguments\n    match {}:\n        case arguments:\n            pass",
            id="match-capture-rebinding",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "global arguments\n    arguments = call.arguments",
            id="global-binding",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "arguments = call.arguments\n    def replace_input():\n        nonlocal arguments\n        arguments = {}\n    replace_input()",
            id="executed-nonlocal-rebinding",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            'setattr(call, "arguments", {})\n    arguments = call.arguments',
            id="direct-retained-owner-mutation",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "mutate(call)\n    arguments = call.arguments",
            id="unproved-call-owner-escape",
        ),
        pytest.param(
            tool_batch._execute_continuation_call_live,
            "arguments = call.arguments",
            "mutate(ctx)\n    arguments = call.arguments",
            id="unproved-context-owner-escape",
        ),
        pytest.param(
            tool_batch._prepare_continuation_call_live,
            'tool_feedback_json = json.dumps(error_payload)\n                llm_messages.append(\n                    {\n                        "role": "tool",\n                        "tool_call_id": tool_call.id,\n                        "content": tool_feedback_json,\n                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),\n                    }\n                )\n                all_cache_hits = False\n                continue\n\n            call.tool_name',
            'tool_feedback_json = json.dumps(error_payload)\n                llm_messages.append(\n                    {\n                        "role": "tool",\n                        "tool_call_id": tool_call.id,\n                        "content": tool_feedback_json,\n                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),\n                    }\n                )\n                all_cache_hits = False\n                prepared = True\n                continue\n\n            call.tool_name',
            id="refusal-fabricates-prepared-handoff",
        ),
        pytest.param(
            tool_batch.ContinuationCallState,
            "    arguments: dict[str, Any] | None = None",
            "    arguments: dict[str, Any] | None = None\n\n    def __post_init__(self):\n        self.arguments = {}",
            id="call-constructor-mutates-input",
        ),
        pytest.param(
            tool_batch.ToolBatchContext,
            "    advisor_checkpoint: AdvisorCheckpointOwner",
            '    advisor_checkpoint: AdvisorCheckpointOwner\n\n    def __post_init__(self):\n        object.__setattr__(self, "advisor_checkpoint", foreign_owner)',
            id="context-constructor-substitutes-receiver",
        ),
        pytest.param(
            canonical.canonical_json,
            "normalized = _normalize_for_canonical(obj)",
            "obj.clear()\n    normalized = _normalize_for_canonical(obj)",
            id="canonical-observer-mutates-input",
        ),
        pytest.param(
            required._find_missing_required_paths,
            "    missing_paths: list[str] = []",
            "    value.clear()\n    missing_paths: list[str] = []",
            id="required-path-observer-mutates-input",
        ),
        pytest.param(
            resource.promised_resources,
            "    resources: set[Resource] = set()",
            "    arguments.clear()\n    resources: set[Resource] = set()",
            id="resource-observer-mutates-input",
        ),
        pytest.param(
            registry.resolve_tool_effects,
            "    if tool_name in ASYNC_TOOL_EFFECTS:",
            "    arguments.clear()\n    if tool_name in ASYNC_TOOL_EFFECTS:",
            id="effect-observer-mutates-input",
        ),
    ],
)
def test_complete_executable_contract_refuses_hidden_origin_changes(
    monkeypatch: pytest.MonkeyPatch,
    function: object,
    old: str,
    new: str,
) -> None:
    _mutate_source(monkeypatch, function, old, new)
    with pytest.raises(AdvisorBindingError, match="advisor continuation provenance"):
        continuation_advisor_execution_owner(tool_batch)
    with pytest.raises(census.CensusError, match="advisor continuation provenance"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


@pytest.mark.parametrize(
    "name",
    [
        "AdvisorCheckpointOwner",
        "cast",
        "canonical_json",
        "begin_dispatch_or_arg_error",
        "build_canonicalization_sentinel",
        "promised_resources",
        "_find_missing_required_paths",
        "resolve_tool_effects",
        "dataclass",
        "field",
        "replace",
    ],
)
def test_actual_supplier_rebinding_is_not_authorized_by_same_source(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    def unrelated_supplier(*args: object, **kwargs: object) -> object:
        raise AssertionError("source receipt must not execute the substitute")

    monkeypatch.setattr(tool_batch, name, unrelated_supplier)
    with pytest.raises(AdvisorBindingError, match=rf"batch\.{name} no longer resolves"):
        continuation_advisor_execution_owner(tool_batch)
    with pytest.raises(census.CensusError, match=rf"batch\.{name} no longer resolves"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


def test_actual_manifest_membership_is_a_required_path_premise(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer import redaction

    assert "request_advisor_hint" in redaction.MANIFEST
    monkeypatch.setattr(
        redaction, "MANIFEST", {name: entry for name, entry in redaction.MANIFEST.items() if name != "request_advisor_hint"}
    )
    with pytest.raises(census.CensusError, match="actual redaction manifest"):
        census._advisor_admission_handler()


def test_actual_sent_table_membership_cannot_be_vacuous(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.tools import wire_projection

    monkeypatch.setattr(wire_projection, "_WIRE_TOOL_DEFS", {})
    monkeypatch.setattr(tool_batch, "_WIRE_TOOL_DEFS", wire_projection._WIRE_TOOL_DEFS)
    with pytest.raises(census.CensusError, match="actual sent-wire dialect"):
        census._advisor_admission_handler()


def test_same_module_destructive_wrapper_cannot_hide_behind_wrapped_source(monkeypatch: pytest.MonkeyPatch) -> None:
    original = canonical.canonical_json

    def destructive(payload: dict[str, object]) -> str:
        payload.clear()
        return original(payload)

    # Model the ordinary same-module @wraps escape: both supplier bindings
    # point at a wrapper whose globals are canonical's actual namespace.
    wrapper = FunctionType(
        destructive.__code__,
        vars(canonical),
        "destructive",
        closure=destructive.__closure__,
    )
    wrapper = wraps(original)(wrapper)
    assert wrapper.__globals__ is vars(canonical)
    assert inspect.getsource(wrapper) == inspect.getsource(original)
    monkeypatch.setattr(canonical, "canonical_json", wrapper)
    monkeypatch.setattr(tool_batch, "canonical_json", wrapper)
    # Audit imports the same owner in an ordinary source decoration.
    from elspeth.web.composer import audit

    monkeypatch.setattr(audit, "canonical_json", wrapper)
    with pytest.raises(census.CensusError, match=r"canonical\.canonical_json has unsupported wrapper indirection"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


def test_owned_boundary_wrappers_are_real_positive_inputs() -> None:
    from elspeth.web.composer.tools import wire_projection

    for observer in (resource.promised_resources, required._optional_ancestor_present, wire_projection.decode_wire_arguments):
        assert "__wrapped__" in vars(observer)
        assert "__trust_boundary__" in vars(observer)
    assert continuation_advisor_execution_owner(tool_batch) is tool_batch._execute_continuation_call_live


def test_validator_table_constructor_source_cannot_install_a_destructive_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.tools import wire_projection

    _mutate_source(
        monkeypatch,
        wire_projection,
        'Draft202012Validator(tool.thawed_function()["parameters"])',
        'DestructiveValidator(tool.thawed_function()["parameters"])',
    )
    with pytest.raises(census.CensusError, match=r"wire_module\.source changed its complete executable source contract"):
        census._advisor_admission_handler()


def test_validator_entry_must_be_the_nominal_schema_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.tools import wire_projection

    class DestructiveValidator:
        def __init__(self, schema: object) -> None:
            self.schema = schema

        def iter_errors(self, raw: dict[str, object]) -> tuple[()]:
            raw.clear()
            return ()

    tables = MappingProxyType(
        {
            dialect: MappingProxyType(
                {
                    name: DestructiveValidator(validator.schema) if name == "request_advisor_hint" else validator
                    for name, validator in validators.items()
                }
            )
            for dialect, validators in wire_projection._WIRE_VALIDATORS.items()
        }
    )
    monkeypatch.setattr(wire_projection, "_WIRE_VALIDATORS", tables)
    with pytest.raises(census.CensusError, match="wire classifier is not the actual Draft202012Validator"):
        census._advisor_admission_handler()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "if type(ctx.advisor_checkpoint) is not AdvisorCheckpointOwner:",
            "if False:",
        ),
        (
            "if type(ctx.advisor_checkpoint) is not AdvisorCheckpointOwner:",
            "if not isinstance(ctx.advisor_checkpoint, AdvisorCheckpointOwner):",
        ),
        (
            'raise AuditIntegrityError("Manual Advisor admission requires the owned checkpoint owner")',
            "pass",
        ),
    ],
    ids=["guard-bypassed", "subclass-admitted", "foreign-refusal-bypassed"],
)
def test_exact_nominal_admission_guard_is_part_of_the_executable_contract(
    monkeypatch: pytest.MonkeyPatch,
    old: str,
    new: str,
) -> None:
    _mutate_source(monkeypatch, tool_batch._execute_continuation_call_live, old, new)
    with pytest.raises(census.CensusError, match="changed its complete executable source contract"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


def test_pre_model_schema_observer_cannot_destroy_complete_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    _mutate_source(
        monkeypatch,
        dispatch._schema_errors,
        "return _errors_against(Draft202012Validator(_closed_root_schema(tool_name)), arguments)",
        "arguments.pop('schema_excerpt', None)\n    return _errors_against(Draft202012Validator(_closed_root_schema(tool_name)), arguments)",
    )
    with pytest.raises(census.CensusError, match=r"dispatch\._schema_errors changed its complete executable source contract"):
        census._advisor_admission_handler()
    assert census.census_model_wire()["request_advisor_hint"].site.startswith("unresolved:")


def test_advisor_pre_model_schema_supplier_is_the_actual_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer import advisor_checkpoint

    def destructive(tool_name: str, arguments: dict[str, object]) -> None:
        arguments.pop("unknown_complete_input_marker", None)
        dispatch.require_schema_valid_arguments(tool_name, arguments)

    monkeypatch.setattr(advisor_checkpoint, "require_schema_valid_arguments", destructive)
    with pytest.raises(census.CensusError, match=r"advisor\.require_schema_valid_arguments no longer resolves"):
        census._advisor_admission_handler()


def test_pre_model_schema_validator_is_the_actual_nominal_supplier(monkeypatch: pytest.MonkeyPatch) -> None:
    class ForeignValidator:
        def __init__(self, schema: object) -> None:
            self.schema = schema

        def iter_errors(self, arguments: dict[str, object]) -> tuple[()]:
            arguments.clear()
            return ()

    monkeypatch.setattr(dispatch, "Draft202012Validator", ForeignValidator)
    with pytest.raises(census.CensusError, match=r"dispatch\.Draft202012Validator no longer resolves"):
        census._advisor_admission_handler()
