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


@pytest.mark.parametrize("fault", ("property", "foreign-slot", "foreign-method", "code-only", "default"))
def test_live_source_record_binding_refuses_runtime_slot_method_and_code_substitution(monkeypatch, fault):
    from elspeth.web.composer import continuation_evidence_graph as graph
    from elspeth.web.composer import continuation_prompt_producers as producers

    assert continuation_advisor_execution_owner(tool_batch) is tool_batch._execute_continuation_call_live
    owner = graph.RestoredTurnEvidence
    if fault == "property":
        monkeypatch.setattr(owner, "messages", property(lambda self: ()))
    elif fault == "foreign-slot":
        monkeypatch.setattr(owner, "messages", vars(producers.ContinuationToolMessagePublication)["messages"])
    elif fault == "foreign-method":
        monkeypatch.setattr(owner, "__init__", producers.ContinuationToolMessagePublication.__init__)
    elif fault == "code-only":
        method = owner.__init__
        constants = tuple(value + " changed" if type(value) is str else value for value in method.__code__.co_consts)
        assert constants != method.__code__.co_consts
        monkeypatch.setattr(method, "__code__", method.__code__.replace(co_consts=constants))
    else:
        assert owner.__init__.__defaults__ == (None, None, None, None)
        monkeypatch.setattr(owner.__init__, "__defaults__", (None, None, None, "foreign"))
    with pytest.raises(AdvisorBindingError, match=r"source record|source-record"):
        continuation_advisor_execution_owner(tool_batch)


@pytest.mark.parametrize(
    ("member", "fault"),
    [(name, "code") for name in ("require_call", "register_feedback", "bind_completed_outcome", "register_result")]
    + [("require_call", "descriptor"), ("register_feedback", "globals")],
)
def test_publication_existing_methods_keep_actual_own_code_descriptor_and_globals(monkeypatch, member, fault):
    from elspeth.web.composer.continuation_prompt_producers import ContinuationToolMessagePublication

    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    owner = ContinuationToolMessagePublication
    method = vars(owner)[member]
    if fault == "descriptor":
        monkeypatch.setattr(owner, member, property(lambda self: None))
    elif fault == "globals":
        substitute = FunctionType(method.__code__, dict(method.__globals__), name=method.__name__, argdefs=method.__defaults__)
        monkeypatch.setattr(owner, member, substitute)
    else:
        assert method.__code__.co_names
        monkeypatch.setattr(method, "__code__", method.__code__.replace(co_names=("foreign", *method.__code__.co_names[1:])))
    with pytest.raises(AdvisorBindingError, match=r"source record producers\.ContinuationToolMessagePublication"):
        continuation_advisor_execution_owner(tool_batch)


def test_publication_constructor_refuses_defaults_subclass_before_truth_or_iteration(monkeypatch):
    from elspeth.web.composer.continuation_prompt_producers import ContinuationToolMessagePublication

    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    touched = []

    class MisleadingDefaults(tuple):
        def __bool__(self):
            touched.append("truth")
            return False

        def __iter__(self):
            touched.append("iteration")
            return iter(())

    defaults = MisleadingDefaults(("foreign catalog",))
    method = ContinuationToolMessagePublication.__init__
    assert method.__defaults__ is None
    monkeypatch.setattr(method, "__defaults__", defaults)
    assert method.__defaults__ is defaults and not touched
    with pytest.raises(AdvisorBindingError, match=r"source record.*exact defaults container"):
        continuation_advisor_execution_owner(tool_batch)
    assert not touched, "the finite checker must refuse the foreign container before invoking its hooks"


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


