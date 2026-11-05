"""Complete nominal result retention; no continuation worker or SQL fixtures."""

import cProfile
import json
import sys
from contextlib import nullcontext
from dataclasses import replace
from hashlib import sha256

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
from elspeth.web import async_workers
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer import continuation_tool_batch as tool_batch
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
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
from elspeth.web.continuation_retention_memo import (
    RetentionStageMemo,
    borrow_v4_preparation_memo_scope,
    legacy_advisor_preparation_memo_scope,
    retention_stage_memo_scope,
    v4_preparation_memo_scope,
)
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.schemas import (
    ValidationCheck,
    ValidationError,
    ValidationReadiness,
    ValidationReadinessBlocker,
    ValidationResult,
    ValidationWarning,
)
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from tests.unit.web.test_required_sql_finish_once import publication_ticket


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


def _tool_clock():
    return ContinuationActionClock(ContinuationActionPolicy(preparation=10, guard=1, quota=1, physical=1, retention=1))


def _canonical_calls(operation):
    # Count actual original call frames without replacing any eligibility key.
    with cProfile.Profile() as profile:
        result = operation()
    counts = {}
    for name, function in (("parse", tool_batch._ORIGINAL_TOOL_PARSER), ("render", tool_batch._ORIGINAL_TOOL_RENDERER)):
        counts[name] = sum(entry.callcount for entry in profile.getstats() if entry.code is function.__code__)
    return result, counts


@pytest.mark.asyncio
async def test_v4_tool_canonical_proof_crosses_sql_children_with_fresh_parses_and_values():
    source = {"text": 'Keep "quotes",\n café 😀', "nested": [{"value": [1, 0.25]}]}
    encoded = canonical_json(source)
    assert _canonical_calls(lambda: None) == (None, {"parse": 0, "render": 0})
    assert _canonical_calls(lambda: tool_batch._read_json_value(encoded)) == (source, {"parse": 1, "render": 1})

    def cut():
        with borrow_v4_preparation_memo_scope():
            return _canonical_calls(lambda: tool_batch._read_json_value(encoded))

    with v4_preparation_memo_scope(_tool_clock()) as parent:
        first = await async_workers.run_required_sql_finish_once(publication_ticket(), cut)
        second = await async_workers.run_required_sql_finish_once(publication_ticket(), cut)
        assert isinstance(first, RequiredSQLReturned) and isinstance(second, RequiredSQLReturned)
        first_value, first_counts = first.value
        second_value, second_counts = second.value
        assert first_counts == {"parse": 2, "render": 1} and second_counts == {"parse": 1, "render": 0}
        assert first_value == second_value == source and first_value is not second_value
        first_value["nested"][0]["value"].append("mutated fresh result")
        assert second_value == source
        assert len(parent._canonical_verifications) == 1
        assert all(type(weight) is int for weight in parent._canonical_verifications.values())
        for producer, key in parent._canonical_verifications:
            assert producer.__closure__ is None
            assert key.canonical == encoded and key.dependencies.functions == tool_batch._ORIGINAL_TOOL_REFERENCES
    assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ("none", "ordinary", "legacy"))
async def test_tool_canonical_memo_does_not_change_non_v4_single_parse(scope):
    context = (
        nullcontext()
        if scope == "none"
        else retention_stage_memo_scope()
        if scope == "ordinary"
        else legacy_advisor_preparation_memo_scope(_tool_clock())
    )
    with context:
        for _ in range(2):
            assert _canonical_calls(lambda: tool_batch._read_json_value('{"value":1}')) == ({"value": 1}, {"parse": 1, "render": 1})


