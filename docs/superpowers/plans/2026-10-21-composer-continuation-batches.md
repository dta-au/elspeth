# Composer Continuation Batch Owner Implementation Packet

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume the actual existing batch/call/P4 owners once without repeating an executed handler, assistant append, proposal, interpretation event or audit publication.

**Architecture:** Extract the exact b003 `tool_batch.run_tool_batch` body into nominal once-only batch and per-call carriers, preserving its branch order. The existing v1 entry drives these carriers to completion in memory. The v2 entry loads the selected complete accumulator and advances one durable phase, using the actual SQL result owners from storage and the actual Advisor/provider owner from main C6. Its return always contains the actual selected checkpoint after internal SQL, never a new frame copied over an independently advanced head.

**Tech Stack:** Python dataclasses, existing Composer response/validation/dispatch types, strict private chunk codecs, SQLAlchemy current-owner CAS and ordinary pytest.

**Source binding:** Production extraction is pinned to `b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73` (tree `6a34e912debc469e9ce7e2048d433c08d5e606a9`). The existing owner has reported genuine local GREEN for the later test-only `2e869240a388d1ede0454f1fbb9b019d22a9a9da` (tree `a44b6334aa674c5868ef79b4c2035b5563e47efe`): local Python 3.13, without coverage; PostgreSQL affected production inputs match b003. These are qualification limits, not execution of this plan. The clean combined target is `codex/composer-continuation-integration-20261021`; this packet remains documentation only.

## B0: Freeze the frames-to-batch entry and result contracts

This interface packet is a dependency contract for the concurrently authored frames driver. It is not an executable implementation stub; B1 onward supplies the complete carrier/extraction/codec bodies before any v2 entry is enabled.

**Create:** `src/elspeth/web/composer/continuation_tool_batch.py`.
**Modify:** `src/elspeth/web/composer/tool_batch.py`.

- [ ] Define these immutable outward contracts in `continuation_tool_batch.py`. Use normal imports from the modules that own each named type. `ContinuationToolBatchAdvance.dispatch` is non-null only after the complete actual issued batch (or its actual executed prefix on stop/failure) has reached the existing `_DispatchOutcome` construction. P4 remains an explicit phase owned by the extracted actual service P4 adapter and its `PersistedComposerTurnReceipt` application; `dispatch` is never evidence that P4 committed.

```python
from __future__ import annotations

from dataclasses import dataclass

from elspeth.web.composer._compose_loop_carriers import _DispatchOutcome, _PersistOutcome
from elspeth.web.composer.continuation import ContinuationFrameStack
from elspeth.web.composer.continuation_codec import PrivateChunk


@dataclass(frozen=True, slots=True)
class ContinuationToolBatchAdvance:
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]
    checkpoint_id: str
    dispatch: _DispatchOutcome | None
    advisor_calls_used: int
    persist: _PersistOutcome | None = None
```

- [ ] Add the exact keyword-only v2 entry below to the existing `tool_batch.py` owner, with its complete body supplied by B1 onward. Keep v1 `run_tool_batch`'s public signature unchanged. The selected root must have `ToolBatchFrame` on top, and its accumulator and response references must resolve through `chunks`. `call_model`, `ctx`, `acc` and `llm_messages` are freshly restored actual owners from the frames bootstrap, with `llm_messages` the SAME shared shelf used by TurnEvidence/recorder/transcript. `required_work` and `provider_owner` are the actual current request owners; no stored lease or old task is restored. Every internal SQL effect call carries `checkpoint_id` and `stack.cursor` selected together.

```python-signature
async def resume_continuation_tool_batch(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack,
    chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str,
    required_work: RequiredWorkBinding,
    provider_owner: ProviderInvocationOwner,
) -> ContinuationToolBatchAdvance:
```

- [ ] Use these exact nominal carrier names in the extraction and codec packet: `ContinuationBatchState` owns the once-only batch initialization, all loop-carried values and the current per-call value; `ContinuationCallState` owns one admitted call's current preparation/result/application phase. The phase vocabulary is closed: batch `uninitialized|call_ready|call_active|dispatch_ready|p4_pending|p4_result_retained|complete`; call `unprepared|prepared|owner_result_retained|application_pending|applied`. The typed public `ToolBatchFrame.phase` remains `tool_ready|effect_reconciling|result_retained`; it mirrors these private phases without widening the main root's closed union. A callback, generic dict or `ToolResult` alone never substitutes for an owner receipt. Newly added carrier fields and their full codecs are supplied in B1; the driver treats them as opaque private accumulator chunks.

- [ ] Bind P4's exact owner entry as `ComposerServiceImpl.persist_continuation_tool_batch`, with this signature. B6 retains the existing actual `_persist_turn_audit` / P4 source body and calls its concrete `persist_continuation_compose_turn` finish-once facade with `ContinuationOwnerCommit`; cold retained P4 application uses `restore_continuation_evidence` / `apply_retained_checkpoint`, never repeats the writer and never remints a live witness.

```python-signature
async def persist_continuation_tool_batch(
    self,
    *,
    dispatch: _DispatchOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack,
    chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str,
    required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
```

The `dispatch` input comes from the retained batch carrier and must match its selected accumulator, not an arbitrary new caller result. The returned `dispatch` remains available to the existing classification phase; completed P4 is proved by the actual retained P4 receipt consumed in the same selected-head CAS that advances its private phase. The frames driver may depend on these two signatures now; it must not enable v2 before B1 onward is complete and the dependency tests are GREEN.

## B1: Retain the exact nominal result and batch/call values

**Dependency:** Install only B1's pure `ToolResultValue` and complete result codecs before storage C3.8e read-result tests. The actual current `CompositionState.from_dict` owner and the existing `continuation_codec.owned_json` provide the pure reconstruction/JSON prerequisites. B1's `_state_from_tool_json` uses that actual state owner, requires canonical lossless bytes and an exact positive version, and applies the existing state credential projection on encode and decode. It does not depend on or install F7's evidence restoration/authorization machinery. F7 and the complete batch/call/read/writer/service cluster remain separately deferred. No provider execution, batch runtime, SQL hooks, current read admission or v2 enablement belongs to this prerequisite.

- [ ] Add the complete `ToolResultValue` sum member and codec below to `continuation_tool_batch.py`. It retains every actual `ToolResult` field, including edge contracts, the original validation snapshot identity and private withholding bit; its public `to_dict()` alone is insufficient. A selected discovery response crosses its actual current nominal contract before encoding and after decode. Mutation/private state uses the actual state credential projection. A restored validation snapshot is historical data; the normal current-policy producer/preflight checks still run before a new execution or provider projection.

```python
"""Pure complete ToolResult retention; no batch runtime or read authority."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.audit import _normalize_audit_payload
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.response_contracts import ResponseContract
from elspeth.web.composer.state import CompositionState, ValidationSummary
from elspeth.web.composer.tool_result_envelope import ValidationGuidance
from elspeth.web.composer.tools._common import AppliedComponentEcho, ToolResult
from elspeth.web.composer.tools._registry import ASYNC_TOOL_EFFECTS, response_contract_for
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.execution.schemas import ValidationResult


class ToolResultValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.tool-result.v1"] = "composer.tool-result.v1"
    tool_name: str
    success: bool
    state_json: str
    validation_json: str
    affected_nodes: tuple[str, ...]
    data_json: str | None
    prior_validation_json: str | None
    runtime_preflight_json: str | None
    post_call_hints: tuple[str, ...]
    plugin_schemas_json: str | None
    validation_guidance_json: str | None
    applied_component_json: str | None
    validation_snapshot_hash: str | None
    state_validation_withheld: bool


_VALIDATION = TypeAdapter(ValidationSummary)
_RUNTIME = TypeAdapter(ValidationResult)
_GUIDANCE = TypeAdapter(ValidationGuidance)
_SCHEMAS = TypeAdapter(dict[str, dict[str, JsonValue]])
_APPLIED = TypeAdapter(AppliedComponentEcho)


def _read_json_value(encoded: str) -> JsonValue:
    value, error = parse_json_strict(encoded)
    if error is not None or canonical_json(value) != encoded:
        raise AuditIntegrityError("tool continuation value is not complete canonical JSON")
    return owned_json(value)


def _optional_json(value: Any) -> str | None:
    return None if value is None else canonical_json(owned_json(deep_thaw(value)))


def _dump_owned[T](adapter: TypeAdapter[T], value: T) -> str:
    return canonical_json(owned_json(adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)))


def _decode_owned[T](adapter: TypeAdapter[T], encoded: str) -> T:
    _read_json_value(encoded)
    try:
        result = adapter.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation member violates its owned contract") from error
    if _dump_owned(adapter, result) != encoded:
        raise AuditIntegrityError("tool continuation member lost or normalized fields")
    return result


def _validation_json(value: ValidationSummary) -> str:
    if type(value) is not ValidationSummary:
        raise TypeError("tool validation requires its nominal summary")
    payload = _VALIDATION.dump_python(value, mode="json", warnings="error", fallback=owned_json)
    # These owned fields are sets, unlike all ordered errors/edges/hints.
    # Their wire order cannot depend on the encoder process's hash seed.
    for contract in payload["semantic_contracts"]:
        requirement = contract["requirement"]
        for field in ("accepted_content_kinds", "accepted_text_framings", "accepted_value_types"):
            requirement[field] = sorted(requirement[field])
    return canonical_json(owned_json(payload))


def _decode_validation(encoded: str) -> ValidationSummary:
    _read_json_value(encoded)
    try:
        result = _VALIDATION.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation validation violates its owned contract") from error
    if _validation_json(result) != encoded:
        raise AuditIntegrityError("tool continuation validation lost or normalized fields")
    return result


def _state_from_tool_json(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> CompositionState:
    value = _read_json_value(encoded)
    if type(value) is not dict:
        raise AuditIntegrityError("tool continuation state is not its owned object")
    if "version" not in value or type(value["version"]) is not int or value["version"] < 1:
        raise AuditIntegrityError("tool continuation state lost its exact positive version")
    try:
        state = CompositionState.from_dict(value)
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation state violates its owned contract") from error
    if canonical_json(state.to_dict()) != encoded:
        raise AuditIntegrityError("tool continuation state lost fields during decode")
    require_no_credential_material_in_state(state, surface="continuation tool state", env_ref_names=env_ref_names)
    return state


def _response_contract(tool_name: str) -> ResponseContract | None:
    if tool_name in ASYNC_TOOL_EFFECTS:
        return None
    try:
        return response_contract_for(tool_name)
    except KeyError as error:
        raise AuditIntegrityError("tool continuation result has no registered tool") from error


def encode_tool_result_value(
    tool_name: str,
    result: ToolResult,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResultValue:
    if type(result) is not ToolResult or type(result.updated_state) is not CompositionState:
        raise TypeError("tool continuation codec requires the actual nominal result and state")
    state_json = canonical_json(result.updated_state.to_dict())
    _state_from_tool_json(state_json, env_ref_names=env_ref_names)
    contract = _response_contract(tool_name)
    if contract is not None:
        admitted = admit_discovery_result(tool_name, result)
        data = None if admitted.response is None else admitted.response.to_wire()
    else:
        data = None if result.data is None else owned_json(_normalize_audit_payload(deep_thaw(result.data)))
    value = ToolResultValue(
        tool_name=tool_name,
        success=result.success,
        state_json=state_json,
        validation_json=_validation_json(result.validation),
        affected_nodes=result.affected_nodes,
        data_json=_optional_json(data),
        prior_validation_json=_validation_json(result.prior_validation) if result.prior_validation is not None else None,
        runtime_preflight_json=_dump_owned(_RUNTIME, result.runtime_preflight) if result.runtime_preflight is not None else None,
        post_call_hints=result.post_call_hints,
        plugin_schemas_json=_optional_json(result.plugin_schemas),
        validation_guidance_json=_optional_json(result.validation_guidance),
        applied_component_json=_optional_json(result.applied_component),
        validation_snapshot_hash=result._validation_snapshot_hash,
        state_validation_withheld=result._state_validation_withheld,
    )
    # Refuse a value that this build cannot restore completely; no lossy fallback.
    decode_tool_result_value(value, pre_state=result.updated_state, env_ref_names=env_ref_names)
    return value


def decode_tool_result_value(
    value: ToolResultValue,
    *,
    pre_state: CompositionState | None = None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResult:
    if type(value) is not ToolResultValue:
        raise TypeError("tool result decoder requires its closed owned value")
    # model_copy/model_construct can bypass a model's validation.
    try:
        admitted = ToolResultValue.model_validate_json(canonical_json(value.model_dump(mode="json", warnings="error")), strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("tool continuation value violates its closed model") from error
    restored = _state_from_tool_json(admitted.state_json, env_ref_names=env_ref_names)
    if pre_state is not None and type(pre_state) is not CompositionState:
        raise TypeError("tool result pre-state must be its nominal state")
    state = pre_state if pre_state is not None and canonical_json(pre_state.to_dict()) == admitted.state_json else restored
    data: dict[str, JsonValue] | list[JsonValue] | None
    if admitted.data_json is None:
        data = None
    else:
        parsed_data = _read_json_value(admitted.data_json)
        if not isinstance(parsed_data, dict | list):
            raise AuditIntegrityError("tool result data is not its owned object or array")
        data = parsed_data
    result = ToolResult(
        success=admitted.success,
        updated_state=state,
        validation=_decode_validation(admitted.validation_json),
        affected_nodes=admitted.affected_nodes,
        data=data,
        prior_validation=_decode_validation(admitted.prior_validation_json) if admitted.prior_validation_json is not None else None,
        runtime_preflight=_decode_owned(_RUNTIME, admitted.runtime_preflight_json) if admitted.runtime_preflight_json is not None else None,
        post_call_hints=admitted.post_call_hints,
        plugin_schemas=_decode_owned(_SCHEMAS, admitted.plugin_schemas_json) if admitted.plugin_schemas_json is not None else None,
        validation_guidance=_decode_owned(_GUIDANCE, admitted.validation_guidance_json)
        if admitted.validation_guidance_json is not None
        else None,
        applied_component=_decode_owned(_APPLIED, admitted.applied_component_json) if admitted.applied_component_json is not None else None,
        _validation_snapshot_hash=admitted.validation_snapshot_hash,
        _state_validation_withheld=admitted.state_validation_withheld,
    )
    if _response_contract(admitted.tool_name) is not None:
        discovery = admit_discovery_result(admitted.tool_name, result)
        selected_data = None if discovery.response is None else discovery.response.to_wire()
        if _optional_json(selected_data) != admitted.data_json:
            raise AuditIntegrityError("retained discovery data changed under its selected response contract")
    return result


def decode_tool_result_json(
    encoded: str,
    *,
    pre_state: CompositionState | None = None,
    env_ref_names: frozenset[str] = frozenset(),
) -> ToolResult:
    parsed = _read_json_value(encoded)
    try:
        value = ToolResultValue.model_validate_json(canonical_json(parsed), strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("retained tool result violates its complete owned codec") from error
    if canonical_json(value.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("retained tool result lost fields during decode")
    return decode_tool_result_value(value, pre_state=pre_state, env_ref_names=env_ref_names)


class RetainedBatchFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["plugin_crash", "advisor_failure", "advisor_timeout"]
    error_class: str = Field(min_length=1, max_length=128)

```

Complete nested validation, runtime, guidance, schema and component values must round-trip through their actual owned types without dropped or synthesized fields. `ValidationSummary` retains internal plugin/review facts, edge contracts and semantic contracts; only its three declared semantic requirement sets receive sorted wire order. Ordered error/edge/hint/reference tuples retain their original order. Known async results select the actual `ASYNC_TOOL_EFFECTS` registry and have no discovery contract; unknown tools refuse. Discovery results cross `admit_discovery_result` on encode and decode, including its success/data invariant. A restored validation snapshot or discovery response grants no current-policy, current content-version or read authorization.

- [ ] Create `tests/unit/web/composer/test_continuation_tool_result.py` with this complete ordinary pure test module. Run it separately before the deferred B0/F7/B4/P4 owners exist; it imports no `PreparedP4Value`, `project_p4_receipt`, bootstrap or SQL fixture. The later B7 receipt/SQL tests remain deferred with their actual owners. A present `data_json="null"` must refuse for the registered mutation and both actual async names through both decoders; actual absent data remains valid.

```python
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
```

- [ ] Fix the concrete read-receipt integration omission in storage C3.8e. Add required `tool_result_json: str` to `ContinuationReadOwnerResult`; preserve all its existing response and observed-version fields. In `build_read_owner_result`, import the pure `encode_tool_result_value` above, and set `tool_result_json=canonical_json(encode_tool_result_value(tool_name, result).model_dump(mode="json"))` in the actual DTO construction. In `re_admit_read_owner_result`, decode this exact field using `decode_tool_result_json`, admit that nominal result through `admit_discovery_result`, and require its complete `canonical_json(admitted.to_dict()) == result.response_json`. This is a complete nominal variant, never a `ToolResult.to_dict()` reconstruction. The existing actual source/row/version authorization remains unchanged.

```python
# Inside re_admit_read_owner_result, after the selected envelope checks.
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json
from elspeth.web.composer.discovery_response import admit_discovery_result
nominal = decode_tool_result_json(result.tool_result_json)
if canonical_json(admit_discovery_result(result.tool_name, nominal).to_dict()) != result.response_json:
    raise AuditIntegrityError("retained read envelope differs from its actual complete nominal result")
```

The next executable B1/B2 slice installs only the complete live carriers, the closed `RetainedBatchFailure` value already included in the pure module above, and the legacy extraction in B2. It does not install `ContinuationBatchValue`, capture/restore, `ToolBatchContext` continuation fields, B0/v2 entries or B3+ persistence. The actual pure state decoder is already available; it is not an F7 placeholder. Full cold activation still requires F7/F14 current evidence/materialization and the complete selected read/writer/service/protocol cluster. The current B1 snapshot design also requires a separately reviewed representation/refusal contract for the live `BatchAccumulator.authority_revocation`; the live extraction preserves that exact owner rather than silently serializing or losing it.

The live-only slice must qualify the actual Reserve constructor/formal/store closure before application: `batch.state` at the initializer and both finally copybacks is not covered by F5's exact `live.state` receipt. No naming exception, inventory refresh or broad state-writer grant substitutes for that proof. Actual relocated origins, common effects and existing unknown-form refusals remain obligations.

- [ ] Add the full live carrier definitions below to `tool_batch.py`, after its existing `BatchAccumulator`. They hold every value crossing the once-only batch and per-call boundaries. Loop-invariant deployment/current-context collaborators stay in the actual `ToolBatchContext`; provider clocks, tasks, callbacks and exceptions are never decoded from JSON. `original_failure` fields are retained only while their actual Python owner lives. The closed failure value later records an interrupted local failure without recreating an old exception object.

```python
@dataclass(slots=True)
class ContinuationCallState:
    index: int
    tool_call: _AdmittedToolCall
    pre_state: CompositionState
    pre_version: int
    phase: Literal["unprepared", "prepared", "owner_result_retained", "application_pending", "applied"] = "unprepared"
    tool_name: str = ""
    private_raw_arguments: str = ""
    private_arguments_canonical: str | None = None
    private_authority_canonical: str | None = None
    unknown_audit_arguments: dict[str, Any] | None = None
    wire: _CallWireFacts = field(default_factory=_CallWireFacts)
    arguments: dict[str, Any] | None = None
    audit_arguments: Mapping[str, Any] | str | None = None
    audit: DispatchAudit | None = None
    effect_key: str | None = None
    owner_result_kind: Literal["proposal", "persisted_turn", "read", "interpretation"] | None = None
    owner_result_json: str | None = None
    memory_result: ToolResult | AdmittedDiscoveryResult | None = None
    original_creator_id: str | None = None
    original_creator_epoch: int | None = None
    completion_json: str | None = None
    owner_tool_receipt_json: str | None = None
    advisor_invocation_json: str | None = None


@dataclass(slots=True)
class ContinuationBatchState:
    state: CompositionState
    last_validation: ValidationSummary | None
    last_runtime_preflight: ValidationResult | None
    advisor_calls_used: int
    entry_state: CompositionState
    early_reviewed: bool = False
    phase: Literal["uninitialized", "call_ready", "call_active", "dispatch_ready", "p4_pending", "p4_result_retained", "complete"] = (
        "uninitialized"
    )
    next_tool_index: int = 0
    assistant_message_index: int | None = None
    mixed_abandonment_batch: bool = False
    provider_model_version: str = ""
    turn_has_mutation: bool = False
    turn_has_discovery: bool = False
    all_cache_hits: bool = True
    tool_outcomes: list[_ToolOutcome] = field(default_factory=list)
    plugin_crash: ComposerPluginCrashError | None = None
    plugin_crash_cause: BaseException | None = None
    advisor_failure: Exception | None = None
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None = None
    pre_state_id: str | None = None
    decoded_args_by_call_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    private_authorings_by_call: dict[str, PrivateToolAuthoring] = field(default_factory=dict)
    proposals_this_turn: int = 0
    mutation_success_observed: bool = False
    refused_resources: set[Resource] = field(default_factory=set)
    promises_by_call: dict[str, frozenset[Resource]] = field(default_factory=dict)
    current_call: ContinuationCallState | None = None
    stop_issued_batch: bool = False
    p4: _PersistOutcome | None = None
    retained_failure: RetainedBatchFailure | None = None
    p4_receipt_json: str | None = None
    p4_input_json: str | None = None
    p4_verification_failed: bool = False
```

Import `field` from `dataclasses`, `_PersistOutcome` from `_compose_loop_carriers`, and `RetainedBatchFailure` from `continuation_tool_batch.py`. Replace no existing v1 carrier. Add optional actual continuation fields to `ToolBatchContext`: `continuation_materialization: CurrentContinuationMessageProducers | None = None` and `continuation: ContinuationBatchExecution | None = None`; use TYPE_CHECKING imports for those forward annotations. V2 requires the actual materialization owner; v1 leaves both unset.

For the deferred snapshot/carrier implementation below, add these imports to `continuation_tool_batch.py` when those future owners are implemented. They are not part of the pure prerequisite module above.

```python
from collections.abc import Mapping
from typing import Self

from pydantic import Field, model_validator
```

- [ ] Add these closed snapshot models to `continuation_tool_batch.py`. The audit stores a measured active duration, never a reusable monotonic anchor; a restore creates a new process-local anchor. A stopped process does not spend provider/action time by reconstructing its old clock. Original exception objects remain only on the live carrier. The actual writer receipt in B4 owns final invocation timing if an SQL owner commits before the caller returns.

```python
import time
from datetime import datetime

from elspeth.contracts.composer_audit import ComposerToolInvocation, PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.web.composer.audit import DispatchAudit



class DispatchAuditValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool_call_id: str
    tool_name: str
    arguments_canonical: str
    arguments_hash: str
    version_before: int
    started_at: datetime
    elapsed_active_ns: int = Field(ge=0)
    actor: str
    authority_arguments_canonical: str | None
    authority_arguments_hash: str | None
    strict_sent: bool | None
    wire_conformant: bool | None


def capture_dispatch_audit(audit: DispatchAudit) -> DispatchAuditValue:
    if type(audit) is not DispatchAudit:
        raise TypeError("dispatch snapshot requires its actual nominal audit")
    return DispatchAuditValue(
        tool_call_id=audit.tool_call_id, tool_name=audit.tool_name, arguments_canonical=audit.arguments_canonical,
        arguments_hash=audit.arguments_hash, version_before=audit.version_before, started_at=audit.started_at,
        elapsed_active_ns=max(0, time.monotonic_ns() - audit.started_ns), actor=audit.actor,
        authority_arguments_canonical=audit.authority_arguments_canonical, authority_arguments_hash=audit.authority_arguments_hash,
        strict_sent=audit.strict_sent, wire_conformant=audit.wire_conformant,
    )


def restore_dispatch_audit(value: DispatchAuditValue) -> DispatchAudit:
    return DispatchAudit(
        tool_call_id=value.tool_call_id, tool_name=value.tool_name, arguments_canonical=value.arguments_canonical,
        arguments_hash=value.arguments_hash, version_before=value.version_before, started_at=value.started_at,
        started_ns=time.monotonic_ns() - value.elapsed_active_ns, actor=value.actor,
        authority_arguments_canonical=value.authority_arguments_canonical, authority_arguments_hash=value.authority_arguments_hash,
        strict_sent=value.strict_sent, wire_conformant=value.wire_conformant,
    )


class ToolOutcomeValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    call_id: str
    response_kind: Literal["tool", "advisor", "none"]
    tool: ToolResultValue | None
    advisor_json: str | None
    error_class: str | None
    error_category: ToolArgumentErrorCategory | None
    error_message: str | None
    pre_version: int
    post_version: int
    strict_sent: bool | None
    wire_conformant: bool | None

    @model_validator(mode="after")
    def response_bundle(self) -> Self:
        if ((self.response_kind == "tool") != (self.tool is not None)
            or (self.response_kind == "advisor") != (self.advisor_json is not None)):
            raise ValueError("outcome lost its closed actual response variant")
        return self


class ContinuationCallValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    index: int = Field(ge=0)
    call_id: str
    pre_state_json: str
    pre_version: int
    phase: Literal["prepared", "owner_result_retained", "application_pending", "applied"]
    tool_name: str
    private_raw_arguments: str
    private_arguments_canonical: str | None
    private_authority_canonical: str | None
    unknown_audit_arguments_json: str | None
    strict_sent: bool | None
    wire_conformant: bool | None
    arguments_json: str
    audit_arguments_json: str
    audit: DispatchAuditValue
    effect_key: str | None
    owner_result_kind: Literal["proposal", "persisted_turn", "read", "interpretation"] | None
    owner_result_json: str | None
    memory_result: ToolResultValue | None
    memory_admitted: bool
    original_creator_id: str
    original_creator_epoch: int = Field(gt=0)
    completion_json: str | None = None
    owner_tool_receipt_json: str | None = None
    advisor_invocation_json: str | None = None


class ContinuationBatchValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.tool-batch.v1"] = "composer.tool-batch.v1"
    phase: Literal["call_ready", "call_active", "dispatch_ready", "p4_pending", "p4_result_retained", "complete"]
    state_json: str
    entry_state_json: str
    early_reviewed: bool = False
    last_validation_json: str | None
    last_runtime_preflight_json: str | None
    advisor_calls_used: int = Field(ge=0)
    next_tool_index: int = Field(ge=0)
    assistant_message_index: int = Field(ge=0)
    mixed_abandonment_batch: bool
    provider_model_version: str
    turn_has_mutation: bool
    turn_has_discovery: bool
    all_cache_hits: bool
    tool_outcomes: tuple[ToolOutcomeValue, ...]
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None
    pre_state_id: str | None
    decoded_args_json: str
    private_authorings_json: str
    proposals_this_turn: int = Field(ge=0)
    mutation_success_observed: bool
    refused_resources: tuple[tuple[str, str], ...]
    promises_by_call: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    current_call: ContinuationCallValue | None
    stop_issued_batch: bool
    failure: RetainedBatchFailure | None
    p4_receipt_json: str | None
    p4_input_json: str | None = None
    p4_verification_failed: bool = False

    @model_validator(mode="after")
    def once_only_cursor(self) -> Self:
        ids = tuple(item.call_id for item in self.tool_outcomes)
        if len(ids) != len(set(ids)) or len(ids) != self.next_tool_index:
            raise ValueError("batch cursor differs from its exact completed outcome prefix")
        if self.phase == "call_ready" and (self.current_call is not None or self.stop_issued_batch or self.failure is not None):
            raise ValueError("safe per-call boundary contains pending application or failure")
        if self.phase == "call_active" and (self.current_call is None or self.current_call.index != self.next_tool_index):
            raise ValueError("active batch lost its exact per-call cursor")
        if self.phase in ("p4_result_retained", "complete") and self.p4_receipt_json is None:
            raise ValueError("P4-completed phase has no actual retained owner receipt")
        return self


_AUTHORINGS = TypeAdapter(tuple[PrivateToolAuthoring, ...])


class ContinuationRetainedToolFailure(Exception):
    def __init__(self, failure: RetainedBatchFailure) -> None:
        self.failure = failure
        super().__init__("The interrupted tool turn retained its audit but lost its live exception owner.")
```

- [ ] Add the complete capture/restore functions below to `tool_batch.py`. Imports are explicit nominal types; no `locals()`, dynamic attribute walk, pickling or arbitrary exception deserialization is used. All `tool_outcomes` are interpreted against the complete original admitted batch, with its original call IDs/order. Rebind a read's unchanged state to its actual pre-state object on restore, preserving aliases used by the original callback and response contracts.

```python
def capture_continuation_batch_value(batch: ContinuationBatchState, calls: tuple[_AdmittedToolCall, ...]) -> ContinuationBatchValue:
    from elspeth.web.composer.continuation_tool_batch import (
        _AUTHORINGS, _RUNTIME, _VALIDATION, ContinuationBatchValue, ContinuationCallValue, RetainedBatchFailure,
        ToolOutcomeValue, capture_dispatch_audit, encode_tool_result_value, owned_json,
    )
    if batch.assistant_message_index is None:
        raise AuditIntegrityError("batch snapshot precedes its once-only assistant entry")
    by_id = {call.id: call for call in calls}
    values = []
    for outcome in batch.tool_outcomes:
        if outcome.call.id not in by_id or by_id[outcome.call.id] != outcome.call:
            raise AuditIntegrityError("batch outcome substituted its admitted provider call")
        if type(outcome.response) is ToolResult:
            kind, tool, advisor = "tool", encode_tool_result_value(outcome.call.function.name, outcome.response), None
        elif outcome.response is None:
            kind, tool, advisor = "none", None, None
        elif type(outcome.response) in (dict, MappingProxyType):
            kind, tool, advisor = "advisor", None, canonical_json(owned_json(outcome.response))
        else:
            raise AuditIntegrityError("batch outcome has no closed continuation response variant")
        values.append(ToolOutcomeValue(call_id=outcome.call.id, response_kind=kind, tool=tool, advisor_json=advisor,
            error_class=outcome.error_class, error_category=outcome.error_category, error_message=outcome.error_message,
            pre_version=outcome.pre_version, post_version=outcome.post_version,
            strict_sent=outcome.strict_sent, wire_conformant=outcome.wire_conformant))
    current = None
    if batch.current_call is not None:
        call = batch.current_call
        if (call.audit is None or call.arguments is None or call.audit_arguments is None
            or call.original_creator_id is None or call.original_creator_epoch is None):
            raise AuditIntegrityError("active tool snapshot lost complete prepared dispatch identity")
        memory = call.memory_result.result if type(call.memory_result) is AdmittedDiscoveryResult else call.memory_result
        current = ContinuationCallValue(index=call.index, call_id=call.tool_call.id,
            pre_state_json=canonical_json(call.pre_state.to_dict()), pre_version=call.pre_version, phase=call.phase,
            tool_name=call.tool_name, private_raw_arguments=call.private_raw_arguments,
            private_arguments_canonical=call.private_arguments_canonical, private_authority_canonical=call.private_authority_canonical,
            unknown_audit_arguments_json=canonical_json(owned_json(call.unknown_audit_arguments)) if call.unknown_audit_arguments is not None else None,
            strict_sent=call.wire.strict_sent, wire_conformant=call.wire.wire_conformant,
            arguments_json=canonical_json(owned_json(call.arguments)), audit_arguments_json=canonical_json(owned_json(call.audit_arguments)),
            audit=capture_dispatch_audit(call.audit), effect_key=call.effect_key, owner_result_kind=call.owner_result_kind,
            owner_result_json=call.owner_result_json, memory_result=encode_tool_result_value(call.tool_name, memory) if memory is not None else None,
            memory_admitted=type(call.memory_result) is AdmittedDiscoveryResult,
            original_creator_id=call.original_creator_id, original_creator_epoch=call.original_creator_epoch,
            completion_json=call.completion_json, owner_tool_receipt_json=call.owner_tool_receipt_json,
            advisor_invocation_json=call.advisor_invocation_json)
    failure = batch.retained_failure
    if batch.plugin_crash is not None:
        failure = RetainedBatchFailure(kind="plugin_crash", error_class=type(batch.plugin_crash.original_exc).__name__)
    elif batch.advisor_failure is not None:
        failure = RetainedBatchFailure(kind="advisor_failure", error_class=type(batch.advisor_failure).__name__)
    elif batch.advisor_compose_timeout is not None:
        failure = RetainedBatchFailure(kind="advisor_timeout", error_class="TimeoutError")
    return ContinuationBatchValue(
        phase=batch.phase, state_json=canonical_json(batch.state.to_dict()),
        entry_state_json=canonical_json(batch.entry_state.to_dict()), early_reviewed=batch.early_reviewed,
        last_validation_json=canonical_json(_VALIDATION.dump_python(batch.last_validation, mode="json", warnings="error", fallback=owned_json)) if batch.last_validation is not None else None,
        last_runtime_preflight_json=canonical_json(_RUNTIME.dump_python(batch.last_runtime_preflight, mode="json", warnings="error", fallback=owned_json)) if batch.last_runtime_preflight is not None else None,
        advisor_calls_used=batch.advisor_calls_used, next_tool_index=batch.next_tool_index,
        assistant_message_index=batch.assistant_message_index, mixed_abandonment_batch=batch.mixed_abandonment_batch,
        provider_model_version=batch.provider_model_version, turn_has_mutation=batch.turn_has_mutation,
        turn_has_discovery=batch.turn_has_discovery, all_cache_hits=batch.all_cache_hits, tool_outcomes=tuple(values),
        advisor_compose_timeout=batch.advisor_compose_timeout, pre_state_id=batch.pre_state_id,
        decoded_args_json=canonical_json(owned_json(batch.decoded_args_by_call_id)),
        private_authorings_json=canonical_json(_AUTHORINGS.dump_python(tuple(batch.private_authorings_by_call.values()), mode="json", warnings="error")),
        proposals_this_turn=batch.proposals_this_turn, mutation_success_observed=batch.mutation_success_observed,
        refused_resources=tuple(sorted(batch.refused_resources)),
        promises_by_call=tuple((key, tuple(sorted(value))) for key, value in sorted(batch.promises_by_call.items())),
        current_call=current, stop_issued_batch=batch.stop_issued_batch, failure=failure,
        p4_receipt_json=batch.p4_receipt_json,
        p4_input_json=batch.p4_input_json, p4_verification_failed=batch.p4_verification_failed,
    )


def restore_continuation_batch_value(value: ContinuationBatchValue, calls: tuple[_AdmittedToolCall, ...]) -> ContinuationBatchState:
    from elspeth.web.composer.continuation_tool_batch import (
        _AUTHORINGS, _RUNTIME, _VALIDATION, _read_json_value, _state_from_tool_json,
        decode_tool_result_value, restore_dispatch_audit,
    )
    if tuple(item.call_id for item in value.tool_outcomes) != tuple(call.id for call in calls[:value.next_tool_index]):
        raise AuditIntegrityError("retained batch lost its exact original completed prefix")
    state = _state_from_tool_json(value.state_json)
    decoded = _read_json_value(value.decoded_args_json)
    if type(decoded) is not dict or any(type(item) is not dict for item in decoded.values()):
        raise AuditIntegrityError("retained batch decoded arguments lost their object maps")
    batch = ContinuationBatchState(state=state,
        entry_state=_state_from_tool_json(value.entry_state_json), early_reviewed=value.early_reviewed,
        last_validation=_VALIDATION.validate_json(value.last_validation_json, strict=True) if value.last_validation_json is not None else None,
        last_runtime_preflight=_RUNTIME.validate_json(value.last_runtime_preflight_json, strict=True) if value.last_runtime_preflight_json is not None else None,
        advisor_calls_used=value.advisor_calls_used, phase=value.phase, next_tool_index=value.next_tool_index,
        assistant_message_index=value.assistant_message_index, mixed_abandonment_batch=value.mixed_abandonment_batch,
        provider_model_version=value.provider_model_version, turn_has_mutation=value.turn_has_mutation,
        turn_has_discovery=value.turn_has_discovery, all_cache_hits=value.all_cache_hits,
        advisor_compose_timeout=value.advisor_compose_timeout, pre_state_id=value.pre_state_id,
        decoded_args_by_call_id=decoded, private_authorings_by_call={item.tool_call_id: item for item in _AUTHORINGS.validate_json(value.private_authorings_json, strict=True)},
        proposals_this_turn=value.proposals_this_turn, mutation_success_observed=value.mutation_success_observed,
        refused_resources=set(value.refused_resources), promises_by_call={key: frozenset(resources) for key, resources in value.promises_by_call},
        stop_issued_batch=value.stop_issued_batch, retained_failure=value.failure, p4_receipt_json=value.p4_receipt_json,
        p4_input_json=value.p4_input_json, p4_verification_failed=value.p4_verification_failed)
    for call, item in zip(calls, value.tool_outcomes, strict=False):
        response = (decode_tool_result_value(item.tool) if item.tool is not None else
                    _read_json_value(item.advisor_json) if item.advisor_json is not None else None)
        batch.tool_outcomes.append(_ToolOutcome(call=call, response=response,
            error_class=item.error_class, error_category=item.error_category, error_message=item.error_message,
            pre_version=item.pre_version, post_version=item.post_version,
            strict_sent=item.strict_sent, wire_conformant=item.wire_conformant))
    if value.current_call is not None:
        item = value.current_call
        if item.index >= len(calls) or calls[item.index].id != item.call_id or calls[item.index].function.name != item.tool_name:
            raise AuditIntegrityError("retained current call differs from its actual provider response")
        pre_state = _state_from_tool_json(item.pre_state_json)
        arguments, audit_arguments = _read_json_value(item.arguments_json), _read_json_value(item.audit_arguments_json)
        unknown = _read_json_value(item.unknown_audit_arguments_json) if item.unknown_audit_arguments_json is not None else None
        if type(arguments) is not dict or type(audit_arguments) not in (dict, str) or unknown is not None and type(unknown) is not dict:
            raise AuditIntegrityError("retained current call lost its exact argument variants")
        memory = decode_tool_result_value(item.memory_result, pre_state=pre_state) if item.memory_result is not None else None
        if item.memory_admitted:
            if memory is None:
                raise AuditIntegrityError("retained admitted result lost its actual nominal tool value")
            memory = admit_discovery_result(item.tool_name, memory)
        batch.current_call = ContinuationCallState(index=item.index, tool_call=calls[item.index], pre_state=pre_state,
            pre_version=item.pre_version, phase=item.phase, tool_name=item.tool_name,
            private_raw_arguments=item.private_raw_arguments, private_arguments_canonical=item.private_arguments_canonical,
            private_authority_canonical=item.private_authority_canonical, unknown_audit_arguments=unknown,
            wire=_CallWireFacts(item.strict_sent, item.wire_conformant), arguments=arguments, audit_arguments=audit_arguments,
            audit=restore_dispatch_audit(item.audit), effect_key=item.effect_key, owner_result_kind=item.owner_result_kind,
            owner_result_json=item.owner_result_json, memory_result=memory,
            original_creator_id=item.original_creator_id, original_creator_epoch=item.original_creator_epoch,
            completion_json=item.completion_json, owner_tool_receipt_json=item.owner_tool_receipt_json,
            advisor_invocation_json=item.advisor_invocation_json)
    return batch
```

`p4_receipt_json` is the exact actual P4 owner receipt, not an `_PersistOutcome`/witness serialization. Import `MappingProxyType` normally and the named `ContinuationBatchValue`/`ContinuationCallValue` result types for annotations.

## B2: Extract the actual once-only initializer and one-call lexical owner

**Modify:** `composer/tool_batch.py`. Use the complete current implementation below after the B1 live carriers. The legacy signature remains unchanged. The complete current preparation/suffix bodies, nested default captures, Advisor current-context arguments, error classes and narrow passthroughs are preserved. Current `.10` mutation authority remains on the same actual `BatchAccumulator` passed to execution and finish: its revocation assignment, stop condition and dispatch field must not be replaced by the historical pre-revocation projection.

Manual Advisor admission deliberately narrows the receiver contract at this touched boundary. Require `type(ctx.advisor_checkpoint) is AdvisorCheckpointOwner`, then call `AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)` explicitly. This retains the same actual instance, its settings and prompt-size cap, and the entire decoded mapping. Foreign owners and subclasses raise the first-party `AuditIntegrityError`; per-instance overrides of this one admission method are ignored. That is a material compatibility change from dynamic instance dispatch, not a claim that every historical extension remains supported. Other Advisor methods, provider selection, audit, budgets and guard decisions remain unchanged. The source census must join the actual class supplier and both positional arguments to this nominal guard; its receipt does not prove unrelated service lifecycle behavior.

Only aliases consumed by a stage, or explicitly returned through its finally copyback, belong in that stage. This avoids unused historical alias baggage. The preparation closure retains its original default captures; execution's forwarding closure captures the actual batch and call, whose `pre_state` is the pre-call state, while its untouched dispatch/preflight/version helper defaults remain lexical to the one actual call. One-element loop `continue` finishes that call; `break` sets the batch stop flag; exceptions execute copyback and keep their original identity. The initializer refuses repeated successful initialization; this is an in-process invariant, not durable retry, cold reentry or new authorization. A failed initializer is not automatically replayable.

The B3+ live caller fences below forward the same actual `acc` through every execute/finish branch: `resume_continuation_tool_batch`, `stop_continuation_tool_batch`, `resume_continuation_manual_advisor`, `persist_continuation_tool_batch`, and `_stop_prepared_continuation_tool_batch_value`. `ContinuationBatchExecution.acc` is that identical input object; B8/B9 timing wrappers forward it unchanged. B3 checkpoint copies the batch's four mutable value fields into that object before `capture_continuation_batch_owners(..., acc=self.acc, ...)` and does not reset its `authority_revocation`. B5.1 explicitly widens the current `Literal["applied"]` result only when its real manual-Advisor child branch is installed. These are executable live-call signature repairs, not cold activation. B1's historical value/capture/restore design and F22's four-field accumulator reconstruction still require the separately implemented coupled batch/call/accumulator codec: retain the actual state identity graph, current-call pre-state/result aliases and exact revocation together. Until that owner exists and is qualified, these cold entries remain blocked; neither the P4 value nor a fresh default accumulator may stand in for it. The existing local capture of four fields alone does not prove durable revocation retention.

The finish projection computes the completed call-ID set once, then retains recorder invocation order. This removes repeated set construction without changing the selected cohort or audit fields. No SQL/provider activation, continuation context placeholder or legacy tool resurrection is introduced.

Preserve main's H3/C6 closed decision/control admission; do not restore the old lexical `cannot_complete` predicate during this extraction. The initializer retains whole-response credential admission, the whole-batch approval cap, actual tool-ID preflight, progress, and the once-only assistant/native append and transcript observation. It performs no handler or SQL mutation and must not fabricate an empty issued turn after failure. The later cold entry must decode its actual existing accumulator once a `ToolBatchFrame` exists; the live initializer is not a cold replay mechanism.

Keep the actual manual Advisor current-owner arguments after `current_state=state` in the relocated legacy branch:

```python-call-keywords
session_id=ctx.session_id,
session_operation_context=ctx.session_operation_context,
```

The later v2 branch uses F13's actual current-owner producer. Retain the safe request-rejection branch, actual exception identities and `raise ... from ...` chains; assertions, memory/recursion/system errors and `AuditIntegrityError` keep their original narrow passthrough behavior. Process-lost failures use only `RetainedBatchFailure`, never reconstructed provider exceptions. Preserve the exact pre-call state in resource-refusal accounting, lexical helper/default captures, finally copyback, complete issued call tuple, executed outcome prefix and original recorder invocation links. The complete qualified bodies below supersede the older alias-copy outline; none of these resource, authority or audit constraints is relaxed by that replacement.

```python
async def _initialize_continuation_batch_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
) -> None:
    """Admit and publish the one actual assistant batch before any call."""
    if batch.phase != "uninitialized":
        raise AuditIntegrityError("tool batch initialization is not repeatable")
    recorder = ctx.recorder
    current_state_id = ctx.current_state_id
    progress = ctx.progress
    turn_sessions_service = ctx.turn_sessions_service
    turn_session_uuid = ctx.turn_session_uuid
    state = acc.state
    last_validation = acc.last_validation
    last_runtime_preflight = acc.last_runtime_preflight
    advisor_calls_used = acc.advisor_calls_used

    assistant_message_index = len(llm_messages)

    completion = call_model.completion
    assistant_message = completion.message
    raw_assistant_content = assistant_message.content
    admitted_batch = completion.tool_batch
    assistant_tool_calls = admitted_batch.calls
    mixed_abandonment_batch = False
    if len(assistant_tool_calls) > 1:
        for candidate_call in assistant_tool_calls:
            if candidate_call.function.name != "request_advisor_hint":
                continue
            try:
                candidate_arguments = bounded_json_loads(candidate_call.function.arguments, label="advisor batch admission")
            except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError):
                continue
            if (
                type(candidate_arguments) is dict
                and "trigger" in candidate_arguments
                and candidate_arguments["trigger"] == ADVISOR_TRIGGER_CANNOT_COMPLETE
            ):
                mixed_abandonment_batch = True
                break
    if ctx.turn_evidence is not None:
        ctx.turn_evidence.invalidate_abandonment()
    # Scan the complete response metadata before progress, history, audit,
    # proposal, blob, or state effects. Internal adapters can supply an
    # already-admitted completion without passing through ProviderGateway, so
    # this common owner must cover prose, identifiers, reasoning, and mixed
    # batches all-or-nothing as well.
    require_no_credential_material_in_llm_metadata(
        content=raw_assistant_content,
        tool_calls=tuple((call.id, call.function.name) for call in assistant_tool_calls),
        reasoning_content=completion.provider_metadata.reasoning_content,
        reasoning_details=completion.provider_metadata.reasoning_details,
        thinking_blocks=completion.provider_metadata.thinking_blocks,
        model_returned=completion.provider_metadata.model_returned,
        provider_request_id=completion.provider_metadata.provider_request_id,
        finish_reason=completion.provider_metadata.finish_reason,
        provider_served=completion.provider_metadata.provider_served,
        surface="composer_tool_batch",
    )
    from elspeth.web.credential_guard import credential_state_for_tool_batch

    batch_credential_state = credential_state_for_tool_batch(state, tuple(call.function.name for call in assistant_tool_calls))
    for tool_call in assistant_tool_calls:
        require_no_credential_material_in_tool_wire(
            tool_call.function.name,
            tool_call.function.arguments,
            surface="composer_tool_wire",
            credential_state=batch_credential_state,
        )
    provider_model_version = completion.provider_metadata.model_returned or ctx.provenance.model_identifier
    if ctx.review_required:
        approval_required_mutations = sum(
            1
            for tool_call in assistant_tool_calls
            if is_mutation_tool(tool_call.function.name)
            and (
                not is_blob_store_only_mutation_tool(tool_call.function.name)
                or is_approval_required_blob_store_only_mutation_tool(tool_call.function.name)
            )
        )
        if approval_required_mutations > _MAX_PENDING_PROPOSALS_PER_TURN:
            raise _pending_proposal_cap_error(approval_required_mutations, ctx=ctx, state=state)
    await _preflight_session_tool_call_ids(
        admitted_batch,
        sessions_service=turn_sessions_service,
        session_id=turn_session_uuid,
        recorder=recorder,
    )

    await emit_progress(
        progress,
        tool_batch_progress_event(
            tuple(tool_call.function.name for tool_call in assistant_tool_calls),
        ),
    )

    # Append the assistant message (with tool_calls metadata)
    from elspeth.web.composer.provider_thinking import attach_provider_thinking

    llm_messages.append(
        attach_provider_thinking(
            {
                "role": "assistant",
                "content": assistant_message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_tool_calls
                ],
            },
            completion.provider_metadata.thinking_blocks,
        )
    )

    # Execute each tool call, tracking whether this turn has
    # budgeted discovery or mutation work. Advisor-only turns are
    # intentionally neither: they spend the advisor-specific budget,
    # not the generic discovery/composition turn budgets.
    if ctx.turn_evidence is not None:
        ctx.turn_evidence.transcript.observe_messages(llm_messages)
    turn_has_mutation = False
    turn_has_discovery = False
    all_cache_hits = True
    # Step 1 — execute tool calls in async land while accumulating
    # immutable _ToolOutcome records. Step 2 (in _persist_turn_audit)
    # performs audit writes from this list; cancellation before Step 2
    # leaves the DB unchanged for the current assistant response.
    tool_outcomes: list[_ToolOutcome] = []
    plugin_crash: ComposerPluginCrashError | None = None
    plugin_crash_cause: BaseException | None = None
    advisor_failure: Exception | None = None
    advisor_compose_timeout: Literal["pre_call", "in_flight"] | None = None
    pre_state_id: str | None = current_state_id
    decoded_args_by_call_id: dict[str, dict[str, Any]] = {}
    private_authorings_by_call: dict[str, PrivateToolAuthoring] = {}
    proposals_this_turn = 0
    mutation_success_observed = False
    refused_resources: set[Resource] = set()
    promises_by_call: dict[str, frozenset[Resource]] = {}
    batch.state = state
    batch.last_validation = last_validation
    batch.last_runtime_preflight = last_runtime_preflight
    batch.advisor_calls_used = advisor_calls_used
    batch.turn_has_mutation = turn_has_mutation
    batch.turn_has_discovery = turn_has_discovery
    batch.all_cache_hits = all_cache_hits
    batch.tool_outcomes = tool_outcomes
    batch.plugin_crash = plugin_crash
    batch.plugin_crash_cause = plugin_crash_cause
    batch.advisor_failure = advisor_failure
    batch.advisor_compose_timeout = advisor_compose_timeout
    batch.pre_state_id = pre_state_id
    batch.decoded_args_by_call_id = decoded_args_by_call_id
    batch.private_authorings_by_call = private_authorings_by_call
    batch.proposals_this_turn = proposals_this_turn
    batch.mutation_success_observed = mutation_success_observed
    batch.refused_resources = refused_resources
    batch.promises_by_call = promises_by_call
    batch.assistant_message_index = assistant_message_index
    batch.mixed_abandonment_batch = mixed_abandonment_batch
    batch.provider_model_version = provider_model_version
    batch.phase = "call_ready" if assistant_tool_calls else "dispatch_ready"


def _append_continuation_tool_outcome(
    batch: ContinuationBatchState,
    call: ContinuationCallState,
    *,
    response: Any,
    error_class: str | None,
    error_category: ToolArgumentErrorCategory | None,
    error_message: str | None,
    post_version: int,
) -> None:
    if error_class is not None or (isinstance(response, ToolResult) and not response.success):
        batch.refused_resources.update(unavailable_resources(batch.promises_by_call[call.tool_call.id], call.pre_state))
    batch.tool_outcomes.append(
        _ToolOutcome(
            call=call.tool_call,
            response=response,
            error_class=error_class,
            error_category=error_category,
            error_message=error_message,
            pre_version=call.pre_version,
            post_version=post_version,
            strict_sent=call.wire.strict_sent,
            wire_conformant=call.wire.wire_conformant,
        )
    )


async def _prepare_continuation_call_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> Literal["prepared", "applied", "stop"]:
    """Retain the real preparation boundary; early refusals remain outcomes."""
    recorder = ctx.recorder
    anti_anchor = ctx.anti_anchor
    actor = ctx.actor
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    advisor_calls_used = batch.advisor_calls_used
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    tool_outcomes = batch.tool_outcomes
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    advisor_compose_timeout = batch.advisor_compose_timeout
    pre_state_id = batch.pre_state_id
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    proposals_this_turn = batch.proposals_this_turn
    mutation_success_observed = batch.mutation_success_observed
    refused_resources = batch.refused_resources
    promises_by_call = batch.promises_by_call
    mixed_abandonment_batch = batch.mixed_abandonment_batch
    from elspeth.web.composer.redaction import MANIFEST

    prepared = False
    before = len(tool_outcomes)
    try:
        for tool_call in (call.tool_call,):
            if ctx.cancellation_requested.is_set():
                # The enclosing compose-loop critical section has observed a
                # cancellation. If no tool started, abort without publishing a
                # fabricated empty assistant turn. Otherwise return the completed
                # prefix so P4 can atomically publish its results before the
                # original cancellation resumes. Never start another tool after
                # the client has gone away.
                if not tool_outcomes:
                    raise _ToolBatchCancellationRequested
                break
            tool_name = tool_call.function.name
            promises_by_call[tool_call.id] = frozenset()
            pre_version = state.version
            private_arguments_canonical = private_authority_canonical = None
            private_raw_arguments = tool_call.function.arguments
            private_authorings_by_call[tool_call.id] = PrivateToolAuthoring(tool_call.id, tool_name, None, None, private_raw_arguments)
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
            unknown_audit_arguments: dict[str, Any] | None = None
            if tool_name not in MANIFEST:
                if is_discovery_tool(tool_name) or is_mutation_tool(tool_name) or is_session_aware_tool(tool_name):
                    raise AuditIntegrityError(f"Registered composer tool {tool_name!r} is missing from the redaction manifest")
                unknown_audit_arguments = cast(
                    dict[str, Any],
                    unknown_tool_arguments_redaction(telemetry=ctx.redaction_telemetry),
                )
                decoded_args_by_call_id[tool_call.id] = unknown_audit_arguments
                _replace_llm_tool_call_arguments(
                    llm_messages,
                    tool_call_id=tool_call.id,
                    arguments=unknown_audit_arguments,
                    dialect=ctx.tool_contract_dialect,
                    semantic=False,
                )
            # The loop always sends every tool of the dialect's W, so the sent set
            # is exactly its keys. A name outside it (hallucinated or unknown) is
            # never decoded (D17): its arguments flow on as today, with no facts.
            sent_wire_tools = _WIRE_TOOL_DEFS[ctx.tool_contract_dialect]
            tool_was_sent = tool_name in sent_wire_tools
            call_wire_facts = _CallWireFacts()
            call.wire = call_wire_facts
            if tool_was_sent and ctx.tool_contract_dialect is ToolContractDialect.OPENAI_STRICT:
                call_wire_facts.strict_sent = sent_wire_tools[tool_name].strict_capable

            def _append_tool_outcome(
                *,
                response: Any,
                error_class: str | None,
                error_category: ToolArgumentErrorCategory | None,
                error_message: str | None,
                post_version: int,
                _tool_outcomes: list[_ToolOutcome] = tool_outcomes,
                _tool_call: Any = tool_call,
                _pre_version: int = pre_version,
                _wire: _CallWireFacts = call_wire_facts,
                _state: CompositionState = state,
            ) -> None:
                if error_class is not None or (isinstance(response, ToolResult) and not response.success):
                    refused_resources.update(unavailable_resources(promises_by_call[_tool_call.id], _state))
                _tool_outcomes.append(
                    _ToolOutcome(
                        call=_tool_call,
                        response=response,
                        error_class=error_class,
                        error_category=error_category,
                        error_message=error_message,
                        pre_version=_pre_version,
                        post_version=post_version,
                        strict_sent=_wire.strict_sent,
                        wire_conformant=_wire.wire_conformant,
                    )
                )

            if mixed_abandonment_batch:
                detail = "cannot_complete must be the only tool call in its batch; every call in this batch was refused before effects."
                category = ToolArgumentErrorCategory.MODEL_VALIDATION
                rejection = ToolArgumentError(
                    argument="tool_calls",
                    expected="cannot_complete as the only call in its batch",
                    actual_type="mixed tool-call batch",
                    category=category,
                )
                payload = {"status": "ARG_ERROR", "error": detail, "error_category": category.value}
                audit = begin_dispatch(
                    tool_call.id,
                    tool_name,
                    tool_call.function.arguments,
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=None,
                )
                recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
                recorder.record(
                    finish_arg_error(
                        audit, error_class=type(rejection).__name__, error_category=category, error_message=detail, error_payload=payload
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(rejection).__name__,
                    error_category=category,
                    error_message=detail,
                    post_version=state.version,
                )
                tool_feedback_json = json.dumps(payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                turn_has_discovery = True
                all_cache_hits = False
                continue

            try:
                decoded_arguments = bounded_json_loads(tool_call.function.arguments, label=f"{tool_name} arguments")
            except (json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError) as exc:
                # Track budget class even when args are unparseable.
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (1/3): JSON-decode failure.
                # Open the audit envelope with the raw (pre-parse) string
                # so the trail records what the LLM tried, even when it
                # wasn't valid JSON. ``error_message`` is class-name only
                # because ``str(exc)`` for JSONDecodeError can echo column
                # offsets that reference the un-truncated raw bytes.
                audit_arguments: Mapping[str, Any] | str = (
                    unknown_audit_arguments if unknown_audit_arguments is not None else tool_call.function.arguments
                )
                if unknown_audit_arguments is None:
                    replay_arguments = {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": type(exc).__name__,
                    }
                    # Keep the original malformed wire bytes in the audit record
                    # for ordinary JSON decode failures, but send a valid JSON
                    # placeholder in the next provider request. The gateway's
                    # tool-call contract cannot replay malformed JSON arguments.
                    _replace_llm_tool_call_arguments(
                        llm_messages,
                        tool_call_id=tool_call.id,
                        arguments=replay_arguments,
                        dialect=ctx.tool_contract_dialect,
                        semantic=False,
                    )
                    if isinstance(exc, JsonBoundaryError):
                        audit_arguments = replay_arguments
                        decoded_args_by_call_id[tool_call.id] = dict(replay_arguments)
                # No arguments were decoded, so wire_conformant stays None.
                audit = begin_dispatch(
                    tool_call.id,
                    tool_name,
                    audit_arguments,
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=call_wire_facts.wire_conformant,
                )
                error_payload = {"error": f"Invalid JSON in arguments: {exc}"}
                # JsonBoundaryError is ELSPETH's own final-in-practice class (no
                # subclasses); the other three caught classes are decode failures.
                wire_category = (
                    ToolArgumentErrorCategory.WIRE_JSON_BOUNDS
                    if type(exc) is JsonBoundaryError
                    else ToolArgumentErrorCategory.WIRE_JSON_INVALID
                )
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(exc).__name__,
                        error_category=wire_category,
                        error_message=type(exc).__name__,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(exc).__name__,
                    error_category=wire_category,
                    error_message=type(exc).__name__,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            # Copy receipt semantics before envelope rejection, redaction or
            # finalizer rebinding can replace them with a display projection.
            try:
                private_arguments_canonical = canonical_json(decoded_arguments)
            except (TypeError, ValueError) as exc:
                private_arguments_canonical = canonical_json(build_canonicalization_sentinel(exc, decoded_arguments))
            private_authorings_by_call[tool_call.id] = replace(
                private_authorings_by_call[tool_call.id], arguments_canonical=private_arguments_canonical
            )
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])

            # Exact-type: bounded_json_loads is called with no object_hook /
            # object_pairs_hook, so json.loads builds a plain dict, and its own
            # _validate_decoded_json has already held every node to exactly
            # dict/list/str/bool/int/float/None. A Mapping that is not a dict
            # cannot reach this line.
            if type(decoded_arguments) is not dict:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (2/3): non-dict arguments.
                # The LLM produced valid JSON but not a JSON object. The
                # canonicalized record wraps the (possibly scalar/list)
                # value under ``_decoded_non_object`` so the audit trail
                # captures it deterministically.
                if tool_was_sent:
                    call_wire_facts.wire_conformant = False
                audit, canonicalization_failed = begin_dispatch_or_arg_error(
                    tool_call.id,
                    tool_name,
                    unknown_audit_arguments if unknown_audit_arguments is not None else {"_decoded_non_object": decoded_arguments},
                    version_before=state.version,
                    actor=actor,
                    strict_sent=call_wire_facts.strict_sent,
                    wire_conformant=call_wire_facts.wire_conformant,
                )
                error_category: ToolArgumentErrorCategory
                if canonicalization_failed is None:
                    err_msg = f"Tool '{tool_name}' arguments must be a JSON object, got {type(decoded_arguments).__name__}."
                    not_object = _pre_dispatch_argument_error(tool_name, ToolArgumentErrorCategory.WIRE_NOT_OBJECT)
                    error_class = type(not_object).__name__
                    error_category = not_object.category
                    error_message = f"non-object arguments ({type(decoded_arguments).__name__})"
                else:
                    err_msg = f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."
                    error_class = type(canonicalization_failed).__name__
                    error_category = ToolArgumentErrorCategory.CANONICALIZATION
                    error_message = type(canonicalization_failed).__name__
                error_payload = {"error": err_msg}
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=error_class,
                        error_category=error_category,
                        error_message=error_message,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=error_class,
                    error_category=error_category,
                    error_message=error_message,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            if tool_was_sent:
                # Decode the provider arguments against the W that was sent:
                # classify wire conformance, unwrap the set_pipeline envelope, and
                # on openai_strict strip ``null`` at promoted positions. A
                # malformed envelope or no-argument marker rejects before S.
                try:
                    decoded = decode_wire_arguments(tool_name, ctx.tool_contract_dialect, decoded_arguments)
                except ToolArgumentError as envelope_rejection:
                    call_wire_facts.wire_conformant = False
                    if is_discovery_tool(tool_name):
                        turn_has_discovery = True
                    else:
                        turn_has_mutation = True
                    audit_arguments = {
                        "_redaction_status": INVALID_TOOL_ARGUMENTS_REDACTION_STATUS,
                        "error_class": type(envelope_rejection).__name__,
                    }
                    decoded_args_by_call_id[tool_call.id] = dict(audit_arguments)
                    _replace_llm_tool_call_arguments(
                        llm_messages,
                        tool_call_id=tool_call.id,
                        arguments=audit_arguments,
                        dialect=ctx.tool_contract_dialect,
                        semantic=False,
                    )
                    audit = begin_dispatch(
                        tool_call.id,
                        tool_name,
                        audit_arguments,
                        version_before=state.version,
                        actor=actor,
                        strict_sent=call_wire_facts.strict_sent,
                        wire_conformant=call_wire_facts.wire_conformant,
                    )
                    envelope_error = wire_argument_repair_message(sent_wire_tools[tool_name])
                    error_payload = {"error": envelope_error}
                    recorder.record(
                        finish_arg_error(
                            audit,
                            error_class=type(envelope_rejection).__name__,
                            error_category=envelope_rejection.category,
                            error_message=envelope_error,
                            error_payload=error_payload,
                        )
                    )
                    _append_tool_outcome(
                        response=None,
                        error_class=type(envelope_rejection).__name__,
                        error_category=envelope_rejection.category,
                        error_message=envelope_error,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(error_payload)
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    all_cache_hits = False
                    continue
                # From here on ``arguments`` is the decoded (semantic) form: S is
                # the contract every later gate, handler and validator reads.
                arguments = cast(dict[str, Any], deep_thaw(decoded.semantic))
                call_wire_facts.wire_conformant = decoded.wire_conformant
            else:
                arguments = cast(dict[str, Any], decoded_arguments)
            _retain_llm_tool_argument_parts(llm_messages, tool_call_id=tool_call.id)
            try:
                private_arguments_canonical = canonical_json(arguments)
                if tool_name == "set_pipeline":
                    private_authority_canonical = composer_authority_canonical_json(arguments)
            except (TypeError, ValueError) as exc:
                private_arguments_canonical = canonical_json(build_canonicalization_sentinel(exc, arguments))
                private_authority_canonical = None
            private_authorings_by_call[tool_call.id] = replace(
                private_authorings_by_call[tool_call.id],
                arguments_canonical=private_arguments_canonical,
                authority_arguments_canonical=private_authority_canonical,
            )
            recorder.record_private_tool_authoring(private_authorings_by_call[tool_call.id])
            if unknown_audit_arguments is not None:
                audit_arguments = unknown_audit_arguments
            elif tool_name == "set_pipeline":
                audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
            else:
                audit_arguments = arguments
            decoded_args_by_call_id[tool_call.id] = audit_arguments
            promises_by_call[tool_call.id] = promised_resources(tool_name, arguments)

            # Open the audit envelope ONCE per dispatch — the cache,
            # required-paths, ToolArgumentError, plugin-crash, and
            # success branches below all read from this envelope so
            # ``started_at``/``arguments_canonical``/``version_before``
            # are consistent regardless of which branch fires.
            audit, canonicalization_failed = begin_dispatch_or_arg_error(
                tool_call.id,
                tool_name,
                audit_arguments,
                version_before=state.version,
                actor=actor,
                strict_sent=call_wire_facts.strict_sent,
                wire_conformant=call_wire_facts.wire_conformant,
            )
            if audit_arguments is not arguments:
                _replace_llm_tool_call_arguments(
                    llm_messages,
                    tool_call_id=tool_call.id,
                    arguments=audit_arguments,
                    dialect=ctx.tool_contract_dialect,
                    semantic=True,
                )
            if canonicalization_failed is not None:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                error_payload = {
                    "error": f"Tool '{tool_name}' arguments are not canonical JSON ({type(canonicalization_failed).__name__})."
                }
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(canonicalization_failed).__name__,
                        error_category=ToolArgumentErrorCategory.CANONICALIZATION,
                        error_message=type(canonicalization_failed).__name__,
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(canonicalization_failed).__name__,
                    error_category=ToolArgumentErrorCategory.CANONICALIZATION,
                    error_message=type(canonicalization_failed).__name__,
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                all_cache_hits = False
                continue

            call.tool_name = tool_name
            call.private_raw_arguments = private_raw_arguments
            call.private_arguments_canonical = private_arguments_canonical
            call.private_authority_canonical = private_authority_canonical
            call.unknown_audit_arguments = unknown_audit_arguments
            call.arguments = arguments
            call.audit_arguments = audit_arguments
            call.audit = audit
            call.phase = "prepared"
            if ctx.session_operation_context is not None:
                call.original_creator_id = ctx.session_operation_context.fence.operation_id
                call.original_creator_epoch = ctx.session_operation_context.fence.operation_epoch
            prepared = True
    finally:
        batch.state = state
        batch.last_validation = last_validation
        batch.last_runtime_preflight = last_runtime_preflight
        batch.advisor_calls_used = advisor_calls_used
        batch.turn_has_mutation = turn_has_mutation
        batch.turn_has_discovery = turn_has_discovery
        batch.all_cache_hits = all_cache_hits
        batch.tool_outcomes = tool_outcomes
        batch.plugin_crash = plugin_crash
        batch.plugin_crash_cause = plugin_crash_cause
        batch.advisor_failure = advisor_failure
        batch.advisor_compose_timeout = advisor_compose_timeout
        batch.pre_state_id = pre_state_id
        batch.decoded_args_by_call_id = decoded_args_by_call_id
        batch.private_authorings_by_call = private_authorings_by_call
        batch.proposals_this_turn = proposals_this_turn
        batch.mutation_success_observed = mutation_success_observed
        batch.refused_resources = refused_resources
        batch.promises_by_call = promises_by_call
    if prepared:
        return "prepared"
    if len(tool_outcomes) == before + 1:
        call.phase = "applied"
        return "applied"
    if len(tool_outcomes) != before:
        raise AuditIntegrityError("tool preparation produced more than one actual outcome")
    batch.stop_issued_batch = True
    return "stop"


async def _execute_continuation_call_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState,
    call: ContinuationCallState,
) -> Literal["applied"]:
    """Execute the original suffix with the actual caller's revocation owner."""
    recorder = ctx.recorder
    anti_anchor = ctx.anti_anchor
    discovery_cache = ctx.discovery_cache
    runtime_preflight_cache = ctx.runtime_preflight_cache
    session_id = ctx.session_id
    user_id = ctx.user_id
    user_message_id = ctx.user_message_id
    user_message_content = ctx.user_message_content
    current_state_id = ctx.current_state_id
    initial_version = ctx.initial_version
    deadline = ctx.deadline
    progress = ctx.progress
    session_scope = ctx.session_scope
    turn_sessions_service = ctx.turn_sessions_service
    turn_session_uuid = ctx.turn_session_uuid
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    advisor_calls_used = batch.advisor_calls_used
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    tool_outcomes = batch.tool_outcomes
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    advisor_compose_timeout = batch.advisor_compose_timeout
    pre_state_id = batch.pre_state_id
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    proposals_this_turn = batch.proposals_this_turn
    mutation_success_observed = batch.mutation_success_observed
    refused_resources = batch.refused_resources
    promises_by_call = batch.promises_by_call
    provider_model_version = batch.provider_model_version
    from elspeth.web.composer.redaction import redact_tool_call_arguments

    if call.audit is None or call.arguments is None or call.audit_arguments is None:
        raise AuditIntegrityError("tool execution preceded its retained prepared dispatch")
    tool_name = call.tool_name
    arguments = call.arguments
    audit_arguments = call.audit_arguments
    audit = call.audit
    before = len(tool_outcomes)
    ran_to_end = False

    def _append_tool_outcome(
        *,
        response: Any,
        error_class: str | None,
        error_category: ToolArgumentErrorCategory | None,
        error_message: str | None,
        post_version: int,
        _batch: ContinuationBatchState = batch,
        _call: ContinuationCallState = call,
    ) -> None:
        _append_continuation_tool_outcome(
            _batch,
            _call,
            response=response,
            error_class=error_class,
            error_category=error_category,
            error_message=error_message,
            post_version=post_version,
        )

    try:
        for tool_call in (call.tool_call,):
            # A refused producer cannot supply a new prerequisite to another call
            # authored in this same response. Existing saved resources remain usable.
            if is_mutation_tool(tool_name) and has_unavailable_prerequisite(tool_name, arguments, refused_resources, state):
                reject = _candidate_rejection_result if tool_name == "set_pipeline" else _saved_graph_rejection_result
                blocked = reject(
                    state,
                    "This mutation depends on a new resource or connection from an earlier refused call in this batch. "
                    "Nothing from this call was applied. Inspect the saved pipeline and repair the refused prerequisite "
                    "in a new turn before retrying this dependent change.",
                    error_code="batch_dependency_refused",
                    rejected_component=None,
                )
                blocked = finalize_tool_result(
                    blocked,
                    tool_name=tool_name,
                    catalog=ctx.policy_catalog,
                    context=ToolContext(catalog=ctx.policy_catalog, plugin_snapshot=ctx.plugin_snapshot),
                    prior_validation=last_validation or ctx.policy_catalog.validate_composition_state(state).validation,
                )
                recorder.record(finish_success(audit, result_payload=blocked.to_dict(), version_after=state.version))
                _append_tool_outcome(
                    response=blocked, error_class=None, error_category=None, error_message=None, post_version=state.version
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = _serialize_tool_result(blocked)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                await emit_progress(progress, tool_completed_progress_event(tool_name, False))
                turn_has_mutation = True
                all_cache_hits = False
                continue

            # Check discovery cache before executing
            if is_cacheable_discovery_tool(tool_name):
                cache_key = _make_cache_key(tool_name, arguments)
                if cache_key in discovery_cache:
                    # Cache hit — return cached result, no budget charge.
                    # Audit-recorded with cache_hit=True so the trail
                    # captures every LLM decision-point, even those
                    # served from cache without re-running the handler.
                    await _await_started_tool_stage(
                        emit_progress(
                            progress,
                            ComposerProgressEvent(
                                phase="using_tools",
                                headline="I'm reusing recently checked tool information.",
                                evidence=("The same discovery request was already answered for this compose step.",),
                                likely_next="ELSPETH will continue from the cached tool result.",
                            ),
                        ),
                        audit=audit,
                        recorder=recorder,
                    )
                    try:
                        cached_admitted = (
                            admitted_result_from_cached_discovery_payload(tool_name, state, discovery_cache[cache_key])
                            if response_contract_for(tool_name) is not None
                            else None
                        )
                        cached_result = (
                            cached_admitted.result
                            if cached_admitted is not None
                            else _result_from_cached_discovery_payload(state, discovery_cache[cache_key])
                        )
                        cached_result = normalize_tool_result_validation(cached_result, ctx.policy_catalog)
                        if cached_admitted is not None:
                            cached_admitted = AdmittedDiscoveryResult(cached_result, cached_admitted.response, cached_admitted.contract)
                        cached_payload = {
                            "success": cached_result.success,
                            "data": cached_admitted.response.to_wire()
                            if cached_admitted is not None and cached_admitted.response is not None
                            else cached_result.data,
                            "cache_hit": True,
                        }
                        cached_outcome = cached_admitted.to_tool_result() if cached_admitted is not None else cached_result
                        cached_json = (
                            serialize_admitted_discovery_result(cached_admitted)
                            if cached_admitted is not None
                            else _serialize_tool_result(cached_result)
                        )
                    except Exception as cache_exc:
                        recorder.record(finish_plugin_crash(audit, exc=cache_exc))
                        raise
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=cached_payload,
                            version_after=state.version,
                            cache_hit=True,
                        )
                    )
                    _append_tool_outcome(
                        response=cached_outcome,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    # Cache hits are exclusively for discovery tools
                    # (`is_cacheable_discovery_tool` gates above). A
                    # discovery success is an *observation* — the model
                    # gained schema knowledge but did not change state.
                    # The §7.7 anchor is broken only by mutation
                    # progress, so tracker stays untouched here.
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": cached_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(
                                cached_admitted if cached_admitted is not None else cached_result, cached_json
                            ),
                        }
                    )
                    continue

            all_cache_hits = False

            # Validate schema-declared required arguments at the
            # Tier 3 boundary BEFORE entering tool handler code.
            # This walks nested object/array schemas, so malformed
            # set_pipeline payloads like source.plugin omissions are
            # caught here; any KeyError that still escapes
            # execute_tool() is an internal bug and must crash.
            # Unknown tool names skip validation — execute_tool()
            # handles them with a failure result downstream.
            required_paths = _TOOL_REQUIRED_PATHS[tool_name] if tool_name in _TOOL_REQUIRED_PATHS else ()
            missing = _find_missing_required_paths(arguments, required_paths)
            if missing:
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # ARG_ERROR pre-dispatch site (3/3): schema-required
                # paths missing. ``missing`` is a list of dotted/indexed
                # path strings — operator-controlled schema field names,
                # safe to echo verbatim.
                err_msg = f"Tool '{tool_name}' missing required argument(s): {', '.join(missing)}"
                error_payload = {"error": err_msg}
                missing_paths = _pre_dispatch_argument_error(tool_name, ToolArgumentErrorCategory.MISSING_REQUIRED_PATH)
                recorder.record(
                    finish_arg_error(
                        audit,
                        error_class=type(missing_paths).__name__,
                        error_category=missing_paths.category,
                        error_message=f"missing: {', '.join(missing)}",
                        error_payload=error_payload,
                    )
                )
                _append_tool_outcome(
                    response=None,
                    error_class=type(missing_paths).__name__,
                    error_category=missing_paths.category,
                    error_message=f"missing: {', '.join(missing)}",
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
                tool_feedback_json = json.dumps(error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                continue

            prevalidated_unapplied_result: ToolResult | None = None
            preproposal_exception: BaseException | None = None
            candidate_prior_validation: ValidationSummary | None = None
            candidate_context: ToolContext | None = None
            candidate: Any = None
            pipeline_custody_result: Literal["not_required", "ready"] = "not_required"
            interpretation_requirements_are_internal = False
            explicit_approval_required = (
                ctx.review_required
                and is_mutation_tool(tool_name)
                # create_blob stays immediate because it allocates the blob id a
                # later composition proposal may reference. Destructive blob-only
                # writes such as update_blob/delete_blob still require approval.
                and (not is_blob_store_only_mutation_tool(tool_name) or is_approval_required_blob_store_only_mutation_tool(tool_name))
            )
            auto_mutation = (
                ctx.mutation_ceiling
                if (
                    not explicit_approval_required
                    and is_mutation_tool(tool_name)
                    and (not is_blob_store_only_mutation_tool(tool_name) or is_approval_required_blob_store_only_mutation_tool(tool_name))
                )
                else None
            )

            # The freeform compose loop is a second authoring surface beside the
            # shared planner. Its atomic set_pipeline call is the equivalent
            # terminal candidate seam: validate the provider draft first, then
            # splice deployment-REQUIRED controls exactly once before proposal
            # custody or auto-commit. Incomplete/invalid drafts remain byte-exact
            # and continue through the ordinary repair path; in particular, the
            # server never launders a provider-authored reserved disclosure into
            # an internal one merely by attempting finalization.
            if tool_name == "set_pipeline" and not explicit_approval_required:
                try:
                    candidate_prior_validation = (
                        last_validation if last_validation is not None else ctx.policy_catalog.validate_composition_state(state).validation
                    )
                    candidate_context = ToolContext(
                        catalog=ctx.policy_catalog,
                        plugin_snapshot=ctx.plugin_snapshot,
                        data_dir=ctx.custody.data_dir,
                        require_data_dir_for_paths=True,
                        session_engine=ctx.custody.session_engine,
                        session_id=session_id,
                        session_operation_context=ctx.session_operation_context,
                        session_operation_authority=ctx.session_operation_authority,
                        secret_service=ctx.custody.secret_service,
                        secret_wiring_policy=ctx.custody.secret_wiring_policy,
                        user_id=user_id,
                        current_validation=candidate_prior_validation,
                        max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                        user_message_id=user_message_id,
                        user_message_content=user_message_content,
                        composer_model_identifier=ctx.provenance.model_identifier,
                        composer_model_version=provider_model_version,
                        composer_provider=ctx.provenance.provider or "unknown",
                        composer_skill_hash=ctx.provenance.skill_hash,
                        tool_arguments_hash=audit.binding_arguments_hash,
                        mutation_ceiling=auto_mutation,
                    )
                    finalization = await _finalize_complete_set_pipeline_candidate(
                        arguments,
                        state,
                        candidate_context,
                        plugin_snapshot=ctx.plugin_snapshot,
                        policy_catalog=ctx.policy_catalog,
                    )
                    arguments = cast(dict[str, Any], finalization.arguments)
                    candidate_context = finalization.context
                    candidate = finalization.candidate
                    if finalization.changed:
                        audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                        audit = rebind_dispatch_arguments(audit, audit_arguments)
                        decoded_args_by_call_id[tool_call.id] = audit_arguments
                        transcript_arguments = (
                            redact_tool_call_arguments(tool_name, arguments, telemetry=ctx.redaction_telemetry)
                            if candidate.prepared_inline_blob is not None
                            else audit_arguments
                        )
                        _replace_llm_tool_call_arguments(
                            llm_messages,
                            tool_call_id=tool_call.id,
                            arguments=transcript_arguments,
                            dialect=ctx.tool_contract_dialect,
                            semantic=True,
                        )
                        interpretation_requirements_are_internal = True
                except BaseException as exc:
                    # Re-raise exactly once from inside dispatch_with_audit so the
                    # established ARG_ERROR / PLUGIN_CRASH authority records the
                    # failure without any proposal, blob, or state publication.
                    preproposal_exception = exc

            if explicit_approval_required:
                assert turn_sessions_service is not None
                assert turn_session_uuid is not None
                if proposals_this_turn >= _MAX_PENDING_PROPOSALS_PER_TURN:
                    # Shadowed by the batch preflight above (same population, same
                    # cap); kept so a later change to either count cannot open an
                    # unbounded proposal turn.
                    raise _pending_proposal_cap_error(proposals_this_turn + 1, ctx=ctx, state=state)

                from pydantic import ValidationError as PydanticValidationError

                # The LLM may produce arguments that fail the redaction
                # MANIFEST's argument_model — most commonly a misplaced
                # field like ``nodes[*].schema`` belonging at
                # ``nodes[*].options.schema``. The runtime validator
                # would reject these the same way. On redaction
                # ValidationError we fall through to normal dispatch:
                # execute_tool emits a clean ToolArgumentError, the
                # existing post-dispatch arg-error handling records
                # the failure for audit (with the
                # ``_redaction_status: invalid_tool_arguments`` marker),
                # and the compose loop continues so the model can
                # self-correct. The previous behaviour — letting the
                # ValidationError propagate — crashed the compose
                # request with HTTP 500 (session 100dc5cb… 2026-05-14:
                # frontend rendered a generic ApiError as a bare
                # "retry" button with no diagnostic).
                redacted_arguments: Mapping[str, Any] | None
                try:
                    redacted_arguments = redact_tool_call_arguments(
                        tool_name,
                        arguments,
                        telemetry=ctx.redaction_telemetry,
                    )
                except PydanticValidationError:
                    redacted_arguments = None

                proposal_tool_name = tool_name
                proposal_arguments: Mapping[str, Any] = arguments
                proposal_summary_arguments: Mapping[str, Any] = arguments
                proposal_redacted_arguments = redacted_arguments

                if redacted_arguments is not None and tool_name == "set_pipeline":
                    if preproposal_exception is not None:
                        redacted_arguments = None
                    else:
                        try:
                            candidate_prior_validation = (
                                last_validation
                                if last_validation is not None
                                else ctx.policy_catalog.validate_composition_state(state).validation
                            )
                            candidate_context = ToolContext(
                                catalog=ctx.policy_catalog,
                                plugin_snapshot=ctx.plugin_snapshot,
                                data_dir=ctx.custody.data_dir,
                                require_data_dir_for_paths=True,
                                session_engine=ctx.custody.session_engine,
                                session_id=session_id,
                                session_operation_context=ctx.session_operation_context,
                                session_operation_authority=ctx.session_operation_authority,
                                secret_service=ctx.custody.secret_service,
                                secret_wiring_policy=ctx.custody.secret_wiring_policy,
                                user_id=user_id,
                                current_validation=candidate_prior_validation,
                                max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                                user_message_id=user_message_id,
                                user_message_content=user_message_content,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                                composer_skill_hash=ctx.provenance.skill_hash,
                                tool_arguments_hash=audit.binding_arguments_hash,
                            )
                            finalization = await _finalize_complete_set_pipeline_candidate(
                                arguments,
                                state,
                                candidate_context,
                                plugin_snapshot=ctx.plugin_snapshot,
                                policy_catalog=ctx.policy_catalog,
                            )
                            arguments = cast(dict[str, Any], finalization.arguments)
                            candidate_context = finalization.context
                            candidate = finalization.candidate
                            if finalization.changed:
                                audit_arguments = cast(dict[str, Any], inline_custody_audit_projection(arguments))
                                audit = rebind_dispatch_arguments(audit, audit_arguments)
                                decoded_args_by_call_id[tool_call.id] = audit_arguments
                                redacted_arguments = redact_tool_call_arguments(
                                    tool_name,
                                    arguments,
                                    telemetry=ctx.redaction_telemetry,
                                )
                                _replace_llm_tool_call_arguments(
                                    llm_messages,
                                    tool_call_id=tool_call.id,
                                    arguments=redacted_arguments if candidate.prepared_inline_blob is not None else audit_arguments,
                                    dialect=ctx.tool_contract_dialect,
                                    semantic=True,
                                )
                                interpretation_requirements_are_internal = True
                            finalized_candidate_result = finalize_tool_result(
                                candidate.result,
                                tool_name=tool_name,
                                catalog=ctx.policy_catalog,
                                context=candidate_context,
                                prior_validation=candidate_prior_validation,
                            )
                            proposal_acceptable = candidate.acceptable
                            if proposal_acceptable and candidate.prepared_inline_blob is not None:
                                if session_id is None or ctx.custody.session_engine is None:
                                    raise AuditIntegrityError("Inline proposal custody requires session context")
                                custody = prepare_pipeline_custody(
                                    arguments,
                                    candidate.prepared_inline_blob,
                                    session_id=session_id,
                                    max_storage_per_session=ctx.custody.max_blob_storage_per_session_bytes,
                                )

                                # From this point forward every authority-bearing
                                # and externally visible copy uses source.blob_id.
                                arguments = cast(dict[str, Any], deep_thaw(custody.arguments))
                                audit = rebind_dispatch_arguments(audit, arguments)
                                decoded_args_by_call_id[tool_call.id] = arguments
                                # Authority retains the exact review draft. Only its
                                # redacted projection belongs in provider history.
                                redacted_arguments = redact_tool_call_arguments(
                                    tool_name,
                                    arguments,
                                    telemetry=ctx.redaction_telemetry,
                                )
                                _replace_llm_tool_call_arguments(
                                    llm_messages,
                                    tool_call_id=tool_call.id,
                                    arguments=redacted_arguments,
                                    dialect=ctx.tool_contract_dialect,
                                    semantic=True,
                                )
                                safe_candidate_context = replace(
                                    candidate_context,
                                    tool_arguments_hash=audit.binding_arguments_hash,
                                )
                                custody_outcome = await _try_finalize_proposal_custody(
                                    custody,
                                    engine=ctx.custody.session_engine,
                                    data_dir=ctx.custody.data_dir,
                                    max_storage_per_session=ctx.custody.max_blob_storage_per_session_bytes,
                                    session_operation_context=ctx.session_operation_context,
                                    session_operation_authority=ctx.session_operation_authority,
                                )
                                if custody_outcome == "quota_exceeded":
                                    proposal_acceptable = False
                                    finalized_candidate_result = finalize_tool_result(
                                        _candidate_rejection_result(
                                            state,
                                            "Session blob quota exceeded while reserving inline proposal custody.",
                                            error_code="BLOB_QUOTA_EXCEEDED",
                                            rejected_component="source",
                                        ),
                                        tool_name=tool_name,
                                        catalog=ctx.policy_catalog,
                                        context=safe_candidate_context,
                                        prior_validation=candidate_prior_validation,
                                    )
                                else:
                                    pipeline_custody_result = "ready"
                                    candidate = await run_sync_in_worker(
                                        build_set_pipeline_candidate,
                                        arguments,
                                        state,
                                        safe_candidate_context,
                                    )
                                    finalized_candidate_result = finalize_tool_result(
                                        candidate.result,
                                        tool_name=tool_name,
                                        catalog=ctx.policy_catalog,
                                        context=safe_candidate_context,
                                        prior_validation=candidate_prior_validation,
                                    )
                                    proposal_acceptable = candidate.acceptable

                        except BaseException as exc:
                            # Candidate finalization is one-time pre-proposal work.
                            # Re-raise it inside dispatch_with_audit below without
                            # rerunning the operation.
                            preproposal_exception = exc
                            redacted_arguments = None
                        else:
                            if not proposal_acceptable:
                                # ToolResult.data is deep-frozen by __post_init__, so
                                # the membership test must accept a mappingproxy. See
                                # _prevalidation_feedback_seed for the full contract.
                                feedback_data = dict(_prevalidation_feedback_seed(finalized_candidate_result.data))
                                feedback_data.update(
                                    _PrevalidationRejectedStatus(
                                        status="PREVALIDATION_REJECTED",
                                        applied=False,
                                        candidate_version=finalized_candidate_result.updated_state.version,
                                        message=(
                                            "The candidate pipeline failed prevalidation, was not applied, and was not "
                                            "submitted for approval. Repair the reported validation errors and retry."
                                        ),
                                    )
                                )
                                # ``updated_state=state``: nothing was applied, so the
                                # envelope's ``version`` — which the skill teaches as
                                # "the state version after the call" — must be the
                                # unapplied one. Keeping the candidate's state made the
                                # wire say the mutation landed while the audit
                                # (``_version_after``), the loop (which refuses the
                                # candidate state) and ``_append_tool_outcome`` all
                                # recorded that it had not (systems seat SYS-R3-3).
                                # Contained to this result: it reaches only
                                # ``_do_dispatch`` -> ``dispatch_with_audit`` ->
                                # ``_serialize_tool_result`` / ``_append_tool_outcome``,
                                # and ``pipeline_commit`` builds its own candidate from
                                # the proposal rather than reading this one.
                                #
                                # ``prior_validation=None`` and ``affected_nodes=()``
                                # for the same reason: both describe CHANGES, and
                                # nothing changed. ``to_dict`` emits
                                # ``validation_delta`` only when ``prior_validation``
                                # is set, and the delta it emitted was measurably
                                # false — on a real compose loop it reported
                                # ``resolved_errors`` naming the two errors the
                                # unapplied state still had, next to a ``version``
                                # saying nothing was applied, while the skill tells
                                # the model to act on the delta and never re-read
                                # state to check it (LLM seat LLM-R3B-1). The
                                # candidate's own ``affected_nodes`` said the same
                                # thing in the other direction: components touched by
                                # a state that was discarded. ``validation`` stays the
                                # candidate's — it is the rejection the model repairs
                                # from, and the skill says whose it is. This is now
                                # the APPROVAL_REQUIRED envelope's shape on the same
                                # path: unapplied state, empty affected_nodes, no
                                # delta.
                                prevalidated_unapplied_result = replace(
                                    finalized_candidate_result,
                                    data=feedback_data,
                                    updated_state=state,
                                    prior_validation=None,
                                    affected_nodes=(),
                                )
                                # Route the rejected result through canonical
                                # dispatch/outcome without proposal publication.
                                redacted_arguments = None

                proposal_redacted_arguments = redacted_arguments
                if redacted_arguments is not None and tool_name in _MUTATION_TOOLS and tool_name != "set_pipeline":
                    try:
                        preview_result = await run_sync_in_worker(
                            execute_tool,
                            tool_name,
                            arguments,
                            state,
                            ctx.policy_catalog,
                            plugin_snapshot=ctx.plugin_snapshot,
                            data_dir=ctx.custody.data_dir,
                            session_engine=ctx.custody.session_engine,
                            session_id=session_id,
                            session_operation_context=ctx.session_operation_context,
                            session_operation_authority=ctx.session_operation_authority,
                            secret_service=ctx.custody.secret_service,
                            secret_wiring_policy=ctx.custody.secret_wiring_policy,
                            user_id=user_id,
                            prior_validation=last_validation,
                            runtime_preflight=None,
                            max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                            user_message_id=user_message_id,
                            user_message_content=user_message_content,
                            composer_model_identifier=ctx.provenance.model_identifier,
                            composer_model_version=provider_model_version,
                            composer_provider=ctx.provenance.provider or "unknown",
                            composer_skill_hash=ctx.provenance.skill_hash,
                            tool_arguments_hash=audit.binding_arguments_hash,
                            validate_arguments=True,
                            require_data_dir_for_paths=True,
                            raise_schema_argument_errors=True,
                        )
                        if (
                            preview_result.success
                            and preview_result.updated_state.version > state.version
                            and preview_result.validation.is_valid
                        ):
                            finalized_state = await run_sync_in_worker(
                                wire_required_controls_state,
                                preview_result.updated_state,
                                ctx.plugin_snapshot,
                                ctx.policy_catalog,
                            )
                            if finalized_state is not preview_result.updated_state:
                                finalized_validation = await run_sync_in_worker(
                                    ctx.policy_catalog.validate_composition_state,
                                    finalized_state,
                                )
                                if not finalized_validation.validation.is_valid:
                                    raise AuditIntegrityError("Required-control proposal finalization produced an invalid composition")
                                proposal_arguments = owned_composition_state_authority(finalized_validation.authored_state)
                                proposal_summary_arguments = owned_composition_state_review_arguments(proposal_arguments)
                                proposal_tool_name = "set_pipeline"
                                proposal_redacted_arguments = redact_tool_call_arguments(
                                    proposal_tool_name,
                                    cast(dict[str, Any], proposal_summary_arguments),
                                    telemetry=ctx.redaction_telemetry,
                                )
                    except ToolArgumentError:
                        # Preserve the established explicit-approval contract for
                        # ordinary incremental proposals: semantic tool-argument
                        # rejection occurs only if the user approves execution.
                        pass
                    except BaseException as exc:
                        preproposal_exception = exc
                        proposal_redacted_arguments = None

                if proposal_redacted_arguments is not None and proposal_tool_name == tool_name:
                    proposal_arguments = arguments
                    proposal_summary_arguments = arguments

                if proposal_redacted_arguments is not None:
                    proposal_summary = build_tool_proposal_summary(
                        tool_name=proposal_tool_name,
                        arguments=proposal_summary_arguments,
                        redacted_arguments=proposal_redacted_arguments,
                    )
                    if proposal_tool_name == "set_pipeline":
                        proposal_base = (
                            PresentBase(
                                state_id=UUID(current_state_id),
                                composition_content_hash=composition_content_hash(state),
                            )
                            if current_state_id is not None
                            else AbsentBase()
                        )
                        pipeline_proposal = PipelineProposal.create(
                            pipeline=proposal_arguments,
                            base=proposal_base,
                            repair_count=0,
                            skill_hash=ctx.provenance.skill_hash,
                        )
                        if type(ctx.session_operation_context) is not SessionOperationContext:
                            raise AuditIntegrityError("Composition proposal creation requires exact session operation authority")
                        proposal = await _await_started_tool_stage(
                            turn_sessions_service.create_pipeline_composition_proposal(
                                session_id=turn_session_uuid,
                                session_operation_context=ctx.session_operation_context,
                                plan=PipelinePlanResult(
                                    proposal=pipeline_proposal,
                                    tool_call_id=tool_call.id,
                                    custody_result=pipeline_custody_result,
                                    model_identifier=ctx.provenance.model_identifier,
                                    model_version=provider_model_version,
                                    provider=ctx.provenance.provider or "unknown",
                                ),
                                summary=proposal_summary.summary,
                                rationale=proposal_summary.rationale,
                                affects=proposal_summary.affects,
                                arguments_redacted_json=proposal_summary.arguments_redacted_json,
                                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                                user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                            ),
                            audit=audit,
                            recorder=recorder,
                        )
                    else:
                        if type(ctx.session_operation_context) is not SessionOperationContext:
                            raise AuditIntegrityError("Composition proposal creation requires exact session operation authority")
                        proposal = await _await_started_tool_stage(
                            turn_sessions_service.create_composition_proposal(
                                session_id=turn_session_uuid,
                                session_operation_context=ctx.session_operation_context,
                                tool_call_id=tool_call.id,
                                tool_name=proposal_tool_name,
                                summary=proposal_summary.summary,
                                rationale=proposal_summary.rationale,
                                affects=proposal_summary.affects,
                                arguments_json=proposal_arguments,
                                arguments_redacted_json=proposal_summary.arguments_redacted_json,
                                base_state_id=UUID(current_state_id) if current_state_id is not None else None,
                                actor=f"composer-web:user:{user_id}" if user_id is not None else "composer-web:anonymous",
                                user_message_id=UUID(user_message_id) if user_message_id is not None else None,
                                composer_model_identifier=ctx.provenance.model_identifier,
                                composer_model_version=provider_model_version,
                                composer_provider=ctx.provenance.provider or "unknown",
                                composer_skill_hash=ctx.provenance.skill_hash,
                                tool_arguments_hash=audit.binding_arguments_hash,
                            ),
                            audit=audit,
                            recorder=recorder,
                        )
                    proposals_this_turn += 1
                    proposal_result = ToolResult(
                        success=True,
                        updated_state=state,
                        validation=(
                            candidate_prior_validation
                            if candidate_prior_validation is not None
                            else (
                                last_validation
                                if last_validation is not None
                                else ctx.policy_catalog.validate_composition_state(state).validation
                            )
                        ),
                        affected_nodes=(),
                        # Built inline and never bound to a name, so nothing stands between
                        # construction and the freeze in ``ToolResult.__post_init__``: there is no
                        # local, alias, ``cast`` widening or callee that could re-shape it.
                        # No ``success`` inside the payload: the envelope's own ``success`` already
                        # says it, and ``status`` is the discriminator a reader keys on
                        # (elspeth-e405ad7cd2, F1).
                        data=_ProposalPayload(
                            status="APPROVAL_REQUIRED",
                            proposal_id=str(proposal.id),
                            tool_name=proposal_tool_name,
                            summary=proposal.summary,
                            message="The requested pipeline change is pending human approval and has not been applied.",
                        ),
                    )
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=proposal_result.to_dict(),
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=proposal_result,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    anti_anchor.record_success()
                    tool_feedback_json = _serialize_tool_result(proposal_result)
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_mutation = True
                    continue
                # ``None`` means invalid LLM arguments, an unacceptable
                # prevalidated set_pipeline candidate, or captured one-time
                # pre-proposal work. All fall through to the canonical
                # dispatch/outcome path below.

            await _await_started_tool_stage(emit_progress(progress, tool_started_progress_event(tool_name)), audit=audit, recorder=recorder)

            # Advisor escape-hatch interception. The request_advisor_hint
            # tool is intercepted here BEFORE execute_tool() because the
            # action is an async LiteLLM call to a frontier model rather
            # than a sync state mutation. The tool is not registered in
            # _DISCOVERY_TOOLS or _MUTATION_TOOLS, so execute_tool() would
            # return "Unknown tool" if this branch did not handle it.
            #
            # Audit envelope was already opened above by
            # begin_dispatch_or_arg_error; this branch closes it with the
            # truthful dispatch status: ARG_ERROR for local advisor-argument
            # rejection, SUCCESS for completed policy/provider outcomes
            # whose semantic status is encoded in result_payload. The inner
            # LLM call is recorded separately via _call_advisor_with_audit
            # firing a ComposerLLMCall record.
            if tool_name == "request_advisor_hint":
                if resolve_tool_effects(tool_name, arguments).domains:
                    raise AssertionError("Advisor dispatch must not author composition domains.")
                # Successful advisor guidance is governed solely by the
                # advisor budget so the composer can read it. Advisor
                # policy/error feedback with no usable guidance is still
                # a non-mutating correction turn, so it consumes discovery
                # budget before the loop asks the primary model again.
                # F3: validate argument types and total prompt size at the
                # Tier-3 trust boundary. _TOOL_REQUIRED_PATHS only checks
                # key presence, not value shape. Without this check the
                # LLM could send a non-list (silently iterated char-by-
                # char) or a megabyte-scale value (unbounded provider
                # cost). ARG_ERRORs do NOT consume advisor budget — no
                # outbound call is made — but anti-anchor counts them
                # so repeated identical bad-arg calls trigger the §7.7
                # structural hint.
                if type(ctx.advisor_checkpoint) is not AdvisorCheckpointOwner:
                    raise AuditIntegrityError("Manual Advisor admission requires the owned checkpoint owner")
                advisor_arg_error = AdvisorCheckpointOwner._validate_advisor_arguments(ctx.advisor_checkpoint, arguments)
                if not isinstance(advisor_arg_error, RequestAdvisorHintArgumentsModel):
                    advisor_rejection_payload = advisor_arg_error.to_payload()
                    recorder.record(
                        finish_arg_error(
                            audit,
                            error_class=advisor_arg_error.error_class,
                            error_category=advisor_arg_error.category,
                            error_message=advisor_arg_error.error,
                            error_payload=advisor_rejection_payload,
                        )
                    )
                    _append_tool_outcome(
                        response=None,
                        error_class=advisor_arg_error.error_class,
                        error_category=advisor_arg_error.category,
                        error_message=advisor_arg_error.error,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash, category=advisor_arg_error.category)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_rejection_payload))
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                budget = ctx.advisor_max_calls_per_compose
                if advisor_calls_used >= budget:
                    budget_payload = {
                        "status": "BUDGET_EXHAUSTED",
                        "budget_used": advisor_calls_used,
                        "budget_remaining": 0,
                        "guidance": (
                            f"Advisor budget exhausted ({advisor_calls_used}/{budget} calls "
                            "used this compose request). Return to the validator output and "
                            "the recovery cheat sheet — no more frontier hints are available "
                            "until the operator raises the budget or the next compose request."
                        ),
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=budget_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=budget_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    # Budget exhaustion is a structural signal back to
                    # the LLM, not a tool-failure pattern — do NOT count
                    # it for §7.7 anchor tracking, since the issue isn't
                    # the LLM repeating an identical request, it's the
                    # operator's policy refusing further hints.
                    tool_feedback_json = json.dumps(advisor_feedback_payload(budget_payload))
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                remaining = _remaining_compose_seconds(deadline)
                if remaining <= 0:
                    # Nothing was raised here: the pre-call deadline check refused
                    # the call, so the payload names no exception class. The
                    # ``COMPOSE_TIMEOUT`` status is the discriminant.
                    pre_call_timeout_payload: dict[str, Any] = {
                        "status": "COMPOSE_TIMEOUT",
                        "error": "Advisor call exceeded the remaining compose deadline.",
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=pre_call_timeout_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=pre_call_timeout_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    advisor_compose_timeout = "pre_call"
                    break

                elif remaining < _MIN_USEFUL_ADVISOR_SECONDS:
                    deadline_payload = {
                        "status": "DEADLINE_TOO_CLOSE",
                        "outbound_call_made": False,
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=deadline_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=deadline_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    tool_feedback_json = json.dumps(advisor_feedback_payload(deadline_payload))
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                advisor_timeout = ctx.advisor_timeout_seconds
                effective_advisor_timeout = min(advisor_timeout, remaining)
                advisor_deadline_limited = remaining <= advisor_timeout

                # F2: consume budget BEFORE the outbound call, not after.
                # The cost guard's purpose is to bound outbound LiteLLM
                # calls regardless of outcome. Counting only successes
                # would let a flaky provider rack up unlimited failed
                # outbound calls until anti-anchor or the discovery-
                # turn limit fired. ARG_ERROR (above) and pre-call compose
                # timeout (above) do NOT consume advisor budget because no
                # outbound advisor call is made.
                advisor_calls_used += 1

                abandonment_context = None
                try:
                    if ctx.turn_evidence is not None:
                        ctx.turn_evidence.transcript.observe_messages(llm_messages)
                    if advisor_arg_error.trigger == ADVISOR_TRIGGER_CANNOT_COMPLETE:
                        if ctx.turn_evidence is None or user_message_content is None:
                            raise AuditIntegrityError("Abandonment review requires current request-owned evidence")
                        abandonment_context = ctx.turn_evidence.abandonment_context(
                            request=user_message_content,
                            current_state=state,
                            plugin_snapshot=ctx.plugin_snapshot,
                            messages=llm_messages,
                        )
                    advisor_outcome = await _await_started_tool_stage(
                        ctx.advisor_checkpoint._call_advisor_for_tool(
                            advisor_arg_error,
                            recorder=recorder,
                            timeout=effective_advisor_timeout,
                            provider_owner=ctx.provider_owner,
                            abandonment_context=abandonment_context,
                            advisor_transcript=ctx.turn_evidence.transcript.snapshot() if ctx.turn_evidence is not None else None,
                            current_state=state,
                            session_id=ctx.session_id,
                            session_operation_context=ctx.session_operation_context,
                        ),
                        audit=audit,
                        recorder=recorder,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
                    # Lifecycle exceptions: do not absorb. Propagate so
                    # cancellation and shutdown work normally; the inner
                    # ComposerLLMCall record was already fired by the
                    # advisor method's finally block, so the audit trail
                    # captures the cancelled call regardless.
                    raise
                except TimeoutError as advisor_exc:
                    if advisor_deadline_limited:
                        timeout_payload = {
                            "status": "COMPOSE_TIMEOUT",
                            "error": "Advisor call exceeded the remaining compose deadline.",
                            "error_class": type(advisor_exc).__name__,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=timeout_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=timeout_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_compose_timeout = "in_flight"
                        break
                    # Advisor-specific timeout with compose budget still
                    # remaining: return structured tool feedback so the
                    # composer can continue within its global deadline.
                    advisor_error_payload = {
                        "status": "ADVISOR_ERROR",
                        "error": "Advisor call failed; no guidance returned.",
                        "error_class": type(advisor_exc).__name__,
                        "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(
                        finish_success(
                            audit,
                            result_payload=advisor_error_payload,
                            version_after=state.version,
                        )
                    )
                    _append_tool_outcome(
                        response=advisor_error_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_error_payload))
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                except Exception as advisor_context_error:
                    advisor_outcome = _AdvisorFirstPartyFailure(original_exc=advisor_context_error)

                if type(advisor_outcome) is _AdvisorProviderFailure:
                    # The service owns the Tier-3 provider taxonomy and returns
                    # this explicit recoverable outcome. Budget was already
                    # consumed above: the outbound attempt counts whether or not
                    # it produced guidance.
                    # Advisor failure IS counted for §7.7 anchor
                    # tracking — repeated identical failed advisor
                    # calls indicate the LLM is spamming a broken
                    # prompt, exactly the pattern the tracker exists
                    # to break.
                    if advisor_outcome.provider_failure is None:
                        advisor_error_payload = {
                            "status": "ADVISOR_ERROR",
                            "error": "Advisor call failed; no guidance returned.",
                            "error_class": advisor_outcome.error_class,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=advisor_error_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=advisor_error_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        anti_anchor.record_failure(tool_name, audit.arguments_hash)
                        tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_error_payload))
                    else:
                        request_rejection_payload = {
                            "status": "ADVISOR_ERROR",
                            "error": advisor_outcome.provider_failure.details.explanation,
                            "error_class": advisor_outcome.error_class,
                            "budget_used": advisor_calls_used,
                            "budget_remaining": budget - advisor_calls_used,
                            "request_rejection": advisor_outcome.provider_failure.details.to_wire(),
                        }
                        recorder.record(
                            finish_success(
                                audit,
                                result_payload=request_rejection_payload,
                                version_after=state.version,
                            )
                        )
                        _append_tool_outcome(
                            response=request_rejection_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        anti_anchor.record_failure(tool_name, audit.arguments_hash)
                        tool_feedback_json = json.dumps(advisor_feedback_payload(request_rejection_payload))
                    llm_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": tool_feedback_json,
                            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                        }
                    )
                    turn_has_discovery = True
                    continue

                if type(advisor_outcome) is _AdvisorFirstPartyFailure:
                    # A controlled-code fault is not provider feedback. Close the
                    # outer dispatch truthfully, carry it through P4, and let the
                    # driver re-raise this exact exception object after the tool
                    # row has been published.
                    first_party_exc = advisor_outcome.original_exc
                    if isinstance(first_party_exc, CommonProviderCostRefused):
                        refusal_payload = {
                            "success": False,
                            "status": "PROVIDER_POLICY_REFUSED",
                            "code": first_party_exc.code,
                            "outbound_call_made": first_party_exc.call_id is not None,
                        }
                        recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                        _append_tool_outcome(
                            response=refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        break
                    if isinstance(first_party_exc, PromptGuardRefused):
                        # The tool completed with a semantic refusal. It is neither
                        # provider-outage feedback nor a plugin implementation fault.
                        refusal_payload = {"success": False, "outbound_call_made": False, **first_party_exc.to_payload()}
                        recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                        _append_tool_outcome(
                            response=refusal_payload,
                            error_class=None,
                            error_category=None,
                            error_message=None,
                            post_version=state.version,
                        )
                        advisor_failure = first_party_exc
                        break
                    recorder.record(finish_plugin_crash(audit, exc=first_party_exc))
                    _append_tool_outcome(
                        response=None,
                        error_class=type(first_party_exc).__name__,
                        error_category=None,
                        error_message=type(first_party_exc).__name__,
                        post_version=state.version,
                    )
                    advisor_failure = first_party_exc
                    break

                if type(advisor_outcome) is not _AdvisorCallSuccess:
                    raise AuditIntegrityError("Advisor call returned an unknown owned outcome")
                guidance = advisor_outcome.guidance
                advisor_meta = advisor_outcome.metadata

                abandonment_decision = None
                review_binding = None
                advisor_note = (
                    "ADVICE only — call the appropriate mutation tool to "
                    "apply any change. Do not echo this guidance back as "
                    "configuration without verifying it against the schema."
                )
                if advisor_outcome.abandonment_response is not None:
                    if abandonment_context is None or ctx.turn_evidence is None:
                        raise AuditIntegrityError("Abandonment decision lost its request-owned context")
                    decision = advisor_outcome.abandonment_response
                    ctx.turn_evidence.accept_abandonment(
                        abandonment_context, decision, current_state=state, plugin_snapshot=ctx.plugin_snapshot
                    )
                    abandonment_decision = decision.decision
                    review_binding = abandonment_context.binding
                    guidance = decision.reason
                    if decision.next_action is not None:
                        guidance += "\nSupported next action: " + decision.next_action
                    if decision.question is not None:
                        guidance += "\nQuestion for the user: " + decision.question
                    if decision.decision == "STOP_CONFIRMED":
                        advisor_note = (
                            "Stop authoring this request: no remaining authorized useful action can proceed. Explain the confirmed limit "
                            "or ask the precise question; preserve the draft and do not claim completion. The final reply still receives ordinary END review."
                        )
                    elif decision.decision == "CONTINUE_WITH_LIMITATION":
                        advisor_note = (
                            "Continue the supported work with the advisor's next action. Retain the precise limitation for final review; "
                            "it does not stop the whole request or establish pipeline completion or graph clearance."
                        )
                    else:
                        advisor_note = "Continue with the advisor's supported action. This is not pipeline completion or graph clearance."
                success_payload = {
                    "status": "SUCCESS",
                    "guidance": guidance,
                    "model": advisor_meta["model"],
                    "prompt_tokens": advisor_meta["prompt_tokens"],
                    "completion_tokens": advisor_meta["completion_tokens"],
                    "cached_prompt_tokens": advisor_meta["cached_prompt_tokens"],
                    "advisor_latency_ms": advisor_meta["latency_ms"],
                    "budget_used": advisor_calls_used,
                    "budget_remaining": budget - advisor_calls_used,
                    "note": advisor_note,
                    "abandonment_decision": abandonment_decision,
                    "review_binding": review_binding,
                }
                recorder.record(
                    finish_success(
                        audit,
                        result_payload=success_payload,
                        version_after=state.version,
                    )
                )
                _append_tool_outcome(
                    response=success_payload,
                    error_class=None,
                    error_category=None,
                    error_message=None,
                    post_version=state.version,
                )
                # Successful advisor call is progress, not a failure —
                # the §7.7 tracker is reset for this tool name so a
                # subsequent set_pipeline failure does not inherit a
                # stale advisor anchor count.
                anti_anchor.record_success()
                tool_feedback_json = json.dumps(advisor_feedback_payload(success_payload))
                feedback_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_feedback_json,
                    PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                }
                if abandonment_context is not None and ctx.turn_evidence is not None:
                    ctx.turn_evidence.seal_abandonment_feedback(abandonment_context, feedback_message)
                llm_messages.append(feedback_message)
                continue

            # Session-aware async tool dispatch.
            #
            # ``_SESSION_AWARE_TOOL_HANDLERS`` holds handlers that AWAIT
            # session-service writers/readers; they cannot be dispatched
            # through the sync ``run_sync_in_worker(execute_tool, ...)``
            # path because execute_tool() is sync. The compose loop
            # intercepts these tool names BEFORE the generic dispatch
            # and awaits the handler directly, then routes the result
            # through the same audit envelope discipline as the sync
            # path (finish_success / finish_arg_error / finish_plugin_crash).
            #
            # Currently registered: ``request_interpretation_review``.
            # Adding another session-aware tool extends
            # ``_SESSION_AWARE_TOOL_HANDLERS`` and the per-tool
            # kwarg-build dict below; no new dispatch branch is needed.
            if is_session_aware_tool(tool_name):
                # Preflight is infrastructure work, before the session-aware
                # handler. Its failure/deadline carriers must reach the phase
                # owner without being recategorized as a handler crash.
                try:
                    review_preflight = await _await_started_tool_stage(
                        _pending_review_runtime_findings(state, ctx), audit=audit, recorder=recorder
                    )
                except ComposerRuntimePreflightError as preflight_exc:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_exc.original_exc,
                        partial_state=preflight_exc.partial_state,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=preflight_exc.failed_turn,
                    ) from preflight_exc.original_exc
                try:
                    session_aware_outcome = await _await_started_tool_stage(
                        ctx.session_tools._dispatch_session_aware_tool(
                            tool_name=tool_name,
                            tool_call_id=tool_call.id,
                            arguments=arguments,
                            state=state,
                            audit=audit,
                            recorder=recorder,
                            session_id=session_id,
                            session_operation_context=ctx.session_operation_context,
                            current_state_id=current_state_id,
                            composer_model_version=provider_model_version,
                            llm_messages=llm_messages,
                            anti_anchor=anti_anchor,
                            policy_catalog=ctx.policy_catalog,
                            review_preflight=review_preflight,
                        ),
                        audit=audit,
                        recorder=recorder,
                    )
                except (AssertionError, MemoryError, RecursionError, SystemError):
                    # Same narrow-class discipline as the sync execute_tool
                    # handler below: interpreter/Tier-1 invariant states must
                    # unwind, never be laundered into a recoverable envelope.
                    raise
                except AuditIntegrityError:
                    # Tier-1 audit invariant — same passthrough as the sync path.
                    raise
                except Exception as tool_exc:
                    # elspeth-9c01c943a5: the session-aware dispatch converts
                    # ToolArgumentError internally, so any exception reaching
                    # here is a plugin/service bug. Without this capture it
                    # escaped run_tool_batch and surfaced as an uncoded 500
                    # with the accumulated turn state silently dropped. Route
                    # it through the same plugin-crash carrier as the sync
                    # path so the turn persists and the route layer answers
                    # with the coded envelope.
                    recorder.record(finish_plugin_crash(audit, exc=tool_exc))
                    _append_tool_outcome(
                        response=None,
                        error_class=type(tool_exc).__name__,
                        error_category=None,
                        error_message=type(tool_exc).__name__,
                        post_version=state.version,
                    )
                    plugin_crash = ComposerPluginCrashError.capture(
                        tool_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                    )
                    plugin_crash_cause = tool_exc
                    break
                all_cache_hits = False
                _append_tool_outcome(
                    response=session_aware_outcome.result,
                    error_class=session_aware_outcome.error_class,
                    error_category=session_aware_outcome.error_category,
                    error_message=session_aware_outcome.error_message,
                    post_version=session_aware_outcome.post_version,
                )
                if session_aware_outcome.is_discovery:
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                if session_aware_outcome.result is not None:
                    state = session_aware_outcome.result.updated_state
                    last_validation = session_aware_outcome.result.validation
                continue

            # Precompute runtime preflight for preview_pipeline outside
            # the general side-effectful tool worker. This keeps
            # execute_tool() synchronous and bounds the async I/O cost
            # before it enters the worker thread pool.
            runtime_preflight_callback: RuntimePreflight | None = None
            structural_preflight_callback: RuntimePreflight | None = None
            preview_claims_admitted = True
            if tool_name == "preview_pipeline":
                try:
                    _admit_source_proof_claims(state)
                except MalformedSourceProofClaim:
                    # The ordinary producer owns the saved-graph refusal. Do not
                    # inspect any resource before its complete scalar admission.
                    preview_claims_admitted = False
            if tool_name == "preview_pipeline" and preview_claims_admitted:
                try:
                    preview_preflight = await ctx.preflight.cached_runtime_preflight(
                        state,
                        user_id=user_id,
                        session_id=session_id,
                        cache=runtime_preflight_cache,
                        initial_version=initial_version,
                        session_scope=session_scope,
                        llm_calls=recorder.llm_calls,
                        plugin_snapshot=ctx.plugin_snapshot,
                        session_operation_context=ctx.session_operation_context,
                    )
                except asyncio.CancelledError as cancellation:
                    recorder.record(finish_cancelled(audit, exc=cancellation))
                    raise
                except ComposerRuntimePreflightError as preflight_exc:
                    recorder.record(finish_plugin_crash(audit, exc=preflight_exc.original_exc))
                    raise ComposerRuntimePreflightError.capture(
                        preflight_exc.original_exc,
                        state=state,
                        initial_version=initial_version,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                    ) from preflight_exc.original_exc

                def _make_preflight_callback(
                    _result: ValidationResult = preview_preflight,
                ) -> RuntimePreflight:
                    def _callback(_state: CompositionState) -> ValidationResult:
                        return _result

                    return _callback

                runtime_preflight_callback = _make_preflight_callback()

                # elspeth-229e9e8195: while an interpretation review is pending
                # the strict verdict above is handoff-shaped and every stage past
                # ``review_interpretations`` is SKIPPED_AFTER_FAILURE — so also
                # precompute the interpretation-tolerant preflight and let the
                # preview surface its structural findings during authoring
                # instead of first at completion. Same cache and snapshot as the
                # completion path's ``_pending_handoff_outstanding_findings``, so
                # the two tolerant lookups share one engine run per state. A
                # tolerant-pass infrastructure failure propagates exactly like
                # the strict one (the completion path takes the same position):
                # an explicit failure beats silently withholding findings this
                # turn claimed to check for.
                if is_pending_interpretation_handoff(preview_preflight):
                    try:
                        structural_preflight_result = await _await_started_tool_stage(
                            ctx.preflight.cached_runtime_preflight(
                                state,
                                user_id=user_id,
                                session_id=session_id,
                                cache=runtime_preflight_cache,
                                initial_version=initial_version,
                                session_scope=session_scope,
                                llm_calls=recorder.llm_calls,
                                plugin_snapshot=ctx.plugin_snapshot,
                                session_operation_context=ctx.session_operation_context,
                                interpretation_tolerant=True,
                            ),
                            audit=audit,
                            recorder=recorder,
                        )
                    except ComposerRuntimePreflightError as preflight_exc:
                        recorder.record(finish_plugin_crash(audit, exc=preflight_exc.original_exc))
                        raise ComposerRuntimePreflightError.capture(
                            preflight_exc.original_exc,
                            state=state,
                            initial_version=initial_version,
                            tool_invocations=recorder.invocations,
                            llm_calls=recorder.llm_calls,
                        ) from preflight_exc.original_exc

                    def _make_structural_callback(
                        _result: ValidationResult = structural_preflight_result,
                    ) -> RuntimePreflight:
                        def _callback(_state: CompositionState) -> ValidationResult:
                            return _result

                        return _callback

                    structural_preflight_callback = _make_structural_callback()

            # All tool calls are offloaded to a worker to avoid blocking
            # the event loop.
            # Blob and secret tools perform synchronous filesystem
            # writes and SQLAlchemy transactions that would otherwise
            # stall the single-process web server for all concurrent
            # requests (rate-limit checks, websocket heartbeats,
            # progress broadcasts).
            #
            # Cancel-safety: tool calls are NOT wrapped in
            # asyncio.wait_for — they always run to completion.
            # The cooperative deadline is checked BETWEEN operations
            # (before LLM calls, after tool batches), so side effects
            # and state publication are never split.  LLM calls use
            # per-call wait_for because they are pure network I/O
            # with no side effects.
            #
            # Tool handlers raise ToolArgumentError at Tier-3 boundaries
            # (LLM supplied wrong types, semantically invalid values,
            # or malformed encodings that cannot be coerced).  The
            # compose loop catches ONLY that class and feeds the error
            # back to the LLM for retry.
            #
            # Any other exception — TypeError, ValueError, UnicodeError,
            # KeyError, AttributeError — escaping execute_tool() is a
            # plugin bug (Tier 1/2) and MUST crash.  Per
            # docs/guides/data-trust-and-error-handling.md §Plugin
            # Ownership: System Code, Not User Code, silently
            # laundering a plugin bug as an LLM-argument error
            # is worse than crashing: it pollutes the audit trail with
            # a confident but wrong Tier-3 story, and the LLM's "retry"
            # cannot correct a fault in our own code.
            # Dispatch under the structural audit envelope. The helper
            # records exactly one ComposerToolInvocation before this
            # await returns or raises, on every path:
            #   SUCCESS         → finish_success (with sentinel-canonical
            #                     fallback if canonical_json raises)
            #   ARG_ERROR       → finish_arg_error (caller's except block
            #                     below builds the LLM message)
            #   PLUGIN_CRASH (narrow re-raise) → finish_plugin_crash, then
            #                     re-raised so the outer compose loop exits
            #   PLUGIN_CRASH (general) → finish_plugin_crash, then the
            #                     caller's except block below wraps with
            #                     ComposerPluginCrashError.capture()
            #
            # Closes panel-review blockers B1 (canonical_json failure on
            # success path silently bypassed audit) and B2 (narrow
            # re-raise exited the loop unrecorded).
            # Bind loop-locals as default arguments so the closures
            # capture this iteration's values. The compose loop
            # awaits the helper synchronously inside the same
            # iteration, so late binding would not actually misfire
            # — but `B023 do not let function definitions reference
            # late-bound loop variables` is the project's structural
            # safeguard against future refactors that batch
            # iterations or introduce concurrency. Same pattern the
            # adjacent `_make_preflight_callback` already uses for
            # ``preview_preflight``.
            async def _do_dispatch(
                _tool_name: str = tool_name,
                _arguments: dict[str, Any] = arguments,
                _state: CompositionState = state,
                _last_validation: ValidationSummary | None = last_validation,
                _runtime_preflight_callback: RuntimePreflight | None = runtime_preflight_callback,
                _structural_preflight_callback: RuntimePreflight | None = structural_preflight_callback,
                _user_message_id: str | None = user_message_id,
                _user_message_content: str | None = user_message_content,
                _composer_model_identifier: str = ctx.provenance.model_identifier,
                _composer_model_version: str = provider_model_version,
                _composer_provider: str = ctx.provenance.provider or "unknown",
                _composer_skill_hash: str = ctx.provenance.skill_hash,
                _tool_arguments_hash: str = audit.binding_arguments_hash,
                _interpretation_requirements_are_internal: bool = interpretation_requirements_are_internal,
                _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
                _preproposal_exception: BaseException | None = preproposal_exception,
                _auto_mutation: ComposerMutationCeiling | None = auto_mutation,
            ) -> Any:
                if _preproposal_exception is not None:
                    raise _preproposal_exception
                if _prevalidated_unapplied_result is not None:
                    return _prevalidated_unapplied_result
                try:
                    dispatched_result = await run_sync_in_worker(
                        execute_tool,
                        _tool_name,
                        _arguments,
                        _state,
                        ctx.policy_catalog,
                        plugin_snapshot=ctx.plugin_snapshot,
                        data_dir=ctx.custody.data_dir,
                        session_engine=ctx.custody.session_engine,
                        session_id=session_id,
                        session_operation_context=ctx.session_operation_context,
                        session_operation_authority=ctx.session_operation_authority,
                        secret_service=ctx.custody.secret_service,
                        secret_wiring_policy=ctx.custody.secret_wiring_policy,
                        user_id=user_id,
                        prior_validation=_last_validation,
                        runtime_preflight=_runtime_preflight_callback,
                        structural_preflight=_structural_preflight_callback,
                        max_blob_storage_per_session_bytes=ctx.custody.max_blob_storage_per_session_bytes,
                        user_message_id=_user_message_id,
                        user_message_content=_user_message_content,
                        composer_model_identifier=_composer_model_identifier,
                        composer_model_version=_composer_model_version,
                        composer_provider=_composer_provider,
                        composer_skill_hash=_composer_skill_hash,
                        tool_arguments_hash=_tool_arguments_hash,
                        mutation_ceiling=_auto_mutation,
                        validate_arguments=True,
                        require_data_dir_for_paths=True,
                        raise_schema_argument_errors=True,
                        _interpretation_requirements_are_internal=_interpretation_requirements_are_internal,
                    )
                except ComposerAutoMutationRefused as exc:
                    acc.authority_revocation = exc.revocation
                    return _failure_result(_state, str(exc), error_code="auto_mutation_revoked", with_state_validation=False)
                if (
                    _tool_name != "set_pipeline"
                    and is_mutation_tool(_tool_name)
                    and dispatched_result.success
                    and dispatched_result.updated_state.version > _state.version
                    and dispatched_result.validation.is_valid
                ):
                    dispatched_result = await _finalize_completed_incremental_mutation(
                        dispatched_result,
                        _state,
                        plugin_snapshot=ctx.plugin_snapshot,
                        policy_catalog=ctx.policy_catalog,
                    )
                if is_discovery_tool(_tool_name) and response_contract_for(_tool_name) is not None:
                    return admit_discovery_result(_tool_name, dispatched_result)
                return dispatched_result

            # ``_arg_error_payload`` is a module-level helper (F2 — testable
            # without spinning up the full compose loop). The nested
            # factory below binds ``tool_name`` to match the dispatch
            # ``arg_error_payload_factory`` signature. Default-arg binding
            # captures the loop-local ``tool_name`` at definition time.
            def _arg_error_payload_factory(
                _exc: ToolArgumentError,
                _tool_name: str = tool_name,
            ) -> Mapping[str, Any]:
                return _arg_error_payload(_exc, _tool_name)

            def _version_after(
                _result: Any,
                _prevalidated_unapplied_result: ToolResult | None = prevalidated_unapplied_result,
                _state: CompositionState = state,
            ) -> int:
                # The handler's ToolResult carries the new state on
                # ``updated_state``. Read here so the helper records
                # the post-mutation version inside the recorder call.
                if _prevalidated_unapplied_result is not None:
                    return _state.version
                if isinstance(_result, AdmittedDiscoveryResult):
                    return _result.result.updated_state.version
                return cast(int, _result.updated_state.version)

            try:
                outcome = await dispatch_with_audit(
                    recorder=recorder,
                    audit=audit,
                    do_dispatch=_do_dispatch,
                    version_after_provider=_version_after,
                    arg_error_payload_factory=_arg_error_payload_factory,
                )
            except ToolArgumentError as exc:
                # The audit record was already written by the helper.
                # Build the LLM-facing tool message and continue.
                if is_discovery_tool(tool_name):
                    turn_has_discovery = True
                else:
                    turn_has_mutation = True
                # Trust-boundary redaction: the echoed message reaches the
                # LLM API and (via audit) the Landscape. ToolArgumentError
                # is structurally safe by construction — the keyword-only
                # constructor accepts (argument, expected, actual_type)
                # and composes args[0] from those fields alone, so the
                # message cannot carry a raw LLM-supplied value. Belt-
                # and-suspenders: read ``exc.args[0]`` rather than
                # ``str(exc)`` so a future subclass that overrides
                # ``__str__`` to embed ``__cause__`` context (which may
                # carry DB URLs, filesystem paths, or secret fragments
                # from deeper layers) cannot leak through this path.
                # Handlers that use
                # ``raise ToolArgumentError(...) from exc`` get the
                # cause preserved on ``__cause__`` for debug/audit but
                # NOT echoed to the LLM.
                await emit_progress(
                    progress,
                    ComposerProgressEvent(
                        phase="using_tools",
                        headline="A tool request needed correction.",
                        evidence=("The tool rejected the request shape without exposing raw values.",),
                        likely_next="ELSPETH will ask the model to adjust the visible tool request.",
                    ),
                )
                arg_error_payload = _arg_error_payload(exc, tool_name)
                _append_tool_outcome(
                    response=None,
                    error_class=type(exc).__name__,
                    error_category=exc.category,
                    error_message=str(exc.args[0] if exc.args else "ToolArgumentError"),
                    post_version=state.version,
                )
                anti_anchor.record_failure(tool_name, audit.arguments_hash, category=exc.category)
                tool_feedback_json = json.dumps(arg_error_payload)
                llm_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_feedback_json,
                        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json),
                    }
                )
                continue
            except (AssertionError, MemoryError, RecursionError, SystemError):
                # Plugin-ownership policy exception — DOCUMENTED DIVERGENCE.
                #
                # docs/guides/data-trust-and-error-handling.md §Plugin
                # Ownership: System Code, Not User Code says a defective
                # plugin MUST crash rather than be wrapped and laundered
                # as a recoverable error.  The web server relaxes this for
                # ordinary exception classes (see the wider except
                # Exception below) because crashing the whole ASGI
                # process on one bad request would take down every
                # other concurrent session.
                #
                # The exceptions listed on this handler are NOT
                # relaxed: they represent states where the interpreter
                # or our own Tier-1 invariants are compromised and any
                # subsequent work — including the partial-state
                # persistence inside ``ComposerPluginCrashError.capture``
                # — would be operating on potentially-poisoned memory
                # or data.
                #
                # - AssertionError: a plain ``assert`` fired inside
                #   plugin code.  Asserts encode Tier-1 invariants
                #   (crash on any anomaly — see
                #   docs/guides/data-trust-and-error-handling.md §The
                #   Three-Tier Trust Model).  Writing the
                #   composition_states row after an invariant failure
                #   would persist data the invariant said was
                #   impossible.
                # - MemoryError / RecursionError: interpreter-level
                #   resource exhaustion.  The subsequent DB write may
                #   itself fail or corrupt state; better to unwind.
                # - SystemError: CPython internal invariant breach.
                #
                # ``BaseException``-only classes (SystemExit,
                # KeyboardInterrupt, GeneratorExit) already propagate
                # through ``except Exception`` below without any
                # handling here.
                #
                # Audit-record-then-raise discipline: ``dispatch_with_audit``
                # writes a ``PLUGIN_CRASH`` invocation BEFORE the
                # narrow-class exception leaves the helper. Building the
                # invocation reads only pre-captured scalars from the
                # frozen :class:`DispatchAudit` envelope plus
                # ``type(exc).__name__`` — no poisoned memory work.
                # Closes blocker B2 from the panel review (2026-05-04).
                raise
            except ComposerRuntimePreflightError as preflight_exc:
                # A successful mutation may precede its structural preflight.
                # Preserve that new state instead of recapturing the old loop state.
                raise ComposerRuntimePreflightError(
                    original_exc=preflight_exc.original_exc,
                    partial_state=preflight_exc.partial_state,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                    failed_turn=preflight_exc.failed_turn,
                ) from preflight_exc.original_exc
            except AuditIntegrityError:
                # Tier-1 audit invariant. Do not let the plugin-bug
                # catch-all below launder it into ComposerPluginCrashError.
                raise
            except Exception as tool_exc:
                # Plugin-bug path: any exception class OTHER than
                # ToolArgumentError escaping execute_tool() is a plugin
                # bug (docs/guides/data-trust-and-error-handling.md
                # §Plugin Ownership: System Code, Not User Code).
                # Capture the loop-local ``state`` — which has been
                # rebound to result.updated_state on every successful
                # prior iteration — so the route layer can persist the
                # accumulated mutations into composition_states before
                # returning the 500. Without this, any tool call that
                # successfully mutated state prior to the crash would
                # be silently dropped from the state history.
                #
                # Web-server policy exception: the plugin-ownership rule
                # (docs/guides/data-trust-and-error-handling.md §Plugin
                # Ownership: System Code, Not User Code) says a
                # defective plugin must crash.  In the pipeline engine
                # (single-shot CLI process) that is straightforward —
                # abort the run.  In the web server a single malformed
                # request reaching a buggy tool handler would take the
                # ASGI worker down and abort every other concurrent
                # session, including audit writes, websocket progress
                # streams, and unrelated users.  We wrap the exception
                # into a typed ComposerPluginCrashError that surfaces
                # to the operator as an HTTP 500 with
                # ``type(exc).__name__`` in the structured log, and
                # preserves the original on ``__cause__`` for the ASGI
                # error machinery.  The handler directly above
                # re-raises the narrow set of exception classes that
                # MUST NOT be laundered, so the concession below is
                # bounded.
                #
                # Wrap narrow-scope: only exceptions from the
                # execute_tool call are wrapped here. Bugs in
                # _call_llm_before_deadline / _build_messages surface
                # through their own exception classes
                # (ComposerServiceError, ComposerConvergenceError).
                # Record PLUGIN_CRASH BEFORE raising — done structurally
                # by ``dispatch_with_audit``. The ``capture()`` helper
                # takes the recorder buffer (including the helper's
                # final crash record) so the route handler's
                # ``_handle_plugin_crash`` gets the complete sequence.
                _append_tool_outcome(
                    response=None,
                    error_class=type(tool_exc).__name__,
                    error_category=None,
                    error_message=type(tool_exc).__name__,
                    post_version=state.version,
                )
                plugin_crash = ComposerPluginCrashError.capture(
                    tool_exc,
                    state=state,
                    initial_version=initial_version,
                    tool_invocations=recorder.invocations,
                    llm_calls=recorder.llm_calls,
                )
                plugin_crash_cause = tool_exc
                break

            # SUCCESS path — the helper already recorded
            # ComposerToolStatus.SUCCESS via finish_success (with the
            # sentinel-canonical fallback for non-finite floats /
            # non-serializable result types). Update loop-local state
            # from outcome.result and continue with the LLM-message
            # append.
            version_before_tool = state.version
            admitted_result = outcome.result if isinstance(outcome.result, AdmittedDiscoveryResult) else None
            result = admitted_result.result if admitted_result is not None else outcome.result
            if (
                prevalidated_unapplied_result is None
                and is_mutation_tool(tool_name)
                and result.success
                and result.updated_state.version > state.version
                and result.validation.is_valid
            ):
                # The mutation has succeeded and its applied version is already
                # audited. A separate preflight failure cannot relabel that tool
                # as a crash or erase the version the route must persist.
                try:
                    findings = await _pending_review_runtime_findings(result.updated_state, ctx)
                except ComposerRuntimePreflightError as preflight_exc:
                    raise ComposerRuntimePreflightError(
                        original_exc=preflight_exc.original_exc,
                        partial_state=preflight_exc.partial_state,
                        tool_invocations=recorder.invocations,
                        llm_calls=recorder.llm_calls,
                        failed_turn=preflight_exc.failed_turn or ctx.failed_turn,
                    ) from preflight_exc.original_exc
                if findings is not None:
                    result = replace(result, runtime_preflight=findings)
            if prevalidated_unapplied_result is None:
                state = result.updated_state
                last_validation = result.validation
                last_runtime_preflight = result.runtime_preflight or last_runtime_preflight
            # Mark the (plugin_type, plugin_name) pair as
            # schema-loaded for this session when a get_plugin_schema
            # call returned a discovery success. The per-turn system
            # context renders this set as
            # ``composer_progress.schemas_loaded_this_session`` so
            # the model can compute its own schemas_gap without
            # re-introspecting plugins it has already seen. See
            # ``SchemaDisclosureTracker.mark_plugin_schema_loaded`` and
            # ``prompts.build_context_string``.
            #
            # Lifted from the prior inline compose-loop body into the
            # extracted ``_dispatch_tool_batch`` helper as part of the
            # parallel decomposition (commit f918f4269). The RC5.2
            # commit that introduced this tracker explicitly anticipated
            # the lift in its message: "the single line that will need
            # lift-and-shift into ``dispatch_tools`` when the parallel
            # ``_compose_loop`` decomposition lands".
            if tool_name == "get_plugin_schema" and result.success:
                ctx.schema_disclosure.mark_plugin_schema_loaded(
                    session_id,
                    str(arguments["plugin_type"]),
                    str(arguments["name"]),
                )
            # §7.7 anchor tracking. ``finish_success`` records the audit
            # invocation regardless of ``result.success`` — the dispatch
            # itself ran without raising. But for anchor purposes we look
            # at ToolResult.success: a set_pipeline that returned a
            # validation-rejected state is the dominant anchor pattern
            # observed in the Tier 1 RED, and indistinguishable from a
            # ToolArgumentError as far as the LLM's retry loop is concerned.
            #
            # Successes only break the anchor when they are MUTATION
            # successes. Discovery successes (get_plugin_schema, list_*,
            # get_pipeline_state) are observations — empirically the model
            # interleaves them between failed mutation retries (see the
            # smoke session 55895523-... where 4 set_pipeline failures
            # were broken up by 1 get_pipeline_state and 1 get_plugin_schema,
            # both successful, both irrelevant to whether the model has
            # progressed on the anchored mutation).
            #
            # Mutation means a CompositionState version advance here.
            # Blob-store side effects such as create_blob/update_blob are
            # useful work, but they do not make an empty pipeline exist.
            if prevalidated_unapplied_result is not None:
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
            elif result.success:
                if _tool_result_mutated_composition_state(version_before=version_before_tool, result=result):
                    mutation_success_observed = True
                    anti_anchor.record_success()
            else:
                anti_anchor.record_failure(tool_name, audit.arguments_hash)
            result_json = (
                serialize_admitted_discovery_result(admitted_result) if admitted_result is not None else _serialize_tool_result(result)
            )
            if (
                tool_name in {"get_blob_content", "read_attachment", "inspect_source"}
                and admitted_result is not None
                and ctx.turn_evidence is not None
            ):
                ctx.turn_evidence.register_blob_page(
                    tool_call_id=tool_call.id,
                    admitted=admitted_result,
                    selected_content=result_json,
                    producer_call_id=ctx.turn_evidence.current_call_id("primary"),
                )
            await emit_progress(progress, tool_completed_progress_event(tool_name, result.success))
            _append_tool_outcome(
                response=admitted_result.to_tool_result() if admitted_result is not None else result,
                error_class=None,
                error_category=None,
                error_message=None,
                post_version=state.version,
            )

            # Cached payloads omit rejection entries; only successful discovery is reusable.
            if is_cacheable_discovery_tool(tool_name) and result.success:
                cache_key = _make_cache_key(tool_name, arguments)
                discovery_cache[cache_key] = _cached_discovery_payload(admitted_result if admitted_result is not None else result)

            llm_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result_json,
                    PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(
                        admitted_result if admitted_result is not None else result, result_json
                    ),
                }
            )

            if not is_discovery_tool(tool_name):
                turn_has_mutation = True
            else:
                turn_has_discovery = True

            if acc.authority_revocation is not None:
                break

            # The completed preview has its ordinary audit, message, outcome and
            # classification. An unavailable saved claim closes this issued batch;
            # P4 will persist only this executed prefix before the loop terminates.
            if admitted_result is not None and preview_source_proof_stop(admitted_result, state) is not None:
                break

        else:
            ran_to_end = True
    finally:
        batch.state = state
        batch.last_validation = last_validation
        batch.last_runtime_preflight = last_runtime_preflight
        batch.advisor_calls_used = advisor_calls_used
        batch.turn_has_mutation = turn_has_mutation
        batch.turn_has_discovery = turn_has_discovery
        batch.all_cache_hits = all_cache_hits
        batch.tool_outcomes = tool_outcomes
        batch.plugin_crash = plugin_crash
        batch.plugin_crash_cause = plugin_crash_cause
        batch.advisor_failure = advisor_failure
        batch.advisor_compose_timeout = advisor_compose_timeout
        batch.pre_state_id = pre_state_id
        batch.decoded_args_by_call_id = decoded_args_by_call_id
        batch.private_authorings_by_call = private_authorings_by_call
        batch.proposals_this_turn = proposals_this_turn
        batch.mutation_success_observed = mutation_success_observed
        batch.refused_resources = refused_resources
        batch.promises_by_call = promises_by_call
        call.audit = audit
        call.arguments = arguments
        call.audit_arguments = audit_arguments
    if len(tool_outcomes) != before + 1:
        raise AuditIntegrityError("completed tool call has no exact single actual outcome")
    call.phase = "applied"
    batch.stop_issued_batch = batch.stop_issued_batch or not ran_to_end
    return "applied"


def _finish_continuation_batch_live(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    batch: ContinuationBatchState,
) -> tuple[_DispatchOutcome, int]:
    """Project the actual complete issued batch and executed prefix."""
    state = batch.state
    last_validation = batch.last_validation
    last_runtime_preflight = batch.last_runtime_preflight
    tool_outcomes = batch.tool_outcomes
    decoded_args_by_call_id = batch.decoded_args_by_call_id
    private_authorings_by_call = batch.private_authorings_by_call
    turn_has_mutation = batch.turn_has_mutation
    turn_has_discovery = batch.turn_has_discovery
    all_cache_hits = batch.all_cache_hits
    plugin_crash = batch.plugin_crash
    plugin_crash_cause = batch.plugin_crash_cause
    advisor_failure = batch.advisor_failure
    advisor_compose_timeout = batch.advisor_compose_timeout
    mutation_success_observed = batch.mutation_success_observed
    pre_state_id = batch.pre_state_id
    advisor_calls_used = batch.advisor_calls_used
    recorder = ctx.recorder
    completion = call_model.completion
    assistant_message = completion.message
    raw_assistant_content = assistant_message.content
    assistant_tool_calls = completion.tool_batch.calls
    outcome_ids = {outcome.call.id for outcome in tool_outcomes}
    dispatch = _DispatchOutcome(
        state=state,
        last_validation=last_validation,
        last_runtime_preflight=last_runtime_preflight,
        tool_outcomes=tuple(tool_outcomes),
        decoded_args_by_call_id=decoded_args_by_call_id,
        private_authorings=tuple(private_authorings_by_call[outcome.call.id] for outcome in tool_outcomes),
        private_invocations=tuple(invocation for invocation in recorder.invocations if invocation.tool_call_id in outcome_ids),
        turn_has_mutation=turn_has_mutation,
        turn_has_discovery=turn_has_discovery,
        all_cache_hits=all_cache_hits,
        plugin_crash=plugin_crash,
        plugin_crash_cause=plugin_crash_cause,
        advisor_failure=advisor_failure,
        advisor_compose_timeout=advisor_compose_timeout,
        assistant_message=assistant_message,
        raw_assistant_content=raw_assistant_content,
        assistant_tool_calls=assistant_tool_calls,
        mutation_success_observed=mutation_success_observed,
        pre_state_id=pre_state_id,
        authority_revocation=acc.authority_revocation,
    )
    return dispatch, advisor_calls_used


async def run_tool_batch(
    *,
    call_model: _CallModelOutcome,
    ctx: ToolBatchContext,
    acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]],
) -> tuple[_DispatchOutcome, int]:
    """Drive the real legacy batch without continuation storage or restored owners."""
    batch = ContinuationBatchState(
        acc.state, acc.last_validation, acc.last_runtime_preflight, acc.advisor_calls_used, entry_state=acc.state
    )
    await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)
    calls = call_model.completion.tool_batch.calls
    while batch.next_tool_index < len(calls) and not batch.stop_issued_batch:
        call = ContinuationCallState(batch.next_tool_index, calls[batch.next_tool_index], batch.state, batch.state.version)
        batch.current_call = call
        batch.phase = "call_active"
        prepared = await _prepare_continuation_call_live(call_model=call_model, ctx=ctx, llm_messages=llm_messages, batch=batch, call=call)
        if prepared == "stop":
            break
        if prepared == "prepared":
            await _execute_continuation_call_live(
                call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch, call=call
            )
        batch.next_tool_index += 1
        batch.current_call = None
        batch.phase = "call_ready"
    batch.phase = "dispatch_ready"
    return _finish_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, batch=batch)
```

Qualification uses the ordinary `test_continuation_batch_live.py` cases for real initialization, mutation aliases, preparation and execution continues, plugin break, narrow exception identity at the extracted helper and the unchanged outer `ComposerToolFailure` evidence carrier (same `original_exception`, direct `__cause__` and recorded invocation cohort), and exact original revocation; retain all existing dispatch, dependent-resource, wire/audit, proposal/Advisor, preflight, mutation-authority and P4 tests. The existing public pre-tool cancellation test and the existing two-call worker cancellation/persist-prefix test remain mandatory separate oracles. Source span comparisons and native controls establish the extraction; regex inventories or a document hash are not behavioral proof. Before application, close the exact Reserve proof dependency described in B1 and qualify original complete scanner APIs with their finite negative controls. No partial green enables the cold/v2 path.

### B2 nominal Advisor admission tests and source-census dependency

Create `tests/unit/web/composer/test_advisor_nominal_admission.py` with this complete module. It exercises the existing actual session/service/context/decoder/validator using the ordinary admitted-completion fixture. Budget zero makes admission and refusal observable before SDK dispatch; the imported existing END stub is a unit-test boundary and does not qualify publication/provider settlement. Retain the separate real SDK/audit/guard/required-work/cancellation obligations above.

```python
"""Nominal manual-Advisor admission through the real B2 preparation/execution.

These focused unit tests use the existing admitted-completion fixture and real
session/service/context/argument decoder/Advisor validator. Budget zero makes
the post-admission provider decision observable without a synthetic Advisor
answer. Existing SDK/guard/required-work/audit tests remain separate obligations.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import tool_batch
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer import test_advisor_tool as advisor_fixtures

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean
_composer_available_by_default = advisor_fixtures._composer_available_by_default


def _arguments():
    return {
        "trigger": "proactive_security_safety",
        "problem_summary": "Review the complete optional schema and input handling.",
        "recent_errors": ["No secret material is present."],
        "attempted_actions": ["Read the complete published schema."],
        "schema_excerpt": '{"optional_tail":"must reach actual admission"}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("instance_override", [False, True], ids=["ordinary-owner", "instance-override-ignored"])
@pytest.mark.parametrize("payload_kind", ["valid", "unknown-key", "prompt-cap"])
async def test_actual_owner_receives_complete_arguments_and_its_actual_prompt_cap(
    monkeypatch,
    tmp_path,
    instance_override,
    payload_kind,
):
    settings = advisor_fixtures._make_settings(budget=0, data_dir=tmp_path).model_copy(update={"composer_advisor_max_prompt_tokens": 1000})
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(),
        settings=settings,
    )
    owner = service._advisor_checkpoint
    assert type(owner) is AdvisorCheckpointOwner
    assert owner._settings is settings
    payload = _arguments()
    if payload_kind == "unknown-key":
        payload["unknown_complete_input_marker"] = "must not be projected away"
    elif payload_kind == "prompt-cap":
        payload["recent_errors"] = ["X" * 1500] * 3

    prepared = []
    admitted = []
    overrides = []
    execute = tool_batch._execute_continuation_call_live
    validate = AdvisorCheckpointOwner._validate_advisor_arguments

    async def observe_execute(**kwargs):
        prepared.append((kwargs["ctx"].advisor_checkpoint, kwargs["call"].arguments))
        return await execute(**kwargs)

    def observe_actual_validator(self, arguments):
        admitted.append((self, arguments))
        return validate(self, arguments)

    def forbidden_instance_override(arguments):
        overrides.append(arguments)
        raise AssertionError("an instance attribute must not replace nominal admission")

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    monkeypatch.setattr(AdvisorCheckpointOwner, "_validate_advisor_arguments", observe_actual_validator)
    if instance_override:
        monkeypatch.setattr(owner, "_validate_advisor_arguments", forbidden_instance_override)

    initial_state = advisor_fixtures._empty_state()
    turns = [
        advisor_fixtures._make_tool_call("nominal_advisor", "request_advisor_hint", payload),
        advisor_fixtures._make_text_only_response("The request remains available for the next turn."),
    ]
    with (
        patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as primary,
        patch("litellm.acompletion", new_callable=AsyncMock) as sdk,
    ):
        primary.side_effect = turns
        sdk.side_effect = AssertionError("budget zero or argument refusal must precede Advisor SDK dispatch")
        result = await service.compose(
            "Review the complete input safely.",
            [],
            initial_state,
            session_id=session_id,
        )

    assert primary.call_count == 2
    assert sdk.call_count == 0
    assert overrides == []
    assert len(prepared) == len(admitted) == 1
    assert prepared[0][0] is admitted[0][0] is owner
    assert prepared[0][1] is admitted[0][1]
    assert admitted[0][1] == payload
    assert admitted[0][1]["schema_excerpt"] == payload["schema_excerpt"]
    invocations = [item for item in result.tool_invocations if item.tool_name == "request_advisor_hint"]
    assert len(invocations) == 1
    invocation = invocations[0]
    assert json.loads(invocation.arguments_canonical) == payload
    assert invocation.version_before == 1
    assert result.state == initial_state
    assert invocation.result_canonical is not None
    response = json.loads(invocation.result_canonical)
    if payload_kind == "valid":
        assert invocation.version_after == 1
        assert invocation.status.name == "SUCCESS"
        assert response["status"] == "BUDGET_EXHAUSTED"
        assert response["budget_used"] == response["budget_remaining"] == 0
    else:
        assert invocation.version_after is None
        assert invocation.status.name == "ARG_ERROR"
        assert response["status"] == "ARG_ERROR"
        if payload_kind == "prompt-cap":
            assert invocation.error_category is ToolArgumentErrorCategory.PROMPT_BUDGET
            assert "prompt size" in response["error"]
        else:
            assert invocation.error_category is ToolArgumentErrorCategory.SCHEMA_SHAPE
            assert "must conform to the published schema" in response["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver_kind", ["foreign", "subclass"])
async def test_foreign_or_subclass_receiver_is_a_first_party_invariant_before_admission(
    monkeypatch,
    tmp_path,
    receiver_kind,
):
    settings = advisor_fixtures._make_settings(budget=0, data_dir=tmp_path)
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(),
        settings=settings,
    )
    owned = service._advisor_checkpoint
    attempts = []

    class ForeignOwner:
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("foreign receiver reached admission")

    class DerivedOwner(AdvisorCheckpointOwner):
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("subclass receiver reached admission")

    if receiver_kind == "foreign":
        replacement = ForeignOwner()
    else:
        replacement = DerivedOwner(
            settings=owned._settings,
            composer_skill_text=owned._composer_skill_text,
            advisor_endpoint_base_url=owned._advisor_endpoint_base_url,
            advisor_endpoint_api_key=owned._advisor_endpoint_api_key,
            sessions_service=owned._sessions_service,
            chargeable_admission=owned._chargeable_admission,
            prompt_guard_mount=owned._prompt_guard_mount,
            secret_service=owned._secret_service,
        )
    monkeypatch.setattr(service, "_advisor_checkpoint", replacement)
    prepared = []
    execute = tool_batch._execute_continuation_call_live

    async def observe_execute(**kwargs):
        prepared.append((kwargs["ctx"].advisor_checkpoint, kwargs["call"].arguments))
        return await execute(**kwargs)

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    payload = _arguments()
    with (
        patch.object(service._provider_gateway, "_call_llm", new_callable=AsyncMock) as primary,
        patch("litellm.acompletion", new_callable=AsyncMock) as sdk,
    ):
        primary.return_value = advisor_fixtures._make_tool_call("nominal_advisor", "request_advisor_hint", payload)
        sdk.side_effect = AssertionError("foreign receiver must fail before Advisor SDK dispatch")
        with pytest.raises(AuditIntegrityError, match="Manual Advisor admission requires the owned checkpoint owner"):
            await service.compose(
                "Review the complete input safely.",
                [],
                advisor_fixtures._empty_state(),
                session_id=session_id,
            )

    assert primary.call_count == 1
    assert sdk.call_count == 0
    assert attempts == []
    assert len(prepared) == 1
    assert prepared[0][0] is replacement
    assert prepared[0][1] == payload

```

The accompanying source-census change is an application prerequisite: `scripts/cicd/composer_advisor_binding.py` and its complete executable-contract templates join the actual live adapter, once-only initializer, preparation, retained carriers and execution to the owned nominal admission. `composer_wire_census.py` selects that execution owner only after the receipt succeeds; partial extraction cannot fall back to a decoy monolith. Preserve the original unextracted input contract and all MODEL/READ/TAUGHT wire rows. The receipt binds actual cast/decoder/observer suppliers, the real trust-boundary wrapper, the pre-copy nominal wire validator and sent schema. It compares complete syntax with locations/comments excluded, so evaluated defaults/decorators, retained aliases/fields, lexical rebinding and direct owner escapes cannot disappear from a selected-name scan. It intentionally avoids copying whole service, Advisor and completion classes.

Apply the complete accompanying census source/test candidates together with this B2 amendment; do not run an older receipt against the nominal-call shape or edit its expected outputs to pass. Qualification must include the ordinary source-census controls and the existing full wire census tests, including malformed/unknown/optional argument forwarding, wrong receiver/subset, eager defaults/decorators, actual alias/field mutation, imports/nonlocal captures, supplier substitutions, destructive wrapped observers and validator substitution. Rebase the separate finite Reserve receipt to this exact intentional production change; earlier failed and qualified native windows remain historical evidence, not proof of these new bytes.

Run these focused tests through the root's existing frozen qualification checkout, followed by the affected whole-tree gates named in CONTRIBUTING:

```bash
python -m pytest tests/unit/web/composer/test_advisor_nominal_admission.py tests/unit/web/composer/test_continuation_batch_live.py tests/unit/web/composer/test_advisor_tool.py tests/unit/scripts/test_composer_advisor_binding.py tests/unit/scripts/test_composer_wire_census.py -n 0
```

No source review or focused unit result activates deferred SQL, read/writer, cold, physical or v2 owners.

## B3: Persist complete actual owners and enforce the public/private phase agreement

- [ ] Add these complete chunk/phase functions to `continuation_tool_batch.py`. The batch private codec is already in main's closed `PrivateChunkRef` vocabulary. Only a completely applied per-call boundary is `tool_ready`; dispatch/P4 phases never gain that label. A partial result/application cannot hide behind the public root's cheap property.

```python
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import encode_private_chunk, verify_private_chunk


def encode_batch_chunk(value: ContinuationBatchValue) -> PrivateChunk:
    if type(value) is not ContinuationBatchValue:
        raise TypeError("batch encoding requires the complete owned value")
    _state_from_tool_json(value.state_json)
    _state_from_tool_json(value.entry_state_json)
    if value.current_call is not None:
        _state_from_tool_json(value.current_call.pre_state_json)
    return encode_private_chunk(codec="composer.tool-batch.v1", canonical=canonical_json(value.model_dump(mode="json")))


def decode_batch_chunk(chunk: PrivateChunk) -> ContinuationBatchValue:
    if chunk.reference.codec != "composer.tool-batch.v1":
        raise AuditIntegrityError("batch frame selected another private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    parsed = _read_json_value(chunk.canonical)
    try:
        value = ContinuationBatchValue.model_validate_json(canonical_json(parsed), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("batch snapshot violates its closed complete codec") from exc
    if canonical_json(value.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("batch snapshot lost fields during decode")
    _state_from_tool_json(value.state_json)
    _state_from_tool_json(value.entry_state_json)
    if value.current_call is not None:
        _state_from_tool_json(value.current_call.pre_state_json)
    return value


def batch_public_phase(value: ContinuationBatchValue) -> Literal["tool_ready", "effect_reconciling", "result_retained"]:
    if value.phase == "call_ready":
        return "tool_ready"
    if value.phase == "call_active" and value.current_call is not None and value.current_call.phase == "prepared":
        return "effect_reconciling"
    return "result_retained"


def validate_batch_frame(frame: ToolBatchFrame, chunk: PrivateChunk) -> ContinuationBatchValue:
    if frame.accumulator != chunk.reference:
        raise AuditIntegrityError("batch frame substituted its complete private accumulator")
    value = decode_batch_chunk(chunk)
    if frame.phase != batch_public_phase(value) or frame.next_tool_index != value.next_tool_index:
        raise AuditIntegrityError("public batch phase disagrees with its complete actual private phase")
    return value
```

- [ ] In storage `yield_segment_on_connection`, after decoding its exact selected root and before any successor/checkpoint/attempt mutation, load its complete private chunks with the existing `load_private_chunks` and call `validate_batch_frame` for every `ToolBatchFrame` using its exact accumulator reference. Require the existing `root.is_handoff_safe`. A top batch must be at `call_ready`; a paused parent may instead have its exact admitted manual Advisor child at `input_ready`. This is a direct named read of actual chunks under the existing transaction, not reflection or caller assertion. Add the complete check below at that seam:

```python
from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
sid, operation_id = str(claim.session_id), claim.operation_id
root = frame
selected_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
by_ref = {item.reference: item for item in selected_chunks}
for index, batch_frame in enumerate(root.frames):
    if type(batch_frame) is ToolBatchFrame:
        value = validate_batch_frame(batch_frame, by_ref[batch_frame.accumulator])
        if index == len(root.frames)-1:
            if value.phase != "call_ready":
                raise AuditIntegrityError("batch handoff preceded its actual settled per-call boundary")
        else:
            child = root.frames[index+1]
            if index != len(root.frames)-2 or type(child) is not AdvisorFrame or child.phase != "input_ready":
                raise AuditIntegrityError("paused batch has no exact handoff-safe actual Advisor child")
            source = decode_continuation_advisor_invocation(by_ref[child.review])
            manual = (source.kind == "manual" and value.phase == "call_active" and value.current_call is not None
                and value.current_call.phase == "prepared" and value.current_call.advisor_invocation_json == by_ref[child.review].canonical)
            early = (source.kind == "early" and value.phase == "dispatch_ready" and not value.early_reviewed and value.current_call is None)
            if not (manual or early):
                raise AuditIntegrityError("paused batch differs from its actual complete manual/EARLY Advisor source")
```

- [ ] Add this exact complete bundle read to storage's private authority. It is required after actual SQL owner work because that transaction may create a result/frontier chunk that the old caller did not possess. A bundle contains the complete actual root and every selected private chunk from one current-authorized transaction. No caller frames are relabeled with a later checkpoint.

```python
@dataclass(frozen=True, slots=True)
class ContinuationSnapshotBundle:
    snapshot: StoredContinuationSnapshot
    chunks: tuple[PrivateChunk, ...]


def read_continuation_bundle(self, running: ComposerOperationRunning) -> ContinuationSnapshotBundle:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        snapshot = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        stack = decode_continuation_snapshot(snapshot.payload_json)
        return ContinuationSnapshotBundle(snapshot, load_private_chunks(conn,
            session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, stack=stack))
```

Put the dataclass in `sessions/continuation.py`, method in `ComposerAsyncOperationAuthority`, and import the nominal type there. No route exposes this bundle.

- [ ] Add the complete current execution owner below to `tool_batch.py`. Its authority is the SAME nominal actual `operation_authority` passed by C4 into `advance_continuation`, forwarded as `ctx.continuation_authority`. It owns checkpoint projection and original cancellation custody, but never owns the outer worker's cohort assertion or lease release. Import `run_required_sql_finish_once` from `web.async_workers`, `RequiredSQLFinishOnce`/`RequiredSQLRaised`/`RequiredSQLReturned` from `web.required_sql_outcomes`, and binding/source types from `web.required_work`; all result/projection checks use the actual registered SQL owner.

```python
@dataclass(slots=True)
class ContinuationBatchExecution:
    call_model: _CallModelOutcome
    ctx: ToolBatchContext
    acc: BatchAccumulator
    llm_messages: list[dict[str, Any]]
    batch: ContinuationBatchState
    stack: ContinuationFrameStack
    chunks: tuple[PrivateChunk, ...]
    checkpoint_id: str
    required_work: RequiredWorkBinding
    provider_owner: ProviderInvocationOwner | None
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list)

    @property
    def authority(self) -> ComposerAsyncOperationAuthority:
        value = self.ctx.continuation_authority
        if type(value) is not ComposerAsyncOperationAuthority:
            raise AuditIntegrityError("batch lost its actual nominal operation authority")
        return value

    def _running(self) -> ComposerOperationRunning:
        if (type(self.required_work) is not RequiredWorkBinding or type(self.required_work.running) is not ComposerOperationRunning
            or type(self.ctx.continuation_authority) is not ComposerAsyncOperationAuthority
            or self.required_work.running.session_operation_context != self.ctx.session_operation_context
            or self.ctx.continuation_materialization is None):
            raise AuditIntegrityError("batch execution lost its actual current authority/materialization owners")
        return self.required_work.running

    def _remember_cancellations(self, values: tuple[asyncio.CancelledError, ...]) -> None:
        for value in values:
            if all(value is not prior for prior in self.deferred_cancellations):
                self.deferred_cancellations.append(value)

    def failure_with_cancellations(self, primary: BaseException) -> BaseException:
        values = tuple(self.deferred_cancellations)
        self.deferred_cancellations.clear()
        return BaseExceptionGroup("Batch SQL failure and original cancellations", [primary, *values]) if values else primary

    def raise_deferred(self) -> None:
        values = tuple(self.deferred_cancellations)
        self.deferred_cancellations.clear()
        if len(values) == 1:
            raise values[0]
        if values:
            raise BaseExceptionGroup("Batch original cancellations", list(values))

    async def refresh_after_owner(self) -> None:
        from elspeth.web.sessions.continuation import ContinuationSnapshotBundle
        running = self._running()
        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
                                                          RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        outcome = await run_required_sql_finish_once(sql, self.authority.read_continuation_bundle, running)
        self.required_work.coordinator.validate_continuation_sql_outcome(ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome)
        self._remember_cancellations(outcome.deferred_cancellations)
        projection.begin_projection()
        if type(outcome) is RequiredSQLRaised:
            projection.complete_owned(outcome.error)
            raise self.failure_with_cancellations(outcome.error)
        if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not ContinuationSnapshotBundle:
            error = AuditIntegrityError("batch refresh received no actual complete SQL snapshot bundle")
            projection.complete_owned(error)
            raise self.failure_with_cancellations(error)
        bundle = outcome.value
        actual = decode_continuation_snapshot(bundle.snapshot.payload_json)
        prior_batch = tuple(frame for frame in self.stack.frames if type(frame) is ToolBatchFrame)
        actual_batch = tuple(frame for frame in actual.frames if type(frame) is ToolBatchFrame)
        if (len(prior_batch) != 1 or len(actual_batch) != 1
            or actual_batch[0].accumulator != prior_batch[0].accumulator):
            error = AuditIntegrityError("another batch phase advanced while an actual owner result was being joined")
            projection.complete_owned(error)
            raise self.failure_with_cancellations(error)
        self.stack, self.chunks, self.checkpoint_id = actual, bundle.chunks, bundle.snapshot.checkpoint_id
        projection.complete_owned()

    async def checkpoint(self, *, consumed_effect_key: str | None = None, defer_cancellation: bool = False,
                         retained_provider: tuple[str, str, PrivateChunkRef] | None = None,
                         admitted_primary: tuple[str, str, PrivateChunkRef] | None = None,
                         completed_memory: bool = False) -> None:
        from elspeth.web.composer.continuation_execution import capture_continuation_batch_owners, merge_continuation_chunks
        from elspeth.web.composer.continuation_tool_batch import encode_batch_chunk, batch_public_phase
        running = self._running()
        self.acc.state, self.acc.last_validation = self.batch.state, self.batch.last_validation
        self.acc.last_runtime_preflight, self.acc.advisor_calls_used = self.batch.last_runtime_preflight, self.batch.advisor_calls_used
        root, values = capture_continuation_batch_owners(ctx=self.ctx, acc=self.acc, llm_messages=self.llm_messages,
                                                        stack=self.stack, chunks=self.chunks)
        value = capture_continuation_batch_value(self.batch, self.call_model.completion.tool_batch.calls)
        chunk = encode_batch_chunk(value)
        top = root.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("batch checkpoint lost its actual top frame")
        frame = top.model_copy(update={"phase": batch_public_phase(value), "accumulator": chunk.reference,
                                      "next_tool_index": value.next_tool_index,
                                      "settled_effect_ids": (*top.settled_effect_ids, consumed_effect_key)
                                          if consumed_effect_key is not None and consumed_effect_key not in top.settled_effect_ids
                                          else top.settled_effect_ids})
        pending = root.pending_owner_results
        if consumed_effect_key is not None:
            matched = tuple(item for item in pending if item.effect_key == consumed_effect_key)
            if len(matched) != 1:
                raise AuditIntegrityError("result application consumed no exact retained actual owner")
            pending = tuple(item for item in pending if item.effect_key != consumed_effect_key)
        root = root.model_copy(update={"frames": (*root.frames[:-1], frame), "pending_owner_results": pending})
        values = merge_continuation_chunks(values, (chunk,))
        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                          RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        if admitted_primary is not None:
            if retained_provider is not None or consumed_effect_key is not None or completed_memory:
                raise AuditIntegrityError("received batch admission cannot consume another actual owner")
            key, digest, response = admitted_primary
            outcome = await run_required_sql_finish_once(sql, self.authority.commit_received_provider_capture, running,
                expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root), chunks=values,
                effect_key=key, input_sha256=digest, response=response, kind="batch_admitted")
        elif retained_provider is not None:
            if consumed_effect_key is not None:
                raise AuditIntegrityError("one checkpoint cannot consume unrelated SQL and provider owners")
            key, digest, response = retained_provider
            outcome = await run_required_sql_finish_once(sql, self.authority.commit_retained_provider_application, running,
                expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root), chunks=values,
                effect_key=key, input_sha256=digest, response=response)
        elif completed_memory:
            if consumed_effect_key is not None:
                raise AuditIntegrityError("memory application cannot consume another business owner")
            outcome = await run_required_sql_finish_once(sql, self.authority.commit_retained_memory_application, running,
                expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root), chunks=values)
        elif consumed_effect_key is None:
            outcome = await run_required_sql_finish_once(sql, self.authority.commit_transition, running,
                expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root), chunks=values,
                event=ContinuationEvent("tool_prepared", canonical_json({"phase": value.phase, "next_tool_index": value.next_tool_index})))
        else:
            outcome = await run_required_sql_finish_once(sql, self.authority.commit_owner_application, running,
                expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
                snapshot_json=encode_continuation_snapshot(root), chunks=values, effect_key=consumed_effect_key)
        self.required_work.coordinator.validate_continuation_sql_outcome(ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome)
        self._remember_cancellations(outcome.deferred_cancellations)
        projection.begin_projection()
        if type(outcome) is RequiredSQLRaised:
            projection.complete_owned(outcome.error)
            raise self.failure_with_cancellations(outcome.error)
        if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not StoredContinuationSnapshot:
            error = AuditIntegrityError("batch checkpoint did not return its actual committed SQL root")
            projection.complete_owned(error)
            raise self.failure_with_cancellations(error)
        self.stack, self.chunks, self.checkpoint_id = decode_continuation_snapshot(outcome.value.payload_json), values, outcome.value.checkpoint_id
        projection.complete_owned()
        if not defer_cancellation:
            self.raise_deferred()
```

F9 defines the concrete `merge_continuation_chunks` digest/codec/length checked merge in `continuation_execution.py`; use that single exact function. It is a pure owner-value merge, not an execution callback.

- [ ] Add this complete actual owner-application transaction to `ComposerAsyncOperationAuthority`. It permits mandatory application of an already committed effect after Stop/deadline without admitting a new business effect. Its selected old pending ref, actual SQL receipt/result chunk, current context and expected root are all verified before private bytes change. It preserves financial/action frontiers exactly and consumes only the selected actual owner result. Ordinary new-action checkpoints continue through the strict `commit_transition` guard.

```python
def commit_owner_application(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], effect_key: str,
) -> StoredContinuationSnapshot:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        selected = tuple(item for item in prior.pending_owner_results if item.effect_key == effect_key)
        if len(selected) != 1:
            raise AuditIntegrityError("result application has no exact actual pending owner")
        effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id,
                                                   effects.c.effect_key == effect_key)).one()
        if (effect.disposition != "committed" or effect.receipt_key is None
            or effect.result_chunk_key != f"value:{selected[0].result.codec}:{selected[0].result.digest}"):
            raise AuditIntegrityError("result application differs from its actual committed owner receipt")
        if (following.pending_owner_results != tuple(item for item in prior.pending_owner_results if item.effect_key != effect_key)
            or following.effect_frontier != prior.effect_frontier or following.pending_settlement_ids != prior.pending_settlement_ids
            or following.semantic_frontier != prior.semantic_frontier or following.active_action_json != prior.active_action_json
            or following.active_action_source != prior.active_action_source):
            raise AuditIntegrityError("pure result application changed another owner or financial/action frontier")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=snapshot_json, event_batch=(ContinuationEvent("effect_committed",
                canonical_json({"effect_key": effect_key, "phase": "owner_result_applied"})),), handoff=False)
```

This port updates private continuation state only; actual graph/proposal/interpretation/title/message writers still enforce their own nominal current-context mutation contracts. A caller cannot use this method to obtain a SQL graph witness or make a provider dispatch.

## B4: Retain the actual tool completion inside its business owner transaction

**Create:** `src/elspeth/web/composer/continuation_tool_receipts.py`.
**Modify:** `sessions/continuation.py`, `sessions/models.py`, `sessions/service.py`, `coordination/repository.py`, `composer/tool_batch.py`, `composer/session_tool.py`, `composer/audit.py` and their existing protocol declarations.

This is the integration refinement to storage C3.8b/e. The business result remains its existing nominal proposal/event/read record. A nullable private `tool_receipt_json` on the actual effect additionally retains the complete actual invocation and internal `ToolResultValue`. This closes the cut after the business commit and before the caller's `finish_success`; recovery never measures tool latency from its later wall clock. P4 itself has no tool completion and leaves this column NULL. No new public response or chunk codec is introduced.

- [ ] Add `Column("tool_receipt_json", Text, nullable=True)` to `effects` in the C2 table definition. Include that column in the exact reflected-shape schema expectation and epoch-79 schema fixture; it is not a second migration or an optional column on an existing incompatible continuation table. Add the two nullable fields below to both live `ContinuationCallState` and strict `ContinuationCallValue`, and pass them explicitly in both capture/restore constructors. Their defaults are `None`; no other call value is omitted.

```python
completion_json: str | None = None
owner_tool_receipt_json: str | None = None
```

- [ ] Add this complete closed value/receipt module. Import actual `CompositionProposalRecord` from `sessions.schemas`, and `InterpretationEventRecord`/`InterpretationSource` from `contracts.composer_interpretation`. All pure projection paths use the same actual full result representation as B1. The rate-cap branch retains the actual safe structured argument-error payload created by the real catch; it does not serialize an exception or infer a zero-use provider result.

```python
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus, PrivateToolAuthoring, ToolArgumentErrorCategory
from elspeth.contracts.composer_interpretation import InterpretationSource
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.audit import DispatchAudit, finish_arg_error, finish_success
from elspeth.web.composer.continuation_tool_batch import (
    ToolResultValue, _VALIDATION, _read_json_value, _state_from_tool_json,
    decode_tool_result_json, decode_tool_result_value, encode_tool_result_value,
)
from elspeth.web.composer.discovery_response import AdmittedDiscoveryResult
from elspeth.web.composer.tools._common import ToolResult


class ToolCompletionValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    projection: Literal["proposal", "interpretation", "interpretation_cap", "read"]
    tool_name: str
    state_json: str
    validation_json: str
    validation_snapshot_hash: str | None = None
    proposal_tool_name: str | None = None
    error_class: str | None = None
    error_category: ToolArgumentErrorCategory | None = None
    error_message: str | None = None
    error_payload_json: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuationToolCompletion:
    value: ToolCompletionValue
    audit: DispatchAudit
    private_authoring: PrivateToolAuthoring


class RetainedToolReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    effect_key: str
    tool_result: ToolResultValue | None
    invocation_json: str


_INVOCATION = TypeAdapter(ComposerToolInvocation)


def tool_result_canonical(result: ToolResult | AdmittedDiscoveryResult) -> str:
    from elspeth.web.composer.audit import _normalize_audit_payload
    return canonical_json(_normalize_audit_payload(result.to_dict()))


def decode_retained_tool_receipt(encoded: str) -> RetainedToolReceipt:
    value = RetainedToolReceipt.model_validate_json(encoded, strict=True)
    if canonical_json(value.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("retained tool receipt is not complete canonical JSON")
    invocation = _INVOCATION.validate_json(value.invocation_json, strict=True)
    if canonical_json(_INVOCATION.dump_python(invocation, mode="json")) != value.invocation_json:
        raise AuditIntegrityError("retained tool invocation lost actual fields")
    if value.tool_result is not None:
        result = decode_tool_result_value(value.tool_result)
        if (invocation.status is not ComposerToolStatus.SUCCESS
            or invocation.tool_name != value.tool_result.tool_name
            or invocation.version_after != result.updated_state.version
            or invocation.result_canonical != tool_result_canonical(result)):
            raise AuditIntegrityError("retained tool result differs from its actual invocation")
    elif invocation.status is not ComposerToolStatus.ARG_ERROR or invocation.result_canonical is None:
        raise AuditIntegrityError("retained non-result is not its actual argument-error completion")
    return value


def tool_result_from_owner(
    completion: ToolCompletionValue, *, kind: str, canonical_result: str,
) -> ToolResult | None:
    from elspeth.web.sessions.continuation_owner_results import (
        decode_interpretation_owner_result, decode_proposal_owner_result, decode_read_owner_result,
    )
    state = _state_from_tool_json(completion.state_json)
    validation = _VALIDATION.validate_json(completion.validation_json, strict=True)
    if completion.projection == "read" and kind == "read":
        retained = decode_read_owner_result(canonical_result)
        if retained.tool_name != completion.tool_name:
            raise AuditIntegrityError("read completion changed its actual tool")
        return decode_tool_result_json(retained.tool_result_json, pre_state=state)
    if completion.projection == "proposal" and kind == "proposal":
        proposal = decode_proposal_owner_result(canonical_result)
        if completion.proposal_tool_name != proposal.tool_name:
            raise AuditIntegrityError("proposal completion changed its actual proposed tool")
        return ToolResult(success=True, updated_state=state, validation=validation, affected_nodes=(), data={
            "status": "APPROVAL_REQUIRED", "proposal_id": str(proposal.id), "tool_name": proposal.tool_name,
            "summary": proposal.summary,
            "message": "The requested pipeline change is pending human approval and has not been applied.",
        })
    if completion.projection in ("interpretation", "interpretation_cap") and kind == "interpretation":
        event = decode_interpretation_owner_result(canonical_result)
        if completion.projection == "interpretation_cap":
            if event.interpretation_source is not InterpretationSource.AUTO_INTERPRETED_NO_SURFACES:
                raise AuditIntegrityError("rate-cap completion has another interpretation owner")
            if (completion.error_class != "ToolArgumentError" or completion.error_category is None
                or completion.error_message is None or completion.error_payload_json is None):
                raise AuditIntegrityError("rate-cap completion lost its actual safe rejection")
            _read_json_value(completion.error_payload_json)
            return None
        if event.interpretation_source is InterpretationSource.AUTO_INTERPRETED_OPT_OUT:
            if event.kind is None:
                raise AuditIntegrityError("opt-out interpretation completion lost its actual kind")
            return ToolResult(success=True, updated_state=state, validation=validation, affected_nodes=(),
                _validation_snapshot_hash=completion.validation_snapshot_hash, data={
                "_kind": "interpretation_review_suppressed_by_opt_out", "event_id": str(event.id),
                "kind": event.kind.value, "interpretation_source": event.interpretation_source.value,
                "interpretation_review_disabled": True,
                "message": "Interpretation review suppressed because this session has opted out.",
            })
        if event.affected_node_id is None or event.kind is None:
            raise AuditIntegrityError("pending interpretation completion lost its actual component/kind")
        return ToolResult(success=True, updated_state=state, validation=validation,
            _validation_snapshot_hash=completion.validation_snapshot_hash,
            affected_nodes=(event.affected_node_id,), data={
                "_kind": "interpretation_review_pending", "event_id": str(event.id),
                "kind": event.kind.value, "interpretation_source": event.interpretation_source.value,
                "message": "Interpretation review staged for user review. Waiting for user acceptance/amendment before the pipeline can finalise.",
            })
    raise AuditIntegrityError("business result has no matching closed tool projection")


def finish_owner_tool_receipt(
    completion: ContinuationToolCompletion, *, effect_key: str, kind: str, canonical_result: str,
) -> RetainedToolReceipt:
    if type(completion) is not ContinuationToolCompletion:
        raise TypeError("owner completion requires its actual nominal dispatch projection")
    if (completion.audit.tool_call_id != completion.private_authoring.tool_call_id
        or completion.audit.tool_name != completion.value.tool_name):
        raise AuditIntegrityError("owner completion changed its original actual dispatch identity")
    result = tool_result_from_owner(completion.value, kind=kind, canonical_result=canonical_result)
    if result is None:
        value = completion.value
        payload = _read_json_value(value.error_payload_json) if value.error_payload_json is not None else None
        if type(payload) is not dict or value.error_category is None or value.error_message is None or value.error_class is None:
            raise AuditIntegrityError("argument-error completion lost its actual safe payload")
        invocation = finish_arg_error(completion.audit, error_class=value.error_class,
            error_category=value.error_category, error_message=value.error_message, error_payload=payload)
        result_value = None
    else:
        invocation = finish_success(completion.audit, result_payload=result.to_dict(), version_after=result.updated_state.version)
        result_value = encode_tool_result_value(completion.value.tool_name, result)
    invocation = replace(invocation, private_authoring=completion.private_authoring)
    receipt = RetainedToolReceipt(effect_key=effect_key, tool_result=result_value,
        invocation_json=canonical_json(_INVOCATION.dump_python(invocation, mode="json")))
    return decode_retained_tool_receipt(canonical_json(receipt.model_dump(mode="json")))


def invocation_from_receipt(receipt: RetainedToolReceipt, *, audit: DispatchAudit) -> ComposerToolInvocation:
    invocation = _INVOCATION.validate_json(receipt.invocation_json, strict=True)
    if (invocation.tool_call_id != audit.tool_call_id or invocation.tool_name != audit.tool_name
        or invocation.arguments_canonical != audit.arguments_canonical or invocation.arguments_hash != audit.arguments_hash
        or invocation.authority_arguments_canonical != audit.authority_arguments_canonical
        or invocation.authority_arguments_hash != audit.authority_arguments_hash
        or invocation.started_at != audit.started_at or invocation.version_before != audit.version_before
        or invocation.actor != audit.actor or invocation.strict_sent != audit.strict_sent
        or invocation.wire_conformant != audit.wire_conformant):
        raise AuditIntegrityError("retained invocation differs from the actual prepared dispatch")
    return invocation
```

`ToolCompletionValue.validation_json` is the actual final normalized validation selected before the business writer: proposal uses the exact existing `candidate_prior_validation` / `last_validation` / current catalog expression; interpretation uses the actual current catalog normalization of the unchanged state; read uses the actual admitted `ToolResult.validation`. The SQL owner checks that this exact value was retained in its selected prepared call, rather than trusting a new projection supplied after recovery.

- [ ] Extend `ContinuationOwnerCommit` with `tool_completion: ContinuationToolCompletion | None = None`. Import the nominal type under `TYPE_CHECKING` in the dataclass owner and lazily in the verifier to avoid the existing service/codec cycle. Add this full verifier to `sessions/continuation.py`; call it from `require_owner_commit` immediately before returning its actual selected snapshot, as included in the complete final verifier tail below (one call at that return boundary). B1 stores `completion_json` before a tool effect is prepared. P4 has no completion and remains valid without a top per-call projection.

```python
def require_selected_tool_completion(conn: Connection, commit: ContinuationOwnerCommit, stored: StoredContinuationSnapshot) -> None:
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, capture_dispatch_audit
    from elspeth.web.composer.continuation_tool_receipts import ContinuationToolCompletion
    if commit.tool_completion is None:
        return
    if type(commit.tool_completion) is not ContinuationToolCompletion:
        raise AuditIntegrityError("SQL tool completion has no nominal current dispatch owner")
    root = decode_continuation_snapshot(stored.payload_json)
    if type(root.frames[-1]) is not ToolBatchFrame:
        raise AuditIntegrityError("SQL tool completion has no selected per-call frame")
    frame = root.frames[-1]
    values = load_private_chunks(conn, session_id=str(commit.running.claim.session_id),
        operation_id=commit.running.claim.operation_id, stack=root)
    selected = tuple(value for value in values if value.reference == frame.accumulator)
    if len(selected) != 1:
        raise AuditIntegrityError("SQL tool completion lost its exact selected accumulator")
    batch = decode_batch_chunk(selected[0])
    call = batch.current_call
    completion = commit.tool_completion
    if (call is None or call.audit is None or call.completion_json != canonical_json(completion.value.model_dump(mode="json"))
        or call.pre_state_json != completion.value.state_json
        or call.audit.model_copy(update={"elapsed_active_ns": 0}) != capture_dispatch_audit(completion.audit).model_copy(update={"elapsed_active_ns": 0})
        or completion.private_authoring not in TypeAdapter(tuple[PrivateToolAuthoring, ...]).validate_json(batch.private_authorings_json)):
        raise AuditIntegrityError("SQL tool completion differs from its selected prepared actual call")
```

- [ ] In `retain_owner_result_on_connection`, after validating the actual kind/result and before `put_chunk`, construct the complete invocation if supplied. Include `tool_receipt_json` in its existing `update(effects)` values. This computation executes inside that business transaction; a failure rolls back the proposal/event/read receipt and its private invocation together. Use the exact code below and leave P4's value NULL:

```python
tool_receipt_json = None
if commit.tool_completion is not None:
    from elspeth.web.composer.continuation_tool_receipts import finish_owner_tool_receipt
    actual_tool_receipt = finish_owner_tool_receipt(commit.tool_completion,
        effect_key=commit.effect_key, kind=kind, canonical_result=canonical_result)
    tool_receipt_json = canonical_json(actual_tool_receipt.model_dump(mode="json"))
```

The added update member is `tool_receipt_json=tool_receipt_json`. The existing full private owner result remains unchanged. No caller can later replace the invocation: its effect is already committed and `require_owner_commit` refuses a second write.

- [ ] Add this complete selected read facet to the same authority. `load_pending_owner_result` remains its existing two-tuple API for F7. The new read performs both reads under one locked current-authorized transaction by extracting C3.8b's entire method body into `_load_pending_owner_result_on_connection(conn,running,*,expected_checkpoint_id,expected_cursor,effect_key)`; its public wrapper opens the existing transaction and returns that exact helper. The body is unchanged except removal of the outer `with`. The new facet calls that actual helper inside its own held transaction.

```python
def load_pending_tool_owner_result(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int, effect_key: str,
) -> tuple[str, str, RetainedToolReceipt]:
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        kind, canonical = _load_pending_owner_result_on_connection(conn, running,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor, effect_key=effect_key)
        row = conn.execute(select(effects).where(effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id, effects.c.effect_key == effect_key)).one()
        if row.tool_receipt_json is None:
            raise AuditIntegrityError("committed tool owner has no actual invocation/result receipt")
        receipt = decode_retained_tool_receipt(row.tool_receipt_json)
        if receipt.effect_key != effect_key:
            raise AuditIntegrityError("committed tool receipt changed its stable actual effect identity")
        return kind, canonical, receipt
```

- [ ] Extend the actual rate-cap writer and protocol, `SessionServiceImpl.record_auto_interpreted_no_surfaces_event` and `_RepositoryInterpretationMutations.record_auto_interpreted_no_surfaces_event`, with optional `continuation: ContinuationOwnerCommit | None = None`, forwarding it through the existing `mutate` callback. In the repository method, after its existing `_require_active`, resolve the actual connection with `_resolve_mutation_connection(state._connection_token)` and call `require_owner_commit` before allocating/inserting. Change its final existing row conversion return to `result = <the same expression>` and append this exact block inside the same transaction:

```python
if continuation is not None:
    retain_owner_result_on_connection(connection, continuation, kind="interpretation", receipt_id=str(result.id),
        canonical_result=encode_interpretation_owner_result(result))
return result
```

Use its actual `self.__state` local as `state`, the same active connection/context access as C3.8d's neighboring `create_or_reconcile_pending`. The complete existing no-surfaces row shape, NULL surface fields, actual model provenance and immediate resolution are preserved. The `SessionToolOwner` catch computes its existing `error_message` and `_arg_error_payload(exc,tool_name)` before this writer and supplies the actual `interpretation_cap` completion through B5's owner binding. Its later audit/LLM error branch applies that exact retained invocation; it does not invoke `finish_arg_error` a second time. Telemetry remains operational and is not an execution receipt.

- [ ] Add the actual positive controls to the existing SQL owner tests in C3.8b/d/e: pass a selected complete `ToolCompletionValue`/actual `DispatchAudit`/actual private authoring in the prepared accumulator; commit each real writer; then decode `effects.tool_receipt_json` and require its complete invocation/result equality. The full new focused tests are in B7, including the after-commit/before-caller-finish cut, malformed/foreign prepared completion refusal with unchanged bytes, and rate-cap event once-only recovery.

- [ ] Add the distinct actual SQL committer fields below to the same C2 effect table. They record provenance, not authority to adopt another creator's uncommitted business SQL. A `session_sql` effect may commit only under its immutable preparing sequence/generation; its actual mutation and receipt share one transaction. Only storage C3.8's selected immutable `read` exception may retain a result under a different current sequence/generation, with fresh current authorization and version/response admission. Keep its original preparing coordinates and record the actual committer separately. A committed business result may be applied under a later authorized owner without executing the writer again. Do not change a stable effect key or remint a historical witness to evade the creator check.

```python-expression
Column("committer_sequence", Integer, nullable=True),
Column("committer_generation", Integer, nullable=True),
CheckConstraint("(committer_sequence IS NULL) = (committer_generation IS NULL)",
                name="ck_composer_effect_committer_pair"),
ForeignKeyConstraint(
    ["session_id", "operation_id", "committer_sequence", "committer_generation"],
    ["composer_segment_attempts.session_id", "composer_segment_attempts.operation_id",
     "composer_segment_attempts.sequence", "composer_segment_attempts.generation"],
    name="fk_composer_effect_committer_attempt",
),
```

The target is C2's actual `composer_segment_attempts` table. At main step 9, replace only `require_owner_commit`'s tail from its owner/disposition/input check through `return stored` with the complete tail below. Retain the preceding nominal context binding, exact-True mutation guard, unconditional actual history/segment six-field claim join, selected-head CAS, and `allow_completed_read` refusal for non-read owners. The preparing-attempt lookup and unset-committer checks supplement creator equality; they do not replace it. The foreign-creator branch is exclusively the existing selected-read exception. Its actual read retention still runs the C3.8e current version/response checks before writing. Install this refinement with the complete B1/C3.8c/read/writer/service/protocol dependency cluster, never as a partial writer activation:

```python
prepared_by = conn.execute(select(attempts).where(
    attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
    attempts.c.sequence == effect.creator_sequence, attempts.c.generation == effect.creator_generation,
)).one_or_none()
if (effect.owner not in ("session_sql", "read") or effect.disposition != "prepared"
    or effect.input_sha256 != commit.input_sha256 or prepared_by is None
    or effect.committer_sequence is not None or effect.committer_generation is not None):
    raise ContinuationCASConflict("SQL owner has no exact uncommitted prepared effect")
same_creator = (effect.creator_sequence, effect.creator_generation) == (claim.segment_sequence, claim.ownership_generation)
if not same_creator:
    if not allow_completed_read or effect.owner != "read":
        raise ContinuationCASConflict("business SQL effect changed its immutable preparing owner")
    require_selected_read_input_on_connection(conn, commit, stored)
require_selected_tool_completion(conn, commit, stored)
return stored
```

`retain_owner_result_on_connection` additionally sets `committer_sequence=commit.running.claim.segment_sequence` and `committer_generation=commit.running.claim.ownership_generation` in its actual effect update. A second writer cannot pass the already-committed disposition/CAS. In `validate_historical_effect_reference` change only the lookup defining `creator` to the actual committer lookup below, and separately verify the preparing attempt exists. Existing expected creator, exact state row creator/hash, receipt/result and parent checks remain unchanged. `ValidatedHistoricalEffectReference.creator_operation_id/epoch` describe the actual committed SQL writer.

```python
prepared_by = conn.execute(select(attempts).where(
    attempts.c.session_id == str(session_id), attempts.c.operation_id == operation_id,
    attempts.c.sequence == effect.creator_sequence, attempts.c.generation == effect.creator_generation,
)).one_or_none()
creator = conn.execute(select(attempts).where(
    attempts.c.session_id == str(session_id), attempts.c.operation_id == operation_id,
    attempts.c.sequence == effect.committer_sequence, attempts.c.generation == effect.committer_generation,
)).one_or_none() if effect.committer_sequence is not None and effect.committer_generation is not None else None
if prepared_by is None or creator is None or creator.session_operation_id != expected_creator_operation_id:
    raise AuditIntegrityError("historical graph effect lost its preparation or actual SQL committer")
```

Root C6.5 uses these committer fields for graph/read `original_attempt_id`; actual provider evidence continues to use its separately validated physical quota attempt. Proposal historical authority remains the proposal row's actual immutable attempt, never the parent's mutable current fence.

**Cold-cut policy:** before a business SQL commit, replacement of its preparing sequence/generation refuses the old prepared proposal, interpretation or P4 effect; an existing row and empty committer fields are not permission to execute it. After the actual business transaction committed its result/receipt, the pending-result path applies that committed value without another writer. The selected immutable read exception still requires the exact selected input and fresh current read authorization/version/response admission. B9's separately proved Stop refusal may close an uncommitted effect without a business write; it is not permission to retry it. Cross-creator execution of a still-prepared business SQL effect remains HOLD. Any broader recovery requirement conflicts with this policy and needs an explicit policy decision and complete actual-owner design, not a broader verifier, reminted effect or stand-in. Retain all subsequent owner bodies as integration material; none overrides this refusal.

- [ ] Add this complete public-private authority facade for new tool effect preparation. It returns the actual complete stored head; an existing same-key effect is never silently prepared again. B5 first inspects its selected root's effect frontier and either consumes the actual committed result or uses the already-prepared selected effect under the verifier above. Import `Literal` normally.

```python
def prepare_tool_action(
    self, running: ComposerOperationRunning, *, effect_key: str, input_sha256: str,
    owner: Literal["session_sql", "read"], snapshot_json: str,
    expected_checkpoint_id: str, expected_cursor: int,
    read_tool_name: str | None = None, read_tool_arguments_json: str | None = None,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.advisor_output import ReadContinuationAction
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    selected = decode_continuation_snapshot(snapshot_json)
    action = (decode_prescribed_continuation_action(selected.active_action_json)
              if selected.active_action_json is not None else None)
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return prepare_effect_on_connection(conn, running, effect_key=effect_key,
            input_sha256=input_sha256, owner=owner, next_snapshot_json=snapshot_json,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
            action_owner="read_tool" if owner == "read" and type(action) is ReadContinuationAction else None,
            read_tool_name=read_tool_name, read_tool_arguments_json=read_tool_arguments_json)
```

## B5: Drive one complete call without repeating its entry or actual writer

**Modify:** `composer/tool_batch.py`, `composer/session_tool.py`, `composer/audit.py`.

- [ ] Add the exact initialized entry below. The frames driver calls this once after retaining a complete admitted primary response. It appends/observes the assistant through B2's actual initializer once, creates a private accumulator and persists the complete owner graph before returning. The initial public frame is inserted by this function, so the driver never guesses an opaque accumulator. Import `dataclasses.replace`, `ToolBatchFrame`, `encode_batch_chunk`, `capture_continuation_batch_value` and the existing codec functions normally.

```python
async def start_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_execution import merge_continuation_chunks
    if (type(stack.frames[-1]) is not ComposeFrame or stack.frames[-1].phase != "response_retained"
        or stack.frames[-1].pending_response is None or stack.frames[-1].provider_effect_key is None
        or stack.frames[-1].provider_input_sha256 is None):
        raise AuditIntegrityError("batch entry has no selected complete primary response")
    parent = stack.frames[-1]
    batch = ContinuationBatchState(acc.state, acc.last_validation, acc.last_runtime_preflight, acc.advisor_calls_used, entry_state=acc.state)
    await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, llm_messages=llm_messages, batch=batch)
    value = capture_continuation_batch_value(batch, call_model.completion.tool_batch.calls)
    chunk = encode_batch_chunk(value)
    child = ToolBatchFrame(frame_kind="tool_batch", phase=batch_public_phase(value), response=parent.pending_response,
        accumulator=chunk.reference, next_tool_index=0, settled_effect_ids=())
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch,
        stack.model_copy(update={"frames": (*stack.frames, child)}), merge_continuation_chunks(chunks, (chunk,)),
        checkpoint_id, required_work, ctx.provider_owner)
    execution.ctx = replace(ctx, continuation=execution)
    await execution.checkpoint(admitted_primary=(parent.provider_effect_key,
        parent.provider_input_sha256, parent.pending_response))
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, None, batch.advisor_calls_used)
```

The `ToolBatchFrame` constructor uses main's exact fields; its `frame_kind` discriminator is `tool_batch`. Main's batch field `response` is the actual selected admitted completion chunk. This function does not start a provider call or reserve another user-message ID.

- [ ] Add this complete capture-only SQL facet to `ComposerAsyncOperationAuthority`. It is the narrow cancellation-tolerant owner for already received provider bookkeeping and initial issued-batch admission. It never authorizes a new action, handler, graph change, counter advance, effect or semantic decision. The shared C5.9 validator binds actual selected bytes, immutable physical creator and settled usage before any new private chunk is inserted.

```python
def commit_received_provider_capture(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], effect_key: str, input_sha256: str,
    response: PrivateChunkRef, kind: Literal["capture", "batch_admitted"],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
    from elspeth.web.sessions.continuation import require_retained_provider_application_on_connection
    if kind not in ("capture", "batch_admitted"):
        raise AuditIntegrityError("received provider capture has no closed owned purpose")
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=effect_key, input_sha256=input_sha256, response=response)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if following.model_copy(update={"frames": prior.frames}) != prior:
            raise AuditIntegrityError("received provider capture changed a logical/action/financial frontier")
        indexes = tuple(index for index, frame in enumerate(prior.frames) if type(frame) is ComposeFrame)
        if len(indexes) != 1:
            raise AuditIntegrityError("received provider capture has no exact paused compose owner")
        compose_index = indexes[0]
        extra = 1 if kind == "batch_admitted" else 0
        if len(following.frames) != len(prior.frames) + extra:
            raise AuditIntegrityError("received provider capture changed its actual child stack")
        old_compose, new_compose = prior.frames[compose_index], following.frames[compose_index]
        if type(new_compose) is not ComposeFrame:
            raise AuditIntegrityError("received provider capture replaced its nominal compose owner")
        stable = new_compose.model_copy(update={
            "iteration": old_compose.iteration, "messages": old_compose.messages,
            "evidence": old_compose.evidence, "audit_buffer": old_compose.audit_buffer,
            "transcript": old_compose.transcript, "caches": old_compose.caches,
        })
        if stable != old_compose or any(
            following.frames[index] != frame for index, frame in enumerate(prior.frames) if index != compose_index
        ):
            raise AuditIntegrityError("received provider capture changed state, counters, phase or another caller")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        actual_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        if kind == "batch_admitted":
            parent, child = prior.frames[-1], following.frames[-1]
            if (type(parent) is not ComposeFrame or parent.phase != "response_retained"
                or parent.pending_response != response or type(child) is not ToolBatchFrame
                or child.response != response or child.next_tool_index != 0 or child.settled_effect_ids):
                raise AuditIntegrityError("received batch did not append its original once-only unissued child")
            batch = validate_batch_frame(child, continuation_chunk(actual_chunks, child.accumulator))
            received = decode_continuation_response(continuation_chunk(actual_chunks, response))
            if (batch.next_tool_index != 0 or batch.current_call is not None or batch.tool_outcomes
                or batch.stop_issued_batch or batch.failure is not None
                or batch.phase != ("call_ready" if received.tool_batch.calls else "dispatch_ready")):
                raise AuditIntegrityError("received batch admission executed or dropped an actual issued call")
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("checkpoint", canonical_json({"phase": kind, "effect_key": effect_key})),), handoff=False)
```

The SQL transaction rolls back inserted chunks on any failed comparison. An identical repeated admission meets a changed selected head and fails CAS; cold restore selects the existing child instead of rerunning the initializer. Capture-only callers use `kind="capture"` with the same actual source tuple after the real provider audit/settlement owner finishes. No blanket allowance is added to `commit_transition`.

- [ ] Add this actual required-SQL projection helper as a method on `ContinuationBatchExecution`. It validates the registered owner before returning any value and preserves the original cancellation objects. Each caller below additionally requires its specific nominal result type. This is not a generic business callback or an execution-authority factory.

```python
def accept_sql_result[T](self, *, sql: RequiredWorkTicket, projection: RequiredWorkTicket,
                        source: RequiredWorkSource, outcome: RequiredSQLFinishOnce[T]) -> T:
    self.required_work.coordinator.validate_continuation_sql_outcome(ticket=sql,
        expected_source=source, actual_outcome=outcome)
    self._remember_cancellations(outcome.deferred_cancellations)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise self.failure_with_cancellations(outcome.error)
    if type(outcome) is not RequiredSQLReturned:
        error = AuditIntegrityError("batch SQL operation returned no registered actual outcome")
        projection.complete_owned(error)
        raise self.failure_with_cancellations(error)
    projection.complete_owned()
    return outcome.value
```

Import the actual ticket class from `required_work.py` (its existing source name is `RequiredWorkTicket`). Add `defer_cancellation: bool = False` to `checkpoint` and replace its final unconditional `self.raise_deferred()` with `if not defer_cancellation: self.raise_deferred()`. Mandatory projection of a completed result uses `True`; the complete per-call application checkpoint delivers the original cancellation afterward. A cancellation before a new business dispatch still propagates before that dispatch.

- [ ] Add these complete methods on that same actual execution owner. `prepare_owner` checkpoints the actual call projection before preparing a new business effect; `joined_owner` adopts the complete actual returned SQL head and its nominal receipt. An existing prepared session SQL effect is reusable only by its same immutable preparing sequence/generation. A different current creator may retain only a selected immutable read under `require_owner_commit`'s narrow exception, preparing-attempt/input checks and fresh read admission. A committed result takes the actual pending-result branch and never calls the writer again. The complete method bodies below do not grant cross-creator business SQL recovery; that cold cut remains HOLD.

```python
def actual_call(self) -> ContinuationCallState:
    call = self.batch.current_call
    if call is None or call.audit is None or call.arguments is None:
        raise AuditIntegrityError("continuation call has no complete actual prepared carrier")
    return call


async def prepare_owner(self, completion: ToolCompletionValue) -> ContinuationOwnerCommit:
    from elspeth.core.canonical import stable_hash
    from elspeth.web.composer.continuation_tool_receipts import ContinuationToolCompletion
    call = self.actual_call()
    if completion.tool_name != call.tool_name or completion.state_json != canonical_json(call.pre_state.to_dict()):
        raise AuditIntegrityError("business owner projection changed the original prepared call/state")
    encoded = canonical_json(completion.model_dump(mode="json"))
    if call.completion_json is not None and call.completion_json != encoded:
        raise AuditIntegrityError("business owner changed a retained actual completion projection")
    call.completion_json = encoded
    batch_frame = next(frame for frame in reversed(self.stack.frames) if type(frame) is ToolBatchFrame)
    call.effect_key = f"batch:{batch_frame.response.digest}:call:{call.index}"
    input_sha256 = stable_hash({"schema": "composer.tool-input.v1", "response": batch_frame.response.digest,
        "index": call.index, "call_id": call.tool_call.id, "arguments": call.audit.binding_arguments_hash,
        "completion": completion.model_dump(mode="json")})
    await self.checkpoint()
    frontier = tuple(item for item in self.stack.effect_frontier if item.effect_id == call.effect_key)
    if not frontier:
        sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                          RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        owner = "read" if completion.projection == "read" else "session_sql"
        outcome = await run_required_sql_finish_once(sql, self.authority.prepare_tool_action,
            self._running(), effect_key=call.effect_key, input_sha256=input_sha256, owner=owner,
            snapshot_json=encode_continuation_snapshot(self.stack), expected_checkpoint_id=self.checkpoint_id,
            expected_cursor=self.stack.cursor, read_tool_name=call.tool_name if owner == "read" else None,
            read_tool_arguments_json=canonical_json(call.arguments) if owner == "read" else None)
        value = self.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        if type(value) is not StoredContinuationSnapshot:
            raise AuditIntegrityError("tool preparation did not return its actual stored complete head")
        self.stack, self.checkpoint_id = decode_continuation_snapshot(value.payload_json), value.checkpoint_id
    elif len(frontier) != 1 or frontier[0].disposition != "prepared":
        raise AuditIntegrityError("business writer reached an already settled or ambiguous actual effect")
    authoring = self.batch.private_authorings_by_call.get(call.tool_call.id)
    if type(authoring) is not PrivateToolAuthoring:
        raise AuditIntegrityError("business owner lost its actual complete private authoring")
    from elspeth.web.composer.continuation_runtime import CurrentContinuationBoundary
    from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED
    sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
                                                      RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    observed = await run_required_sql_finish_once(sql, self.authority.observe_continuation_boundary,
        self._running(), expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor)
    boundary = self.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=observed)
    if type(boundary) is not CurrentContinuationBoundary:
        raise AuditIntegrityError("business preparation lost its actual current SQL Stop observation")
    self.raise_deferred()
    if boundary.cancel_requested:
        raise asyncio.CancelledError(COMPOSER_CANCEL_REQUESTED)
    return ContinuationOwnerCommit(self._running(), call.effect_key, input_sha256,
        self.checkpoint_id, self.stack.cursor, ContinuationToolCompletion(completion, call.audit, authoring))


async def load_actual_owner(self) -> None:
    from elspeth.web.composer.continuation_tool_receipts import RetainedToolReceipt, decode_retained_tool_receipt
    call = self.actual_call()
    if call.effect_key is None:
        raise AuditIntegrityError("pending tool owner has no stable actual effect identity")
    sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
                                                      RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    outcome = await run_required_sql_finish_once(sql, self.authority.load_pending_tool_owner_result,
        self._running(), expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor, effect_key=call.effect_key)
    value = self.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=outcome)
    if (type(value) is not tuple or len(value) != 3 or value[0] not in ("proposal", "read", "interpretation")
        or type(value[1]) is not str or type(value[2]) is not RetainedToolReceipt):
        raise AuditIntegrityError("tool owner read returned no actual closed receipt/result")
    call.owner_result_kind, call.owner_result_json = value[0], value[1]
    call.owner_tool_receipt_json = canonical_json(value[2].model_dump(mode="json"))
    receipt = decode_retained_tool_receipt(call.owner_tool_receipt_json)
    call.memory_result = decode_tool_result_value(receipt.tool_result, pre_state=call.pre_state) if receipt.tool_result is not None else None
    call.phase = "owner_result_retained"


async def joined_owner(self) -> None:
    await self.refresh_after_owner()
    await self.load_actual_owner()


async def retain_memory_completion(self, result: ToolResult | AdmittedDiscoveryResult) -> None:
    from elspeth.web.composer.continuation_tool_receipts import RetainedToolReceipt, _INVOCATION
    call = self.actual_call()
    nominal = result.result if type(result) is AdmittedDiscoveryResult else result
    if type(nominal) is not ToolResult:
        raise AuditIntegrityError("completed handler returned no actual nominal tool result")
    if call.owner_tool_receipt_json is not None:
        self.invocation_for_result(call.audit, result)
        return
    invocation = finish_success(call.audit, result_payload=result.to_dict(), version_after=nominal.updated_state.version)
    authoring = self.batch.private_authorings_by_call[call.tool_call.id]
    invocation = replace(invocation, private_authoring=authoring)
    receipt = RetainedToolReceipt(effect_key=f"memory:{call.tool_call.id}",
        tool_result=encode_tool_result_value(call.tool_name, nominal),
        invocation_json=canonical_json(_INVOCATION.dump_python(invocation, mode="json")))
    call.memory_result = result
    call.owner_tool_receipt_json = canonical_json(receipt.model_dump(mode="json"))
    call.phase = "application_pending"
    await self.checkpoint(defer_cancellation=True)


def invocation_for_result(self, audit: DispatchAudit, result: ToolResult | AdmittedDiscoveryResult) -> ComposerToolInvocation:
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt, invocation_from_receipt, tool_result_canonical
    call = self.actual_call()
    if call.owner_tool_receipt_json is None:
        raise AuditIntegrityError("completed tool lost its retained actual invocation")
    invocation = invocation_from_receipt(decode_retained_tool_receipt(call.owner_tool_receipt_json), audit=audit)
    if invocation.result_canonical != tool_result_canonical(result):
        raise AuditIntegrityError("caller application changed its retained actual tool completion")
    return invocation
```

- [ ] Add this complete pure selector on `ContinuationBatchExecution` and extend the original `if cache_key in discovery_cache:` condition to `if cache_key in discovery_cache and not (ctx.continuation is not None and ctx.continuation.prescribed_read_requires_owner()):`. Only an exact currently prescribed read bypasses the shortcut, because it needs the ordinary actual read owner/SQL action receipt. Repeated evidence still retains its same content/coverage premise; a new physical read does not manufacture semantic novelty.

```python
def prescribed_read_requires_owner(self) -> bool:
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action, ReadContinuationAction
    if self.stack.active_action_json is None:
        return False
    action = decode_prescribed_continuation_action(self.stack.active_action_json)
    call = self.actual_call()
    return (type(action) is ReadContinuationAction and action.tool_name == call.tool_name
            and action.arguments_json == canonical_json(call.arguments))
```

The complete private `completion_json` and `owner_tool_receipt_json` fields are copied explicitly by B1 capture/restore. `memory_result` has its existing `memory_admitted` discriminator. A retained in-memory graph result is private computation evidence; it does not create a fake committed SQL effect or satisfy semantic graph publication. P4 is still the actual graph writer.

- [ ] Define this complete unsupported-handler application, and invoke it in `resume_continuation_tool_batch` before entering B2's execution suffix. The classifier is storage's actual `continuation_tool_support`; it inspects the real effect domains/arguments. This guard is before inline blob custody, preview/execute, secret writes or any handler/preparer. Registered supported graph/catalog/read/interpretation/Advisor paths continue through their ordinary source branches.

```python
def _refuse_unsupported_continuation_call(*, execution: ContinuationBatchExecution) -> None:
    call, batch, ctx = execution.actual_call(), execution.batch, execution.ctx
    exc = ToolArgumentError(argument="tool_name", expected="a tool with a supported continuation effect owner",
        actual_type="unsupported continuation effect")
    payload = _arg_error_payload(exc, call.tool_name)
    ctx.recorder.record(finish_arg_error(call.audit, error_class=type(exc).__name__,
        error_category=exc.category, error_message=exc.args[0], error_payload=payload))
    _append_continuation_tool_outcome(batch=batch, call=call, response=None, error_class=type(exc).__name__,
        error_category=exc.category, error_message=exc.args[0], post_version=batch.state.version)
    ctx.anti_anchor.record_failure(call.tool_name, call.audit.arguments_hash, category=exc.category)
    encoded = json.dumps(payload)
    execution.llm_messages.append({"role": "tool", "tool_call_id": call.tool_call.id, "content": encoded,
        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(encoded)})
    batch.turn_has_mutation = True
    call.phase = "applied"
```

- [ ] In B2's `_execute_continuation_call_live`, define `_sync_batch_locals` as a local function containing its existing complete shared copy-out block (the exact assignments already listed in B2, without initializer-only assignments), plus `call.audit=audit`, `call.arguments=arguments`, `call.audit_arguments=audit_arguments`. Call it immediately before each new await into the continuation execution owner below. The original `finally` still executes the same complete copy-out. This local closure is never serialized. It captures the exact same actual values as that `finally`, not a separately reconstructed subset.

- [ ] At both actual proposal writer calls, immediately before allocating `pipeline_proposal`/calling `create_composition_proposal`, construct the complete actual proposal projection below after existing candidate/preflight/required-control finalization. Pass `continuation=proposal_commit` to the existing writer; leave every other writer argument unchanged. Immediately after the actual await returns, call `await ctx.continuation.joined_owner()` when v2 is active. Replace that branch's one `finish_success(...)` expression with `ctx.continuation.invocation_for_result(audit,proposal_result)` for v2, retaining the complete existing finish call for v1. The actual result and its retained tool receipt must compare equal; no timing is recomputed.

```python
proposal_commit = None
if ctx.continuation is not None:
    _sync_batch_locals()
    proposal_validation = (candidate_prior_validation if candidate_prior_validation is not None else
        last_validation if last_validation is not None else ctx.policy_catalog.validate_composition_state(state).validation)
    proposal_commit = await ctx.continuation.prepare_owner(ToolCompletionValue(projection="proposal",
        tool_name=tool_name, proposal_tool_name=proposal_tool_name, state_json=canonical_json(state.to_dict()),
        validation_json=canonical_json(_VALIDATION.dump_python(proposal_validation, mode="json"))))
```

- [ ] Add this early known-proposal application immediately inside B2's per-call loop before any candidate/custody preparation. It is reached only after B5's resume entry has loaded an actual committed pending proposal receipt under current authorization. Its body is the exact original successful proposal response branch, with the actual retained result replacing the freshly returned proposal. It does not call either proposal writer or its preparation code.

```python
if ctx.continuation is not None and call.owner_result_kind == "proposal":
    if type(call.memory_result) is not ToolResult:
        raise AuditIntegrityError("retained proposal has no actual complete tool result")
    proposal_result = call.memory_result
    recorder.record(ctx.continuation.invocation_for_result(audit, proposal_result))
    proposals_this_turn += 1
    _append_tool_outcome(response=proposal_result, error_class=None, error_category=None,
                         error_message=None, post_version=state.version)
    anti_anchor.record_success()
    tool_feedback_json = _serialize_tool_result(proposal_result)
    llm_messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_feedback_json,
        PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json)})
    turn_has_mutation = True
    continue
```

- [ ] Extend `audit.dispatch_with_audit` with optional `continuation: ContinuationBatchExecution | None = None` (TYPE_CHECKING import; lazy concrete import at actual use). Forward `ctx.continuation` from the existing batch call. Immediately after its existing `result = await do_dispatch()` add `if continuation is not None: await continuation.retain_memory_completion(result)`. This remains inside the original nested try, so all original exception primacy and the single `finally` record point remain intact. Replace only its SUCCESS construction of `invocation` with this full expression; preserve the existing surrounding `try/except` and all ARG_ERROR/CANCELLED/PLUGIN_CRASH branches unchanged:

```python
invocation = (continuation.invocation_for_result(audit, success_result) if continuation is not None else
    finish_success(audit, result_payload=_result_to_audit_payload(success_result), version_after=success_version_after))
```

- [ ] In `_do_dispatch` add the exact first branch below, before `_preproposal_exception`. It returns a previously retained actual computation/result through the original normal application code. That code still performs the existing fresh preflight/checkpoint protections before a new action, schema-disclosure/anti-anchor/cache updates, blob-page evidence registration, exact tool feedback, outcome append and classification. At the final successful handler return, retain read results through the actual read owner as shown. Add `continuation_read_observations=observations` to its actual `execute_tool` call. `observations` is created only for a supported actual read; pure graph calls pass `None`.

```python
if ctx.continuation is not None and call.memory_result is not None:
    return call.memory_result
observations = None
read_commit = None
if ctx.continuation is not None and continuation_tool_support(_tool_name, _arguments) == "read":
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadObservations
    observations = ContinuationReadObservations()
    _sync_batch_locals()
    read_commit = await ctx.continuation.prepare_owner(ToolCompletionValue(projection="read", tool_name=_tool_name,
        state_json=canonical_json(_state.to_dict()),
        validation_json=canonical_json(_VALIDATION.dump_python(_state.validate(), mode="json"))))
```

Replace the final discovery-admission/return span at original `_do_dispatch` lines 3052–3054 with the complete body below, after its unchanged incremental-mutation finalizer. It is not executed for an unsupported handler because the pre-dispatch classifier already refused it.

```python
if read_commit is not None:
    from elspeth.web.sessions.continuation_owner_results import build_read_owner_result
    retained = build_read_owner_result(tool_name=_tool_name, arguments=_arguments,
        result=dispatched_result, pre_state=_state, observations=observations)
    sql, projection = ctx.continuation.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                                 RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    outcome = await run_required_sql_finish_once(sql, ctx.continuation.authority.retain_read_owner_result,
                                                read_commit, result=retained)
    actual = ctx.continuation.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    if type(actual) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("read completion did not retain its actual SQL receipt")
    await ctx.continuation.joined_owner()
if is_discovery_tool(_tool_name) and response_contract_for(_tool_name) is not None:
    return admit_discovery_result(_tool_name, dispatched_result)
return dispatched_result
```

The `observations` narrowing must be explicit before `build_read_owner_result`: require `type(observations) is ContinuationReadObservations`, raising `AuditIntegrityError` otherwise. The selected read receipt captures exact immutable blob versions and actual H3c inspections. On pure application, call transport's complete `retain_read_attachment_history(stack=...,chunks=...,effect_key=...,result=decode_read_owner_result(call.owner_result_json))` and install its returned stack/chunks before the same `commit_owner_application` CAS consumes that result. Prior inspections are history only; current trust projection still uses the frames/producer owner.

- [ ] Add the complete resumption entry below. A warm/cold caller always starts from the selected private accumulator and complete actual owners. Each invocation advances at most one call; a ready boundary can yield before the next call. The `finally` copy-out in B2 supplies the actual executed prefix even on a live exception. A committed business result is always consumed after successful pure application and complete owner capture. The issued full tool tuple is preserved in `_DispatchOutcome`.

```python
async def resume_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.tools.continuation_support import continuation_tool_support
    if type(stack.frames[-1]) is not ToolBatchFrame:
        raise AuditIntegrityError("tool resume did not select its actual top batch")
    value = validate_batch_frame(stack.frames[-1], continuation_chunk(chunks, stack.frames[-1].accumulator))
    calls = call_model.completion.tool_batch.calls
    batch = restore_continuation_batch_value(value, calls)
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
                                            checkpoint_id, required_work, provider_owner)
    execution.ctx = replace(ctx, continuation=execution)
    execution._running()
    if batch.phase in ("dispatch_ready", "p4_pending", "p4_result_retained", "complete"):
        dispatch, used = _finish_continuation_batch_live(call_model=call_model, ctx=execution.ctx, acc=execution.acc, batch=batch)
        return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, dispatch, used)
    if batch.current_call is None:
        if batch.next_tool_index >= len(calls) or batch.stop_issued_batch:
            batch.phase = "dispatch_ready"
            await execution.checkpoint()
            dispatch, used = _finish_continuation_batch_live(call_model=call_model, ctx=execution.ctx, acc=execution.acc, batch=batch)
            return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, dispatch, used)
        call = ContinuationCallState(batch.next_tool_index, calls[batch.next_tool_index], batch.state, batch.state.version)
        batch.current_call = call
        prepared = await _prepare_continuation_call_live(call_model=call_model, ctx=execution.ctx,
            llm_messages=llm_messages, batch=batch, call=call)
        if prepared != "prepared":
            if prepared == "stop":
                batch.stop_issued_batch = True
            if len(batch.tool_outcomes) == batch.next_tool_index + 1:
                batch.next_tool_index += 1
            batch.current_call = None
            batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(calls) else "call_ready"
            await execution.checkpoint()
            return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, None, batch.advisor_calls_used)
        batch.phase = "call_active"
        await execution.checkpoint()
    call = execution.actual_call()
    if call.effect_key is not None and any(item.effect_key == call.effect_key for item in execution.stack.pending_owner_results):
        await execution.load_actual_owner()
    support = continuation_tool_support(call.tool_name, call.arguments)
    if support == "unsupported":
        _refuse_unsupported_continuation_call(execution=execution)
    else:
        advanced = await _execute_continuation_call_live(call_model=call_model, ctx=execution.ctx, acc=execution.acc,
            llm_messages=llm_messages, batch=batch, call=call)
        if advanced == "advisor_pending":
            return ContinuationToolBatchAdvance(execution.stack, execution.chunks,
                execution.checkpoint_id, None, batch.advisor_calls_used)
        if advanced != "applied":
            raise AuditIntegrityError("actual call returned an unknown owned advancement")
    if len(batch.tool_outcomes) != batch.next_tool_index + 1:
        raise AuditIntegrityError("completed call did not produce exactly its original outcome")
    consumed = call.effect_key if call.owner_result_kind is not None else None
    completed_memory = consumed is None and call.memory_result is not None
    if call.owner_result_kind == "read":
        from elspeth.web.composer.continuation_attachment_history import retain_read_attachment_history
        from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result
        if call.owner_result_json is None or call.effect_key is None:
            raise AuditIntegrityError("read application lost its actual retained owner result")
        execution.stack, execution.chunks = retain_read_attachment_history(stack=execution.stack,
            chunks=execution.chunks, effect_key=call.effect_key, result=decode_read_owner_result(call.owner_result_json))
    batch.next_tool_index += 1
    batch.current_call = None
    batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(calls) else "call_ready"
    await execution.checkpoint(consumed_effect_key=consumed, completed_memory=completed_memory)
    dispatch = None
    if batch.phase == "dispatch_ready":
        dispatch, _ = _finish_continuation_batch_live(call_model=call_model, ctx=execution.ctx, acc=execution.acc, batch=batch)
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, dispatch, batch.advisor_calls_used)
```

- [ ] Add this exact actual session callback method to `ContinuationBatchExecution`. Extend `SessionToolOwner._build_session_aware_kwargs` and `_dispatch_session_aware_tool` with optional `continuation_execution: ContinuationBatchExecution | None = None`, and forward it from the existing batch call. In `_build_session_aware_kwargs`, use `continuation_execution.create_pending_interpretation_event` for `create_pending` when non-null; otherwise retain the current complete `functools.partial`. The handler still owns all argument, credential, current-component, draft, duplicate and cap checks. A dedup/preflight return with no new writer takes the ordinary memory-completion checkpoint; it is not misrepresented as a newly inserted event.

```python
async def create_pending_interpretation_event(
    self, *, session_id: UUID, composition_state_id: UUID, affected_node_id: str,
    tool_call_id: str, user_term: str, kind: InterpretationKind, llm_draft: str,
    model_identifier: str | None, model_version: str | None, provider: str | None,
    composer_skill_hash: str | None,
) -> InterpretationEventRecord:
    call = self.actual_call()
    if session_id != self._running().claim.session_id or tool_call_id != call.tool_call.id:
        raise AuditIntegrityError("interpretation callback changed the actual selected request/call")
    if self.ctx.turn_sessions_service is None:
        raise AuditIntegrityError("interpretation callback lost its actual session service")
    validation = self.ctx.policy_catalog.validate_composition_state(call.pre_state).validation
    commit = await self.prepare_owner(ToolCompletionValue(projection="interpretation", tool_name=call.tool_name,
        state_json=canonical_json(call.pre_state.to_dict()),
        validation_json=canonical_json(_VALIDATION.dump_python(validation, mode="json")),
        validation_snapshot_hash=self.ctx.policy_catalog.snapshot.snapshot_hash))
    result = await self.ctx.turn_sessions_service.create_pending_interpretation_event(session_id=session_id,
        composition_state_id=composition_state_id, affected_node_id=affected_node_id, tool_call_id=tool_call_id,
        user_term=user_term, kind=kind, llm_draft=llm_draft, model_identifier=model_identifier,
        model_version=model_version, provider=provider, composer_skill_hash=composer_skill_hash,
        session_operation_context=self._running().session_operation_context, continuation=commit)
    await self.joined_owner()
    return result
```

- [ ] Immediately after actual `handler(**kwargs)` returns and `normalize_tool_result_validation` has run, call `await continuation_execution.retain_memory_completion(result)` if non-null, then replace the existing SUCCESS `finish_success` expression with `continuation_execution.invocation_for_result(audit,result)` for that branch. This compares the complete actual business result with its same-transaction receipt when a writer ran; otherwise it first checkpoints the real pure/preflight/dedup result. Keep the existing exact anti-anchor, response serialization/guard parts and `_SessionAwareDispatchOutcome` construction.

- [ ] In the real rate-cap catch, move only its existing safe `error_message` and `_arg_error_payload` assignments before the no-surfaces writer, then prepare the exact cap projection below and pass it as `continuation=cap_commit`. Keep its actual telemetry first and all existing writer provenance arguments unchanged. Join the actual owner immediately after the writer returns. The non-cap branch retains its ordinary `finish_arg_error` behavior.

```python
cap_commit = None
if continuation_execution is not None:
    validation = policy_catalog.validate_composition_state(state).validation
    cap_commit = await continuation_execution.prepare_owner(ToolCompletionValue(
        projection="interpretation_cap", tool_name=tool_name, state_json=canonical_json(state.to_dict()),
        validation_json=canonical_json(_VALIDATION.dump_python(validation, mode="json")),
        error_class=type(exc).__name__, error_category=exc.category,
        error_message=error_message, error_payload_json=canonical_json(arg_error_payload)))
```

After joining, use `invocation_from_receipt(decode_retained_tool_receipt(continuation_execution.actual_call().owner_tool_receipt_json),audit=audit)` at its one ARG_ERROR record point. Require non-null receipt JSON explicitly. This uses the actual completed invocation, including its original safe error payload and active latency; no recovered exception is constructed.

- [ ] At the beginning of `_dispatch_session_aware_tool`, before the missing-state check or handler/dedup/cap evaluation, add this complete known-owner branch. It only accepts a receipt previously loaded by B5's actual selected SQL facet. Its response application is the original successful/error suffix without rerunning an event writer or a handler. The nominal outcome contains no old lease, exception or callable.

```python
if continuation_execution is not None and continuation_execution.actual_call().owner_result_kind == "interpretation":
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt, invocation_from_receipt
    call = continuation_execution.actual_call()
    if call.owner_tool_receipt_json is None:
        raise AuditIntegrityError("retained interpretation has no actual invocation completion")
    receipt = decode_retained_tool_receipt(call.owner_tool_receipt_json)
    invocation = invocation_from_receipt(receipt, audit=audit)
    recorder.record(invocation)
    if receipt.tool_result is None:
        if invocation.error_category is None or invocation.error_class is None or invocation.error_message is None:
            raise AuditIntegrityError("retained cap rejection lost its actual argument-error fields")
        payload = _read_json_value(invocation.result_canonical)
        anti_anchor.record_failure(tool_name, audit.arguments_hash, category=invocation.error_category)
        encoded = json.dumps(payload)
        llm_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": encoded,
            PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(encoded)})
        return _SessionAwareDispatchOutcome(result=None, is_discovery=False,
            error_class=invocation.error_class, error_category=invocation.error_category,
            error_message=invocation.error_message, post_version=state.version)
    result = decode_tool_result_value(receipt.tool_result, pre_state=state)
    if result.updated_state.version > state.version:
        anti_anchor.record_success()
    encoded = _serialize_tool_result(result)
    llm_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": encoded,
        PROMPT_GUARD_CONTENT_PARTS_KEY: tool_result_content_parts(result, encoded)})
    return _SessionAwareDispatchOutcome(result=result, is_discovery=False,
        error_class=None, error_message=None, post_version=result.updated_state.version)
```

The invocation model's `error_category` is the actual nominal `ToolArgumentErrorCategory`, not a reconstructed string. `_read_json_value` takes a string, so require `invocation.result_canonical is not None` in the same explicit error-field check before decoding.

### B5.1: Pause and apply the actual manual Advisor phase

- [ ] Add this exact pure Stop projection to `continuation_tool_batch.py`. A merely prepared call has no completed outcome to publish. It may be abandoned only before any actual business/provider owner was prepared or any memory result was admitted. The completed prefix, original assistant turn, recorder and counters are retained unchanged. A call with an actual effect/result first reconciles and applies that result through the ordinary owner; Stop cannot erase it.

```python
def stop_closed_batch_value(before: ContinuationBatchValue) -> ContinuationBatchValue:
    if type(before) is not ContinuationBatchValue or before.stop_issued_batch or before.failure is not None:
        raise AuditIntegrityError("Stop did not select an open actual issued batch")
    if before.phase == "call_ready":
        if before.current_call is not None:
            raise AuditIntegrityError("ready Stop prefix contains an active call")
    elif before.phase == "call_active":
        call = before.current_call
        if (call is None or call.phase != "prepared" or call.effect_key is not None
            or call.owner_result_kind is not None or call.owner_result_json is not None
            or call.memory_admitted or call.memory_result is not None
            or call.completion_json is not None or call.owner_tool_receipt_json is not None):
            raise AuditIntegrityError("Stop cannot abandon an actual prepared owner or retained result")
    else:
        raise AuditIntegrityError("Stop must drain an unsafe application or P4 before closing the prefix")
    return before.model_copy(update={
        "phase": "dispatch_ready", "stop_issued_batch": True, "current_call": None,
    })
```

- [ ] Add this complete Stop-only SQL facet on `ComposerAsyncOperationAuthority`. It closes the actual settled call prefix without inventing outcomes for the unissued suffix. The actual durable parent cancellation, selected accumulator and exact two-field private change are verified under the same current-authorized transaction. It admits no handler or fresh business effect; P4 still runs over this actual prefix.

```python
def stop_continuation_batch(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    accumulator: PrivateChunk,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, stop_closed_batch_value, validate_batch_frame
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        cancelled = conn.execute(select(jobs.c.cancel_requested_at).where(jobs.c.session_id == sid,
            jobs.c.operation_id == operation_id)).scalar_one()
        if cancelled is None:
            raise AuditIntegrityError("Stop-only batch close has no actual parent cancellation")
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        top = root.frames[-1]
        manual = top if type(top) is AdvisorFrame else None
        batch_index = len(root.frames) - (2 if manual is not None else 1)
        if batch_index < 0 or type(root.frames[batch_index]) is not ToolBatchFrame:
            raise AuditIntegrityError("Stop-only batch close lost its actual selected prefix")
        batch_frame = root.frames[batch_index]
        values = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        selected = tuple(item for item in values if item.reference == batch_frame.accumulator)
        if len(selected) != 1:
            raise AuditIntegrityError("Stop-only batch close lost its exact private accumulator")
        before = validate_batch_frame(batch_frame, selected[0])
        if manual is not None:
            reviews = tuple(item for item in values if item.reference == manual.review)
            if len(reviews) != 1:
                raise AuditIntegrityError("Stop lost its actual manual Advisor source")
            invocation = decode_continuation_advisor_invocation(reviews[0])
            if (manual.phase != "input_ready" or manual.response is not None
                or manual.provider_effect_key is not None or manual.provider_input_sha256 is not None
                or invocation.kind != "manual" or before.current_call is None
                or before.current_call.tool_name != "request_advisor_hint"
                or before.current_call.advisor_invocation_json != reviews[0].canonical):
                raise AuditIntegrityError("Stop cannot discard a dispatched or unrelated Advisor child")
        after = decode_batch_chunk(accumulator)
        if after != stop_closed_batch_value(before):
            raise AuditIntegrityError("Stop-only close altered its actual completed prefix or started another call")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=(accumulator,))
        following = root.model_copy(update={"frames": (*root.frames[:batch_index], batch_frame.model_copy(update={
            "phase": "result_retained", "accumulator": accumulator.reference}))})
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=encode_continuation_snapshot(following), event_batch=(ContinuationEvent("tool_prepared",
                canonical_json({"phase": "stop_closed_issued_prefix", "completed_calls": before.next_tool_index})),), handoff=False)


async def stop_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation_execution import continuation_chunk, merge_continuation_chunks
    from elspeth.web.composer.continuation import AdvisorFrame
    from elspeth.web.composer.continuation_tool_batch import stop_closed_batch_value
    batch_index = len(stack.frames) - (2 if type(stack.frames[-1]) is AdvisorFrame else 1)
    if batch_index < 0 or type(stack.frames[batch_index]) is not ToolBatchFrame:
        raise AuditIntegrityError("Stop batch entry lost its actual selected frame")
    batch_frame = stack.frames[batch_index]
    value = validate_batch_frame(batch_frame, continuation_chunk(chunks, batch_frame.accumulator))
    batch = restore_continuation_batch_value(value, call_model.completion.tool_batch.calls)
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
        checkpoint_id, required_work, ctx.provider_owner)
    stopped = stop_closed_batch_value(value)
    chunk = encode_batch_chunk(stopped)
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                 RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    outcome = await run_required_sql_finish_once(sql, execution.authority.stop_continuation_batch,
        execution._running(), expected_checkpoint_id=checkpoint_id, expected_cursor=stack.cursor, accumulator=chunk)
    actual = execution.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    if type(actual) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("Stop batch SQL returned no actual durable closed prefix")
    batch.phase, batch.stop_issued_batch, batch.current_call = "dispatch_ready", True, None
    dispatch, used = _finish_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, batch=batch)
    execution.raise_deferred()
    return ContinuationToolBatchAdvance(decode_continuation_snapshot(actual.payload_json),
        merge_continuation_chunks(chunks, (chunk,)), actual.checkpoint_id, dispatch, used)
```

The async entry belongs to `tool_batch.py`; its SQL facet belongs to the authority. Frames invokes it only after actual `observe_continuation_boundary(...).cancel_requested` and before starting another issued call. Unsafe completed-result/P4 states first finish their owed projection. A real task cancellation after this transaction preserves the durable prefix for that owed recovery, with its original cancellation propagated after registered SQL projection.

- [ ] Add `advisor_invocation_json: str | None = None` to both call carriers and explicitly copy it in B1 capture/restore. Keep B0's call phase vocabulary: a prepared call with an actual `AdvisorFrame` above it is awaiting that child; no `tool_ready` boundary is claimed. Add the complete push method below to `ContinuationBatchExecution`. The actual source `advisor_calls_used += 1` remains before this push; the full paused batch is checkpointed with that exact increment before constructing the child from refreshed complete owner refs.

```python
async def push_manual_advisor(self, arguments: RequestAdvisorHintArgumentsModel) -> None:
    from elspeth.web.composer.continuation import AdvisorFrame
    from elspeth.web.composer.continuation_advisor import (
        make_continuation_manual_advisor_invocation, encode_continuation_advisor_invocation,
    )
    from elspeth.web.composer.continuation_execution import merge_continuation_chunks
    call = self.actual_call()
    await self.checkpoint()
    invocation = make_continuation_manual_advisor_invocation(arguments=arguments,
        tool_call_id=call.tool_call.id, stack=self.stack)
    review = encode_continuation_advisor_invocation(invocation)
    call.advisor_invocation_json = review.canonical
    await self.checkpoint()
    child = AdvisorFrame(phase="input_ready", review=review.reference, response=None,
        coverage_binding=invocation.coverage_binding, provider_effect_key=None, provider_input_sha256=None)
    following = self.stack.model_copy(update={"frames": (*self.stack.frames, child)})
    values = merge_continuation_chunks(self.chunks, (review,))
    sql, projection = self.required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                      RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    outcome = await run_required_sql_finish_once(sql, self.authority.commit_transition, self._running(),
        expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
        snapshot_json=encode_continuation_snapshot(following), chunks=values,
        event=ContinuationEvent("review_retained", canonical_json({"kind": "manual", "tool_call_id": call.tool_call.id})))
    selected = self.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("manual Advisor push returned no actual selected complete head")
    self.stack, self.chunks, self.checkpoint_id = decode_continuation_snapshot(selected.payload_json), values, selected.checkpoint_id
    self.raise_deferred()
```

- [ ] Insert the complete v2 branch below immediately before the original `remaining = _remaining_compose_seconds(deadline)` statement, after the actual argument and `budget` admission branches. Its increment belongs only to this branch; the existing later v1 `advisor_calls_used += 1` stays unchanged. This places the durable push outside the original provider `try/except Exception`: a SQL/custody failure cannot become `_AdvisorFirstPartyFailure`. When adding this actual child branch, change only `_execute_continuation_call_live`'s return annotation from `Literal["applied"]` to `Literal["applied", "advisor_pending"]`; retain its exact existing keyword-only `call_model`, `ctx`, `acc`, `llm_messages`, `batch` and `call` parameters and final `return "applied"`. The qualified B2-only fence deliberately stays `Literal["applied"]` until this branch exists. The v1 deadline/try/call/response source stays unchanged. C5's actual retained policy and staged clock govern the v2 child and the driver checks remaining segment fit before dispatch. A segment boundary does not turn a logical manual request into `COMPOSE_TIMEOUT`.

```python
if ctx.continuation is not None:
    advisor_calls_used += 1
    _sync_batch_locals()
    await ctx.continuation.push_manual_advisor(advisor_arg_error)
    return "advisor_pending"
```

Capture the returned value in B5 resume. If it is `advisor_pending`, return `ContinuationToolBatchAdvance(execution.stack,execution.chunks,execution.checkpoint_id,None,batch.advisor_calls_used)` immediately, before its completed-outcome postcondition/increment. The durable selected child is now owned by the frames driver; the initializer, argument admission and budget increment are not run again.

- [ ] Add this complete pure response-application owner to `tool_batch.py`. It contains the actual b003 manual response arms (source 2542–2724), with explicit aliases for every value those arms use and explicit copy-back for their reassigned locals. The safe request-rejection payload, original first-party exception, PromptGuard refusal, abandonment feedback and actual metadata remain intact. No provider, preparer or business writer occurs here.

```python
from elspeth.web.composer.advisor_abandonment import AdvisorAbandonmentReview


async def _apply_continuation_manual_outcome(
    *, ctx: ToolBatchContext, llm_messages: list[dict[str, Any]],
    batch: ContinuationBatchState, call: ContinuationCallState,
    advisor_outcome: _AdvisorProviderFailure | _AdvisorFirstPartyFailure | _AdvisorCallSuccess,
    abandonment_context: AdvisorAbandonmentReview | None,
) -> None:
    state, recorder, anti_anchor = batch.state, ctx.recorder, ctx.anti_anchor
    advisor_calls_used, budget = batch.advisor_calls_used, ctx.advisor_max_calls_per_compose
    advisor_failure, turn_has_discovery = batch.advisor_failure, batch.turn_has_discovery
    audit, tool_name = call.audit, call.tool_name
    if audit is None or tool_name != "request_advisor_hint":
        raise AuditIntegrityError("manual Advisor application lost its actual prepared dispatch")
    before, ran_to_end = len(batch.tool_outcomes), False

    def _append_tool_outcome(
        *, response: Any, error_class: str | None, error_category: ToolArgumentErrorCategory | None,
        error_message: str | None, post_version: int,
        _batch: ContinuationBatchState = batch, _call: ContinuationCallState = call,
    ) -> None:
        _append_continuation_tool_outcome(_batch, _call, response=response, error_class=error_class,
            error_category=error_category, error_message=error_message, post_version=post_version)

    try:
        for tool_call in (call.tool_call,):
            if type(advisor_outcome) is _AdvisorProviderFailure:
                if advisor_outcome.provider_failure is None:
                    advisor_error_payload = {
                        "status": "ADVISOR_ERROR", "error": "Advisor call failed; no guidance returned.",
                        "error_class": advisor_outcome.error_class, "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                    }
                    recorder.record(finish_success(audit, result_payload=advisor_error_payload, version_after=state.version))
                    _append_tool_outcome(response=advisor_error_payload, error_class=None, error_category=None,
                        error_message=None, post_version=state.version)
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(advisor_error_payload))
                else:
                    request_rejection_payload = {
                        "status": "ADVISOR_ERROR", "error": advisor_outcome.provider_failure.details.explanation,
                        "error_class": advisor_outcome.error_class, "budget_used": advisor_calls_used,
                        "budget_remaining": budget - advisor_calls_used,
                        "request_rejection": advisor_outcome.provider_failure.details.to_wire(),
                    }
                    recorder.record(finish_success(audit, result_payload=request_rejection_payload, version_after=state.version))
                    _append_tool_outcome(response=request_rejection_payload, error_class=None, error_category=None,
                        error_message=None, post_version=state.version)
                    anti_anchor.record_failure(tool_name, audit.arguments_hash)
                    tool_feedback_json = json.dumps(advisor_feedback_payload(request_rejection_payload))
                llm_messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": tool_feedback_json,
                    PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json)})
                turn_has_discovery = True
                continue
            if type(advisor_outcome) is _AdvisorFirstPartyFailure:
                first_party_exc = advisor_outcome.original_exc
                if isinstance(first_party_exc, PromptGuardRefused):
                    refusal_payload = {"success": False, "outbound_call_made": False, **first_party_exc.to_payload()}
                    recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                    _append_tool_outcome(response=refusal_payload, error_class=None, error_category=None,
                        error_message=None, post_version=state.version)
                    advisor_failure = first_party_exc
                    break
                recorder.record(finish_plugin_crash(audit, exc=first_party_exc))
                _append_tool_outcome(response=None, error_class=type(first_party_exc).__name__, error_category=None,
                    error_message=type(first_party_exc).__name__, post_version=state.version)
                advisor_failure = first_party_exc
                break
            if type(advisor_outcome) is not _AdvisorCallSuccess:
                raise AuditIntegrityError("Advisor call returned an unknown owned outcome")
            guidance, advisor_meta = advisor_outcome.guidance, advisor_outcome.metadata
            abandonment_decision, review_binding = None, None
            advisor_note = (
                "ADVICE only — call the appropriate mutation tool to "
                "apply any change. Do not echo this guidance back as "
                "configuration without verifying it against the schema."
            )
            if advisor_outcome.abandonment_response is not None:
                if abandonment_context is None or ctx.turn_evidence is None:
                    raise AuditIntegrityError("Abandonment decision lost its request-owned context")
                decision = advisor_outcome.abandonment_response
                ctx.turn_evidence.accept_abandonment(abandonment_context, decision,
                    current_state=state, plugin_snapshot=ctx.plugin_snapshot)
                abandonment_decision, review_binding, guidance = decision.decision, abandonment_context.binding, decision.reason
                if decision.next_action is not None:
                    guidance += "\nSupported next action: " + decision.next_action
                if decision.question is not None:
                    guidance += "\nQuestion for the user: " + decision.question
                if decision.decision == "STOP_CONFIRMED":
                    advisor_note = (
                        "Stop authoring this request: no remaining authorized useful action can proceed. Explain the confirmed limit "
                        "or ask the precise question; preserve the draft and do not claim completion. The final reply still receives ordinary END review."
                    )
                elif decision.decision == "CONTINUE_WITH_LIMITATION":
                    advisor_note = (
                        "Continue the supported work with the advisor's next action. Retain the precise limitation for final review; "
                        "it does not stop the whole request or establish pipeline completion or graph clearance."
                    )
                else:
                    advisor_note = "Continue with the advisor's supported action. This is not pipeline completion or graph clearance."
            success_payload = {
                "status": "SUCCESS", "guidance": guidance, "model": advisor_meta["model"],
                "prompt_tokens": advisor_meta["prompt_tokens"], "completion_tokens": advisor_meta["completion_tokens"],
                "cached_prompt_tokens": advisor_meta["cached_prompt_tokens"], "advisor_latency_ms": advisor_meta["latency_ms"],
                "budget_used": advisor_calls_used, "budget_remaining": budget - advisor_calls_used, "note": advisor_note,
                "abandonment_decision": abandonment_decision, "review_binding": review_binding,
            }
            recorder.record(finish_success(audit, result_payload=success_payload, version_after=state.version))
            _append_tool_outcome(response=success_payload, error_class=None, error_category=None,
                error_message=None, post_version=state.version)
            anti_anchor.record_success()
            tool_feedback_json = json.dumps(advisor_feedback_payload(success_payload))
            feedback_message = {"role": "tool", "tool_call_id": tool_call.id, "content": tool_feedback_json,
                PROMPT_GUARD_CONTENT_PARTS_KEY: json_content_parts(tool_feedback_json)}
            if abandonment_context is not None and ctx.turn_evidence is not None:
                ctx.turn_evidence.seal_abandonment_feedback(abandonment_context, feedback_message)
            llm_messages.append(feedback_message)
            continue
        else:
            ran_to_end = True
    finally:
        batch.advisor_failure, batch.turn_has_discovery = advisor_failure, turn_has_discovery
        batch.stop_issued_batch = batch.stop_issued_batch or not ran_to_end
    if len(batch.tool_outcomes) != before + 1:
        raise AuditIntegrityError("manual Advisor application produced no exact single actual outcome")
    call.phase = "applied"
```

- [ ] Add this complete retained manual entry. A failure before its final CAS leaves the actual SQL child in place for another pure application. The original response chunk stays selected until the outcome/history/budget/parent accumulator and child pop commit together. For cold successful responses, F13 returns the actual retained text, usage and latency; no original exception is reconstructed. Actual provider refusal/unknown/first-party-failure dispositions are handled by the frames physical owner before this completed-response entry.

```python
async def resume_continuation_manual_advisor(
    *, ctx: ToolBatchContext, acc: BatchAccumulator, llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...], checkpoint_id: str,
    required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation import AdvisorFrame
    from elspeth.web.composer.continuation_advisor import (
        decode_continuation_advisor_invocation, decode_continuation_manual_advisor_outcome,
    )
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    if len(stack.frames) < 2 or type(stack.frames[-1]) is not AdvisorFrame or type(stack.frames[-2]) is not ToolBatchFrame:
        raise AuditIntegrityError("manual Advisor application has no exact paused batch/child")
    child, frame = stack.frames[-1], stack.frames[-2]
    if (child.phase != "response_retained" or child.response is None
        or child.provider_effect_key is None or child.provider_input_sha256 is None):
        raise AuditIntegrityError("manual Advisor application lacks its actual selected physical response")
    source = decode_continuation_advisor_invocation(continuation_chunk(chunks, child.review))
    if source.kind != "manual" or source.manual_arguments_json is None:
        raise AuditIntegrityError("manual Advisor application selected another review kind")
    arguments = RequestAdvisorHintArgumentsModel.model_validate_json(source.manual_arguments_json, strict=True)
    call_model = _CallModelOutcome(decode_continuation_response(continuation_chunk(chunks, frame.response)))
    batch = restore_continuation_batch_value(validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator)),
                                            call_model.completion.tool_batch.calls)
    call = batch.current_call
    if call is None or call.tool_call.id != source.manual_tool_call_id or call.advisor_invocation_json != continuation_chunk(chunks, child.review).canonical:
        raise AuditIntegrityError("manual response changed its actual original call/source")
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
                                            checkpoint_id, required_work, provider_owner)
    execution.ctx = replace(ctx, continuation=execution)
    outcome = decode_continuation_manual_advisor_outcome(continuation_chunk(chunks, child.response), arguments=arguments)
    abandonment_context = None
    if arguments.trigger == ADVISOR_TRIGGER_CANNOT_COMPLETE:
        if ctx.turn_evidence is None or ctx.user_message_content is None:
            raise AuditIntegrityError("manual abandonment application lost its actual restored evidence")
        abandonment_context = ctx.turn_evidence.abandonment_context(request=ctx.user_message_content,
            current_state=batch.state, plugin_snapshot=ctx.plugin_snapshot, messages=llm_messages)
    await _apply_continuation_manual_outcome(ctx=execution.ctx, llm_messages=llm_messages, batch=batch, call=call,
        advisor_outcome=outcome, abandonment_context=abandonment_context)
    batch.next_tool_index += 1
    batch.current_call = None
    batch.phase = "dispatch_ready" if batch.stop_issued_batch or batch.next_tool_index == len(call_model.completion.tool_batch.calls) else "call_ready"
    execution.stack = stack.model_copy(update={"frames": stack.frames[:-1]})
    await execution.checkpoint(retained_provider=(child.provider_effect_key, child.provider_input_sha256, child.response))
    dispatch = None
    if batch.phase == "dispatch_ready":
        dispatch, _ = _finish_continuation_batch_live(call_model=call_model, ctx=execution.ctx, acc=execution.acc, batch=batch)
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id, dispatch, batch.advisor_calls_used)
```

- [ ] Extend `checkpoint` with `retained_provider: tuple[str,str,PrivateChunkRef] | None = None`. Its provider branch submits the complete captured next root/chunks to the exact authority method below instead of `commit_transition`; pass all three actual selected locator values. Normal new action checkpoints remain strict, and pending SQL owner application remains the existing distinct branch. This method authorizes only owed private application after cancellation/deadline. It verifies actual selected review, physical attempt and settled ledger and preserves the complete execution/financial/action frontier.

```python
def commit_retained_provider_application(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], effect_key: str, input_sha256: str, response: PrivateChunkRef,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import AdvisorFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.sessions.models import token_usage_ledger_table
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        child = prior.frames[-1]
        if (len(prior.frames) < 2 or type(child) is not AdvisorFrame or type(prior.frames[-2]) is not ToolBatchFrame
            or child.phase != "response_retained" or child.response != response
            or (child.provider_effect_key, child.provider_input_sha256) != (effect_key, input_sha256)
            or len(following.frames) != len(prior.frames)-1 or type(following.frames[-1]) is not ToolBatchFrame):
            raise AuditIntegrityError("provider application differs from its actual selected completed child")
        effect = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)).one()
        if (effect.owner != "provider" or effect.disposition != "committed" or effect.input_sha256 != input_sha256
            or effect.result_chunk_key != f"value:{response.codec}:{response.digest}" or effect.provider_attempt_id is None):
            raise AuditIntegrityError("provider application lacks its exact actual response effect")
        actual = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == effect.provider_attempt_id)).one()
        physical = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
        ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == actual.ledger_entry_id)).one_or_none()
        if (actual.settled_at is None or ledger is None or actual.attempt_id in prior.pending_settlement_ids
            or (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token) != (
                sid, physical.session_operation_id, physical.session_operation_epoch, physical.session_operation_lease_token)
            or (ledger.identity_id, ledger.session_id, ledger.source, ledger.run_id) != (actual.identity_id, sid, actual.source, actual.run_id)):
            raise AuditIntegrityError("provider application has no exact settled actual physical ledger join")
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode("utf-8", errors="strict")
        retained = decode_continuation_response(PrivateChunk(response, canonical))
        metadata, usage = retained.provider_metadata, retained.provider_metadata.usage
        if (ledger.model != (metadata.model_returned or effect.requested_model)
            or (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) != (
                usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens)):
            raise AuditIntegrityError("provider application changed the actual retained usage/model ledger")
        if (following.effect_frontier != prior.effect_frontier or following.pending_settlement_ids != prior.pending_settlement_ids
            or following.pending_owner_results != prior.pending_owner_results or following.semantic_frontier != prior.semantic_frontier
            or following.active_action_json != prior.active_action_json
            or following.active_action_source != prior.active_action_source):
            raise AuditIntegrityError("pure provider application changed another execution/action owner")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("review_retained", canonical_json({"effect_key": effect_key, "phase": "advisor_applied"})),), handoff=False)
```

- [ ] Add this complete private memory-result application facet beside the provider one. A pure handler result can finish immediately before Stop; owed tool feedback/audit/evidence application must not be stranded by the new-action guard. This validates the already-stored complete result and exact prefix advancement, permits only private projection, and preserves every business/provider/action frontier. It cannot admit a new graph write, provider action or prescribed action receipt.

```python
def commit_retained_memory_application(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if (type(prior.frames[-1]) is not ToolBatchFrame or type(following.frames[-1]) is not ToolBatchFrame
            or len(prior.frames) != len(following.frames) or prior.frames[-1].response != following.frames[-1].response
            or prior.logical != following.logical or prior.policy_hash != following.policy_hash
            or prior.admission_policy_json != following.admission_policy_json):
            raise AuditIntegrityError("memory application changed its actual selected logical batch")
        old_chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=prior)
        old_by_ref, new_by_ref = {item.reference: item for item in old_chunks}, {item.reference: item for item in chunks}
        before = decode_batch_chunk(old_by_ref[prior.frames[-1].accumulator])
        after = decode_batch_chunk(new_by_ref[following.frames[-1].accumulator])
        call = before.current_call
        if (call is None or call.memory_result is None or call.owner_result_kind is not None
            or call.owner_tool_receipt_json is None or after.current_call is not None
            or after.next_tool_index != before.next_tool_index + 1
            or after.tool_outcomes[:-1] != before.tool_outcomes or not after.tool_outcomes
            or after.tool_outcomes[-1].call_id != call.call_id or after.tool_outcomes[-1].pre_version != call.pre_version
            or after.state_json != call.memory_result.state_json):
            raise AuditIntegrityError("memory application did not consume its exact retained completed prefix")
        receipt = decode_retained_tool_receipt(call.owner_tool_receipt_json)
        if receipt.tool_result != call.memory_result:
            raise AuditIntegrityError("memory application changed its retained actual invocation/result")
        if (following.effect_frontier != prior.effect_frontier or following.pending_settlement_ids != prior.pending_settlement_ids
            or following.pending_owner_results != prior.pending_owner_results or following.semantic_frontier != prior.semantic_frontier
            or following.active_action_json != prior.active_action_json
            or following.active_action_source != prior.active_action_source):
            raise AuditIntegrityError("pure memory application changed an actual execution/action owner")
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("tool_prepared", canonical_json({"phase": "memory_result_applied", "call_id": call.call_id})),), handoff=False)
```

Source `chunks` is B3's complete merged selected bundle, so the new accumulator and all prior refs are present and digest-verified by the loaders.

## B6: Commit and apply P4 once, including its private audit cohort

**Modify:** `composer/turn_audit.py`, `composer/service.py`, `sessions/service.py`, `sessions/protocol.py`, `sessions/continuation_owner_results.py`, `sessions/continuation.py` and the B1 codecs.

`ContinuationToolBatchAdvance.persist` is the actual `_PersistOutcome` returned here. The frames driver uses its exact current-state/assistant/content/failed-turn/verification disposition, not row counts or inferred `ToolResult` fields. Its default `None` preserves every B0 batch constructor.

### B6.1: Retain the complete pure P4 input and actual unavailable disposition

#### First executable prerequisite: actual StatePayload value retention

The complete P4/batch/F7 cluster remains HOLD. Before installing any `decode_prepared_p4`/`decode_batch_chunk` consumer, close exact retained batch state identities and actual accumulator revocation, complete current P4 input/result owners, and selected response-domain credential admission. A generic scan of serialized P4 results incorrectly classifies real FieldMapper labels and plugin-schema property names as credential values; filtered diagnostic equality alone does not prove their raw retained source is safe. Do not activate the older sketches below or waive those fields.

This first complete pure subunit owns only the actual `StatePayload` and `CompositionStateData` value boundary. It retains all nullable SQL columns, validation records, lineage, constructor-frozen mappings and ordered arrays. The actual source-constructor alias is retained in its normalized owned `sources` field. It uses the existing component-aware credential owner and explicit current environment-reference inventory. The temporary graph's inert metadata/version exist only for credential inspection; they are never returned as state, assigned as a SQL version, or treated as authority. Original metadata is separately checked and retained exactly, including None versus an empty object.

- [ ] Add `src/elspeth/web/composer/continuation_p4_state_payload.py` with the complete body below. No live caller or SQL writer changes in this subunit.

```python
"""Complete owned P4 state-payload values, with no SQL or restored authority.

SQL allocates composition-state versions. The temporary graph used solely by
credential admission never becomes a returned state or persistence input.
"""

from pydantic import TypeAdapter, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_tool_batch import _read_json_value
from elspeth.web.composer.state import CompositionState
from elspeth.web.credential_guard import (
    credential_material_tool_arguments_projection,
    require_no_credential_material,
    require_no_credential_material_in_state,
)
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.protocol import CompositionStateData, serialize_composition_validation_errors

_P4_PAYLOAD = TypeAdapter(StatePayload)


def _encode_p4_state_payload(payload: StatePayload) -> str:
    if type(payload) is not StatePayload or type(payload.data) is not CompositionStateData:
        raise TypeError("P4 retention requires the actual state payload")
    data = payload.data
    # Admit the frozen JSON algebra before any serializer can normalize it.
    return canonical_json({
        "data": {
            "sources": owned_json(data.sources),
            "nodes": owned_json(data.nodes),
            "edges": owned_json(data.edges),
            "outputs": owned_json(data.outputs),
            "metadata_": owned_json(data.metadata_),
            "is_valid": data.is_valid,
            "validation_errors": owned_json(serialize_composition_validation_errors(data.validation_errors)),
            "composer_meta": owned_json(data.composer_meta),
        },
        "derived_from_state_id": payload.derived_from_state_id,
    })


def decode_p4_state_payload(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> StatePayload:
    _read_json_value(encoded)
    try:
        payload = _P4_PAYLOAD.validate_json(encoded, strict=True)
    except (ValidationError, TypeError, ValueError) as error:
        raise AuditIntegrityError("retained P4 state payload violates its owned contract") from error
    if _encode_p4_state_payload(payload) != encoded:
        raise AuditIntegrityError("retained P4 state payload lost or normalized fields")
    # The payload owns nullable SQL columns, not a caller-allocated state version.
    # Project only its graph for the existing component-aware credential guard.
    data = payload.data
    graph = {
        "version": 1,
        "sources": owned_json(data.sources) if data.sources is not None else {},
        "nodes": owned_json(data.nodes) if data.nodes is not None else [],
        "edges": owned_json(data.edges) if data.edges is not None else [],
        "outputs": owned_json(data.outputs) if data.outputs is not None else [],
        "metadata": {"name": "Untitled Pipeline", "description": ""},
    }
    require_no_credential_material(
        credential_material_tool_arguments_projection("set_pipeline", graph),
        surface="continuation P4 graph", env_ref_names=env_ref_names,
    )
    require_no_credential_material(
        {"metadata": owned_json(data.metadata_),
         "validation_errors": owned_json(serialize_composition_validation_errors(data.validation_errors)),
         "derived_from_state_id": payload.derived_from_state_id},
        surface="continuation P4 non-graph state fields", env_ref_names=env_ref_names,
    )
    try:
        credential_state = CompositionState.from_dict(graph)
    except (KeyError, TypeError, ValueError) as error:
        raise AuditIntegrityError("retained P4 graph violates its composition contract") from error
    require_no_credential_material_in_state(credential_state, surface="continuation P4 state", env_ref_names=env_ref_names)
    require_no_credential_material(owned_json(data.composer_meta), surface="continuation P4 state metadata", env_ref_names=env_ref_names)
    return payload



def encode_p4_state_payload(payload: StatePayload, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    encoded = _encode_p4_state_payload(payload)
    decode_p4_state_payload(encoded, env_ref_names=env_ref_names)
    return encoded
```

- [ ] Add `tests/unit/web/composer/test_continuation_p4_state_payload.py` below. Qualify the ordinary unit module, existing ToolResult/bootstrap/credential tests, actual canonical static/contract gates and affected original architecture gates with root-owned execution. No tests have been executed by the source author.

```python
"""Actual P4 payload retention; no partial F7 or database owner."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_p4_state_payload import decode_p4_state_payload, encode_p4_state_payload
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.protocol import CompositionStateData, CompositionValidationError


def actual_payload():
    return StatePayload(
        CompositionStateData(
            sources={},
            nodes=({"id": "rename", "node_type": "transform", "plugin": "field_mapper", "input": "rows",
                    "on_success": "output", "on_error": "discard",
                    "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}}},),
            edges=({"id": "edge", "from_node": "source", "to_node": "rename", "edge_type": "on_success", "label": None},),
            outputs=({"name": "output", "plugin": "database", "options": {"url": "${DB_URL}"}, "on_write_failure": "discard"},),
            metadata_={"name": "exact name", "description": "café"},
            is_valid=False,
            validation_errors=(CompositionValidationError(message="exact message", error_code="schema_contract", component="rename"),),
            composer_meta={"validation_lane": "authoring", "nested": {"ordered": ["b", "a"]}},
        ),
        derived_from_state_id="actual-parent-state",
    )


def test_complete_actual_payload_preserves_frozen_values_and_sql_lineage():
    original = actual_payload()
    inventory = frozenset({"DB_URL"})
    encoded = encode_p4_state_payload(original, env_ref_names=inventory)
    restored = decode_p4_state_payload(encoded, env_ref_names=inventory)
    assert type(restored) is StatePayload
    assert type(restored.data) is CompositionStateData
    assert restored == original
    assert encode_p4_state_payload(restored, env_ref_names=inventory) == encoded
    assert restored.derived_from_state_id == "actual-parent-state"
    assert restored.data.nodes[0]["options"]["mapping"]["customer_key"] == "customer_id"
    assert restored.data.composer_meta["nested"]["ordered"] == ("b", "a")
    assert restored.data.validation_errors == (CompositionValidationError(message="exact message", error_code="schema_contract", component="rename"),)
    assert "version" not in json.loads(encoded)
    assert "version" not in json.loads(encoded)["data"]
    with pytest.raises(TypeError):
        restored.data.nodes[0]["options"]["mapping"]["customer_key"] = "changed"
    with pytest.raises(TypeError):
        restored.data.composer_meta["nested"]["ordered"] = ()


def test_nullable_sql_columns_remain_distinct_from_empty_containers():
    absent = StatePayload(CompositionStateData())
    empty = StatePayload(CompositionStateData(sources={}, nodes=(), edges=(), outputs=(), metadata_={}, validation_errors=(), composer_meta={}))
    assert decode_p4_state_payload(encode_p4_state_payload(absent)) == absent
    # Empty metadata is retained by the SQL input DTO but is not a complete
    # CompositionState metadata object. This codec must preserve that valid SQL
    # value while using the current credential owner only for field inspection.
    assert decode_p4_state_payload(encode_p4_state_payload(empty)) == empty
    assert encode_p4_state_payload(absent) != encode_p4_state_payload(empty)


def test_legacy_source_constructor_alias_is_retained_as_actual_owned_sources():
    payload = StatePayload(CompositionStateData(source={"plugin": "csv", "options": {"path": "rows.csv"}, "on_success": "rows", "on_validation_failure": "discard"}))
    encoded = encode_p4_state_payload(payload)
    assert decode_p4_state_payload(encoded) == payload
    assert "source" not in json.loads(encoded)["data"]
    assert json.loads(encoded)["data"]["sources"] == {"source": dict(payload.data.sources["source"])}


@pytest.mark.parametrize("member", ["sources", "nodes", "edges", "outputs", "metadata_", "validation_errors", "composer_meta", "is_valid"])
def test_missing_defaulted_owned_field_cannot_silently_normalize(member):
    encoded = encode_p4_state_payload(actual_payload(), env_ref_names=frozenset({"DB_URL"}))
    value = json.loads(encoded)
    del value["data"][member]
    with pytest.raises(AuditIntegrityError, match="lost or normalized"):
        decode_p4_state_payload(canonical_json(value), env_ref_names=frozenset({"DB_URL"}))


def test_unknown_fields_duplicate_keys_noncanonical_bytes_and_scalar_coercion_refuse():
    encoded = encode_p4_state_payload(actual_payload(), env_ref_names=frozenset({"DB_URL"}))
    unknown = json.loads(encoded)
    unknown["data"]["unknown"] = True
    coerced = json.loads(encoded)
    coerced["data"]["is_valid"] = 1
    for candidate in (canonical_json(unknown), canonical_json(coerced), encoded + " ", '{"data":{},' + encoded[1:]):
        with pytest.raises(AuditIntegrityError):
            decode_p4_state_payload(candidate, env_ref_names=frozenset({"DB_URL"}))


def test_exact_inventory_reference_is_admitted_and_raw_database_credentials_refuse():
    payload = actual_payload()
    encoded = encode_p4_state_payload(payload, env_ref_names=frozenset({"DB_URL"}))
    with pytest.raises(CredentialMaterialRefused):
        decode_p4_state_payload(encoded)
    with pytest.raises(CredentialMaterialRefused):
        decode_p4_state_payload(encoded, env_ref_names=frozenset({"OTHER_URL"}))
    unsafe = replace(payload, data=replace(payload.data, outputs=({"name": "output", "plugin": "database", "options": {"url": "postgresql://user:password@example.test/db"}, "on_write_failure": "discard"},)))  # secret-scan: allow-this-line
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(unsafe, env_ref_names=frozenset({"DB_URL"}))


@pytest.mark.parametrize("where", ["composer_meta", "metadata_", "node_options"])
def test_field_label_projection_does_not_waive_other_credential_slots(where):
    payload = actual_payload()
    if where == "composer_meta":
        data = replace(payload.data, composer_meta={"password": "synthetic-material"})
    elif where == "metadata_":
        data = replace(payload.data, metadata_={"name": "retained", "description": "", "password": "synthetic-material"})
    else:
        node = dict(payload.data.nodes[0])
        node["options"] = dict(node["options"]) | {"password": "synthetic-material"}
        data = replace(payload.data, nodes=(node,))
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(replace(payload, data=data), env_ref_names=frozenset({"DB_URL"}))


def test_actual_database_declared_slot_rejects_opaque_nonreference_material():
    payload = actual_payload()
    output = dict(payload.data.outputs[0])
    output["options"] = {"url": "synthetic-opaque-material"}
    unsafe = replace(payload, data=replace(payload.data, outputs=(output,)))
    with pytest.raises(CredentialMaterialRefused):
        encode_p4_state_payload(unsafe)
```

The two finite omission controls are the exact inner payload re-encode equality and the current component-aware credential call. Unknown/defaulted-field negatives must detect the first; the declared database `url` slot (using an unrecognized opaque value) must detect the second. Keep the ordinary recognized-credential negative too: that remains independently refused by the generic projected-graph scan and must not be misreported as an isolated component-guard kill. Restore exact normal source before qualification.

#### Held full prepared-P4 and result-receipt integration

The following prior full-P4 sketches remain uninstalled integration material. They must be revised against actual current owners before use; this narrow subunit does not make their callable dependencies available.


- [ ] Add `p4_input_json: str | None = None` to the live and strict batch carriers and pass it explicitly in B1 capture/restore. Add this complete value packet to `continuation_tool_batch.py`. `RedactedToolRow` is the actual b003 dataclass including its `visible_event`; the state payload is its actual typed `StatePayload`, not an opaque `CompositionState` instance. `WithheldReply` uses the existing closed origin/content dataclass.

```python
from elspeth.web.composer.withheld_replies import WithheldReply
from elspeth.web.sessions._persist_payload import RedactedToolRow


class PreparedP4Value(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    assistant_content: str
    raw_content: str | None
    redacted_calls_json: str
    redacted_rows_json: str
    withheld_replies_json: str
    assistant_row_uses_current_dispatch: bool
    crash_pending: bool
    expected_current_state_id: str | None
    source_audit_json: str


_P4_ROWS = TypeAdapter(tuple[RedactedToolRow, ...])
_WITHHELD = TypeAdapter(tuple[WithheldReply, ...])
_P4_INVOCATIONS = TypeAdapter(tuple[ComposerToolInvocation, ...])


def p4_semantic_input(value: PreparedP4Value) -> str:
    rows = _P4_ROWS.validate_json(value.redacted_rows_json, strict=True)
    semantic_rows = []
    for row in rows:
        event = row.visible_event
        semantic_rows.append({"tool_call_id": row.tool_call_id, "content": row.content,
            "state": _P4_ROWS.dump_python((row,), mode="json")[0]["composition_state_payload"],
            "visible": ({"tool_call_id": event.tool_call_id, "tool_name": event.tool_name,
                "payload_json": event.payload_json, "payload_hash": event.payload_hash,
                "display_arguments_hash": event.display_arguments_hash,
                "display_result_hash": event.display_result_hash} if event is not None else None)})
    return canonical_json({"schema": "composer.p4-input.v1", "assistant": value.assistant_content,
        "raw": value.raw_content, "calls": _read_json_value(value.redacted_calls_json), "rows": semantic_rows,
        "withheld": _read_json_value(value.withheld_replies_json),
        "matches_dispatch": value.assistant_row_uses_current_dispatch, "crash_pending": value.crash_pending,
        "expected_current_state_id": value.expected_current_state_id,
        "source_audit": _read_json_value(value.source_audit_json)})


def decode_prepared_p4(encoded: str) -> PreparedP4Value:
    value = PreparedP4Value.model_validate_json(encoded, strict=True)
    if canonical_json(value.model_dump(mode="json")) != encoded:
        raise AuditIntegrityError("P4 prepared input lost its complete actual fields")
    _read_json_value(value.redacted_calls_json)
    _P4_ROWS.validate_json(value.redacted_rows_json, strict=True)
    _WITHHELD.validate_json(value.withheld_replies_json, strict=True)
    source = _read_json_value(value.source_audit_json)
    if type(source) is not dict or set(source) != {"invocations", "authorings"}:
        raise AuditIntegrityError("P4 prepared input lost its actual source audit cohort")
    _P4_INVOCATIONS.validate_json(canonical_json(source["invocations"]), strict=True)
    _AUTHORINGS.validate_json(canonical_json(source["authorings"]), strict=True)
    p4_semantic_input(value)
    return value
```

The event's original creator is retained as provenance in `redacted_rows_json`. Before any otherwise authorized uncommitted P4 write, the actual redaction/event producer runs again from the restored shelf/evidence under its current context. Its semantic input must be exactly equal; historical event provenance is not current authority. If that P4 effect was prepared by a different sequence/generation, B4's business-SQL creator refusal still applies: rerunning the producer, equality of its input, or an owed-P4 scope does not authorize the old effect. The complete producer/writer bodies below remain integration material, but cross-creator execution of an already-prepared P4 is HOLD pending an explicit policy resolution. A changed display, state payload, source/result hash or withholding disposition refuses before the writer. No historical event is validated as a live context.

- [ ] Replace only the private `PersistedComposerTurnReceipt` class with the complete class below, preserving its existing field names/default behavior for successful receipts. `unavailable` is the existing actual `AuditOutcome` disposition after rollback during a primary plugin crash, not a committed graph. Add `invocation_ids` and `withheld_hashes` to all actual successful P4 receipt constructors in C3.8b; existing tests without these cohorts use their empty defaults.

```python
class PersistedComposerTurnReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["persisted_turn"] = "persisted_turn"
    effect_key: str
    input_sha256: str
    assistant_id: UUID | None
    current_state_id: UUID | None
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty", "unavailable"]
    creator_operation_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str | None
    chat_message_ids: tuple[UUID, ...]
    unwind_audit_failed: bool = False
    invocation_ids: tuple[str, ...] = ()
    invocation_hashes: tuple[str, ...] = ()
    withheld_hashes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def actual_checkpoint_bundle(self) -> Self:
        if self.checkpoint_disposition == "unavailable":
            if (not self.unwind_audit_failed or self.assistant_id is not None or self.inserted_state_ids
                or self.authored_state_hash is not None or self.chat_message_ids or self.invocation_ids or self.invocation_hashes or self.withheld_hashes):
                raise ValueError("unavailable P4 receipt invented committed rows")
            return self
        if self.unwind_audit_failed or self.assistant_id is None:
            raise ValueError("successful P4 receipt lost its actual assistant identity")
        if self.checkpoint_disposition == "committed":
            if not self.inserted_state_ids or self.current_state_id != self.inserted_state_ids[-1] or self.authored_state_hash is None:
                raise ValueError("P4 receipt lost its actual inserted checkpoint bundle")
        elif self.inserted_state_ids or self.authored_state_hash is not None:
            raise ValueError("P4 receipt invented a newly inserted checkpoint")
        if self.checkpoint_disposition == "empty" and self.current_state_id is not None:
            raise ValueError("empty P4 receipt invented a saved state")
        if self.checkpoint_disposition == "no_new_checkpoint" and self.current_state_id is None:
            raise ValueError("inherited P4 receipt lost its actual saved state")
        if self.assistant_id not in self.chat_message_ids or len(set(self.chat_message_ids)) != len(self.chat_message_ids):
            raise ValueError("P4 receipt lost its actual distinct transcript rows")
        if len(set(self.invocation_ids)) != len(self.invocation_ids) or len(self.invocation_hashes) != len(self.invocation_ids):
            raise ValueError("P4 receipt repeated a full invocation")
        return self
```

### B6.2: Extend the actual P4 transaction with full invocation/withheld rows

- [ ] Append optional `continuation_invocations: tuple[ComposerToolInvocation,...] = ()` and `continuation_withheld_replies: tuple[WithheldReply,...] = ()` to both existing `persist_compose_turn` methods and their protocol signatures. Forward both through the async `_sync` unchanged. Require both to be empty when `continuation is None`. Before its SQL transaction require every invocation to be nominal, unique, and in the exact same order as `redacted_tool_rows`; require each withheld reply nominal and its complete content credential-safe. Use this complete guard:

```python
if continuation is None and (continuation_invocations or continuation_withheld_replies):
    raise AuditIntegrityError("continuation audit cohort has no actual prepared SQL owner")
if continuation is not None:
    if (tuple(item.tool_call_id for item in continuation_invocations) != tuple(row.tool_call_id for row in redacted_tool_rows)
        or any(type(item) is not ComposerToolInvocation for item in continuation_invocations)
        or len({item.tool_call_id for item in continuation_invocations}) != len(continuation_invocations)):
        raise AuditIntegrityError("P4 full invocation cohort differs from its exact actual tool row prefix")
    for item in continuation_withheld_replies:
        if type(item) is not WithheldReply:
            raise AuditIntegrityError("P4 withheld cohort has no nominal reply owner")
        require_no_credential_material(item.content, surface="continuation withheld reply",
            max_string_bytes=len(item.content.encode("utf-8")))
```

In the existing sequence reservation add `len(continuation_invocations)+len(continuation_withheld_replies)` to its exact count. After the existing private-failure insertion loop and before checkpoint witness/receipt construction insert the complete actual audit cohort below. These are the existing full audit/withheld envelope shapes; v1 keeps its existing empty defaults and sequence layout.

```python
audit_offset = 1 + 2 * len(redacted_tool_rows) + len(private_tool_failures)
for index, invocation in enumerate(continuation_invocations):
    content, envelope = redacted_tool_invocation_content_and_envelope(invocation)
    self._insert_chat_message(conn, session_id=session_id, role="audit", content=content,
        raw_content=None, tool_calls=[envelope], sequence_no=base_seq + audit_offset + index,
        writer_principal=writer_principal, composition_state_id=current_state_id, tool_call_id=None,
        parent_assistant_id=None, created_at=now, session_operation_context=session_operation_context)
audit_offset += len(continuation_invocations)
for index, reply in enumerate(continuation_withheld_replies):
    self._insert_chat_message(conn, session_id=session_id, role="audit", content=reply.content,
        raw_content=None, tool_calls=[withheld_reply_envelope(reply.origin, reply.content)],
        sequence_no=base_seq + audit_offset + index, writer_principal=writer_principal,
        composition_state_id=current_state_id, tool_call_id=None, parent_assistant_id=None,
        created_at=now, session_operation_context=session_operation_context)
```

Imports are the actual `composer.audit_storage.redacted_tool_invocation_content_and_envelope`, `composer.withheld_replies.WithheldReply/withheld_reply_envelope`, `contracts.composer_audit.ComposerToolInvocation` and credential guard. The successful same-transaction P4 receipt additionally records `invocation_ids=tuple(item.tool_call_id for item in continuation_invocations)`, `invocation_hashes=tuple(sha256(canonical_json(redacted_tool_invocation_content_and_envelope(item)[1]).encode("utf-8")).hexdigest() for item in continuation_invocations)` and `withheld_hashes=tuple(sha256(item.content.encode("utf-8")).hexdigest() for item in continuation_withheld_replies)`; import `sha256` normally. Its `chat_message_ids` query covers the full reserved sequence range, including these rows. Its original assistant identity, actual state IDs/hash and current SQL committer are unchanged.

- [ ] Add this complete helper on `SessionServiceImpl` for the actual caught unavailable outcome. Call it immediately before returning each existing `AuditOutcome(assistant_id=None,unwind_audit_failed=True,checkpoint_disposition="unavailable")`, only when `continuation is not None`. The old actual catch chooses this disposition; the helper does not infer rollback from an absent row. If the private receipt also cannot be written, the caller preserves the original live plugin crash as primary with this failure secondary.

```python
def _retain_unavailable_continuation_p4(self, continuation: ContinuationOwnerCommit) -> None:
    context = continuation.running.session_operation_context
    sid = str(continuation.running.claim.session_id)
    with self._session_process_locked_begin(sid) as conn:
        with (self._session_write_lock(conn, sid),
              self._session_composer_mutation_transaction(conn, session_operation_context=context,
                  session_id=sid, expected_kind=SessionOperationKind.COMPOSE)):
            require_owner_commit(conn, continuation, context)
            receipt = PersistedComposerTurnReceipt(effect_key=continuation.effect_key,
                input_sha256=continuation.input_sha256, assistant_id=None, current_state_id=None,
                checkpoint_disposition="unavailable", creator_operation_id=context.fence.operation_id,
                inserted_state_ids=(), authored_state_hash=None, chat_message_ids=(), unwind_audit_failed=True)
            retain_owner_result_on_connection(conn, continuation, kind="persisted_turn",
                receipt_id=f"unavailable:{continuation.effect_key}", canonical_result=canonical_json(receipt.model_dump(mode="json")))
```

This opens an actual fresh current-authorized transaction after the failed business transaction has rolled back. It stores only its truthful result receipt, no failed business rows or witness.

### B6.3: Extract the actual redaction/evidence producer and joined writer

- [ ] Add `prepare_continuation_p4_value` to `turn_audit.py` by moving the exact complete pure span from `redacted_assistant_tool_calls: ... = ()` through `redacted_tool_rows = tuple(redacted_rows)` (b003 276–421), including `visible_event_for`, into the new async function. Use this complete parameter/local preamble and return; all statements between them are the unchanged exact source span. The original v1 `persist_turn_audit` calls this producer and binds its decoded calls/rows before its existing persistence body. Its publication/rollback/witness/primacy behavior remains otherwise unchanged.

```python-signature
async def prepare_continuation_p4_value(
    *, tool_outcomes: tuple[_ToolOutcome, ...], private_authorings: tuple[PrivateToolAuthoring, ...],
    private_invocations: tuple[ComposerToolInvocation, ...],
    assistant_tool_calls: tuple[_AdmittedToolCall, ...], redaction_telemetry: RedactionTelemetry,
    session_operation_context: SessionOperationContext | None, turn_evidence: TurnEvidence | None,
    ingress: CompositionIngressRecord | None, chat_ingress_inputs: list[ChatIngressInput] | None,
    assistant_content: str, raw_content: str | None, withheld_replies: tuple[WithheldReply, ...],
    assistant_row_uses_current_dispatch: bool, crash_pending: bool, current_state_id: str | None,
) -> PreparedP4Value:
```

Copy its original local imports from b003 `persist_turn_audit` 262–274 into the new producer, then append this complete return to the preserved span:

```python
return PreparedP4Value(assistant_content=assistant_content, raw_content=raw_content,
    redacted_calls_json=canonical_json(owned_json(deep_thaw(redacted_assistant_tool_calls))),
    redacted_rows_json=canonical_json(_P4_ROWS.dump_python(redacted_tool_rows, mode="json", warnings="error", fallback=owned_json)),
    withheld_replies_json=canonical_json(_WITHHELD.dump_python(withheld_replies, mode="json")),
    assistant_row_uses_current_dispatch=assistant_row_uses_current_dispatch,
    crash_pending=crash_pending, expected_current_state_id=current_state_id,
    source_audit_json=canonical_json({"invocations": _P4_INVOCATIONS.dump_python(private_invocations, mode="json"),
                                     "authorings": _AUTHORINGS.dump_python(private_authorings, mode="json")}))
```

The v1 call supplies empty `withheld_replies`, its existing substituted assistant content/raw fields and exact boolean. It binds `redacted_assistant_tool_calls=tuple(_read_json_value(value.redacted_calls_json))` after requiring a list of dicts, and `redacted_tool_rows=_P4_ROWS.validate_json(value.redacted_rows_json,strict=True)`. No v1 SQL owner or return shape changes.

- [ ] Extract b003's complete existing private-evidence construction from `authorings = ...` through `private_tool_failures = tuple(...)` (turn_audit 435–484) into `_continuation_private_p4_evidence(*, tool_outcomes, private_authorings, private_invocations, session_id, session_operation_context)`. Preserve every original set-equality, nominal invocation, canonical pair and failure identity check. Its exact parameter types are the same original `persist_turn_audit` parameter annotations; `session_id:str` and `session_operation_context:SessionOperationContext` are non-null here. Its local imports are the original lines432–433. Append `return tuple(private_tool_evidence), private_tool_failures`. Both v1 and v2 call this actual producer, so fresh current physical creator fields are never guessed or copied from an old lease.

- [ ] Add the complete async finish-once method below to the actual `SessionServiceImpl` and matching protocol. This uses the existing synchronous P4 primitive in the actual registered SQL future, joins it through `run_required_sql_finish_once`, and retains original cancellations for projection. It supplements the existing v1 async method; it does not nest two worker futures or return before the real writer finishes.

```python
async def persist_continuation_compose_turn(
    self, *, session_id: str, assistant_content: str, raw_content: str | None,
    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...], redacted_tool_rows: tuple[RedactedToolRow, ...],
    private_tool_evidence: tuple[ComposerDiagnosticEvidence, ...], private_tool_failures: tuple[ComposerDiagnosticEvidence, ...],
    rejection_records: tuple[RejectionRecord, ...], parent_composition_state_id: str | None,
    expected_current_state_id: str | None, plugin_crash_pending: bool,
    session_operation_context: SessionOperationContext, continuation: ContinuationOwnerCommit,
    continuation_invocations: tuple[ComposerToolInvocation, ...], continuation_withheld_replies: tuple[WithheldReply, ...],
    required_work: RequiredWorkTicket,
) -> RequiredSQLFinishOnce[AuditOutcome]:
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,),
        session_id=session_id, context=session_operation_context)
    return await run_required_sql_finish_once(required_work, self.persist_compose_turn,
        session_id=session_id, assistant_content=assistant_content, raw_content=raw_content,
        redacted_assistant_tool_calls=redacted_assistant_tool_calls, redacted_tool_rows=redacted_tool_rows,
        private_tool_evidence=private_tool_evidence, private_tool_failures=private_tool_failures,
        rejection_records=rejection_records, parent_composition_state_id=parent_composition_state_id,
        expected_current_state_id=expected_current_state_id, writer_principal="compose_loop",
        plugin_crash_pending=plugin_crash_pending, session_operation_context=session_operation_context,
        continuation=continuation, continuation_invocations=continuation_invocations,
        continuation_withheld_replies=continuation_withheld_replies)
```

Import `RequiredSQLFinishOnce` from `required_sql_outcomes`, `run_required_sql_finish_once` from `async_workers`; the existing concrete sync primitive retains its async-loop misuse guard and runs in the executor here. This is the v2 P4 method called by the B0 adapter; the earlier shorthand reference to `persist_compose_turn_async(...continuation=...)` is superseded by this exact finish-once entry so committed results and original cancellations can both be projected.

### B6.4: Verify the actual P4 row receipt before projecting it

- [ ] Add this complete pre-write P4 input verifier to `sessions/continuation.py`; call it inside the actual P4 transaction immediately after `require_owner_commit` and before sequence reservation. Pass the actual method arguments unchanged. This binds the writer's real input to the selected full batch and actual prepared input, including private authorings/invocations. Fresh current-context diagnostic/visible producers may change their physical creator but cannot change the logical graph, tool response or audit cohort.

```python
def require_selected_p4_input(
    conn: Connection, commit: ContinuationOwnerCommit, *, assistant_content: str, raw_content: str | None,
    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...], redacted_tool_rows: tuple[RedactedToolRow, ...],
    private_tool_evidence: tuple[ComposerDiagnosticEvidence, ...], private_tool_failures: tuple[ComposerDiagnosticEvidence, ...],
    continuation_invocations: tuple[ComposerToolInvocation, ...], continuation_withheld_replies: tuple[WithheldReply, ...],
) -> None:
    from elspeth.core.canonical import stable_hash
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_tool_batch import (
        _AUTHORINGS, _P4_INVOCATIONS, _P4_ROWS, _WITHHELD, _read_json_value,
        decode_batch_chunk, decode_prepared_p4, p4_semantic_input,
    )
    from elspeth.web.composer.continuation_codec import decode_continuation_response, owned_json
    from elspeth.web.composer.tool_batch import restore_continuation_batch_value
    from elspeth.web.composer.turn_audit import _continuation_private_p4_evidence
    stored = require_owner_commit(conn, commit, commit.running.session_operation_context)
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    if type(frame) is not ToolBatchFrame or commit.effect_key != f"batch:{frame.response.digest}:p4":
        raise AuditIntegrityError("P4 writer has no exact selected batch/effect")
    values = load_private_chunks(conn, session_id=str(commit.running.claim.session_id),
        operation_id=commit.running.claim.operation_id, stack=root)
    by_ref = {item.reference: item for item in values}
    batch_value = decode_batch_chunk(by_ref[frame.accumulator])
    if batch_value.phase != "p4_pending" or batch_value.p4_input_json is None:
        raise AuditIntegrityError("P4 writer reached an unprepared or already applied phase")
    prepared = decode_prepared_p4(batch_value.p4_input_json)
    calls = decode_continuation_response(by_ref[frame.response]).tool_batch.calls
    batch = restore_continuation_batch_value(batch_value, calls)
    authorings = tuple(batch.private_authorings_by_call[item.call.id] for item in batch.tool_outcomes)
    actual = prepared.model_copy(update={"assistant_content": assistant_content, "raw_content": raw_content,
        "redacted_calls_json": canonical_json(owned_json(deep_thaw(redacted_assistant_tool_calls))),
        "redacted_rows_json": canonical_json(_P4_ROWS.dump_python(redacted_tool_rows, mode="json", warnings="error", fallback=owned_json)),
        "withheld_replies_json": canonical_json(_WITHHELD.dump_python(continuation_withheld_replies, mode="json")),
        "source_audit_json": canonical_json({"invocations": _P4_INVOCATIONS.dump_python(continuation_invocations, mode="json"),
                                             "authorings": _AUTHORINGS.dump_python(authorings, mode="json")})})
    if (p4_semantic_input(actual) != p4_semantic_input(prepared)
        or stable_hash(_read_json_value(p4_semantic_input(actual))) != commit.input_sha256):
        raise AuditIntegrityError("P4 writer changed its actual selected full logical input")
    evidence, failures = _continuation_private_p4_evidence(tool_outcomes=tuple(batch.tool_outcomes),
        private_authorings=authorings, private_invocations=continuation_invocations,
        session_id=str(commit.running.claim.session_id), session_operation_context=commit.running.session_operation_context)
    if evidence != private_tool_evidence or failures != private_tool_failures:
        raise AuditIntegrityError("P4 writer changed its actual current-context private evidence producer")
```

Import the nominal annotation types normally or under `TYPE_CHECKING` in this module as appropriate; the verifier's concrete imports execute only after all disabled integration dependencies exist. It is called only for the actual v2 P4 owner, not proposal/read/interpretation transactions. Existing C3.8b low-level P4 tests must install a selected complete P4 batch/prepared value through the B6/B7 fixture; an arbitrary initializer snapshot is no longer accepted as a P4 prepared input.

- [ ] Add this complete authority facet. It supports both pending and already-applied P4 results while a complete batch remains selected for classification. Actual assistant content, full invocation rows, withheld content hashes, same-session row IDs and actual SQL committer are checked inside one current-authorized selected-head transaction. The graph case additionally uses the existing full historical effect validator; no witness is reminted.

```python
def read_continuation_p4_result(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> PersistedComposerTurnReceipt:
    from hashlib import sha256
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.core.canonical import stable_hash
    from elspeth.web.composer.continuation_tool_batch import _P4_ROWS, _read_json_value, decode_batch_chunk, decode_prepared_p4, p4_semantic_input
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.composer.withheld_replies import COMPOSER_WITHHELD_REPLY_KIND
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        frame = root.frames[-1]
        if type(frame) is not ToolBatchFrame:
            raise AuditIntegrityError("P4 receipt read lost its actual selected batch")
        private = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        selected = tuple(item for item in private if item.reference == frame.accumulator)
        if len(selected) != 1:
            raise AuditIntegrityError("P4 receipt read lost its exact selected accumulator")
        batch = decode_batch_chunk(selected[0])
        if batch.p4_input_json is None:
            raise AuditIntegrityError("P4 receipt read has no actual retained prepared input")
        prepared = decode_prepared_p4(batch.p4_input_json)
        key = f"batch:{frame.response.digest}:p4"
        effect = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == key)).one()
        if (effect.owner != "session_sql" or effect.disposition != "committed"
            or effect.input_sha256 != stable_hash(_read_json_value(p4_semantic_input(prepared)))
            or effect.result_chunk_key is None):
            raise AuditIntegrityError("P4 receipt does not match its actual prepared SQL effect")
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
            key=effect.result_chunk_key).decode("utf-8", errors="strict")
        receipt = PersistedComposerTurnReceipt.model_validate_json(canonical, strict=True)
        if (canonical_json(receipt.model_dump(mode="json")) != canonical or receipt.effect_key != key
            or receipt.input_sha256 != effect.input_sha256
            or batch.p4_receipt_json is not None and batch.p4_receipt_json != canonical):
            raise AuditIntegrityError("P4 receipt changed between actual retention and pure application")
        committer = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.committer_sequence, attempts.c.generation == effect.committer_generation)).one()
        if receipt.creator_operation_id != committer.session_operation_id:
            raise AuditIntegrityError("P4 receipt names another actual SQL committer")
        if receipt.checkpoint_disposition == "unavailable":
            if not prepared.crash_pending or effect.receipt_key != f"unavailable:{key}":
                raise AuditIntegrityError("unavailable P4 has no actual primary-crash rollback receipt")
            return receipt
        rows = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid,
            chat_messages_table.c.id.in_(tuple(str(item) for item in receipt.chat_message_ids)))
            .order_by(chat_messages_table.c.sequence_no)).all()
        if (tuple(UUID(row.id) for row in rows) != receipt.chat_message_ids
            or any(row.writer_principal != "compose_loop" for row in rows)):
            raise AuditIntegrityError("P4 receipt substituted/missed actual transcript rows")
        assistant = tuple(row for row in rows if row.id == str(receipt.assistant_id))
        calls = _read_json_value(prepared.redacted_calls_json)
        if type(calls) is not list or any(type(item) is not dict for item in calls):
            raise AuditIntegrityError("P4 receipt lost its complete admitted assistant call tuple")
        expected_calls = calls if calls else None
        if (len(assistant) != 1 or assistant[0].role != "assistant" or assistant[0].content != prepared.assistant_content
            or assistant[0].raw_content != prepared.raw_content
            or canonical_json(assistant[0].tool_calls) != canonical_json(expected_calls)
            or assistant[0].tool_call_id is not None or assistant[0].parent_assistant_id is not None
            or assistant[0].composition_state_id != prepared.expected_current_state_id):
            raise AuditIntegrityError("P4 receipt changed its actual assistant row bytes/binding")
        expected_tools = _P4_ROWS.validate_json(prepared.redacted_rows_json, strict=True)
        actual_tools = tuple(row for row in rows if row.role == "tool")
        if tuple(row.tool_call_id for row in actual_tools) != tuple(row.tool_call_id for row in expected_tools):
            raise AuditIntegrityError("P4 receipt changed its exact actual executed tool prefix")
        for actual_tool, expected_tool in zip(actual_tools, expected_tools, strict=True):
            expected_calls = ([expected_tool.visible_event.with_parent(str(receipt.assistant_id)).to_envelope()]
                              if expected_tool.visible_event is not None else None)
            if (actual_tool.content != expected_tool.content or actual_tool.parent_assistant_id != str(receipt.assistant_id)
                or canonical_json(actual_tool.tool_calls) != canonical_json(expected_calls)):
                raise AuditIntegrityError("P4 receipt substituted actual tool display/event bytes")
        invocation_ids = tuple(envelope["invocation"]["tool_call_id"] for row in rows
            for envelope in (row.tool_calls or ()) if envelope.get("_kind") == "audit")
        invocation_hashes = tuple(sha256(canonical_json(envelope).encode("utf-8")).hexdigest() for row in rows
            for envelope in (row.tool_calls or ()) if envelope.get("_kind") == "audit")
        withheld = tuple(sha256(row.content.encode("utf-8")).hexdigest() for row in rows
            if any(envelope.get("_kind") == COMPOSER_WITHHELD_REPLY_KIND for envelope in (row.tool_calls or ())))
        if invocation_ids != receipt.invocation_ids or invocation_hashes != receipt.invocation_hashes or withheld != receipt.withheld_hashes:
            raise AuditIntegrityError("P4 receipt differs from its actual complete invocation/withheld cohort")
        if receipt.checkpoint_disposition == "committed":
            if receipt.authored_state_hash is None:
                raise AuditIntegrityError("committed P4 receipt lost its actual authored graph hash")
            validate_historical_effect_reference(conn, session_id=running.claim.session_id, operation_id=operation_id,
                effect_key=key, expected_input_sha256=effect.input_sha256,
                expected_creator_operation_id=committer.session_operation_id, expected_receipt_id=str(receipt.assistant_id),
                expected_inserted_state_ids=receipt.inserted_state_ids,
                expected_authored_state_hash=receipt.authored_state_hash)
        return receipt
```

The withheld discriminator is the actual `COMPOSER_WITHHELD_REPLY_KIND` constant. The complete actual row IDs/content/committer, manifest-redacted invocation envelope hashes and typed receipt/input binding are required.

### B6.5: Apply the committed receipt and advance only the actual drained prefix

- [ ] Add `p4_verification_failed: bool = False` to both batch carriers and explicitly copy it in B1. Add this full pure projection function to `continuation_tool_batch.py`. It reconstructs metadata from an actual already-validated receipt; its `audit_outcome=None` deliberately carries no invented live checkpoint witness. The private failure boolean represents an unavailable comparison, while actual live exceptions remain on the current live `_PersistOutcome` and preserve their original identity.

```python
from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt


def project_p4_receipt(
    *, receipt: PersistedComposerTurnReceipt, prepared: PreparedP4Value,
    attempted_calls: int, verification_failure: AuditIntegrityError | None,
) -> _PersistOutcome:
    calls = _read_json_value(prepared.redacted_calls_json)
    if type(calls) is not list or any(type(item) is not dict for item in calls):
        raise AuditIntegrityError("retained P4 assistant calls lost their complete object tuple")
    rows = _P4_ROWS.validate_json(prepared.redacted_rows_json, strict=True)
    assistant_id = str(receipt.assistant_id) if receipt.assistant_id is not None else None
    failed = FailedTurnMetadata(assistant_message_id=assistant_id, tool_calls_attempted=attempted_calls,
        tool_responses_persisted=len(rows) if assistant_id is not None else 0)
    return _PersistOutcome(current_state_id=str(receipt.current_state_id) if receipt.current_state_id is not None else None,
        persisted_assistant_message_id=assistant_id,
        persisted_assistant_content=prepared.assistant_content if assistant_id is not None else None,
        persisted_tool_call_turn=assistant_id is not None,
        persisted_assistant_matches_current_dispatch=assistant_id is not None and prepared.assistant_row_uses_current_dispatch,
        unwind_audit_failed=receipt.unwind_audit_failed, failed_turn=failed,
        redacted_assistant_tool_calls=tuple(calls), redacted_tool_rows=rows, audit_outcome=None,
        effect_verification_failure=verification_failure)
```

`FailedTurnMetadata` comes directly from its actual `contracts.errors` owner. No test replaces the source `_PersistOutcome` constructor or an actual result with a count-only fake.

- [ ] Add this exact P4 cursor function to `continuation_tool_batch.py`. Only full invocation rows, corresponding visible events/private authorings and the actual withheld cohort advance here. Provider LLM calls, planner attempts and private planner candidates retain their existing cursor values because P4 is not their SQL owner. A failed unwind advances none. `RetainedVisibleLink` keeps its original invocation index, and all checks use actual owned values, never a guessed buffer length alone.

```python
from dataclasses import replace as replace_dataclass
from hashlib import sha256
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot


def p4_drained_snapshot(snapshot: RecorderSnapshot, receipt: PersistedComposerTurnReceipt) -> RecorderSnapshot:
    snapshot.validate_links_and_cursors()
    if receipt.unwind_audit_failed:
        return snapshot
    prior = snapshot.drained
    invocations = snapshot.invocations[prior.invocations:]
    authorings = snapshot.private_authorings[prior.private_authorings:]
    withheld = snapshot.withheld_replies[prior.withheld_replies:]
    if (tuple(item.tool_call_id for item in invocations) != receipt.invocation_ids
        or tuple(item.tool_call_id for item in authorings) != receipt.invocation_ids
        or tuple(sha256(canonical_json(redacted_tool_invocation_content_and_envelope(item)[1]).encode("utf-8")).hexdigest()
                 for item in invocations) != receipt.invocation_hashes
        or tuple(sha256(item.content.encode("utf-8")).hexdigest() for item in withheld) != receipt.withheld_hashes):
        raise AuditIntegrityError("P4 drain differs from its actual complete committed cohort")
    events = snapshot.visible_tool_events[prior.visible_tool_events:]
    if any(snapshot.invocations[item.invocation_index].tool_call_id not in receipt.invocation_ids for item in events):
        raise AuditIntegrityError("P4 drain includes visible evidence outside its actual committed cohort")
    drained = replace_dataclass(prior, invocations=len(snapshot.invocations), private_authorings=len(snapshot.private_authorings),
        withheld_replies=len(snapshot.withheld_replies), visible_tool_events=len(snapshot.visible_tool_events))
    result = replace_dataclass(snapshot, drained=drained)
    result.validate_links_and_cursors()
    return result
```

- [ ] Add the complete actual P4 adapter below to `ComposerServiceImpl`. It uses the retained batch for its dispatch, calls the real P4 writer only for an actually uncommitted selected effect, then applies the actual returned SQL receipt through F7. Its complete frame remains selected until the frames driver's actual P5 classification checkpoint; a cold complete phase reprojects the same exact receipt without repeating a writer. Import every named codec/authority/source/result class normally from its actual owner; the local imports below avoid existing module cycles.

```python
async def persist_continuation_tool_batch(
    self, *, dispatch: _DispatchOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    from elspeth.core.canonical import stable_hash
    from elspeth.web.compartments import compartment_ingress_record
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot
    from elspeth.web.composer.continuation_bootstrap import decode_continuation_bootstrap, bootstrap_values
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_evidence_authority import EvidenceRestoreAuthorization
    from elspeth.web.composer.continuation_execution import continuation_chunk, merge_continuation_chunks
    from elspeth.web.composer.continuation_tool_batch import (
        _WITHHELD, _P4_ROWS, _read_json_value, decode_prepared_p4, p4_semantic_input,
        p4_drained_snapshot, project_p4_receipt,
    )
    from elspeth.web.composer.tool_batch import (
        ContinuationBatchExecution, restore_continuation_batch_value, _finish_continuation_batch_live,
    )
    from elspeth.web.composer.turn_audit import (
        prepare_continuation_p4_value, _continuation_private_p4_evidence, build_rejection_records,
    )
    from elspeth.web.sessions.routes._helpers import _chat_ingress_inputs
    if type(stack.frames[-1]) is not ToolBatchFrame:
        raise AuditIntegrityError("P4 adapter lost its exact selected batch")
    if self._sessions_service is None or ctx.turn_sessions_service is not self._sessions_service:
        raise AuditIntegrityError("P4 adapter changed its actual bound session service")
    frame = stack.frames[-1]
    call_model = _CallModelOutcome(decode_continuation_response(continuation_chunk(chunks, frame.response)))
    batch = restore_continuation_batch_value(validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator)),
                                            call_model.completion.tool_batch.calls)
    actual_dispatch, _ = _finish_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, batch=batch)
    if (replace(dispatch, plugin_crash=None, plugin_crash_cause=None, advisor_failure=None)
        != replace(actual_dispatch, plugin_crash=None, plugin_crash_cause=None, advisor_failure=None)):
        raise AuditIntegrityError("P4 caller substituted another dispatch for the selected complete batch")
    batch.plugin_crash, batch.plugin_crash_cause, batch.advisor_failure = dispatch.plugin_crash, dispatch.plugin_crash_cause, dispatch.advisor_failure
    actual_dispatch = dispatch
    if batch.phase not in ("dispatch_ready", "p4_pending", "p4_result_retained", "complete"):
        raise AuditIntegrityError("P4 began before the actual issued call prefix settled")
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
                                            checkpoint_id, required_work, ctx.provider_owner)
    execution.ctx = replace(ctx, continuation=execution)
    running = execution._running()
    compose = next(item for item in reversed(stack.frames) if type(item) is ComposeFrame)
    original = bootstrap_values(decode_continuation_bootstrap(continuation_chunk(chunks, compose.bootstrap)))
    audit_snapshot = decode_recorder_snapshot(continuation_chunk(chunks, compose.audit_buffer))
    if batch.p4_input_json is None:
        if compose.advisor_repair_context_introduced and (actual_dispatch.assistant_message.content or "").strip():
            ctx.recorder.record_withheld_reply("advisor_repair_tool_turn", actual_dispatch.assistant_message.content or "")
        buffer = ctx.recorder.snapshot_for_continuation(drained=audit_snapshot.drained)
        batch.phase = "p4_pending"
        prepared = await prepare_continuation_p4_value(tool_outcomes=actual_dispatch.tool_outcomes,
            private_authorings=actual_dispatch.private_authorings, private_invocations=actual_dispatch.private_invocations,
            assistant_tool_calls=actual_dispatch.assistant_tool_calls,
            redaction_telemetry=ctx.redaction_telemetry, session_operation_context=running.session_operation_context,
            turn_evidence=ctx.turn_evidence,
            ingress=compartment_ingress_record(original.message, own_compartment_id=self._settings.compartment_id),
            chat_ingress_inputs=list(_chat_ingress_inputs(original.records, own_compartment_id=self._settings.compartment_id)),
            assistant_content=(_ADVISOR_REPAIR_INTERMEDIATE_PUBLIC_MESSAGE if compose.advisor_repair_context_introduced
                               else actual_dispatch.assistant_message.content or ""),
            raw_content=None if compose.advisor_repair_context_introduced else actual_dispatch.raw_assistant_content,
            withheld_replies=buffer.withheld_replies[buffer.drained.withheld_replies:],
            assistant_row_uses_current_dispatch=not compose.advisor_repair_context_introduced,
            crash_pending=actual_dispatch.plugin_crash is not None or batch.retained_failure is not None,
            current_state_id=compose.current_state_id)
        batch.p4_input_json = canonical_json(prepared.model_dump(mode="json"))
        await execution.checkpoint()
    else:
        prepared = decode_prepared_p4(batch.p4_input_json)
    key = f"batch:{frame.response.digest}:p4"
    expected_input = stable_hash(_read_json_value(p4_semantic_input(prepared)))
    settled = tuple(item for item in execution.stack.effect_frontier if item.effect_id == key)
    if not settled:
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                     RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        result = await run_required_sql_finish_once(sql, execution.authority.prepare_tool_action, running,
            effect_key=key, input_sha256=expected_input, owner="session_sql", snapshot_json=encode_continuation_snapshot(execution.stack),
            expected_checkpoint_id=execution.checkpoint_id, expected_cursor=execution.stack.cursor)
        selected = execution.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=result)
        if type(selected) is not StoredContinuationSnapshot:
            raise AuditIntegrityError("P4 prepare returned no actual selected SQL head")
        execution.stack, execution.checkpoint_id = decode_continuation_snapshot(selected.payload_json), selected.checkpoint_id
        settled = tuple(item for item in execution.stack.effect_frontier if item.effect_id == key)
    if len(settled) != 1 or settled[0].disposition not in ("prepared", "committed"):
        raise AuditIntegrityError("P4 has an unresolved or foreign actual SQL disposition")
    if settled[0].disposition == "prepared":
        fresh = await prepare_continuation_p4_value(tool_outcomes=actual_dispatch.tool_outcomes,
            private_authorings=actual_dispatch.private_authorings, private_invocations=actual_dispatch.private_invocations,
            assistant_tool_calls=actual_dispatch.assistant_tool_calls, redaction_telemetry=ctx.redaction_telemetry,
            session_operation_context=running.session_operation_context, turn_evidence=ctx.turn_evidence,
            ingress=compartment_ingress_record(original.message, own_compartment_id=self._settings.compartment_id),
            chat_ingress_inputs=list(_chat_ingress_inputs(original.records, own_compartment_id=self._settings.compartment_id)),
            assistant_content=prepared.assistant_content, raw_content=prepared.raw_content,
            withheld_replies=_WITHHELD.validate_json(prepared.withheld_replies_json, strict=True),
            assistant_row_uses_current_dispatch=prepared.assistant_row_uses_current_dispatch,
            crash_pending=prepared.crash_pending, current_state_id=prepared.expected_current_state_id)
        if p4_semantic_input(fresh) != p4_semantic_input(prepared):
            raise AuditIntegrityError("fresh current P4 projection changed the retained actual business input")
        prepared = fresh
        batch.p4_input_json = canonical_json(prepared.model_dump(mode="json"))
        await execution.checkpoint()
        private, failures = _continuation_private_p4_evidence(tool_outcomes=actual_dispatch.tool_outcomes,
            private_authorings=actual_dispatch.private_authorings, private_invocations=actual_dispatch.private_invocations,
            session_id=str(running.claim.session_id), session_operation_context=running.session_operation_context)
        rows = _P4_ROWS.validate_json(prepared.redacted_rows_json, strict=True)
        calls = _read_json_value(prepared.redacted_calls_json)
        if type(calls) is not list or any(type(item) is not dict for item in calls):
            raise AuditIntegrityError("P4 actual input lost its complete assistant call tuple")
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                                     RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        commit = ContinuationOwnerCommit(running, key, expected_input, execution.checkpoint_id, execution.stack.cursor)
        result = await self._sessions_service.persist_continuation_compose_turn(session_id=str(running.claim.session_id),
            assistant_content=prepared.assistant_content, raw_content=prepared.raw_content,
            redacted_assistant_tool_calls=tuple(calls), redacted_tool_rows=rows,
            private_tool_evidence=private, private_tool_failures=failures,
            rejection_records=build_rejection_records(actual_dispatch.tool_outcomes),
            parent_composition_state_id=prepared.expected_current_state_id,
            expected_current_state_id=prepared.expected_current_state_id, plugin_crash_pending=prepared.crash_pending,
            session_operation_context=running.session_operation_context, continuation=commit,
            continuation_invocations=actual_dispatch.private_invocations,
            continuation_withheld_replies=_WITHHELD.validate_json(prepared.withheld_replies_json, strict=True), required_work=sql)
        returned = execution.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=result)
        if type(returned) is not AuditOutcome:
            raise AuditIntegrityError("P4 actual writer returned no nominal audit disposition")
        await execution.refresh_after_owner()
    sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
                                                 RequiredWorkSource.CONTINUATION_READ_PROJECTION)
    result = await run_required_sql_finish_once(sql, execution.authority.read_continuation_p4_result, running,
        expected_checkpoint_id=execution.checkpoint_id, expected_cursor=execution.stack.cursor)
    receipt = execution.accept_sql_result(sql=sql, projection=projection,
        source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=result)
    if type(receipt) is not PersistedComposerTurnReceipt:
        raise AuditIntegrityError("P4 readback returned no actual validated result receipt")
    verification_failure = (AuditIntegrityError("Retained committed compose effect verification is unavailable")
                            if batch.p4_verification_failed else None)
    pending = any(item.effect_key == key for item in execution.stack.pending_owner_results)
    if pending and receipt.checkpoint_disposition == "unavailable":
        if ctx.turn_evidence is None:
            raise AuditIntegrityError("unavailable P4 lost its actual current evidence owner")
        ctx.turn_evidence.mark_verification_unavailable()
    if pending and receipt.checkpoint_disposition == "committed":
        compose = next(item for item in reversed(execution.stack.frames) if type(item) is ComposeFrame)
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
                                                     RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        result = await run_required_sql_finish_once(sql, execution.authority.validate_turn_evidence_restore, running,
            expected_checkpoint_id=execution.checkpoint_id, expected_cursor=execution.stack.cursor,
            evidence=continuation_chunk(execution.chunks, compose.evidence))
        authorization = execution.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=result)
        if type(authorization) is not EvidenceRestoreAuthorization or ctx.turn_evidence is None:
            raise AuditIntegrityError("P4 application lost its current actual evidence authorization")
        expected_state = next((item.response.updated_state for item in reversed(actual_dispatch.tool_outcomes)
            if item.post_version > item.pre_version and type(item.response) is ToolResult), None)
        if expected_state is None:
            raise AuditIntegrityError("committed P4 has no corresponding actual dispatched graph state")
        try:
            ctx.turn_evidence.apply_retained_checkpoint(authorization, effect_key=key, expected_state=expected_state,
                tool_call_ids=tuple(item.call.id for item in actual_dispatch.tool_outcomes))
        except Exception as error:
            verification_failure = error if isinstance(error, AuditIntegrityError) else AuditIntegrityError("Committed compose effect verification failed")
            if verification_failure is not error:
                verification_failure.__cause__ = error
            ctx.turn_evidence.mark_verification_unavailable()
            batch.p4_verification_failed = True
    persist = project_p4_receipt(receipt=receipt, prepared=prepared,
        attempted_calls=len(actual_dispatch.assistant_tool_calls), verification_failure=verification_failure)
    if verification_failure is not None:
        verification_failure.failed_turn = persist.failed_turn
    batch.p4, batch.p4_receipt_json, batch.phase = persist, canonical_json(receipt.model_dump(mode="json")), "complete"
    if pending:
        compose = next(item for item in reversed(execution.stack.frames) if type(item) is ComposeFrame)
        previous_buffer = decode_recorder_snapshot(continuation_chunk(execution.chunks, compose.audit_buffer))
        current_buffer = ctx.recorder.snapshot_for_continuation(drained=previous_buffer.drained)
        drained = p4_drained_snapshot(current_buffer, receipt)
        audit_chunk = encode_recorder_snapshot(drained)
        frames = tuple(item.model_copy(update={"audit_buffer": audit_chunk.reference}) if item is compose else item
                       for item in execution.stack.frames)
        execution.stack = execution.stack.model_copy(update={"frames": frames})
        execution.chunks = merge_continuation_chunks(execution.chunks, (audit_chunk,))
        await execution.checkpoint(consumed_effect_key=key)
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks, execution.checkpoint_id,
        actual_dispatch, batch.advisor_calls_used, persist)
```

The actual live plugin crash object remains the primary source in the warm `dispatch`. Wrap only this v2 adapter's actual writer/join section in `try/except BaseException as secondary`: when `actual_dispatch.plugin_crash is not None`, raise `BaseExceptionGroup("Original tool failure and P4 ownership failure",[actual_dispatch.plugin_crash,secondary])` from the original `plugin_crash_cause`; otherwise re-raise. Do not catch interpreter/Tier-1 failures from the original live dispatch before B2 has applied its existing narrow-class discipline. Cold `RetainedBatchFailure` is an actionable retained failure descriptor, never a fabricated old exception object.

## B8: Bound actual batch work without renewing nested SQL allowances

Apply this packet before B7's behavioral checks. **Create:**
`composer/continuation_local_clock.py`. **Modify:** `composer/tool_batch.py`
and `composer/service.py`.
**Test:** `tests/unit/web/composer/test_continuation_local_clock.py`.
The clock is ephemeral execution state. No codec stores it or a monotonic
anchor. It never supplies mutation authority or changes a required-work
ticket. Provider children retain C5's separate provider clock.

- [ ] Add this complete local clock. Repeated retention pieces consume one
  cumulative V allowance. A nested retention piece pauses H, while the
  original total action deadline continues. Nested pieces of the same
  stage share that stage's current timer. Submitted SQL keeps its actual
  finish-once owner; a timeout is checked after projection even when the
  owner absorbed cancellation while joining its real future.

```python
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy

LocalStage = Literal["preparation", "physical", "retention"]


@dataclass(slots=True)
class _ActiveLocalStage:
    stage: LocalStage
    timer: asyncio.Timeout
    started: float
    deadline: float


class ContinuationLocalActionClock:
    def __init__(self, policy: ContinuationActionPolicy) -> None:
        if policy.guard is not None or policy.quota is not None:
            raise AuditIntegrityError("a local action cannot execute provider guard/quota stages")
        loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        self._deadline = loop.time() + policy.total
        self._remaining: dict[LocalStage, float] = {
            "preparation": policy.preparation, "physical": policy.physical, "retention": policy.retention,
        }
        self._active: list[_ActiveLocalStage] = []

    async def run[T](self, stage: LocalStage, operation: Callable[[], Awaitable[T]]) -> T:
        if asyncio.current_task() is not self._task:
            raise AuditIntegrityError("one local action clock cannot be shared across live tasks")
        loop = asyncio.get_running_loop()
        now = loop.time()
        parent = self._active[-1] if self._active else None
        if parent is not None and parent.stage == stage:
            result = await operation()
            if parent.timer.expired() or loop.time() >= parent.deadline:
                raise TimeoutError("continuation local stage allowance expired")
            return result
        if any(item.stage == stage for item in self._active):
            raise AuditIntegrityError("a paused local stage cannot be recursively reopened")
        if now >= self._deadline or self._remaining[stage] <= 0:
            raise TimeoutError("continuation local action allowance expired")
        if parent is not None:
            if parent.timer.expired() or now >= parent.deadline:
                raise TimeoutError("continuation parent stage allowance expired")
            self._remaining[parent.stage] -= now - parent.started
            parent.timer.reschedule(None)
        deadline = min(self._deadline, now + self._remaining[stage])
        timer = asyncio.timeout_at(deadline)
        current = _ActiveLocalStage(stage, timer, now, deadline)
        self._active.append(current)
        try:
            async with timer:
                result = await operation()
                if timer.expired() or loop.time() >= current.deadline:
                    raise TimeoutError("continuation local stage allowance expired")
                return result
        finally:
            finished = loop.time()
            self._remaining[stage] -= finished - current.started
            if self._active.pop() is not current:
                raise AuditIntegrityError("local action stage ownership changed")
            if parent is not None:
                parent.started = finished
                parent.deadline = min(self._deadline, finished + self._remaining[parent.stage])
                parent.timer.reschedule(parent.deadline)
```

- [ ] Add this complete policy selector and forwarding owner to
  `tool_batch.py`. Add the process-local optional field
  `continuation_local_clock: ContinuationLocalActionClock | None = None`
  to `ToolBatchContext` with its normal TYPE_CHECKING import. All v1 callers
  leave it null; only the five public v2 adapters below mint it.

```python
from collections.abc import Awaitable, Callable
from elspeth.web.composer.continuation import AdvisorFrame
from elspeth.web.composer.continuation_local_clock import ContinuationLocalActionClock
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.tools.continuation_support import continuation_tool_support


async def _run_batch_entry[T](
    *, ctx: ToolBatchContext, stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    operation: Callable[[ToolBatchContext], Awaitable[T]],
    local_sql_only: bool = False,
) -> T:
    clock = _batch_local_clock(stack, chunks, local_sql_only=local_sql_only)
    bounded = replace(ctx, continuation_local_clock=clock)
    async def execute() -> T:
        return await operation(bounded)
    return await clock.run("physical", execute)
```

Add this complete selector in that same module. It selects read only for
the exact selected next supported read. Manual outcome application, Stop
and P4 use the explicit local session-SQL policy. These internal literal
choices are not request/model inputs.

```python
def _batch_local_clock(stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
                       *, local_sql_only: bool = False) -> ContinuationLocalActionClock:
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_execution import continuation_chunk
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    action = policy.actions.session_sql
    index = len(stack.frames) - (2 if type(stack.frames[-1]) is AdvisorFrame else 1)
    if not local_sql_only and index >= 0 and type(stack.frames[index]) is ToolBatchFrame:
        frame = stack.frames[index]
        value = validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator))
        response = decode_continuation_response(continuation_chunk(chunks, frame.response))
        if (value.phase == "call_ready" or value.phase == "call_active" and value.current_call is not None
            and value.current_call.phase == "prepared") and value.next_tool_index < len(response.tool_batch.calls):
            call = response.tool_batch.calls[value.next_tool_index]
            arguments, error = parse_json_strict(call.function.arguments)
            if error is None and type(arguments) is dict and continuation_tool_support(call.function.name, arguments) == "read":
                action = policy.actions.read
    return ContinuationLocalActionClock(action)
```

- [ ] Rename the complete B3 `checkpoint` implementation to
  `_checkpoint_value`, leaving all its branches, required-work projection,
  cancellation preservation and arguments unchanged. Add this exact public
  replacement. Rename complete `refresh_after_owner`, `prepare_owner` and
  `load_actual_owner` bodies to `_refresh_after_owner_value`,
  `_prepare_owner_value` and `_load_actual_owner_value`; retain their full
  existing bodies. Add these wrappers to the same nominal class:

```python
def local_clock(self) -> ContinuationLocalActionClock:
    value = self.ctx.continuation_local_clock
    if type(value) is not ContinuationLocalActionClock:
        raise AuditIntegrityError("actual batch lost its process-local timing owner")
    return value


async def checkpoint(self, *, consumed_effect_key: str | None = None, defer_cancellation: bool = False,
                     retained_provider: tuple[str, str, PrivateChunkRef] | None = None,
                     admitted_primary: tuple[str, str, PrivateChunkRef] | None = None,
                     completed_memory: bool = False) -> None:
    async def retain() -> None:
        await self._checkpoint_value(consumed_effect_key=consumed_effect_key,
            defer_cancellation=defer_cancellation, retained_provider=retained_provider,
            admitted_primary=admitted_primary, completed_memory=completed_memory)
    await self.local_clock().run("retention", retain)


async def refresh_after_owner(self) -> None:
    await self.local_clock().run("retention", self._refresh_after_owner_value)


async def prepare_owner(self, completion: ToolCompletionValue) -> ContinuationOwnerCommit:
    async def retain() -> ContinuationOwnerCommit:
        return await self._prepare_owner_value(completion)
    return await self.local_clock().run("retention", retain)


async def load_actual_owner(self) -> None:
    await self.local_clock().run("retention", self._load_actual_owner_value)
```

- [ ] In `start_continuation_tool_batch`, put its exact
  `_initialize_continuation_batch_live(...)` call in an async local
  `prepare()->None` as below. In the resume body put its exact
  `_prepare_continuation_call_live(...)` call in the exact closure below.
  No preparation reruns on a decoded already-prepared call.

```python
clock = ctx.continuation_local_clock
if type(clock) is not ContinuationLocalActionClock:
    raise AuditIntegrityError("batch initializer lost its actual process-local timing owner")
async def prepare() -> None:
    await _initialize_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc,
        llm_messages=llm_messages, batch=batch)
await clock.run("preparation", prepare)
```

```python
clock = execution.local_clock()
async def prepare() -> Literal["prepared", "applied", "stop"]:
    return await _prepare_continuation_call_live(call_model=call_model, ctx=execution.ctx,
        llm_messages=llm_messages, batch=batch, call=call)
prepared = await clock.run("preparation", prepare)
```

- [ ] Rename the five complete bodies without changing their parameters:
  `start_continuation_tool_batch` → `_start_continuation_tool_batch_value`;
  `resume_continuation_tool_batch` → `_resume_continuation_tool_batch_value`;
  `resume_continuation_manual_advisor` → `_resume_continuation_manual_advisor_value`;
  `stop_continuation_tool_batch` → `_stop_continuation_tool_batch_value`;
  service `persist_continuation_tool_batch` → `_persist_continuation_tool_batch_value`.
  Their exact complete public wrappers are below. The manual/Stop bodies
  receive no new provider clock or SDK admission.

```python
async def start_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _start_continuation_tool_batch_value(call_model=call_model, ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id, required_work=required_work)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, operation=execute)


async def resume_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _resume_continuation_tool_batch_value(call_model=call_model, ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id,
            required_work=required_work, provider_owner=provider_owner)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, operation=execute)


async def resume_continuation_manual_advisor(
    *, ctx: ToolBatchContext, acc: BatchAccumulator, llm_messages: list[dict[str, Any]],
    stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...], checkpoint_id: str,
    required_work: RequiredWorkBinding, provider_owner: ProviderInvocationOwner,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _resume_continuation_manual_advisor_value(ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id,
            required_work=required_work, provider_owner=provider_owner)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, local_sql_only=True, operation=execute)


async def stop_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _stop_continuation_tool_batch_value(call_model=call_model, ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id, required_work=required_work)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, local_sql_only=True, operation=execute)


async def persist_continuation_tool_batch(
    self, *, dispatch: _DispatchOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.tool_batch import _run_batch_entry
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await self._persist_continuation_tool_batch_value(dispatch=dispatch, ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id, required_work=required_work)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, local_sql_only=True, operation=execute)
```

The exact Stop SQL submission and projection in `_stop_continuation_tool_batch_value`
form one async `retain()->StoredContinuationSnapshot` closure. Move its complete
span from `sql,projection = ...reserve_pair` through its nominal `actual`
check into that closure, append `execution.raise_deferred()` then `return actual`, and execute
`actual = await execution.local_clock().run("retention",retain)`.
The continuation clock check precedes ticket reservation. Its existing
post-write prefix projection and `execution.raise_deferred()` remain after
the closure; original cancellation objects are not discarded. P4 business
SQL remains physical work; its complete receipt application/checkpoint is V.

- [ ] Add the complete clock tests below. They use finite controlled
  timings and actual child-task join, with no SDK replacement or DB outcome
  fabrication. The actual held-SQL/fence behavior is independently required
  by END E7 and D/W; pure clock checks cannot certify it.

```python
import asyncio
import pytest
from elspeth.web.composer.continuation_local_clock import ContinuationLocalActionClock
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy


def local_policy(*, physical=0.2, retention=0.08):
    return ContinuationActionPolicy(preparation=0.02, guard=None, quota=None,
        physical=physical, retention=retention)


@pytest.mark.asyncio
async def test_repeated_retention_pieces_share_one_allowance():
    clock = ContinuationLocalActionClock(local_policy())
    await clock.run("retention", lambda: asyncio.sleep(0.05))
    with pytest.raises(TimeoutError):
        await clock.run("retention", lambda: asyncio.sleep(0.05))


@pytest.mark.asyncio
async def test_nested_retention_consumes_v_without_spending_h_twice():
    clock = ContinuationLocalActionClock(local_policy(physical=0.08, retention=0.15))
    async def physical():
        await asyncio.sleep(0.02)
        await clock.run("retention", lambda: asyncio.sleep(0.10))
        await asyncio.sleep(0.02)
        return "actual result"
    assert await clock.run("physical", physical) == "actual result"


@pytest.mark.asyncio
async def test_nested_same_stage_cannot_refresh_retention():
    clock = ContinuationLocalActionClock(local_policy(retention=0.06))
    async def retain():
        await asyncio.sleep(0.04)
        return await clock.run("retention", lambda: asyncio.sleep(0.04))
    with pytest.raises(TimeoutError):
        await clock.run("retention", retain)


@pytest.mark.asyncio
async def test_joined_owner_is_finished_and_projected_before_expiry_refuses():
    clock = ContinuationLocalActionClock(local_policy(retention=0.02))
    actual = asyncio.create_task(asyncio.sleep(0.06, result="actual owned result"))
    cancellations = []
    projected = []
    async def retain():
        while not actual.done():
            try:
                await asyncio.shield(actual)
            except asyncio.CancelledError as original:
                cancellations.append(original)
        projected.append(actual.result())
        return actual.result()
    with pytest.raises(TimeoutError):
        await clock.run("retention", retain)
    assert actual.done() and not actual.cancelled()
    assert projected == ["actual owned result"]
    assert len(cancellations) == 1
```

- [ ] Behavioral RED: reset `_remaining[stage]` to its original allowance
  at every entry; the repeated-retention test must fail. Restore it.
  Omit H pause/resume; the nested retention positive control must fail.
  Restore it. Remove explicit expiry observation after the joined result;
  the final refusal assertion must fail. Restore it. Add the affected real
  B7/F33/D/END tests and run with native exit capture. Commit only these
  three named product files and the named clock test after focused checks
  and applicable whole-tree gates pass.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_local_clock.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/integration/web/composer/test_continuation_provider_capture.py >"$COMPOSER_LOG_DIR/batch-local-clock.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-local-clock.log"
test "$composer_exit" -eq 0
```

## B7: Behavioral RED/GREEN for the exact result, owner and P4 boundaries

**Create:** `tests/unit/web/composer/test_continuation_tool_batch.py`.
**Extend:** `tests/unit/web/sessions/test_continuation_cold_restore.py`, `tests/integration/web/composer/test_continuation_driver_equivalence.py` and `tests/testcontainer/web/test_composer_continuation_postgres.py` from C4b.

The unit codec/owner packet is importable before its behavioral negative control is taken. Missing imports, collection errors or environment setup are not RED evidence. The complete driver tests from the transport companion are the real cold-owner/next-wire oracle; the focused tests here exercise the new internal seams rather than replacing the actual driver with a canned final result.

- [ ] Add this complete pure module. It uses the existing `_empty_state` helper and actual `ToolResult`/validation/receipt dataclasses. The private edge-contract payload is preserved through the full nominal codec. The negative controls deliberately remove one internal field or mislabel an active phase and require the real behavioral assertions to fail when the corresponding guard is temporarily removed.

```python
from uuid import UUID

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.continuation_tool_batch import (
    decode_tool_result_value, encode_tool_result_value, project_p4_receipt, PreparedP4Value,
)
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt
from tests.unit.web.composer._helpers import _empty_state


def test_tool_result_roundtrip_preserves_private_and_nonwire_fields():
    state = _empty_state()
    result = ToolResult(success=False, updated_state=state, validation=state.validate(), affected_nodes=(),
        data={"message": "Keep café and exact field labels"}, prior_validation=state.validate(),
        post_call_hints=("first", "second"), plugin_schemas={"source:csv": {"type": "object", "required": ["path"]}},
        applied_component={"edges": [{"id": "retained-edge", "label": "customer_key"}]},
        _validation_snapshot_hash="f" * 64, _state_validation_withheld=True)
    value = encode_tool_result_value("set_metadata", result)
    restored = decode_tool_result_value(type(value).model_validate_json(value.model_dump_json(), strict=True), pre_state=state)
    assert restored.updated_state is state
    assert restored.prior_validation == result.prior_validation
    assert restored.plugin_schemas == result.plugin_schemas
    assert restored.applied_component == result.applied_component
    assert restored._validation_snapshot_hash == "f" * 64
    assert restored._state_validation_withheld is True
    assert restored.post_call_hints == ("first", "second")
    assert canonical_json(restored.to_dict()) == canonical_json(result.to_dict())


def test_p4_unavailable_receipt_never_invents_rows_or_a_checkpoint():
    receipt = PersistedComposerTurnReceipt(effect_key="batch:x:p4", input_sha256="a" * 64,
        assistant_id=None, current_state_id=None, checkpoint_disposition="unavailable", creator_operation_id="actual-writer",
        inserted_state_ids=(), authored_state_hash=None, chat_message_ids=(), unwind_audit_failed=True)
    prepared = PreparedP4Value(assistant_content="Actual substituted status", raw_content=None,
        redacted_calls_json="[]", redacted_rows_json="[]", withheld_replies_json="[]",
        assistant_row_uses_current_dispatch=False, crash_pending=True, expected_current_state_id=None,
        source_audit_json='{"authorings":[],"invocations":[]}')
    projected = project_p4_receipt(receipt=receipt, prepared=prepared, attempted_calls=3, verification_failure=None)
    assert projected.unwind_audit_failed
    assert projected.persisted_assistant_message_id is None and projected.persisted_assistant_content is None
    assert projected.failed_turn.tool_calls_attempted == 3 and projected.failed_turn.tool_responses_persisted == 0
    assert projected.audit_outcome is None
    with pytest.raises(ValueError):
        PersistedComposerTurnReceipt.model_validate({**receipt.model_dump(), "assistant_id": UUID(int=1)})
    with pytest.raises(ValueError):
        PersistedComposerTurnReceipt.model_validate({**receipt.model_dump(), "inserted_state_ids": (UUID(int=2),)})
    with pytest.raises(ValueError):
        PersistedComposerTurnReceipt.model_validate({**receipt.model_dump(), "unwind_audit_failed": False})
```

- [ ] Add this complete real selected-batch fixture helper to `test_continuation_cold_restore.py`. The frames F7 helpers `new_evidence` and `pin_evidence` create actual ingress, bootstrap, current SQL ownership and private owner chunks. This fixture adds a deliberately explicit admitted call/batch value for SQL owner integration; it does not claim full provider execution. The full driver test covers the real provider-to-tool path separately.

```python
from dataclasses import replace
from uuid import UUID

from elspeth.contracts.composer_audit import PrivateToolAuthoring
from elspeth.core.canonical import canonical_json
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _AdmittedToolBatch
from elspeth.web.composer.audit import begin_dispatch
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import encode_continuation_response, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_tool_batch import encode_batch_chunk, _VALIDATION
from elspeth.web.composer.tool_batch import ContinuationBatchState, ContinuationCallState, capture_continuation_batch_value
from elspeth.web.composer.continuation_tool_receipts import ContinuationToolCompletion, ToolCompletionValue
from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, ContinuationOwnerCommit, prepare_effect_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from tests.unit.web.composer.test_continuation_evidence import new_evidence, pin_evidence
from tests.unit.web.composer.test_continuation_response import completion


async def selected_proposal_call(store, *, prepare_owner: bool = True):
    engine, _repository, authority, _service, sid = store
    _record, _seed, _action, running, evidence, messages = await new_evidence(store)
    stored, _evidence_chunk = pin_evidence(store, running, evidence, messages)
    root = decode_continuation_snapshot(stored.payload_json)
    state = evidence.effects.latest_verified_state
    arguments = {"patch": {"description": "Retained café proposal"}}
    call = _AdmittedToolCall("actual-owner-call", _AdmittedToolFunction("set_metadata", canonical_json(arguments)))
    response = encode_continuation_response(replace(completion(), tool_batch=_AdmittedToolBatch((call,), frozenset((call.id,)))))
    audit = begin_dispatch(call.id, call.function.name, arguments, version_before=state.version, actor="composer-web:user:alice")
    authoring = PrivateToolAuthoring(call.id, call.function.name, canonical_json(arguments), None, call.function.arguments)
    projected = ToolCompletionValue(projection="proposal", tool_name=call.function.name, proposal_tool_name=call.function.name,
        state_json=canonical_json(state.to_dict()), validation_json=canonical_json(_VALIDATION.dump_python(state.validate(), mode="json")))
    effect_key = f"batch:{response.reference.digest}:call:0"
    current = ContinuationCallState(0, call, state, state.version, phase="prepared", tool_name=call.function.name,
        private_raw_arguments=call.function.arguments, private_arguments_canonical=canonical_json(arguments),
        arguments=arguments, audit_arguments=arguments, audit=audit, effect_key=effect_key if prepare_owner else None,
        original_creator_id=running.session_operation_context.fence.operation_id,
        original_creator_epoch=running.session_operation_context.fence.operation_epoch,
        completion_json=canonical_json(projected.model_dump(mode="json")) if prepare_owner else None)
    batch = ContinuationBatchState(state, state.validate(), None, 0, entry_state=state, phase="call_active", assistant_message_index=len(messages),
        provider_model_version="fixture-provider", current_call=current,
        decoded_args_by_call_id={call.id: arguments}, private_authorings_by_call={call.id: authoring})
    accumulator = encode_batch_chunk(capture_continuation_batch_value(batch, (call,)))
    child = ToolBatchFrame(phase="effect_reconciling", response=response.reference, accumulator=accumulator.reference,
        next_tool_index=0, settled_effect_ids=())
    following = root.model_copy(update={"frames": (*root.frames, child)})
    selected = authority.commit_transition(running, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=encode_continuation_snapshot(following), chunks=(response, accumulator),
        event=ContinuationEvent("tool_prepared", canonical_json({"fixture": "actual owner transaction"})))
    if not prepare_owner:
        return running, state, audit, None, evidence.request_id
    with locked_session_transaction(engine, str(sid)) as conn:
        prepared = prepare_effect_on_connection(conn, running, effect_key=effect_key, input_sha256="b" * 64,
            owner="session_sql", next_snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
    commit = ContinuationOwnerCommit(running, effect_key, "b" * 64, prepared.checkpoint_id, prepared.cursor,
        ContinuationToolCompletion(projected, audit, authoring))
    return running, state, audit, commit, evidence.request_id
```

- [ ] Add this complete actual commit-before-caller-finish test in that same module. It proves the actual SQL owner stores one finished invocation, then forbids another `finish_success` and applies the selected current-authorized retained receipt. The full receipt is compared after JSON decode, including original timestamps/active latency/private authoring; no recovery clock contributes to it.

```python
@pytest.mark.asyncio
async def test_active_private_call_cannot_claim_a_public_tool_ready_boundary(operation_store):
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
    _engine, repository, authority, _service, _sid = operation_store
    running, _state, _audit, _commit, _request_id = await selected_proposal_call(operation_store)
    try:
        bundle = authority.read_continuation_bundle(running)
        root = decode_continuation_snapshot(bundle.snapshot.payload_json)
        frame = root.frames[-1]
        assert type(frame) is ToolBatchFrame
        chunk = continuation_chunk(bundle.chunks, frame.accumulator)
        actual = validate_batch_frame(frame, chunk)
        assert actual.phase == "call_active" and actual.current_call.phase == "prepared"
        with pytest.raises(AuditIntegrityError, match="public batch phase"):
            validate_batch_frame(frame.model_copy(update={"phase": "tool_ready"}), chunk)
        assert authority.load_continuation_snapshot(running) == bundle.snapshot
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_proposal_cut_after_commit_reuses_actual_invocation_without_caller_finish(operation_store, monkeypatch):
    import elspeth.web.composer.continuation_tool_receipts as receipts
    from elspeth.web.sessions.continuation_owner_results import decode_proposal_owner_result
    engine, repository, authority, service, sid = operation_store
    running, state, audit, commit, request_id = await selected_proposal_call(operation_store)
    actual_finish = receipts.finish_success
    finished = []
    def counted_finish(*args, **kwargs):
        value = actual_finish(*args, **kwargs)
        finished.append(value)
        return value
    monkeypatch.setattr(receipts, "finish_success", counted_finish)
    try:
        proposal = await service.create_composition_proposal(session_id=sid,
            session_operation_context=running.session_operation_context, tool_call_id=audit.tool_call_id,
            tool_name="set_metadata", summary="Retained café proposal", rationale="Actual requested change", affects=("graph",),
            arguments_json={"patch": {"description": "Retained café proposal"}},
            arguments_redacted_json={"patch": {"description": "Retained café proposal"}}, base_state_id=None,
            actor=audit.actor, user_message_id=UUID(request_id), continuation=commit)
        assert len(finished) == 1
        def forbidden_finish(*args, **kwargs):
            raise AssertionError("a committed proposal must not finish its tool invocation again")
        monkeypatch.setattr(receipts, "finish_success", forbidden_finish)
        head = authority.load_continuation_snapshot(running)
        kind, canonical, receipt = authority.load_pending_tool_owner_result(running,
            expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor, effect_key=commit.effect_key)
        assert kind == "proposal" and decode_proposal_owner_result(canonical) == proposal
        wire = canonical_json(receipt.model_dump(mode="json")).encode().decode()
        restored = receipts.decode_retained_tool_receipt(wire)
        assert restored.tool_result is not None
        invocation = receipts.invocation_from_receipt(restored, audit=audit)
        assert invocation.started_at == finished[0].started_at
        assert invocation.finished_at == finished[0].finished_at and invocation.latency_ms == finished[0].latency_ms
        assert invocation.private_authoring == commit.tool_completion.private_authoring
        assert receipts.decode_tool_result_value(restored.tool_result, pre_state=state).updated_state is state
        with pytest.raises(ContinuationCASConflict):
            await service.create_composition_proposal(session_id=sid,
                session_operation_context=running.session_operation_context, tool_call_id=audit.tool_call_id,
                tool_name="set_metadata", summary="Retained café proposal", rationale="Actual requested change", affects=("graph",),
                arguments_json={"patch": {"description": "Retained café proposal"}},
                arguments_redacted_json={"patch": {"description": "Retained café proposal"}}, base_state_id=None,
                actor=audit.actor, user_message_id=UUID(request_id), continuation=commit)
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Add this current-selected-owner refusal test. A caller-provided result projection cannot change a retained prepared call's identity or validation. The complete DB row values are compared, not filtered counts. Both ordinary SQLite and C4b's PostgreSQL fixture import this same test body.

```python
@pytest.mark.asyncio
async def test_foreign_tool_completion_refuses_before_proposal_or_effect_mutation(operation_store):
    from dataclasses import replace
    from sqlalchemy import select
    from elspeth.web.sessions.models import composition_proposals_table, composer_continuation_effects_table
    engine, repository, _authority, service, sid = operation_store
    running, _state, audit, commit, request_id = await selected_proposal_call(operation_store)
    def rows():
        with engine.connect() as conn:
            return (conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == str(sid))).all(),
                conn.execute(select(composer_continuation_effects_table).where(composer_continuation_effects_table.c.session_id == str(sid))).all())
    before = rows()
    forged = replace(commit, tool_completion=replace(commit.tool_completion,
        value=commit.tool_completion.value.model_copy(update={"proposal_tool_name": "set_pipeline"})))
    try:
        with pytest.raises(AuditIntegrityError, match="selected prepared actual call"):
            await service.create_composition_proposal(session_id=sid,
                session_operation_context=running.session_operation_context, tool_call_id=audit.tool_call_id,
                tool_name="set_metadata", summary="Retained café proposal", rationale="Actual requested change", affects=("graph",),
                arguments_json={"patch": {"description": "Retained café proposal"}},
                arguments_redacted_json={"patch": {"description": "Retained café proposal"}}, base_state_id=None,
                actor=audit.actor, user_message_id=UUID(request_id), continuation=forged)
        assert rows() == before
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Replace C3.8b's old `owner_kind` parameter list with `("direct_proposal","pipeline_proposal")` and remove its unreachable fallback call to `persist_compose_turn_async`. P4 now has a stronger selected-batch input contract and is covered by the complete real fixture/tests below in the same `test_continuation_cold_restore.py`. Keep both actual proposal atomicity cases unchanged.

```python
from hashlib import sha256
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_tool_batch import PreparedP4Value, p4_semantic_input, _read_json_value, _WITHHELD
from elspeth.web.composer.withheld_replies import WithheldReply


async def selected_empty_p4(store):
    engine, _repository, authority, _service, sid = store
    _record, _seed, _action, running, evidence, messages = await new_evidence(store)
    stored, _evidence_chunk = pin_evidence(store, running, evidence, messages)
    root = decode_continuation_snapshot(stored.payload_json)
    state = evidence.effects.latest_verified_state
    response = encode_continuation_response(replace(completion(), tool_batch=_AdmittedToolBatch((), frozenset())))
    reply = WithheldReply("advisor_repair_tool_turn", "Exact unpublished café reply")
    recorder = BufferingRecorder()
    recorder.record_withheld_reply(reply.origin, reply.content)
    buffer = encode_recorder_snapshot(recorder.snapshot_for_continuation(drained=RecorderDrainCursors()))
    prepared_value = PreparedP4Value(assistant_content="Actual P4 intermediate status", raw_content=None,
        redacted_calls_json="[]", redacted_rows_json="[]",
        withheld_replies_json=canonical_json(_WITHHELD.dump_python((reply,), mode="json")),
        assistant_row_uses_current_dispatch=False, crash_pending=True, expected_current_state_id=None,
        source_audit_json=canonical_json({"invocations": [], "authorings": []}))
    batch = ContinuationBatchState(state, state.validate(), None, 0, entry_state=state, phase="p4_pending",
        assistant_message_index=len(messages), provider_model_version="fixture-provider",
        p4_input_json=canonical_json(prepared_value.model_dump(mode="json")))
    accumulator = encode_batch_chunk(capture_continuation_batch_value(batch, ()))
    child = ToolBatchFrame(phase="result_retained", response=response.reference, accumulator=accumulator.reference,
        next_tool_index=0, settled_effect_ids=())
    parent = root.frames[-1].model_copy(update={"audit_buffer": buffer.reference})
    following = root.model_copy(update={"frames": (*root.frames[:-1], parent, child)})
    selected = authority.commit_transition(running, expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=encode_continuation_snapshot(following), chunks=(response, accumulator, buffer),
        event=ContinuationEvent("tool_prepared", canonical_json({"fixture": "actual P4 owner"})))
    key = f"batch:{response.reference.digest}:p4"
    digest = stable_hash(_read_json_value(p4_semantic_input(prepared_value)))
    with locked_session_transaction(engine, str(sid)) as conn:
        selected = prepare_effect_on_connection(conn, running, effect_key=key, input_sha256=digest, owner="session_sql",
            next_snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
    commit = ContinuationOwnerCommit(running, key, digest, selected.checkpoint_id, selected.cursor)
    return running, commit, prepared_value, reply


async def write_empty_p4(service, running, commit, prepared, reply):
    return await service.persist_compose_turn_async(session_id=str(running.claim.session_id),
        assistant_content=prepared.assistant_content, raw_content=prepared.raw_content,
        redacted_assistant_tool_calls=(), redacted_tool_rows=(), private_tool_evidence=(), private_tool_failures=(),
        parent_composition_state_id=None, expected_current_state_id=None, writer_principal="compose_loop",
        plugin_crash_pending=True, session_operation_context=running.session_operation_context,
        continuation=commit, continuation_invocations=(), continuation_withheld_replies=(reply,))


@pytest.mark.asyncio
async def test_p4_actual_rows_withheld_receipt_and_private_head_share_one_transaction(operation_store, monkeypatch):
    import elspeth.web.sessions.service as service_module
    from sqlalchemy import select
    from elspeth.web.sessions.models import chat_messages_table, composer_continuation_effects_table
    engine, repository, authority, service, sid = operation_store
    running, commit, prepared, reply = await selected_empty_p4(operation_store)
    def rows():
        with engine.connect() as conn:
            return (conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid))).all(),
                conn.execute(select(composer_continuation_effects_table).where(composer_continuation_effects_table.c.session_id == str(sid))).all())
    before = rows()
    head_before = authority.load_continuation_snapshot(running)
    actual_retain = service_module.retain_owner_result_on_connection
    failure = AuditIntegrityError("controlled after-P4-retention failure")
    def after_retention(*args, **kwargs):
        actual_retain(*args, **kwargs)
        raise failure
    try:
        with monkeypatch.context() as patch:
            patch.setattr(service_module, "retain_owner_result_on_connection", after_retention)
            with pytest.raises(AuditIntegrityError) as caught:
                await write_empty_p4(service, running, commit, prepared, reply)
            assert caught.value is failure
        assert rows() == before and authority.load_continuation_snapshot(running) == head_before
        actual = await write_empty_p4(service, running, commit, prepared, reply)
        assert actual.assistant_id is not None and not actual.unwind_audit_failed
        head = authority.load_continuation_snapshot(running)
        receipt = authority.read_continuation_p4_result(running,
            expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert str(receipt.assistant_id) == actual.assistant_id
        assert receipt.checkpoint_disposition == "empty" and receipt.inserted_state_ids == ()
        assert receipt.withheld_hashes == (sha256(reply.content.encode("utf-8")).hexdigest(),)
        assert receipt.invocation_ids == () and receipt.invocation_hashes == ()
        assert len(receipt.chat_message_ids) == 2
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "replacement"), (
    ("raw_content", "An unretained assistant original"),
    ("tool_calls", [{"id": "not-issued", "type": "function", "function": {"name": "set_metadata", "arguments": "{}"}}]),
    ("writer_principal", "route_system_message"),
))
async def test_p4_readback_rejects_changed_actual_assistant_bytes(operation_store, field, replacement):
    from sqlalchemy import update
    from elspeth.web.sessions.models import chat_messages_table
    engine, repository, authority, service, sid = operation_store
    running, commit, prepared, reply = await selected_empty_p4(operation_store)
    try:
        actual = await write_empty_p4(service, running, commit, prepared, reply)
        head = authority.load_continuation_snapshot(running)
        assert authority.read_continuation_p4_result(running,
            expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor).assistant_id is not None
        with engine.begin() as conn:
            conn.execute(update(chat_messages_table).where(chat_messages_table.c.session_id == str(sid),
                chat_messages_table.c.id == actual.assistant_id).values({field: replacement}))
        with pytest.raises(AuditIntegrityError, match="transcript rows|assistant row"):
            authority.read_continuation_p4_result(running,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert authority.load_continuation_snapshot(running) == head
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_p4_actual_rollback_retains_unavailable_without_fabricating_assistant(operation_store, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import select
    from elspeth.web.sessions.models import chat_messages_table
    engine, repository, authority, service, sid = operation_store
    running, commit, prepared, reply = await selected_empty_p4(operation_store)
    with engine.connect() as conn:
        before = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid))).all()
    failure = OperationalError("controlled insert failure", {}, RuntimeError("controlled database fault"))
    def fail_actual_insert(*args, **kwargs):
        raise failure
    try:
        with monkeypatch.context() as patch:
            patch.setattr(service, "_insert_chat_message", fail_actual_insert)
            actual = await write_empty_p4(service, running, commit, prepared, reply)
        assert actual.assistant_id is None and actual.unwind_audit_failed
        head = authority.load_continuation_snapshot(running)
        receipt = authority.read_continuation_p4_result(running,
            expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert receipt.checkpoint_disposition == "unavailable" and receipt.unwind_audit_failed
        assert receipt.assistant_id is None and receipt.chat_message_ids == () and receipt.inserted_state_ids == ()
        assert receipt.invocation_ids == () and receipt.withheld_hashes == ()
        with engine.connect() as conn:
            assert conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid))).all() == before
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Run the focused behavior tests once with the complete importable packet. For the codec behavioral RED, temporarily change only `state_validation_withheld=result._state_validation_withheld` in B1's encoder to `state_validation_withheld=False`. Run the exact first command below and require native exit 1 with the assertion `restored._state_validation_withheld is True` failing; imports/collection/setup must succeed. Restore that expression, then run the GREEN commands and require native exit 0. For the owner behavioral negative control, temporarily omit only `tool_receipt_json=tool_receipt_json` from the same-transaction effect update; the actual after-commit test must fail because no retained invocation is readable. Restore the update and rerun the exact focused owner test.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_tool_batch.py::test_tool_result_roundtrip_preserves_private_and_nonwire_fields >"$COMPOSER_LOG_DIR/batch-codec-red.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-codec-red.log"
test "$composer_exit" -eq 1
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_tool_batch.py tests/unit/web/sessions/test_continuation_cold_restore.py >"$COMPOSER_LOG_DIR/batch-owner-green.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-owner-green.log"
test "$composer_exit" -eq 0
```

- [ ] Run the actual existing lexical branch tests after B2's relocation. These test the ordinary v1 entry with its existing real fixtures and external provider fakes, covering full result/guard projection, manual Advisor outcomes, interpretation handlers and the source b003 safe rejection path. Require native exit 0; do not patch the extracted helper's return to a fixture `_DispatchOutcome`.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_tool_batch_prompt_guard_projection.py tests/unit/web/composer/test_session_tool_prompt_guard_projection.py tests/unit/web/composer/test_tool_batch_rejection_llm_call_audit.py tests/unit/web/composer/test_advisor_audit.py tests/unit/web/composer/test_tool_dispatch_boundary.py tests/unit/web/composer/test_pending_review_advisor_terminal.py tests/unit/web/composer/test_wire_fidelity_matrix.py >"$COMPOSER_LOG_DIR/batch-existing-branches.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-existing-branches.log"
test "$composer_exit" -eq 0
```

- [ ] Add this complete unsupported-dispatch counterexample to the real integration module. It substitutes provider answers and a tripwire which forwards every ordinary real handler unchanged. The real worker must durably apply a precise refusal before the preview handler branch, then stop at the existing settled first-call cut. No handler result or final Composer result is fabricated.

```python
@pytest.mark.asyncio
async def test_continuation_unsupported_preview_refuses_before_real_dispatch(operation_store, tmp_path, monkeypatch):
    import litellm
    from sqlalchemy import select
    import elspeth.web.composer.tool_batch as tool_batch_module
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
    from elspeth.web.sessions.models import composition_states_table
    from tests.helpers.continuation_driver import (
        DriverCutObservation, SDKReply, clone_admitted_database, fresh_driver_environment,
        ordinary_driver_script, run_driver_attempt, seed_driver_case, tool_reply,
    )
    case = await seed_driver_case(operation_store, tmp_path / "data")
    database_url = clone_admitted_database(case, tmp_path / "unsupported.db")
    env = fresh_driver_environment(database_url, case)
    sdk = ordinary_driver_script(case)
    primary = sdk.replies["primary"]
    sdk.replies["primary"] = (primary[0], SDKReply(content="Check the current pipeline before the next change.", calls=(
        tool_reply("preview:unsupported", "preview_pipeline", {}),
        tool_reply("not-yet-issued", "set_metadata", {"patch": {"description": "Future call"}}),
    )), *primary[2:])
    cut = DriverCutObservation("partial_batch")
    actual_execute = tool_batch_module.execute_tool
    dispatched = []
    def guarded_execute(tool_name, arguments, state, catalog, **kwargs):
        dispatched.append(tool_name)
        if tool_name == "preview_pipeline":
            raise AssertionError("unsupported preview entered its real dispatch owner")
        return actual_execute(tool_name, arguments, state, catalog, **kwargs)
    try:
        with env.sessions._engine.connect() as conn:
            states_before = conn.execute(select(composition_states_table).where(
                composition_states_table.c.session_id == str(case.session_id))).all()
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk.complete)
            patch.setattr(tool_batch_module, "execute_tool", guarded_execute)
            await run_driver_attempt(env, sdk, patch, cut=cut)
        assert cut.selected is not None and "preview_pipeline" not in dispatched
        root = decode_continuation_snapshot(cut.selected.payload_json)
        frame = root.frames[-1]
        assert type(frame) is ToolBatchFrame
        selected = tuple(canonical for digest, canonical in cut.selected.chunks if digest == frame.accumulator.digest)
        assert len(selected) == 1
        batch = decode_batch_chunk(PrivateChunk(frame.accumulator, selected[0]))
        assert batch.next_tool_index == 1 and batch.current_call is None
        assert len(batch.tool_outcomes) == 1
        assert batch.tool_outcomes[0].call_id == "preview:unsupported"
        assert batch.tool_outcomes[0].error_class == "ToolArgumentError"
        assert "supported continuation effect owner" in batch.tool_outcomes[0].error_message
        with env.sessions._engine.connect() as conn:
            assert conn.execute(select(composition_states_table).where(
                composition_states_table.c.session_id == str(case.session_id))).all() == states_before
    finally:
        env.dispose()
```

Include this test in the D packet's normal pathspec/commit; it uses D1–D3's complete real worker fixtures and imports its own nominal frame/codec dependencies.

### B7.1: Prove actual Stop closure and received-batch admission

- [ ] Add these tests to the same actual-store cold-restore module as
  `selected_proposal_call`. Its `prepare_owner=False` fixture branch retains
  a real selected private call before any effect/result owner is prepared;
  its default branch retains the real prepared SQL owner. Neither fixture
  is credited as a complete SDK/driver run.

```python
from sqlalchemy import select
from elspeth.web.composer.continuation_execution import continuation_chunk
from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, stop_closed_batch_value
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.models import metadata


def batch_store_rows(engine):
    with engine.connect() as conn:
        return {table.name: tuple(conn.execute(select(table)).all()) for table in metadata.sorted_tables}


@pytest.mark.asyncio
async def test_stop_closes_only_the_actual_unissued_call_and_preserves_its_prefix(operation_store):
    engine, repository, authority, _service, sid = operation_store
    running, _state, _audit, absent_commit, _request_id = await selected_proposal_call(operation_store, prepare_owner=False)
    assert absent_commit is None
    try:
        bundle = authority.read_continuation_bundle(running)
        root = decode_continuation_snapshot(bundle.snapshot.payload_json)
        frame = root.frames[-1]
        before_value = decode_batch_chunk(continuation_chunk(bundle.chunks, frame.accumulator))
        assert before_value.phase == "call_active" and before_value.current_call.effect_key is None
        stopped = stop_closed_batch_value(before_value)
        chunk = encode_batch_chunk(stopped)
        kwargs = dict(expected_checkpoint_id=bundle.snapshot.checkpoint_id,
            expected_cursor=bundle.snapshot.cursor, accumulator=chunk)
        before = batch_store_rows(engine)
        with pytest.raises(AuditIntegrityError, match="actual parent cancellation"):
            authority.stop_continuation_batch(running, **kwargs)
        assert batch_store_rows(engine) == before
        actual_cancel = authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        assert actual_cancel is not None and actual_cancel.cancel_requested
        before = batch_store_rows(engine)
        altered = encode_batch_chunk(stopped.model_copy(update={"advisor_calls_used": stopped.advisor_calls_used + 1}))
        with pytest.raises(AuditIntegrityError, match="altered its actual completed prefix"):
            authority.stop_continuation_batch(running, **(kwargs | {"accumulator": altered}))
        assert batch_store_rows(engine) == before
        selected = authority.stop_continuation_batch(running, **kwargs)
        after = decode_continuation_snapshot(selected.payload_json)
        assert after.frames[:-1] == root.frames[:-1]
        assert after.effect_frontier == root.effect_frontier
        assert after.pending_owner_results == root.pending_owner_results
        assert after.frames[-1].response == frame.response
        assert after.frames[-1].next_tool_index == frame.next_tool_index == 0
        assert stopped.tool_outcomes == before_value.tool_outcomes == ()
        assert stopped.phase == "dispatch_ready" and stopped.current_call is None
        committed = batch_store_rows(engine)
        with pytest.raises(ContinuationCASConflict):
            authority.stop_continuation_batch(running, **kwargs)
        assert batch_store_rows(engine) == committed
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_stop_cannot_erase_an_actual_prepared_effect_or_any_store_bytes(operation_store):
    engine, repository, authority, _service, sid = operation_store
    running, _state, _audit, commit, _request_id = await selected_proposal_call(operation_store)
    assert commit is not None
    try:
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        bundle = authority.read_continuation_bundle(running)
        root = decode_continuation_snapshot(bundle.snapshot.payload_json)
        frame = root.frames[-1]
        value = decode_batch_chunk(continuation_chunk(bundle.chunks, frame.accumulator))
        assert value.current_call.effect_key == commit.effect_key
        forged = encode_batch_chunk(value.model_copy(update={"phase": "dispatch_ready",
            "stop_issued_batch": True, "current_call": None}))
        before = batch_store_rows(engine)
        with pytest.raises(AuditIntegrityError, match="actual prepared owner or retained result"):
            authority.stop_continuation_batch(running, expected_checkpoint_id=bundle.snapshot.checkpoint_id,
                expected_cursor=bundle.snapshot.cursor, accumulator=forged)
        assert batch_store_rows(engine) == before
        assert authority.load_continuation_snapshot(running) == bundle.snapshot
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Extend F33's actual
  `tests/integration/web/composer/test_continuation_provider_capture.py`
  with this received-primary initializer race. Its wrapper requests actual
  Stop before the real `batch_admitted` SQL write, checks both a forged
  frontier and duplicate CAS, and lets the real driver drain P4. It never
  supplies a synthetic provider receipt or clears the cancellation.

```python
@pytest.mark.asyncio
async def test_actual_received_batch_is_admitted_once_after_stop_before_p4(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
    case = await seed_driver_case(operation_store, tmp_path / "received-batch-data")
    env = fresh_driver_environment(str(operation_store[0].url), case)
    script = ordinary_driver_script(case)
    actual_capture = env.authority.commit_received_provider_capture
    admitted = []

    def capture(running, *, expected_checkpoint_id, expected_cursor, snapshot_json,
                chunks, effect_key, input_sha256, response, kind):
        kwargs = dict(expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
            snapshot_json=snapshot_json, chunks=chunks, effect_key=effect_key,
            input_sha256=input_sha256, response=response, kind=kind)
        if kind != "batch_admitted" or admitted:
            return actual_capture(running, **kwargs)
        stopped = env.authority.request_v2_cancel(session_id=case.session_id,
            operation_id=case.operation_id, actor_user_id="alice", auth_provider_type="local")
        assert stopped is not None and stopped[0].cancel_requested
        following = decode_continuation_snapshot(snapshot_json)
        frame = following.frames[-1]
        assert type(frame) is ToolBatchFrame and frame.response == response
        batch = decode_batch_chunk(continuation_chunk(chunks, frame.accumulator))
        assert batch.next_tool_index == 0 and batch.current_call is None and not batch.tool_outcomes
        before = capture_tables(env.sessions._engine)
        forged = following.model_copy(update={"effect_frontier": (*following.effect_frontier,
            EffectFrontierEntry(effect_id="invented-batch-effect", disposition="prepared", receipt_id=None))})
        with pytest.raises(AuditIntegrityError):
            actual_capture(running, **(kwargs | {"snapshot_json": encode_continuation_snapshot(forged)}))
        assert capture_tables(env.sessions._engine) == before
        selected = actual_capture(running, **kwargs)
        committed = capture_tables(env.sessions._engine)
        with pytest.raises(ContinuationCASConflict):
            actual_capture(running, **kwargs)
        assert capture_tables(env.sessions._engine) == committed
        admitted.append((selected.checkpoint_id, tuple(script.invocations)))
        return selected

    try:
        monkeypatch.setattr(litellm, "acompletion", script.complete)
        monkeypatch.setattr(env.authority, "commit_received_provider_capture", capture)
        await run_driver_attempt(env, script, monkeypatch)
        assert len(admitted) == 1 and tuple(script.invocations) == admitted[0][1]
        assert script.consumed == {"primary": 1}
        with env.sessions._engine.connect() as conn:
            parent = conn.execute(select(composer_async_operations_table).where(
                composer_async_operations_table.c.session_id == str(case.session_id),
                composer_async_operations_table.c.operation_id == case.operation_id)).one()
        assert parent.status == "failed" and parent.failure_code == "request_cancelled"
        assert parent.result_json is None and parent.cancel_requested_at is not None
    finally:
        env.dispose()
```

- [ ] Behavioral RED: remove only the SQL Stop owner's exact comparison to
  `stop_closed_batch_value(before)`; its changed-counter assertion must fail.
  Restore it. Route `batch_admitted` through ordinary `commit_transition`;
  the real initializer Stop race must fail at the actual canceled write.
  Restore the received-provider facade. Run these actual-store and driver
  tests with native exit capture and then the same two Stop test bodies
  imported into C4b's serial PostgreSQL module.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py tests/integration/web/composer/test_continuation_provider_capture.py >"$COMPOSER_LOG_DIR/batch-stop-and-received.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-stop-and-received.log"
test "$composer_exit" -eq 0
```

- [ ] Run the transport companion's complete driver equivalence cases after its D3 observer is bound to the actual new `persist_continuation_compose_turn` finish-once method. `p4_retained` must observe an actual `RequiredSQLReturned[AuditOutcome]` after the real business transaction commits and before B6's caller application. `partial_batch` must observe the actual settled first-call boundary; manual response must retain the actual child. Its oracle compares actual next provider wire, graph/audit/proposal/events/usage, original request IDs and drained suffixes after discarding owners. Require all named selectors to fire.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/integration/web/composer/test_continuation_driver_equivalence.py tests/integration/web/composer/test_continuation_driver_process_death.py >"$COMPOSER_LOG_DIR/batch-driver-equivalence.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/batch-driver-equivalence.log"
test "$composer_exit" -eq 0
```

- [ ] Add imports of `test_proposal_cut_after_commit_reuses_actual_invocation_without_caller_finish` and `test_foreign_tool_completion_refuses_before_proposal_or_effect_mutation` to the C4b serial PostgreSQL continuation module. Use its real PostgreSQL `operation_store` fixture; no SQLite fallback, dummy marker or skipped proof. Run `pytest tests/ -m testcontainer -n 0` through the main ordinary persistence gate before integration, since both effect schema and owner SQL changed. Run the applicable whole-tree AST/schema/wire gates, ruff and mypy through the normal main plan gate. This docs packet does not execute those commands.

- [ ] Review the diff of exactly these implementation/test paths, run the ordinary branch safety check, then commit the coherent batch/P4 packet with normal hooks. Do not stage lane documents or test logs. A working tree modified after gate capture needs the affected ordinary tests again; it does not inherit a previous tree's pass.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && git add src/elspeth/web/composer/continuation_tool_batch.py src/elspeth/web/composer/continuation_tool_receipts.py src/elspeth/web/composer/tool_batch.py src/elspeth/web/composer/audit.py src/elspeth/web/composer/session_tool.py src/elspeth/web/composer/turn_audit.py src/elspeth/web/composer/service.py src/elspeth/web/sessions/continuation.py src/elspeth/web/sessions/continuation_owner_results.py src/elspeth/web/sessions/models.py src/elspeth/web/sessions/service.py src/elspeth/web/sessions/protocol.py src/elspeth/web/coordination/repository.py tests/unit/web/composer/test_continuation_tool_batch.py tests/unit/web/sessions/test_continuation_cold_restore.py
cd "$COMPOSER_WORKTREE" && git diff --cached --stat
cd "$COMPOSER_WORKTREE" && git commit -m "feat(composer): resume actual tool batches and P4 owner receipts once"
```

The ordinary PostgreSQL module and driver-observer edit belong to their separately owned coherent test packet and are committed there, after their real gate exits are read. No implementation or test run is claimed by writing this plan.

## B9: Refuse only the selected uncommitted tool owner after durable Stop

This closes review E16. It does not weaken `stop_closed_batch_value`, generic
business mutation or generic checkpoints. B6's separately owned owed-P4
authority drains the exact completed prefix after this transition. O8 retains
the separate manual-provider failure path. A committed owner result takes the
existing owed-application path and never enters this refusal transaction.

**Modify:** `composer/continuation_tool_batch.py`, `composer/tool_batch.py`,
`coordination/composer_operation_authority.py`, and the narrow F32 Stop dispatch
selector in `composer/continuation_driver.py`. Tests go to the existing actual
cold-restore module and new
`tests/integration/web/composer/test_continuation_prepared_stop.py`.

- [ ] **B9.1 (2–5 minutes): Add the complete pure selected-call check and
  closed-prefix projection to `continuation_tool_batch.py`.** This computes
  B5's exact existing effect key/input digest from the retained full original
  response, actual call and complete `ToolCompletionValue`. It creates no tool
  result, finished invocation, exception, pending result or business receipt.

```python
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_response


def prepared_stop_batch_identity(
    frame: ToolBatchFrame, value: ContinuationBatchValue, response: PrivateChunk,
) -> tuple[str, str, Literal["session_sql", "read"]]:
    from elspeth.web.composer.continuation_tool_receipts import ToolCompletionValue
    if (type(frame) is not ToolBatchFrame or type(value) is not ContinuationBatchValue
            or frame.response != response.reference or value.phase != "call_active"
            or value.stop_issued_batch or value.failure is not None
            or value.p4_input_json is not None or value.p4_receipt_json is not None):
        raise AuditIntegrityError("prepared Stop lost its exact open selected batch")
    call = value.current_call
    if (call is None or call.phase != "prepared" or call.index != value.next_tool_index
            or call.effect_key is None or call.completion_json is None
            or call.owner_result_kind is not None or call.owner_result_json is not None
            or call.owner_tool_receipt_json is not None or call.memory_admitted or call.memory_result is not None
            or call.advisor_invocation_json is not None):
        raise AuditIntegrityError("prepared Stop cannot discard an admitted result or provider child")
    completion = decode_continuation_response(response)
    calls = completion.tool_batch.calls
    if call.index >= len(calls):
        raise AuditIntegrityError("prepared Stop index exceeds its original issued tuple")
    issued = calls[call.index]
    projected = ToolCompletionValue.model_validate_json(call.completion_json, strict=True)
    if (canonical_json(projected.model_dump(mode="json")) != call.completion_json
            or projected.tool_name != call.tool_name or call.call_id != issued.id
            or call.tool_name != issued.function.name or call.private_raw_arguments != issued.function.arguments
            or call.audit.tool_call_id != issued.id or call.audit.tool_name != call.tool_name
            or call.audit.version_before != call.pre_version
            or projected.state_json != call.pre_state_json or call.pre_state_json != value.state_json
            or call.index != frame.next_tool_index):
        raise AuditIntegrityError("prepared Stop changed the actual issued call, input or graph")
    key = f"batch:{frame.response.digest}:call:{call.index}"
    binding_arguments = call.audit.authority_arguments_hash or call.audit.arguments_hash
    digest = stable_hash({"schema": "composer.tool-input.v1", "response": frame.response.digest,
        "index": call.index, "call_id": call.call_id, "arguments": binding_arguments,
        "completion": projected.model_dump(mode="json")})
    if key != call.effect_key:
        raise AuditIntegrityError("prepared Stop substituted another actual tool effect")
    owner: Literal["session_sql", "read"] = "read" if projected.projection == "read" else "session_sql"
    return key, digest, owner


def stop_refused_owner_batch_value(before: ContinuationBatchValue) -> ContinuationBatchValue:
    call = before.current_call
    if (before.phase != "call_active" or call is None or call.phase != "prepared"
            or call.effect_key is None or call.completion_json is None or before.stop_issued_batch
            or before.failure is not None or call.owner_result_kind is not None
            or call.owner_result_json is not None or call.owner_tool_receipt_json is not None
            or call.memory_admitted or call.memory_result is not None or call.advisor_invocation_json is not None):
        raise AuditIntegrityError("refused Stop prefix contains an owed result or unrelated caller")
    return before.model_copy(update={"phase": "dispatch_ready", "stop_issued_batch": True, "current_call": None})
```

Keep `stop_refused_owner_batch_value` private to this closed SQL transition and
its explicit tests; it is not authority to discard a prepared row. The actual
authority below first verifies and durably refuses that exact row. The original
`stop_closed_batch_value` continues refusing a prepared owner.

- [ ] **B9.2 (2–5 minutes): Add the complete transaction below to the actual
  `ComposerAsyncOperationAuthority`.** Use its existing session lock, CAS and
  imports from C3. No business writer can race this check: supported proposals
  and interpretation rows retain their effect result/invocation in the SAME
  owner transaction (C3.8/B4); immutable reads have their sole SQL completion in
  `retain_read_owner_result`. A result-returning writer acquires the same session
  lock and requires prepared disposition before mutation. If that writer won,
  its committed row/pending result is selected instead and this method refuses
  without mutation. If Stop won, any later ordinary writer still fails its
  unchanged strict current-cancellation/prepared-disposition gate.

```python
def stop_prepared_continuation_batch(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import (
        encode_batch_chunk, prepared_stop_batch_identity, stop_refused_owner_batch_value, validate_batch_frame,
    )
    from elspeth.web.sessions.models import composition_proposals_table, interpretation_events_table, chat_messages_table
    if type(running) is not ComposerOperationRunning:
        raise TypeError("prepared Stop requires its actual nominal current running owner")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context,
            allow_cancel=True, allow_expired_segment=True)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        root = decode_continuation_snapshot(stored.payload_json)
        fence = running.session_operation_context.fence
        current_attempt = conn.execute(select(attempts).where(attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id, attempts.c.sequence == running.claim.segment_sequence,
            attempts.c.generation == running.claim.ownership_generation, attempts.c.claim_attempt == running.claim.attempt,
            attempts.c.session_operation_id == fence.operation_id, attempts.c.session_operation_epoch == fence.operation_epoch,
            attempts.c.session_operation_lease_token == fence.lease_token, attempts.c.revoked_at.is_(None))).one_or_none()
        if current_attempt is None or (root.logical.session_id, root.logical.operation_id) != (sid, operation_id):
            raise AuditIntegrityError("prepared Stop substituted another current logical or physical owner")
        cancelled = conn.execute(select(jobs.c.cancel_requested_at).where(
            jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).scalar_one()
        if cancelled is None:
            raise AuditIntegrityError("prepared Stop has no actual durable parent cancellation")
        if (type(root.frames[-1]) is not ToolBatchFrame or root.pending_owner_results
                or root.pending_settlement_ids):
            raise AuditIntegrityError("prepared Stop must first drain its actual retained owner or financial result")
        frame = root.frames[-1]
        chunks = load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=root)
        before = validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator))
        key, digest, owner = prepared_stop_batch_identity(frame, before, continuation_chunk(chunks, frame.response))
        call = before.current_call
        if call is None:
            raise AuditIntegrityError("prepared Stop lost its exact current call")
        effect = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == key)).one_or_none()
        frontier = tuple(item for item in root.effect_frontier if item.effect_id == key)
        if (effect is None or owner not in ("session_sql", "read") or effect.owner != owner
                or effect.disposition != "prepared" or effect.input_sha256 != digest
                or len(frontier) != 1 or frontier[0].disposition != "prepared" or frontier[0].receipt_id is not None):
            raise ContinuationCASConflict("prepared Stop has no exact uncommitted actual read/SQL effect")
        prepared_by = conn.execute(select(attempts).where(attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id, attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation)).one_or_none()
        call_creator = conn.execute(select(attempts).where(attempts.c.session_id == sid,
            attempts.c.operation_id == operation_id, attempts.c.session_operation_id == call.original_creator_id,
            attempts.c.session_operation_epoch == call.original_creator_epoch)).one_or_none()
        if prepared_by is None or call_creator is None:
            raise AuditIntegrityError("prepared Stop lost its actual preparation or original call history")
        if (effect.receipt_key is not None or effect.result_chunk_key is not None or effect.tool_receipt_json is not None
                or effect.committer_sequence is not None or effect.committer_generation is not None
                or effect.committed_state_ids_json is not None or effect.graph_sha256 is not None
                or effect.provider_attempt_id is not None or effect.physical_sequence is not None
                or effect.physical_generation is not None or effect.requested_model is not None
                or effect.response_finished_at is not None):
            raise AuditIntegrityError("prepared Stop cannot erase an actual business, physical or owner receipt")
        # These positive table checks also reject corruption outside the atomic
        # owner contract. Matching call IDs are already unique/admitted across
        # the original issued tuple; no result is inferred from model wording.
        proposal = conn.execute(select(composition_proposals_table.c.id).where(
            composition_proposals_table.c.session_id == sid,
            composition_proposals_table.c.tool_call_id == call.call_id)).first()
        interpretation = conn.execute(select(interpretation_events_table.c.id).where(
            interpretation_events_table.c.session_id == sid,
            interpretation_events_table.c.tool_call_id == call.call_id)).first()
        visible_result = conn.execute(select(chat_messages_table.c.id).where(
            chat_messages_table.c.session_id == sid, chat_messages_table.c.role == "tool",
            chat_messages_table.c.tool_call_id == call.call_id)).first()
        if proposal is not None or interpretation is not None or visible_result is not None:
            raise AuditIntegrityError("prepared Stop found actual business rows that require owned reconciliation")
        closed = encode_batch_chunk(stop_refused_owner_batch_value(before))
        receipt_id = f"stop:{key}"
        refusal = encode_private_chunk(codec="composer.owner-result.v1", canonical=canonical_json({
            "schema_version": "composer.prepared-tool-stop.v1", "effect_key": key,
            "input_sha256": digest, "tool_call_id": call.call_id, "owner": owner,
            "reason": "durable_stop_before_business_commit"}))
        put_private_chunks(conn, session_id=sid, operation_id=operation_id, values=(closed, refusal))
        changed = conn.execute(update(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == key,
            effects.c.disposition == "prepared", effects.c.input_sha256 == digest,
            effects.c.receipt_key.is_(None), effects.c.result_chunk_key.is_(None),
            effects.c.committer_sequence.is_(None), effects.c.committer_generation.is_(None),
        ).values(disposition="refused", receipt_key=receipt_id, result_chunk_key=private_chunk_key(refusal)))
        if changed.rowcount != 1:
            raise ContinuationCASConflict("prepared Stop lost its exact atomic uncommitted owner")
        following = root.model_copy(update={"frames": (*root.frames[:-1], frame.model_copy(update={
            "phase": "result_retained", "accumulator": closed.reference}))})
        encoded = _effect_snapshot(encode_continuation_snapshot(following), effect_key=key,
            disposition="refused", receipt_id=receipt_id)
        encoded = retain_action_execution_receipt_on_connection(conn, session_id=sid, operation_id=operation_id,
            effect_key=key, disposition="refused", snapshot_json=encoded)
        return append_complete_snapshot(conn, session_id=sid, operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=encoded,
            event_batch=(ContinuationEvent("effect_refused", canonical_json({"phase": "stop_prepared_tool",
                "effect_key": key, "completed_calls": before.next_tool_index})),), handoff=False)
```

Import `private_chunk_key` from the existing continuation SQL owner into the
authority module. The schema's refused-effect CHECK requires BOTH receipt and
result chunk; the explicit refusal chunk above satisfies it without claiming a
successful business result. It is not added to `pending_owner_results` or
`tool_receipt_json`. Original preparing coordinates stay immutable and actual
committer coordinates stay NULL because no business writer committed.

- [ ] **B9.3 (2–5 minutes): Add this complete async entry to `tool_batch.py`.**
  Its SQL/projection runs inside B8's actual existing local V stage; the public
  wrapper uses the same closed local-SQL timing contract. It reloads the
  returned complete actual snapshot and chunks together. The dispatcher then
  invokes B6's owed P4 route, without executing a call initializer or handler.

```python
async def _stop_prepared_continuation_tool_batch_value(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    from elspeth.web.composer.continuation_execution import continuation_chunk
    frame = stack.frames[-1]
    if type(frame) is not ToolBatchFrame:
        raise AuditIntegrityError("prepared Stop entry lost its actual selected batch")
    before = validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator))
    batch = restore_continuation_batch_value(before, call_model.completion.tool_batch.calls)
    execution = ContinuationBatchExecution(call_model, ctx, acc, llm_messages, batch, stack, chunks,
        checkpoint_id, required_work, ctx.provider_owner)

    async def retain() -> StoredContinuationSnapshot:
        sql, projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
            RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
        outcome = await run_required_sql_finish_once(sql, execution.authority.stop_prepared_continuation_batch,
            execution._running(), expected_checkpoint_id=checkpoint_id, expected_cursor=stack.cursor)
        actual = execution.accept_sql_result(sql=sql, projection=projection,
            source=RequiredWorkSource.CONTINUATION_STATE_SQL, outcome=outcome)
        if type(actual) is not StoredContinuationSnapshot:
            raise AuditIntegrityError("prepared Stop returned no actual complete refused checkpoint")
        execution.stack = decode_continuation_snapshot(actual.payload_json)
        execution.checkpoint_id = actual.checkpoint_id
        bundle_sql, bundle_projection = required_work.reserve_pair(RequiredWorkSource.CONTINUATION_READ_SQL,
            RequiredWorkSource.CONTINUATION_READ_PROJECTION)
        bundle_outcome = await run_required_sql_finish_once(bundle_sql, execution.authority.read_continuation_bundle,
            execution._running())
        bundle = execution.accept_sql_result(sql=bundle_sql, projection=bundle_projection,
            source=RequiredWorkSource.CONTINUATION_READ_SQL, outcome=bundle_outcome)
        if type(bundle) is not ContinuationSnapshotBundle or bundle.snapshot != actual:
            raise ContinuationCASConflict("prepared Stop changed the selected head while loading its actual closed owner")
        execution.chunks = bundle.chunks
        execution.raise_deferred()
        return actual

    await execution.local_clock().run("retention", retain)
    current = execution.stack.frames[-1]
    if type(current) is not ToolBatchFrame:
        raise AuditIntegrityError("prepared Stop changed its actual caller kind")
    after = validate_batch_frame(current, continuation_chunk(execution.chunks, current.accumulator))
    batch = restore_continuation_batch_value(after, call_model.completion.tool_batch.calls)
    dispatch, used = _finish_continuation_batch_live(call_model=call_model, ctx=ctx, acc=acc, batch=batch)
    return ContinuationToolBatchAdvance(execution.stack, execution.chunks,
        execution.checkpoint_id, dispatch, used)


async def stop_prepared_continuation_tool_batch(
    *, call_model: _CallModelOutcome, ctx: ToolBatchContext, acc: BatchAccumulator,
    llm_messages: list[dict[str, Any]], stack: ContinuationFrameStack, chunks: tuple[PrivateChunk, ...],
    checkpoint_id: str, required_work: RequiredWorkBinding,
) -> ContinuationToolBatchAdvance:
    async def execute(bounded: ToolBatchContext) -> ContinuationToolBatchAdvance:
        return await _stop_prepared_continuation_tool_batch_value(call_model=call_model, ctx=bounded, acc=acc,
            llm_messages=llm_messages, stack=stack, chunks=chunks, checkpoint_id=checkpoint_id, required_work=required_work)
    return await _run_batch_entry(ctx=ctx, stack=stack, chunks=chunks, local_sql_only=True, operation=execute)
```

Import `ContinuationSnapshotBundle` from `elspeth.web.sessions.continuation`
in `tool_batch.py` alongside its existing `StoredContinuationSnapshot` import.
This new explicit read compares the whole actual
snapshot to the actual transition receipt; it never puts a latest locator on
older precomputed frames.

- [ ] **B9.4 (2–5 minutes per test): Append these complete real-owner tests
  to `tests/unit/web/sessions/test_continuation_cold_restore.py`.** This
  explicit fixture uses B7's actual selected call without its deliberately
  synthetic effect digest, then derives B5's complete real input and invokes
  the actual preparation owner. It is SQL-owner evidence; B9.5 independently
  exercises the whole real SDK-to-worker path.

```python
from sqlalchemy import update
from elspeth.web.composer.continuation_tool_batch import prepared_stop_batch_identity
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.models import composer_continuation_effects_table


async def selected_exact_prepared_stop_call(store):
    _engine, _repository, authority, _service, _sid = store
    running, state, audit, absent, request_id = await selected_proposal_call(store, prepare_owner=False)
    assert absent is None
    bundle = authority.read_continuation_bundle(running)
    root = decode_continuation_snapshot(bundle.snapshot.payload_json)
    frame = root.frames[-1]
    value = decode_batch_chunk(continuation_chunk(bundle.chunks, frame.accumulator))
    projected = ToolCompletionValue(projection="proposal", tool_name=audit.tool_name,
        proposal_tool_name=audit.tool_name, state_json=canonical_json(state.to_dict()),
        validation_json=canonical_json(_VALIDATION.dump_python(state.validate(), mode="json")))
    key = f"batch:{frame.response.digest}:call:0"
    call = value.current_call.model_copy(update={"effect_key": key,
        "completion_json": canonical_json(projected.model_dump(mode="json"))})
    chunk = encode_batch_chunk(value.model_copy(update={"current_call": call}))
    following = root.model_copy(update={"frames": (*root.frames[:-1],
        frame.model_copy(update={"accumulator": chunk.reference}))})
    selected = authority.commit_transition(running, expected_checkpoint_id=bundle.snapshot.checkpoint_id,
        expected_cursor=bundle.snapshot.cursor, snapshot_json=encode_continuation_snapshot(following),
        chunks=(chunk,), event=ContinuationEvent("tool_prepared", canonical_json({"fixture": "complete actual input"})))
    frame = following.frames[-1]
    actual_key, digest, owner = prepared_stop_batch_identity(frame,
        decode_batch_chunk(chunk), continuation_chunk(bundle.chunks, frame.response))
    prepared = authority.prepare_tool_action(running, effect_key=actual_key, input_sha256=digest,
        owner=owner, snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor)
    authoring = PrivateToolAuthoring(call.call_id, call.tool_name, call.private_arguments_canonical,
        call.private_authority_canonical, call.private_raw_arguments)
    commit = ContinuationOwnerCommit(running, key, digest, prepared.checkpoint_id, prepared.cursor,
        ContinuationToolCompletion(projected, audit, authoring))
    return running, state, audit, commit, request_id


@pytest.mark.asyncio
async def test_prepared_stop_refuses_exact_owner_preserving_every_completed_value(operation_store):
    engine, repository, authority, service, sid = operation_store
    running, _state, audit, commit, request_id = await selected_exact_prepared_stop_call(operation_store)
    try:
        bundle = authority.read_continuation_bundle(running)
        root = decode_continuation_snapshot(bundle.snapshot.payload_json)
        frame = root.frames[-1]
        before = decode_batch_chunk(continuation_chunk(bundle.chunks, frame.accumulator))
        kwargs = dict(expected_checkpoint_id=bundle.snapshot.checkpoint_id, expected_cursor=bundle.snapshot.cursor)
        unchanged = batch_store_rows(engine)
        with pytest.raises(AuditIntegrityError, match="durable parent cancellation"):
            authority.stop_prepared_continuation_batch(running, **kwargs)
        assert batch_store_rows(engine) == unchanged
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        actual = authority.stop_prepared_continuation_batch(running, **kwargs)
        selected = authority.read_continuation_bundle(running)
        assert selected.snapshot == actual
        after_root = decode_continuation_snapshot(actual.payload_json)
        after = decode_batch_chunk(continuation_chunk(selected.chunks, after_root.frames[-1].accumulator))
        assert after == before.model_copy(update={"phase": "dispatch_ready", "stop_issued_batch": True, "current_call": None})
        assert after_root.frames[:-1] == root.frames[:-1]
        assert after_root.frames[-1].response == frame.response
        assert after_root.frames[-1].next_tool_index == frame.next_tool_index
        assert after_root.pending_owner_results == root.pending_owner_results == ()
        assert after_root.pending_settlement_ids == root.pending_settlement_ids == ()
        with engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(sid),
                composer_continuation_effects_table.c.effect_key == commit.effect_key)).one()
            assert effect.disposition == "refused" and effect.receipt_key == f"stop:{commit.effect_key}"
            assert effect.tool_receipt_json is None and effect.committer_sequence is None and effect.committer_generation is None
            refused = read_chunk(conn, session_id=str(sid), operation_id=running.claim.operation_id,
                key=effect.result_chunk_key).decode("utf-8")
        assert refused == canonical_json({"schema_version": "composer.prepared-tool-stop.v1",
            "effect_key": commit.effect_key, "input_sha256": commit.input_sha256,
            "tool_call_id": audit.tool_call_id, "owner": "session_sql", "reason": "durable_stop_before_business_commit"})
        immutable = batch_store_rows(engine)
        with pytest.raises(ContinuationCASConflict):
            authority.stop_prepared_continuation_batch(running, **kwargs)
        assert batch_store_rows(engine) == immutable
        # The real ordinary writer cannot use an updated locator to evade
        # durable Stop or turn a refused effect back into a proposal.
        updated = replace(commit, expected_checkpoint_id=actual.checkpoint_id, expected_cursor=actual.cursor)
        from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await service.create_composition_proposal(session_id=sid,
                session_operation_context=running.session_operation_context, tool_call_id=audit.tool_call_id,
                tool_name="set_metadata", summary="Retained café proposal", rationale="Actual requested change",
                affects=("graph",), arguments_json={"patch": {"description": "Retained café proposal"}},
                arguments_redacted_json={"patch": {"description": "Retained café proposal"}}, base_state_id=None,
                actor=audit.actor, user_message_id=UUID(request_id), continuation=updated)
        assert batch_store_rows(engine) == immutable
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_prepared_stop_rejects_changed_effect_input_without_mutating_any_table(operation_store):
    engine, repository, authority, _service, sid = operation_store
    running, _state, _audit, commit, _request_id = await selected_exact_prepared_stop_call(operation_store)
    try:
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        with engine.begin() as conn:
            conn.execute(update(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(sid),
                composer_continuation_effects_table.c.effect_key == commit.effect_key).values(input_sha256="0" * 64))
        head = authority.load_continuation_snapshot(running)
        before = batch_store_rows(engine)
        with pytest.raises(ContinuationCASConflict, match="exact uncommitted"):
            authority.stop_prepared_continuation_batch(running,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert batch_store_rows(engine) == before
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_prepared_stop_never_erases_an_actual_committed_proposal_receipt(operation_store):
    engine, repository, authority, service, sid = operation_store
    running, _state, audit, commit, request_id = await selected_exact_prepared_stop_call(operation_store)
    try:
        proposal = await service.create_composition_proposal(session_id=sid,
            session_operation_context=running.session_operation_context, tool_call_id=audit.tool_call_id,
            tool_name="set_metadata", summary="Retained café proposal", rationale="Actual requested change", affects=("graph",),
            arguments_json={"patch": {"description": "Retained café proposal"}},
            arguments_redacted_json={"patch": {"description": "Retained café proposal"}}, base_state_id=None,
            actor=audit.actor, user_message_id=UUID(request_id), continuation=commit)
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        head = authority.load_continuation_snapshot(running)
        before = batch_store_rows(engine)
        with pytest.raises(AuditIntegrityError, match="retained owner or financial result"):
            authority.stop_prepared_continuation_batch(running,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert batch_store_rows(engine) == before
        kind, retained, receipt = authority.load_pending_tool_owner_result(running,
            expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor, effect_key=commit.effect_key)
        from elspeth.web.sessions.continuation_owner_results import decode_proposal_owner_result
        assert kind == "proposal" and decode_proposal_owner_result(retained) == proposal
        assert receipt.invocation_json is not None
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_prepared_stop_foreign_locator_stale_cursor_and_old_fence_preserve_all_rows(operation_store):
    from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
    engine, repository, authority, service, sid = operation_store
    running, _state, _audit, _commit, _request_id = await selected_exact_prepared_stop_call(operation_store)
    other = None
    released = False
    try:
        authority.request_cancel(session_id=sid, operation_id=running.claim.operation_id,
            cancelled_failure=request_cancelled_error)
        head = authority.load_continuation_snapshot(running)
        before = batch_store_rows(engine)
        with pytest.raises(ContinuationCASConflict):
            authority.stop_prepared_continuation_batch(running,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor - 1)
        assert batch_store_rows(engine) == before
        session = repository.create_session_with_initial_fence(user_id="alice", title="Other actual parent",
            auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30)
        other_store = engine, repository, authority, service, session.id
        admit_continuation(other_store)
        other = start_continuation(repository, authority)
        assert other.claim.session_id == session.id and other.claim.session_id != sid
        before = batch_store_rows(engine)
        with pytest.raises(ContinuationCASConflict):
            authority.stop_prepared_continuation_batch(other,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert batch_store_rows(engine) == before
        repository.release(running.session_operation_context)
        released = True
        before = batch_store_rows(engine)
        with pytest.raises(ComposerOperationFenceLost):
            authority.stop_prepared_continuation_batch(running,
                expected_checkpoint_id=head.checkpoint_id, expected_cursor=head.cursor)
        assert batch_store_rows(engine) == before
    finally:
        if other is not None:
            repository.release(other.session_operation_context)
        if not released:
            repository.release(running.session_operation_context)
```

- [ ] **B9.5 (2–5 minutes per helper/test): Create the complete integration
  module below.** D's actual worker/SQL harness and F35's existing graph
  fixture are prerequisites. Only the SDK is substituted. The preparation
  observer invokes the real transaction and holds its actual required SQL
  future before delivery; its session transaction is already committed, so
  the real Stop writer can run. Root's B5 post-preparation boundary read must
  observe that Stop before granting the caller any business authority. The
  first real cohort then joins and suspends once; every live service/lease/
  recorder/transcript is discarded before the next real attempt. The same
  test runs against actual proposal, interpretation and immutable read paths.

```python
import asyncio
import threading
from dataclasses import dataclass, field

import litellm
import pytest
from sqlalchemy import select

from elspeth.web.composer import tool_batch
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_execution import continuation_chunk
from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk, validate_batch_frame
from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker
from elspeth.web.sessions.continuation import load_private_chunks, load_snapshot
from elspeth.web.sessions.models import (
    chat_messages_table, composer_async_operations_table, composer_continuation_effects_table,
    composer_segment_attempts_table, composition_proposals_table, composition_states_table,
    interpretation_events_table,
)
from elspeth.web.sessions.service import SessionServiceImpl
from tests.helpers.continuation_driver import (
    DeterministicDriverSDK, DriverEnvironment, SDKReply, fresh_driver_environment, run_driver_attempt,
    seed_driver_case, tool_reply,
)
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store


def prepared_stop_script(kind):
    assert kind in ("proposal", "interpretation", "read")
    target = {
        "proposal": ("set_metadata", {"patch": {"description": "This uncommitted proposal must remain absent"}}),
        "interpretation": ("request_interpretation_review", {"affected_node_id": "review_node",
            "kind": "llm_model_choice", "user_term": "llm_model_choice:review_node"}),
        "read": ("list_transforms", {}),
    }[kind]
    discovery = (
        tool_reply("stop:schema:source", "get_plugin_schema", {"plugin_type": "source", "name": "csv"}),
        tool_reply("stop:schema:sink", "get_plugin_schema", {"plugin_type": "sink", "name": "json"}),
    )
    if kind == "interpretation":
        discovery = (*discovery, tool_reply("stop:schema:llm", "get_plugin_schema",
            {"plugin_type": "transform", "name": "llm"}))
    return DeterministicDriverSDK(replies={"primary": (
        SDKReply(content="Inspect the current registered schemas.", calls=discovery),
        SDKReply(content="Inspect the prefix, perform the requested tool, then continue.", calls=(
            tool_reply("stop:prefix", "list_sinks", {}),
            tool_reply("stop:target", target[0], target[1]),
            tool_reply("stop:suffix", "set_metadata", {"patch": {"description": "Must never run after Stop"}}),
        )),
    )})


def selected_stop_batch(env, stored):
    root = decode_continuation_snapshot(stored.payload_json)
    frame = root.frames[-1]
    assert type(frame) is ToolBatchFrame
    with env.sessions._engine.connect() as conn:
        chunks = load_private_chunks(conn, session_id=str(env.case.session_id),
            operation_id=env.case.operation_id, stack=root)
    return root, validate_batch_frame(frame, continuation_chunk(chunks, frame.accumulator))


@dataclass(slots=True)
class StopOwnerObservation:
    entered: asyncio.Event
    release_sql: threading.Event = field(default_factory=threading.Event)
    release_caller: asyncio.Event = field(default_factory=asyncio.Event)
    key: str | None = None
    input_sha256: str | None = None
    prepared_by: tuple[int, int] | None = None
    writer_calls: list[str] = field(default_factory=list)
    read_calls: list[str] = field(default_factory=list)
    actual_jobs: list[asyncio.Task] = field(default_factory=list)


def observe_stop_owner(env, kind, mode, observation, monkeypatch):
    loop = asyncio.get_running_loop()
    original_prepare = env.authority.prepare_tool_action
    original_read = env.authority.retain_read_owner_result
    original_proposal = SessionServiceImpl.create_composition_proposal
    original_interpretation = SessionServiceImpl.create_pending_interpretation_event
    original_execute = tool_batch.execute_tool
    original_job = ComposerAsyncWorker._job

    def retain_locator(stored, effect_key):
        _root, batch = selected_stop_batch(env, stored)
        call = batch.current_call
        if call is None or call.call_id != "stop:target":
            return False
        assert observation.key is None
        with env.sessions._engine.connect() as conn:
            effect = conn.execute(select(composer_continuation_effects_table).where(
                composer_continuation_effects_table.c.session_id == str(env.case.session_id),
                composer_continuation_effects_table.c.operation_id == env.case.operation_id,
                composer_continuation_effects_table.c.effect_key == effect_key)).one()
        observation.key, observation.input_sha256 = effect.effect_key, effect.input_sha256
        observation.prepared_by = effect.creator_sequence, effect.creator_generation
        return True

    def prepare(running, **kwargs):
        actual = original_prepare(running, **kwargs)
        if mode == "prepared" and retain_locator(actual, kwargs["effect_key"]):
            loop.call_soon_threadsafe(observation.entered.set)
            assert observation.release_sql.wait(15), "test did not release the actual prepared SQL producer"
        return actual

    def read(commit, **kwargs):
        actual = original_read(commit, **kwargs)
        if mode == "committed" and kind == "read" and retain_locator(actual, commit.effect_key):
            loop.call_soon_threadsafe(observation.entered.set)
            assert observation.release_sql.wait(15), "test did not release the actual committed read producer"
        return actual

    async def proposal(self, **kwargs):
        if kwargs["tool_call_id"] == "stop:target":
            observation.writer_calls.append("proposal")
        actual = await original_proposal(self, **kwargs)
        if mode == "committed" and kind == "proposal" and kwargs["tool_call_id"] == "stop:target":
            commit = kwargs["continuation"]
            assert retain_locator(env.authority.load_continuation_snapshot(commit.running), commit.effect_key)
            observation.entered.set()
            await observation.release_caller.wait()
        return actual

    async def interpretation(self, **kwargs):
        if kwargs["tool_call_id"] == "stop:target":
            observation.writer_calls.append("interpretation")
        actual = await original_interpretation(self, **kwargs)
        if mode == "committed" and kind == "interpretation" and kwargs["tool_call_id"] == "stop:target":
            commit = kwargs["continuation"]
            assert retain_locator(env.authority.load_continuation_snapshot(commit.running), commit.effect_key)
            observation.entered.set()
            await observation.release_caller.wait()
        return actual

    def execute(tool_name, arguments, state, policy_catalog, **kwargs):
        if tool_name == "list_transforms":
            observation.read_calls.append(tool_name)
        return original_execute(tool_name, arguments, state, policy_catalog, **kwargs)

    async def job(self, claim):
        actual_task = asyncio.current_task()
        assert actual_task is not None
        observation.actual_jobs.append(actual_task)
        return await original_job(self, claim)

    monkeypatch.setattr(env.authority, "prepare_tool_action", prepare)
    monkeypatch.setattr(env.authority, "retain_read_owner_result", read)
    monkeypatch.setattr(SessionServiceImpl, "create_composition_proposal", proposal)
    monkeypatch.setattr(SessionServiceImpl, "create_pending_interpretation_event", interpretation)
    monkeypatch.setattr(tool_batch, "execute_tool", execute)
    monkeypatch.setattr(ComposerAsyncWorker, "_job", job)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("proposal", "interpretation", "read"))
@pytest.mark.parametrize("mode", ("prepared", "committed"))
async def test_actual_held_tool_stop_drains_prefix_without_reissuing_business_owner(
    operation_store, tmp_path, monkeypatch, kind, mode,
):
    case = await seed_driver_case(operation_store, tmp_path / "prepared-stop-data",
        trust_mode="explicit_approve", review_fixture="prompt" if kind == "interpretation" else "none")
    database_url = str(operation_store[0].url)
    sdk = prepared_stop_script(kind)
    env: DriverEnvironment | None = fresh_driver_environment(database_url, case)
    observation = StopOwnerObservation(asyncio.Event())
    first = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(litellm, "acompletion", sdk.complete)
            observe_stop_owner(env, kind, mode, observation, patch)
            first = asyncio.create_task(run_driver_attempt(env, sdk, patch, expect_suspension=True))
            await asyncio.wait_for(observation.entered.wait(), timeout=20)
            assert not first.done() and observation.actual_jobs and not observation.actual_jobs[-1].done()
            stopped = env.authority.request_v2_cancel(session_id=case.session_id, operation_id=case.operation_id,
                actor_user_id="alice", auth_provider_type="local")
            assert stopped is not None and stopped[0].cancel_requested
            assert observation.key is not None and observation.prepared_by is not None
            with env.sessions._engine.connect() as conn:
                effect = conn.execute(select(composer_continuation_effects_table).where(
                    composer_continuation_effects_table.c.session_id == str(case.session_id),
                    composer_continuation_effects_table.c.effect_key == observation.key)).one()
                assert effect.disposition == ("prepared" if mode == "prepared" else "committed")
                if mode == "prepared":
                    assert effect.receipt_key is None and effect.result_chunk_key is None and effect.tool_receipt_json is None
                    assert observation.writer_calls == [] and observation.read_calls == []
                else:
                    assert effect.receipt_key is not None and effect.result_chunk_key is not None
                    assert effect.tool_receipt_json is not None
            if mode == "committed":
                # Interrupt after the real owner committed and before its
                # caller received that result. This is not a no-result proof.
                observation.actual_jobs[-1].cancel("test-known-result-stop-cut")
            observation.release_sql.set()
            observation.release_caller.set()
            retired = await asyncio.wait_for(asyncio.shield(first), timeout=20)
            assert observation.prepared_by == (retired.sequence, retired.generation)
        env.dispose()
        env = None
        cold = fresh_driver_environment(database_url, case)
        refusals = []
        actual_stop = cold.authority.stop_prepared_continuation_batch

        def refuse(running, **kwargs):
            bundle = cold.authority.read_continuation_bundle(running)
            root, before = selected_stop_batch(cold, bundle.snapshot)
            assert before.current_call.call_id == "stop:target"
            assert before.next_tool_index == 1 and tuple(item.call_id for item in before.tool_outcomes) == ("stop:prefix",)
            actual = actual_stop(running, **kwargs)
            following, after = selected_stop_batch(cold, actual)
            assert after == before.model_copy(update={"phase": "dispatch_ready", "stop_issued_batch": True, "current_call": None})
            assert following.frames[:-1] == root.frames[:-1]
            assert (running.claim.segment_sequence, running.claim.ownership_generation) != observation.prepared_by
            refusals.append(actual.checkpoint_id)
            return actual

        try:
            with monkeypatch.context() as patch:
                patch.setattr(litellm, "acompletion", sdk.complete)
                patch.setattr(cold.authority, "stop_prepared_continuation_batch", refuse)
                # Install only observational business counters for the cold
                # attempt; none of the old env's services or closures survive.
                cold_observation = StopOwnerObservation(asyncio.Event())
                observe_stop_owner(cold, kind, "observe_only", cold_observation, patch)
                await run_driver_attempt(cold, sdk, patch)
            assert cold_observation.writer_calls == [] and cold_observation.read_calls == []
            assert len(refusals) == (1 if mode == "prepared" else 0)
            assert observation.writer_calls == ([kind] if mode == "committed" and kind != "read" else [])
            assert observation.read_calls == (["list_transforms"] if mode == "committed" and kind == "read" else [])
            sdk.assert_consumed()
            with cold.sessions._engine.connect() as conn:
                sid = str(case.session_id)
                parent = conn.execute(select(composer_async_operations_table).where(
                    composer_async_operations_table.c.session_id == sid,
                    composer_async_operations_table.c.operation_id == case.operation_id)).one()
                effects = conn.execute(select(composer_continuation_effects_table).where(
                    composer_continuation_effects_table.c.session_id == sid,
                    composer_continuation_effects_table.c.effect_key == observation.key)).all()
                messages = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid)
                    .order_by(chat_messages_table.c.sequence_no)).all()
                proposals = conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == sid)).all()
                interpretations = conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.session_id == sid)).all()
                states = conn.execute(select(composition_states_table).where(composition_states_table.c.session_id == sid)).all()
                history = conn.execute(select(composer_segment_attempts_table).where(
                    composer_segment_attempts_table.c.session_id == sid,
                    composer_segment_attempts_table.c.operation_id == case.operation_id)).all()
                stored = load_snapshot(conn, session_id=sid, operation_id=case.operation_id)
            assert parent.status == "failed" and parent.failure_code == "request_cancelled" and parent.cancel_requested_at is not None
            root = decode_continuation_snapshot(stored.payload_json)
            assert not root.pending_owner_results and not root.pending_settlement_ids
            assert all(type(frame) is not ToolBatchFrame for frame in root.frames)
            assert len(effects) == 1 and effects[0].disposition == ("refused" if mode == "prepared" else "committed")
            assert effects[0].input_sha256 == observation.input_sha256
            assert (effects[0].creator_sequence, effects[0].creator_generation) == observation.prepared_by
            assert len(states) == 1 and states[0].id == str(case.base_state_id)
            assert len(proposals) == (1 if mode == "committed" and kind == "proposal" else 0)
            assert len(interpretations) == (1 if mode == "committed" and kind == "interpretation" else 0)
            assert all(row.tool_call_id == "stop:target" for row in (*proposals, *interpretations))
            target_rows = tuple(row for row in messages if row.role == "tool" and row.tool_call_id == "stop:target")
            assert len(target_rows) == (1 if mode == "committed" else 0)
            assert len(tuple(row for row in messages if row.role == "tool" and row.tool_call_id == "stop:prefix")) == 1
            assert not any(row.role == "tool" and row.tool_call_id == "stop:suffix" for row in messages)
            assert all(row.revoked_at is not None for row in history)
            assert sdk.consumed == {"primary": 2}
        finally:
            cold.dispose()
    finally:
        observation.release_sql.set()
        observation.release_caller.set()
        if first is not None and not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        if env is not None:
            env.dispose()
```

- [ ] **B9.6 (2–5 minutes): Reuse the exact owner tests against the existing
  serial PostgreSQL `operation_store` fixture from C4b.** Append these imports
  to `tests/testcontainer/web/test_composer_continuation_postgres.py`. The
  test functions receive that module's actual PostgreSQL store; they do not
  instantiate SQLite or replace its real row/session lock owners. The full
  driver harness above remains the separate SQLite restart oracle.

```python
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_prepared_stop_refuses_exact_owner_preserving_every_completed_value as test_postgres_prepared_stop_exact_owner,
    test_prepared_stop_rejects_changed_effect_input_without_mutating_any_table as test_postgres_prepared_stop_changed_input,
    test_prepared_stop_never_erases_an_actual_committed_proposal_receipt as test_postgres_prepared_stop_committed_result,
    test_prepared_stop_foreign_locator_stale_cursor_and_old_fence_preserve_all_rows as test_postgres_prepared_stop_foreign_and_stale,
)
```

- [ ] **B9.7 (2–5 minutes per edit/run): Take behavioral RED then GREEN.**
  Start after the full B9/B6/W4 packet imports and collects normally. Remove
  only `effect.input_sha256 != digest` in B9.2: the changed-input negative
  test must fail at its `pytest.raises` assertion. Restore it. Temporarily
  return `False` only from F32's `continuation_batch_prepared_stop_ready`:
  the three real `mode=prepared` cases must fail to reach the failed,
  drained parent. Restore the selector. B5's post-preparation real current
  Stop check and the v2 cooperative watcher are required: removing that
  Stop check must make the actual business-entry counters fail even though
  the unchanged ordinary SQL writer still refuses the mutation. Restore
  it. Reissuing a committed owner in the cold branch must fail the separate
  actual writer/handler count assertion; no returned `ToolResult` or fake
  audit substitutes for this control. Do not report import/setup/timeout
  failures as evidence of the intended behavioral RED.

Run these exact normal commands with both source roots exported by the
main plan setup. Read each native exit and full saved log. The named
PostgreSQL cases require the normal real Docker testcontainer capacity;
their result is distinct from the default SQLite selection. Then run Q's
actual persistence/AST/ruff/mypy/contracts/full integration gates on the
combined implementation tree, including the default and required serial
testcontainer selection before merge.

```bash
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py -k prepared_stop >"$COMPOSER_LOG_DIR/b9-prepared-stop-owners.log" 2>&1
composer_exit=$?
printf 'exit=%s\n' "$composer_exit"
cat "$COMPOSER_LOG_DIR/b9-prepared-stop-owners.log"
test "$composer_exit" -eq 0

cd "$COMPOSER_WORKTREE" && PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest -n 0 tests/integration/web/composer/test_continuation_prepared_stop.py tests/unit/web/composer/test_continuation_dispatch.py >"$COMPOSER_LOG_DIR/b9-prepared-stop-driver.log" 2>&1
composer_exit=$?
printf 'exit=%s\n' "$composer_exit"
cat "$COMPOSER_LOG_DIR/b9-prepared-stop-driver.log"
test "$composer_exit" -eq 0

cd "$COMPOSER_WORKTREE" && PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest -n 0 -m testcontainer tests/testcontainer/web/test_composer_continuation_postgres.py -k prepared_stop >"$COMPOSER_LOG_DIR/b9-prepared-stop-postgres.log" 2>&1
composer_exit=$?
printf 'exit=%s\n' "$composer_exit"
cat "$COMPOSER_LOG_DIR/b9-prepared-stop-postgres.log"
test "$composer_exit" -eq 0
```

- [ ] **B9.8 (2–5 minutes): Commit the bounded repair after native GREEN.**
  Use the normal branch-safety preflight and hooks. Stage only
  `src/elspeth/web/composer/continuation_tool_batch.py`,
  `src/elspeth/web/composer/tool_batch.py`,
  `src/elspeth/web/composer/continuation_driver.py`,
  `src/elspeth/web/coordination/composer_operation_authority.py`,
  `tests/unit/web/sessions/test_continuation_cold_restore.py`,
  `tests/integration/web/composer/test_continuation_prepared_stop.py`, and
  `tests/testcontainer/web/test_composer_continuation_postgres.py` after
  inspecting the exact staged set. Message:
  `fix(composer): refuse stopped uncommitted tool owners and drain their prefix`.

F32's complete additional selector/adapter/branch is literal in the frames
companion. Its prepared-owner branch precedes both the old no-effect Stop
prefix route and END; the transaction above grants the authority, not the
selector. END's unchanged strict `prepare_continuation_stop` runs only after
the real B6 owed-P4 application/classification has popped the batch. A
committed proposal/interpretation/read is applied once and preserved before
that same terminal owner. There is no scheduling retry of an unchanged
prepared stopped batch, new provider call, artificial tool result or fresh
exception reconstructed from a retained descriptor.