@pytest.mark.parametrize(
    ("owner", "old", "new"),
    [
        pytest.param(
            "initializer",
            "assistant_wire = native_assistant_message(completion)",
            "assistant_wire = native_assistant_message(other_completion)",
            id="original-completion-substituted",
        ),
        pytest.param(
            "initializer",
            "_register_continuation_assistant_message(ctx, call_model, assistant_wire)",
            "_register_continuation_assistant_message(ctx, call_model, dict(assistant_wire))",
            id="registered-message-detached",
        ),
        pytest.param(
            "native_message",
            '"arguments": tc.function.arguments,',
            '"arguments": "{}",',
            id="moved-native-arguments-replaced",
        ),
        pytest.param(
            "registration",
            "tool_call_id=tool_call_id,",
            "tool_call_id=None,",
            id="registration-call-identity-lost",
        ),
        pytest.param(
            "publication",
            "    def require_call(",
            "    def __post_init__(self) -> None:\n        self.call.arguments.clear()\n\n    def require_call(",
            id="pre-model-publication-constructor-destroys-input",
        ),
        pytest.param(
            "require_context",
            "        if (",
            "        self.inventory.running.session_operation_context = context\n        if (",
            id="pre-model-context-observer-mutates-owner",
        ),
        pytest.param(
            "observation_require",
            "        if recorder is not self.owner.recorder or audit is not self.audit:",
            "        self.owner.call.arguments.clear()\n        if recorder is not self.owner.recorder or audit is not self.audit:",
            id="pre-model-failure-observer-destroys-input",
        ),
        pytest.param(
            "custody_init",
            "self.observation = ContinuationDispatchFailureObservation(self, self.call.audit)",
            "self.observation = foreign_observation",
            id="custody-installs-foreign-observer",
        ),
        pytest.param(
            "issued_caller",
            "failure_custody=self.failure_custody,",
            "failure_custody=foreign_custody,",
            id="issued-caller-substitutes-custody",
        ),
        pytest.param(
            "execute",
            "on_provider_dispatch=reservation.dispatch,",
            "on_provider_dispatch=lambda: None,",
            id="reservation-dispatch-disconnected",
        ),
        pytest.param(
            "execute",
            "                        reservation.close()",
            "                        pass",
            id="reservation-final-close-removed",
        ),
        pytest.param(
            "wire_module",
            'if definition["name"] == "set_pipeline":',
            'if definition["name"] == "request_advisor_hint":',
            id="wire-factory-replaces-advisor-schema",
        ),
        pytest.param(
            "produce_current",
            "owner = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs, finish_read=finish_read)",
            "owner = foreign_producer",
            id="cold-producer-construction-substituted",
        ),
        pytest.param(
            "first_graph",
            "producer = CurrentContinuationMessageProducers(service, authority, selected, inventory, inputs)",
            "producer = foreign_producer",
            id="first-producer-construction-substituted",
        ),
        pytest.param(
            "producer_init",
            "        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead",
            "        self.inventory = foreign_inventory\n        from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead",
            id="producer-constructor-replaces-owner",
        ),
        pytest.param(
            "producer_declaration",
            "    def __post_init__(self) -> None:",
            "    def __getattribute__(self, name):\n        return foreign_value\n\n    def __post_init__(self) -> None:",
            id="producer-instance-lookup-substituted",
        ),
        pytest.param(
            "load_producers",
            "actual = outcome.value",
            "actual = foreign_production",
            id="producer-projection-substitutes-return",
        ),
        pytest.param(
            "restore_live",
            "return RestoredContinuationRuntime(live, bindings, graph, materialization, source, graph.drained, attachments, names)",
            "return RestoredContinuationRuntime(live, bindings, graph, foreign_producer, source, graph.drained, attachments, names)",
            id="restored-materialization-substituted",
        ),
        pytest.param(
            "first_runtime",
            "materialization=restored.materialization,",
            "materialization=foreign_producer,",
            id="first-runtime-forwarding-substituted",
        ),
        pytest.param(
            "cold_runtime",
            "graph=graph,\n            materialization=production.producers,",
            "graph=graph,\n            materialization=foreign_producer,",
            id="cold-runtime-forwarding-substituted",
        ),
        pytest.param(
            "build_context",
            "message_producers=runtime.materialization,",
            "message_producers=foreign_producer,",
            id="context-receiver-forwarding-substituted",
        ),
        pytest.param(
            "phase_read",
            "following = replace(runtime, source=fresh)",
            "following = replace(runtime, source=fresh, materialization=foreign_producer)",
            id="warm-runtime-replaces-receiver",
        ),
        pytest.param(
            "ordinary_adapter",
            "message_producers: CurrentContinuationMessageProducers | None = None,",
            "message_producers: CurrentContinuationMessageProducers | None = foreign_producer,",
            id="ordinary-none-default-replaced",
        ),
        pytest.param(
            "ordinary_caller",
            "dispatch_result, updated_advisor_calls_used = await self._dispatch_tool_batch(",
            "dispatch_result, updated_advisor_calls_used = await self._dispatch_tool_batch(\n                            message_producers=foreign_producer,",
            id="ordinary-caller-injects-receiver",
        ),
    ],
)
def test_p4_pre_model_observers_preserve_original_input(monkeypatch: pytest.MonkeyPatch, owner: str, old: str, new: str) -> None:
    from elspeth.web.composer import continuation_first_runtime as first
    from elspeth.web.composer import continuation_message_family_builders as builders
    from elspeth.web.composer import continuation_prompt_producers as producers
    from elspeth.web.composer import continuation_runtime as runtime
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_prompt_producers import (
        ContinuationToolMessagePublication,
        CurrentContinuationMessageProducers,
    )
    from elspeth.web.composer.continuation_tool_failure import (
        ContinuationDispatchFailureObservation,
        ContinuationToolFailureCustody,
    )
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.composer.tools import wire_projection

    owners = {
        "initializer": tool_batch._initialize_continuation_batch_live,
        "native_message": builders.native_assistant_message,
        "registration": tool_batch._register_continuation_assistant_message,
        "publication": ContinuationToolMessagePublication,
        "require_context": CurrentContinuationMessageProducers.require_context,
        "observation_require": ContinuationDispatchFailureObservation.require,
        "custody_init": ContinuationToolFailureCustody.__post_init__,
        "issued_caller": ContinuationLivePhases._execute_one_owned,
        "execute": tool_batch._execute_continuation_call_live,
        "wire_module": wire_projection,
        "produce_current": producers._produce_current_messages,
        "first_graph": first._restore_first_graph,
        "producer_init": CurrentContinuationMessageProducers.__post_init__,
        "producer_declaration": CurrentContinuationMessageProducers,
        "load_producers": producers.load_current_message_producers,
        "restore_live": runtime._restore_live,
        "first_runtime": first.restore_first_continuation_runtime,
        "cold_runtime": runtime.restore_continuation_runtime,
        "build_context": runtime.build_runtime_tool_batch_context,
        "phase_read": ContinuationLivePhases._current_phase_read,
        "ordinary_adapter": ComposerServiceImpl._dispatch_tool_batch,
        "ordinary_caller": ComposerServiceImpl._advance_live_compose_iteration,
    }
    # A stale unrelated prerequisite must fail here, never masquerade as a
    # successful opposition. No template replacement is installed by this test.
    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    with monkeypatch.context() as changed:
        _mutate_source(changed, owners[owner], old, new)
        with pytest.raises(AdvisorBindingError, match="changed its complete executable source contract"):
            continuation_advisor_execution_owner(tool_batch)
        row = census.census_model_wire()["request_advisor_hint"]
        assert row.model_class is None
        assert row.site.startswith("unresolved:")
    test_live_b2_chain_reaches_actual_nominal_advisor_model()