@pytest.mark.asyncio
async def test_foreign_stateful_parser_bypasses_warm_proof_and_returns_its_first_graph(monkeypatch):
    encoded = '{"value":1}'
    calls = []

    def stateful(_text):
        calls.append(True)
        return ({"value": 1} if len(calls) == 1 else {"different": 2}), None

    with v4_preparation_memo_scope(_tool_clock()) as parent:
        assert tool_batch._read_json_value(encoded) == {"value": 1}
        monkeypatch.setattr(tool_batch, "parse_json_strict", stateful)
        assert tool_batch._read_json_value(encoded) == {"value": 1}
        assert calls == [True] and len(parent._canonical_verifications) == 1
        with pytest.raises(AuditIntegrityError, match="not complete canonical JSON"):
            tool_batch._read_json_value(encoded)
        assert calls == [True, True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dependency",
    (
        "renderer",
        "normalizer",
        "recursive_normalizer",
        "write",
        "quote",
        "quote_primitive",
        "json_loader",
        "duplicate_hook",
        "nonfinite_hook",
        "numeric_dump",
    ),
)
async def test_warm_tool_proof_bypasses_changed_subordinate_implementations(monkeypatch, dependency):
    targets = {
        "renderer": (tool_batch, "canonical_json_native_strings"),
        "normalizer": (tool_batch.native_json, "_normalize_frozen_and_reject_non_finite"),
        "recursive_normalizer": (tool_batch.hashing, "_normalize_frozen_and_reject_non_finite"),
        "write": (tool_batch.native_json, "_write"),
        "quote": (tool_batch.native_json, "_native_string_bytes"),
        "quote_primitive": (tool_batch.native_json, "encode_basestring"),
        "json_loader": (tool_batch.json_parser.json, "loads"),
        "duplicate_hook": (tool_batch.json_parser, "_reject_duplicate_keys"),
        "nonfinite_hook": (tool_batch.json_parser, "_reject_non_finite"),
        "numeric_dump": (tool_batch.native_json.rfc8785, "dump"),
    }
    module, field = targets[dependency]
    original = module.__dict__[field]
    encoded = '{"value":[1,"café"]}'

    def foreign(*args, **kwargs):
        return original(*args, **kwargs)

    with v4_preparation_memo_scope(_tool_clock()) as parent:
        expected = tool_batch._read_json_value(encoded)
        with monkeypatch.context() as patch:
            patch.setattr(module, field, foreign)
            actual, counts = _canonical_calls(lambda: tool_batch._read_json_value(encoded))
            assert actual == expected and counts == {"parse": 1, "render": 1}
            assert len(parent._canonical_verifications) == 1
        assert _canonical_calls(lambda: tool_batch._read_json_value(encoded)) == (expected, {"parse": 1, "render": 0})


@pytest.mark.asyncio
async def test_unhashable_foreign_renderer_bypasses_without_equality_or_hash_probe(monkeypatch):
    class ForeignRenderer:
        __hash__ = None

        def __call__(self, value):
            return tool_batch._ORIGINAL_TOOL_RENDERER(value)

        def __eq__(self, other):
            raise AssertionError("eligibility must compare exact function identities")

    with v4_preparation_memo_scope(_tool_clock()) as parent:
        assert tool_batch._read_json_value('{"value":1}') == {"value": 1}
        monkeypatch.setattr(tool_batch, "canonical_json_native_strings", ForeignRenderer())
        assert _canonical_calls(lambda: tool_batch._read_json_value('{"value":1}')) == ({"value": 1}, {"parse": 1, "render": 1})
        assert len(parent._canonical_verifications) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change_at", ("before_proof", "after_proof", "proof_error"))
