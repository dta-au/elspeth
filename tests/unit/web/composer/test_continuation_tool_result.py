"""Complete nominal result retention; no continuation worker or SQL fixtures."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.plugin_semantics import (
    ContentKind,
    FieldSemanticFacts,
    FieldSemanticRequirement,
    SemanticEdgeContract,
    SemanticOutcome,
    SemanticValueType,
    TextFraming,
)
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer.continuation_tool_batch import (
    ToolResultValue,
    decode_tool_result_json,
    decode_tool_result_value,
    encode_tool_result_value,
)
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.state import (
    CandidateReviewReferenceFacts,
    CompositionState,
    EdgeContract,
    SchemaContractDetail,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.schemas import (
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
    ValidationWarning,
)


def state():
    return CompositionState.from_dict(
        {
            "sources": {},
            "nodes": [
                {
                    "id": "rename_customer",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rows",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
                }
            ],
            "edges": [{"id": "edge-1", "from_node": "source", "to_node": "rename_customer", "edge_type": "on_success", "label": None}],
            "outputs": [],
            "metadata": {"name": "synthetic", "description": "café"},
            "version": 3,
        }
    )


def full_result():
    semantic = SemanticEdgeContract(
        from_id="source",
        to_id="rename_customer",
        consumer_plugin="field_mapper",
        producer_plugin="csv",
        producer_field="customer_key",
        consumer_field="customer_id",
        producer_facts=FieldSemanticFacts("customer_key", ContentKind.PLAIN_TEXT, configured_by=("schema",)),
        requirement=FieldSemanticRequirement(
            "customer_id",
            frozenset({ContentKind.MARKDOWN, ContentKind.PLAIN_TEXT}),
            frozenset({TextFraming.LINE_COMPATIBLE, TextFraming.COMPACT}),
            "synthetic_requirement",
            accepted_value_types=frozenset({SemanticValueType.STR, SemanticValueType.LIST}),
        ),
        outcome=SemanticOutcome.UNKNOWN,
    )
    error = ValidationEntry(
        component="rejected_mutation",
        message="Exact retained failure",
        severity="high",
        error_code="schema_contract_violation",
        contract=SchemaContractDetail("source", "rename_customer", ("customer_key",), ("other",)),
        plugin_identity=("transform", "field_mapper"),
        rejected_component="node:rename_customer",
        review_reference=CandidateReviewReferenceFacts("unknown-1", ("requirement-2", "requirement-1")),
    )
    validation = ValidationSummary(
        is_valid=False,
        errors=(error,),
        warnings=(ValidationEntry("source", "warning", "medium"),),
        suggestions=(ValidationEntry("output", "suggestion", "low"),),
        edge_contracts=(EdgeContract("source", "rename_customer", ("other",), ("customer_key",), ("customer_key",), False),),
        semantic_contracts=(semantic,),
    )
    prior = ValidationSummary(True, (), edge_contracts=(EdgeContract("source", "output", ("x",), ("x",), (), True),))
    runtime = ValidationResult(
        is_valid=False,
        checks=[
            ValidationCheck(
                name="graph_structure", passed=False, detail="retained check", affected_nodes=("rename_customer",), outcome_code=None
            )
        ],
        errors=[
            ValidationError(
                component_id="rename_customer", component_type="transform", message="runtime error", suggestion="repair", error_code=None
            )
        ],
        warnings=[
            ValidationWarning(
                component_id="source", component_type="source", message="runtime warning", suggestion=None, warning_code="synthetic_warning"
            )
        ],
        readiness=ValidationReadiness(
            authoring_valid=False,
            execution_ready=False,
            completion_ready=False,
            blockers=[
                ValidationReadinessBlocker(
                    code="synthetic_blocker",
                    component_id="rename_customer",
                    component_type="transform",
                    detail="blocked",
                    suggestion="repair",
                    note=None,
                )
            ],
        ),
        semantic_contracts=[],
    )
    return ToolResult(
        success=False,
        updated_state=state(),
        validation=validation,
        affected_nodes=("rename_customer", "source"),
        data={"message": "café", "nested": [1, True, None, 0.25]},
        prior_validation=prior,
        runtime_preflight=runtime,
        post_call_hints=("first", "second"),
        plugin_schemas={"source/csv": {"type": "object", "required": ["path"]}},
        validation_guidance={
            "codes": {"schema_contract_violation": {"explanation": "missing field", "suggested_fix": "repair schema"}},
            "explain_tool": "explain_validation_error",
        },
        applied_component={"edges": [{"id": "retained-edge", "label": "customer_key"}]},
        _validation_snapshot_hash="f" * 64,
        _state_validation_withheld=True,
    )


def wire(value):
    return canonical_json(value.model_dump(mode="json"))


def test_all_fields_roundtrip_preserves_nonpublic_validation_and_private_flags():
    original = full_result()
    encoded = encode_tool_result_value("set_metadata", original)
    restored = decode_tool_result_json(wire(encoded), pre_state=original.updated_state)
    assert type(restored) is ToolResult and restored.updated_state is original.updated_state
    assert restored.success is False
    assert restored.validation == original.validation
    assert restored.validation.edge_contracts == original.validation.edge_contracts
    assert restored.validation.errors[0].plugin_identity == ("transform", "field_mapper")
    assert restored.validation.errors[0].review_reference == original.validation.errors[0].review_reference
    assert restored.validation.semantic_contracts == original.validation.semantic_contracts
    assert restored.affected_nodes == ("rename_customer", "source")
    assert restored.data == original.data
    assert restored.prior_validation == original.prior_validation
    assert restored.runtime_preflight == original.runtime_preflight
    assert restored.post_call_hints == ("first", "second")
    assert restored.plugin_schemas == original.plugin_schemas
    assert restored.validation_guidance == original.validation_guidance
    assert restored.applied_component == original.applied_component
    assert restored._validation_snapshot_hash == "f" * 64
    assert restored._state_validation_withheld is True
    assert canonical_json(restored.updated_state.to_dict()) == encoded.state_json
    assert canonical_json(restored.to_dict()) == canonical_json(original.to_dict())
    assert wire(encode_tool_result_value("set_metadata", restored)) == wire(encoded)


def test_semantic_sets_have_sorted_wire_order_but_ordered_facts_keep_their_order():
    encoded = encode_tool_result_value("set_metadata", full_result())
    validation = json.loads(encoded.validation_json)
    requirement = validation["semantic_contracts"][0]["requirement"]
    assert requirement["accepted_content_kinds"] == ["markdown", "plain_text"]
    assert requirement["accepted_text_framings"] == ["compact", "line_compatible"]
    assert requirement["accepted_value_types"] == ["list", "str"]
    assert validation["errors"][0]["review_reference"]["valid_requirement_ids"] == ["requirement-2", "requirement-1"]
    requirement["accepted_content_kinds"].reverse()
    with pytest.raises(AuditIntegrityError, match="normalized"):
        decode_tool_result_value(encoded.model_copy(update={"validation_json": canonical_json(validation)}))


def test_state_identity_reuse_requires_complete_equal_bytes():
    original = full_result()
    encoded = encode_tool_result_value("set_metadata", original)
    cold = decode_tool_result_value(encoded)
    assert cold.updated_state is not original.updated_state
    assert cold.updated_state == original.updated_state
    other = replace(original.updated_state, metadata=replace(original.updated_state.metadata, description="other"))
    assert other.version == original.updated_state.version
    restored = decode_tool_result_value(encoded, pre_state=other)
    assert restored.updated_state is not other and restored.updated_state == original.updated_state
    assert restored.updated_state.edges == original.updated_state.edges
    assert restored.updated_state.nodes[0].options["mapping"] == {"customer_key": "customer_id"}


@pytest.mark.parametrize("tool_name", ["request_advisor_hint", "request_interpretation_review"])
def test_actual_async_tool_names_retain_complete_results_without_discovery_contract(tool_name):
    original = full_result()
    restored = decode_tool_result_json(wire(encode_tool_result_value(tool_name, original)))
    assert restored.validation == original.validation and restored.data == original.data
    assert restored._state_validation_withheld is True


def test_discovery_uses_actual_contract_on_both_sides_and_keeps_complete_result():
    original = replace(
        full_result(),
        success=True,
        data={
            "available": [PluginSummary(name="csv", description="CSV source", plugin_type="source", config_fields=[])],
            "prohibited": [],
        },
    )
    selected = admit_discovery_result("list_sources", original)
    encoded = encode_tool_result_value("list_sources", original)
    restored = decode_tool_result_json(wire(encoded))
    assert canonical_json(admit_discovery_result("list_sources", restored).to_dict()) == canonical_json(selected.to_dict())
    assert restored.validation == original.validation
    assert restored._validation_snapshot_hash == original._validation_snapshot_hash
    assert restored._state_validation_withheld is True
    with pytest.raises(FrameworkBugError):
        decode_tool_result_value(encoded.model_copy(update={"data_json": '{"available":[],"prohibited":[],"unknown":true}'}))


@pytest.mark.parametrize("success,data", [(True, None), (False, {"available": [], "prohibited": []})])
def test_discovery_invalid_success_data_pair_refuses_encode_and_decode(success, data):
    original = replace(full_result(), success=success, data=data)
    with pytest.raises(FrameworkBugError):
        encode_tool_result_value("list_sources", original)
    value = encode_tool_result_value("set_metadata", original)
    with pytest.raises(FrameworkBugError):
        decode_tool_result_value(value.model_copy(update={"tool_name": "list_sources"}))


def test_failed_discovery_without_data_roundtrips():
    original = replace(full_result(), data=None)
    restored = decode_tool_result_json(wire(encode_tool_result_value("list_sources", original)))
    assert restored.data is None and restored.success is False
    assert restored.validation == original.validation


def test_absent_optional_members_remain_absent_and_private_false_stays_false():
    original = ToolResult(success=True, updated_state=state(), validation=ValidationSummary(True, ()), affected_nodes=())
    restored = decode_tool_result_json(wire(encode_tool_result_value("set_metadata", original)))
    assert restored.data is None and restored.prior_validation is None and restored.runtime_preflight is None
    assert restored.plugin_schemas is None and restored.validation_guidance is None and restored.applied_component is None
    assert restored.post_call_hints == () and restored.affected_nodes == ()
    assert restored._validation_snapshot_hash is None and restored._state_validation_withheld is False


@pytest.mark.parametrize(
    "tool_name,decoder",
    [
        pytest.param("set_metadata", "value", id="mutation-value"),
        pytest.param("set_metadata", "json", id="mutation-json"),
        pytest.param("request_advisor_hint", "value", id="advisor-value"),
        pytest.param("request_advisor_hint", "json", id="advisor-json"),
        pytest.param("request_interpretation_review", "value", id="interpretation-value"),
        pytest.param("request_interpretation_review", "json", id="interpretation-json"),
    ],
)
def test_present_json_null_data_refuses_without_becoming_absent(tool_name, decoder):
    original = replace(full_result(), data=None)
    value = encode_tool_result_value(tool_name, original)
    assert value.data_json is None
    if decoder == "value":
        assert decode_tool_result_value(value).data is None
    else:
        assert decoder == "json"
        assert decode_tool_result_json(wire(value)).data is None
    corrupted = value.model_copy(update={"data_json": "null"})
    with pytest.raises(AuditIntegrityError, match="tool result data is not its owned object or array"):
        if decoder == "value":
            decode_tool_result_value(corrupted)
        else:
            decode_tool_result_json(wire(corrupted))


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_producer_data_is_refused_without_a_sentinel(number):
    with pytest.raises(AuditIntegrityError, match="finite"):
        encode_tool_result_value("set_metadata", replace(full_result(), data={"number": number}))


@pytest.mark.parametrize(
    "member",
    [
        "schema_version",
        "tool_name",
        "success",
        "state_json",
        "validation_json",
        "affected_nodes",
        "data_json",
        "prior_validation_json",
        "runtime_preflight_json",
        "post_call_hints",
        "plugin_schemas_json",
        "validation_guidance_json",
        "applied_component_json",
        "validation_snapshot_hash",
        "state_validation_withheld",
    ],
)
def test_omitted_complete_wire_member_never_reconstructs_a_default(member):
    payload = encode_tool_result_value("set_metadata", full_result()).model_dump(mode="json")
    del payload[member]
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_json(canonical_json(payload))


@pytest.mark.parametrize("member", ["edge_contracts", "warnings", "semantic_contracts"])
def test_omitted_validation_default_is_not_a_historical_empty_value(member):
    value = encode_tool_result_value("set_metadata", full_result())
    payload = json.loads(value.validation_json)
    del payload[member]
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_value(value.model_copy(update={"validation_json": canonical_json(payload)}))


@pytest.mark.parametrize(
    "member,payload",
    [
        ("data_json", "NaN"),
        ("data_json", "Infinity"),
        ("data_json", '{"a":1,"a":2}'),
        ("data_json", ' {"x":1}'),
        ("data_json", "3"),
        ("plugin_schemas_json", "[]"),
        ("validation_guidance_json", '{"codes":{},"unknown":true}'),
        ("applied_component_json", '{"node_id":"source"}'),
    ],
)
def test_finite_malformed_nested_values_refuse(member, payload):
    value = encode_tool_result_value("set_metadata", full_result())
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_value(value.model_copy(update={member: payload}))


def test_unknown_validation_entry_field_cannot_be_dropped():
    value = encode_tool_result_value("set_metadata", full_result())
    validation = json.loads(value.validation_json)
    validation["errors"][0]["undeclared"] = "would otherwise vanish"
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_value(value.model_copy(update={"validation_json": canonical_json(validation)}))


@pytest.mark.parametrize("change", ["unknown_root", "unknown_node", "missing_nodes", "bool_version", "float_version"])
def test_state_malformed_or_lossy_shapes_refuse(change):
    value = encode_tool_result_value("set_metadata", full_result())
    payload = json.loads(value.state_json)
    if change == "unknown_root":
        payload["undeclared"] = True
    elif change == "unknown_node":
        payload["nodes"][0]["undeclared"] = True
    elif change == "missing_nodes":
        del payload["nodes"]
    elif change == "bool_version":
        payload["version"] = True
    else:
        assert change == "float_version"
        payload["version"] = 1.5
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_value(value.model_copy(update={"state_json": canonical_json(payload)}))


def test_real_component_credential_guard_runs_on_encode_and_decode():
    original = full_result()
    payload = original.updated_state.to_dict()
    payload["outputs"] = [
        {"name": "output", "plugin": "database", "options": {"url": "synthetic-literal-value"}, "on_write_failure": "discard"}
    ]
    unsafe = CompositionState.from_dict(payload)
    with pytest.raises(CredentialMaterialRefused):
        encode_tool_result_value("set_metadata", replace(original, updated_state=unsafe))
    encoded = encode_tool_result_value("set_metadata", original)
    with pytest.raises(CredentialMaterialRefused):
        decode_tool_result_value(encoded.model_copy(update={"state_json": canonical_json(unsafe.to_dict())}))


@pytest.mark.parametrize("field,value", [("success", 1), ("state_validation_withheld", "false"), ("schema_version", "unknown")])
def test_invalid_closed_value_scalars_refuse(field, value):
    payload = encode_tool_result_value("set_metadata", full_result()).model_dump(mode="json")
    payload[field] = value
    with pytest.raises(AuditIntegrityError):
        decode_tool_result_json(canonical_json(payload))


def test_unknown_tool_and_non_nominal_result_refuse():
    with pytest.raises(AuditIntegrityError):
        encode_tool_result_value("unregistered_tool", full_result())
    with pytest.raises(TypeError):
        encode_tool_result_value("set_metadata", full_result().to_dict())
    with pytest.raises(TypeError):
        decode_tool_result_value({})
    value = encode_tool_result_value("set_metadata", full_result())
    with pytest.raises(TypeError):
        decode_tool_result_value(value, pre_state={})
    assert type(value) is ToolResultValue