@pytest.mark.parametrize(
    ("owner", "name"),
    [
        ("runtime", "load_current_message_producers"),
        ("first", "CurrentContinuationMessageProducers"),
        ("phases", "ContinuationToolFailureCustody"),
        ("phases", "build_runtime_tool_batch_context"),
        ("initial", "restore_continuation_runtime"),
        ("wire", "canonical_set_pipeline_schema"),
        ("phases", "ContinuationLiveP4"),
        ("p4_application", "ContinuationLiveP4"),
        ("p4_application", "replace"),
        ("p4", "RestoredContinuationRuntime"),
    ],
)
def test_p4_receiver_supplier_identity_is_required(monkeypatch: pytest.MonkeyPatch, owner: str, name: str) -> None:
    from elspeth.web.composer import continuation_composition_initialization as initial
    from elspeth.web.composer import continuation_first_runtime as first
    from elspeth.web.composer import continuation_live_p4 as p4
    from elspeth.web.composer import continuation_live_p4_application as p4_application
    from elspeth.web.composer import continuation_live_phases as phases
    from elspeth.web.composer import continuation_runtime as runtime
    from elspeth.web.composer.tools import wire_projection as wire

    owners = {
        "runtime": runtime,
        "first": first,
        "phases": phases,
        "initial": initial,
        "wire": wire,
        "p4": p4,
        "p4_application": p4_application,
    }

    def foreign(*args: object, **kwargs: object) -> object:
        raise AssertionError("the source census must never invoke a substitute")

    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    with monkeypatch.context() as changed:
        changed.setattr(owners[owner], name, foreign)
        with pytest.raises(AdvisorBindingError, match=rf"{owner}\.{name} no longer resolves"):
            continuation_advisor_execution_owner(tool_batch)
        assert census.census_model_wire()["request_advisor_hint"].model_class is None
    test_live_b2_chain_reaches_actual_nominal_advisor_model()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "self.runtime = runtime = replace(runtime, graph=runtime.graph.with_batch(data))",
            "self.runtime = runtime = replace(runtime, graph=runtime.graph.with_batch(data), materialization=foreign_producer)",
            id="initialize-batch-replaces-producer",
        ),
        pytest.param(
            "self.runtime = owner.runtime",
            "self.runtime = replace(owner.runtime, materialization=foreign_producer)",
            id="persist-batch-replaces-producer",
        ),
    ],
)
def test_p4_phase_runtime_writers_preserve_producer_in_class_source(monkeypatch: pytest.MonkeyPatch, old: str, new: str) -> None:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    getsource = inspect.getsource
    original = getsource(ContinuationLivePhases)
    assert original.count(old) == 1
    changed = original.replace(old, new)
    class_reads: list[object] = []

    def source(subject: object) -> str:
        if subject is ContinuationLivePhases:
            class_reads.append(subject)
            return changed
        return getsource(subject)

    with monkeypatch.context() as mutation:
        mutation.setattr(inspect, "getsource", source)
        failures: list[str] = []
        try:
            continuation_advisor_execution_owner(tool_batch)
        except AdvisorBindingError as error:
            failures.append(str(error))
        # This is the actual class-source consumer. Patching an unselected
        # method's separate source object would never exercise the erased body.
        assert class_reads
        assert failures == [
            "advisor continuation provenance: phases.ContinuationLivePhases changed its complete executable source contract"
        ]
        row = census.census_model_wire()["request_advisor_hint"]
        assert row.model_class is None
        assert row.site.startswith("unresolved:")
    test_live_b2_chain_reaches_actual_nominal_advisor_model()