async def test_dependency_change_during_miss_cannot_publish_or_validate_the_second_graph(monkeypatch, change_at):
    original_memo = RetentionStageMemo.canonical_text_verification
    parsed, rendered = [], []
    original_profile = sys.getprofile()
    assert original_profile is None

    def observe(frame, event, arg):
        if event == "return" and frame.f_code is tool_batch._ORIGINAL_TOOL_PARSER.__code__:
            parsed.append(arg[0])

    def current_renderer(value):
        rendered.append(value)
        return tool_batch._ORIGINAL_TOOL_RENDERER(value)

    def interrupted(memo, key, producer):
        if memo._v4_preparation_owner is None:
            return original_memo(memo, key, producer)

        def proof(text):
            if change_at == "before_proof":
                monkeypatch.setattr(tool_batch, "canonical_json_native_strings", current_renderer)
            producer(text)
            monkeypatch.setattr(tool_batch, "canonical_json_native_strings", current_renderer)
            if change_at == "proof_error":
                raise RuntimeError("interrupted proof error must not replace first-value validation")

        return original_memo(memo, key, proof)

    monkeypatch.setattr(RetentionStageMemo, "canonical_text_verification", interrupted)
    with v4_preparation_memo_scope(_tool_clock()) as parent:
        sys.setprofile(observe)
        try:
            actual = tool_batch._read_json_value('{"value":[1]}')
        finally:
            sys.setprofile(original_profile)
        assert actual == {"value": [1]} and len(parsed) == (1 if change_at == "before_proof" else 2)
        assert rendered == [parsed[0]] and rendered[0] is parsed[0]
        if len(parsed) == 2:
            assert parsed[0] is not parsed[1] and rendered[0] is not parsed[1]
        assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


@pytest.mark.asyncio
async def test_warm_tool_proof_does_not_hide_changed_renderer_refusal(monkeypatch):
    encoded = '{"value":1}'
    calls = []

    def changed(value):
        calls.append(value)
        return tool_batch._ORIGINAL_TOOL_RENDERER(value) + " "

    with v4_preparation_memo_scope(_tool_clock()) as parent:
        assert tool_batch._read_json_value(encoded) == {"value": 1}
        monkeypatch.setattr(tool_batch, "canonical_json_native_strings", changed)
        with pytest.raises(AuditIntegrityError, match="not complete canonical JSON"):
            tool_batch._read_json_value(encoded)
        assert calls == [{"value": 1}] and len(parent._canonical_verifications) == 1


def _tool_read_error(encoded):
    try:
        tool_batch._read_json_value(encoded)
    except Exception as error:
        cause = error.__cause__
        return (
            type(error),
            error.args,
            error.__suppress_context__,
            None if cause is None else (type(cause), cause.args),
            error.__context__ is cause,
        )
    raise AssertionError("controlled invalid text unexpectedly passed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoded",
    (
        '{"a":1,"a":2}',
        '{"a":NaN}',
        '{"a":1e999}',
        '{"a":9007199254740992}',
        '{"a":"\ud800"}',
        '{"\ud800":1}',
        '{"a":"\\ud800"}',
        '{"a": 1}',
        '{"a":',
    ),
)
async def test_v4_tool_proof_preserves_original_refusal_order_and_causes(encoded):
    expected = _tool_read_error(encoded)
    with v4_preparation_memo_scope(_tool_clock()) as parent:
        for _ in range(2):
            assert _tool_read_error(encoded) == expected
        assert parent.cached_entry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ("x" * 1_048_577, "😀" * 270_000), ids=("ascii-over-bound", "utf8-over-bound"))
async def test_v4_tool_proof_over_bound_text_recomputes_without_new_refusal(text):
    encoded = canonical_json({"text": text})
    with v4_preparation_memo_scope(_tool_clock()) as parent:
        first, first_counts = _canonical_calls(lambda: tool_batch._read_json_value(encoded))
        second, second_counts = _canonical_calls(lambda: tool_batch._read_json_value(encoded))
        assert first == second == {"text": text} and first is not second
        assert first_counts == second_counts == {"parse": 1 if len(encoded) > 1_048_576 else 2, "render": 1}
        assert parent.cached_entry_count == 0


def test_native_read_helper_matches_public_wire_and_returns_fresh_mutable_values(monkeypatch):
    source = {"text": 'Keep "quotes", \\ slash,\n\t café 😀', "nested": [{"values": [1, True, None, 0.25, 1e-7]}]}
    public_bytes = canonical_json(source).encode("utf-8")
    original_writer = tool_batch.canonical_json_native_strings
    calls = []

    def observe(value):
        encoded = original_writer(value).encode("utf-8")
        assert encoded == public_bytes
        assert len(encoded) == len(public_bytes) and sha256(encoded).digest() == sha256(public_bytes).digest()
        calls.append(True)
        return encoded.decode("utf-8")

    monkeypatch.setattr(tool_batch, "canonical_json_native_strings", observe)
    first = tool_batch._read_json_value(public_bytes.decode("utf-8"))
    second = tool_batch._read_json_value(public_bytes.decode("utf-8"))
    assert first == second == source and first is not second
    assert first["nested"] is not second["nested"]
    first["nested"][0]["values"].append("changed fresh owner")
    assert second == source and calls == [True, True]