@pytest.mark.parametrize(
    ("owner", "old", "new", "contract"),
    [
        pytest.param(
            "application",
            "owner.runtime = replace(\n        runtime,",
            "owner.runtime = replace(\n        runtime,\n        materialization=foreign_producer,",
            "p4_application.finish_live_p4_application",
            id="application-replaces-materialization",
        ),
        pytest.param(
            "service",
            "continuation=continuation,",
            "continuation=foreign_owner,",
            "service.ComposerServiceImpl._persist_turn_audit",
            id="service-forwards-foreign-owner",
        ),
        pytest.param(
            "publication",
            "await finish_live_p4_application(continuation, effects_before=effects_before)",
            "await finish_live_p4_application(foreign_owner, effects_before=effects_before)",
            "turn_audit.persist_turn_audit",
            id="publication-forwards-foreign-owner",
        ),
        pytest.param(
            "p4_class",
            "    def __post_init__(self) -> None:\n        if (",
            "    def __post_init__(self) -> None:\n        self.runtime = foreign_runtime\n        if (",
            "p4.ContinuationLiveP4",
            id="p4-constructor-replaces-runtime",
        ),
        pytest.param(
            "phase_class",
            "self.p4 = owner = ContinuationLiveP4(",
            "self.p4 = owner = ForeignP4(",
            "phases.ContinuationLivePhases",
            id="phase-constructs-foreign-p4",
        ),
    ],
)
def test_p4_publication_preserves_actual_runtime_owner(
    monkeypatch: pytest.MonkeyPatch, owner: str, old: str, new: str, contract: str
) -> None:
    from elspeth.web.composer import continuation_live_p4_application as application
    from elspeth.web.composer import turn_audit
    from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.service import ComposerServiceImpl

    owners = {
        "application": application.finish_live_p4_application,
        "service": ComposerServiceImpl._persist_turn_audit,
        "publication": turn_audit.persist_turn_audit,
        "p4_class": ContinuationLiveP4,
        "phase_class": ContinuationLivePhases,
    }
    test_live_b2_chain_reaches_actual_nominal_advisor_model()
    with monkeypatch.context() as changed:
        _mutate_source(changed, owners[owner], old, new)
        with pytest.raises(AdvisorBindingError) as refusal:
            continuation_advisor_execution_owner(tool_batch)
        assert str(refusal.value) == f"advisor continuation provenance: {contract} changed its complete executable source contract"
        row = census.census_model_wire()["request_advisor_hint"]
        assert row.model_class is None
        assert row.site.startswith("unresolved:")
    test_live_b2_chain_reaches_actual_nominal_advisor_model()