def test_native_owned_dump_matches_public_runtime_bytes_and_keeps_fresh_decodes(monkeypatch):
    original = full_result().runtime_preflight
    check = original.checks[0].model_copy(update={"detail": 'Keep "quotes", \\ slash,\n\t café 😀'})
    original = original.model_copy(update={"checks": [check]})
    public_bytes = canonical_json(original.model_dump(mode="json")).encode("utf-8")
    native_writer = tool_batch.canonical_json_native_strings
    calls = []

    def observe(value):
        calls.append(True)
        return native_writer(value)

    monkeypatch.setattr(tool_batch, "canonical_json_native_strings", observe)
    encoded = tool_batch._dump_owned(tool_batch._RUNTIME, original)
    assert encoded.encode("utf-8") == public_bytes and calls == [True]
    assert len(encoded.encode("utf-8")) == len(public_bytes)
    assert sha256(encoded.encode("utf-8")).digest() == sha256(public_bytes).digest()
    first = tool_batch._decode_owned(tool_batch._RUNTIME, encoded)
    second = tool_batch._decode_owned(tool_batch._RUNTIME, encoded)
    assert first == second == original and first is not second
    assert first.checks is not second.checks and first.checks[0] is not second.checks[0]
    assert first.readiness.blockers is not second.readiness.blockers
    first.checks.clear()
    first.readiness.blockers.clear()
    assert second == original and tool_batch._dump_owned(tool_batch._RUNTIME, second) == encoded


@pytest.mark.parametrize("helper", ("read", "owned_roundtrip"))
def test_changed_native_writer_is_refused_by_actual_tool_helpers(monkeypatch, helper):
    original = full_result().runtime_preflight
    encoded = tool_batch._dump_owned(tool_batch._RUNTIME, original)
    assert tool_batch._decode_owned(tool_batch._RUNTIME, encoded) == original
    native_writer = tool_batch.canonical_json_native_strings
    calls = []
    changed_call = 1 if helper == "read" else 2

    def changed(value):
        calls.append(True)
        result = native_writer(value)
        return result + " " if len(calls) == changed_call else result

    monkeypatch.setattr(tool_batch, "canonical_json_native_strings", changed)
    refusal = "not complete canonical JSON" if helper == "read" else "lost or normalized fields"
    with pytest.raises(AuditIntegrityError, match=refusal):
        if helper == "read":
            tool_batch._read_json_value(encoded)
        else:
            tool_batch._decode_owned(tool_batch._RUNTIME, encoded)
    assert calls == [True] * changed_call


@pytest.mark.parametrize("fault", ("coerced_bool", "coerced_nested_bool", "unknown_root", "omitted_default"))
def test_native_helpers_preserve_strict_owned_fields_and_complete_roundtrip(fault):
    original = full_result().runtime_preflight
    positive = canonical_json(original.model_dump(mode="json"))
    assert tool_batch._decode_owned(tool_batch._RUNTIME, positive) == original
    payload = json.loads(positive)
    if fault == "coerced_bool":
        payload["is_valid"] = 0
    elif fault == "coerced_nested_bool":
        payload["checks"][0]["passed"] = "false"
    elif fault == "unknown_root":
        payload["undeclared"] = "must not be discarded"
    else:
        assert fault == "omitted_default"
        del payload["warnings"]
    refusal = "lost or normalized fields" if fault == "omitted_default" else "violates its owned contract"
    with pytest.raises(AuditIntegrityError, match=refusal):
        tool_batch._decode_owned(tool_batch._RUNTIME, canonical_json(payload))


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
