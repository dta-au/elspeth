"""Current graph displays and complete-value reads retain their trust boundary."""

import json
from dataclasses import replace
from uuid import UUID

import pytest

from elspeth.contracts.composer_content import ComposerContentScope, MaterializedCompleteValue
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.advisor_context import (
    AdvisorGraphCoverageUnavailable,
    build_advisor_user_message,
    fit_advisor_checkpoint_summary,
    materialize_advisor_complete_values,
)
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompts import STATE_CONTEXT_PREFIX, build_composition_content_view, build_messages
from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER, redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import execute_tool, get_tool_definitions
from elspeth.web.composer.tools._common import materialize_pipeline_state_value, prepare_bounded_pipeline_state
from elspeth.web.composer.tools.sessions import _GET_COMPLETE_PIPELINE_VALUE_DECLARATION, _GET_PIPELINE_STATE_DECLARATION
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.helpers.session_fences import fenced_operation_context


@pytest.fixture
def graph():
    return CompositionState(
        nodes=(
            NodeSpec(
                id="prompt",
                node_type="transform",
                plugin="passthrough",
                input="input",
                on_success="output",
                on_error=None,
                options={"prompt": "雪" * 3_000},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.fixture
def live_context(tool_context, composer_service_with_real_sessions, result_session_id):
    sessions = composer_service_with_real_sessions._sessions_service
    assert sessions is not None
    with fenced_operation_context(sessions._engine, result_session_id) as operation:
        yield replace(
            tool_context,
            session_id=result_session_id,
            user_id="phase3-test-user",
            session_operation_context=operation,
            session_operation_authority=sessions.session_operation_authority,
        )


def _scope(graph, session_id=None):
    return ComposerContentScope(UUID(int=1) if session_id is None else session_id, composition_content_hash(graph))


def _dispatch(reference, graph, context):
    return execute_tool(
        "get_complete_pipeline_value",
        {"reference_id": reference},
        graph,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        session_id=context.session_id,
        session_operation_context=context.session_operation_context,
        session_operation_authority=context.session_operation_authority,
        user_id=context.user_id,
        validate_arguments=True,
    )


def test_composer_and_advisor_use_the_same_current_graph_manifest(graph, tool_context):
    scope = _scope(graph)
    view = build_composition_content_view(graph, content_scope=scope)
    summary = fit_advisor_checkpoint_summary(graph, {}, 0, content_scope=scope)
    assert summary.content_manifest == view.manifest
    assert graph.nodes[0].options["prompt"] not in "".join(part.text for part in summary.values)
    assert "Diagnostic graph view" in summary.values[0].text
    assert all(part.trust is None for part in summary.values)
    references = tuple(reference.reference_id for reference in view.manifest.values)
    first = materialize_advisor_complete_values(summary, graph, content_scope=scope, reference_ids=references)
    second = materialize_advisor_complete_values(summary, graph, content_scope=scope, reference_ids=references)
    assert first[0] is not second[0]
    assert first[0].to_value() == graph.nodes[0].options["prompt"]
    assert first[0].reference == second[0].reference
    with pytest.raises(AuditIntegrityError):
        materialize_advisor_complete_values(summary, graph, content_scope=replace(scope, session_id=UUID(int=2)), reference_ids=references)


def test_bounded_graph_projection_does_not_rewrite_raw_history(graph, tool_context):
    history = [{"role": "assistant", "content": "verbatim", "thinking_blocks": [{"signature": "raw-sig", "thinking": "雪" * 3_000}]}]
    messages = build_messages(
        history,
        graph,
        "continue",
        tool_context.catalog,
        plugin_snapshot=tool_context.plugin_snapshot,
        content_scope=_scope(graph),
        schemas_loaded=frozenset(),
    )
    assert messages[2] == history[0]
    context = next(message["content"] for message in messages if message["content"].startswith(STATE_CONTEXT_PREFIX))
    payload = json.loads(context.split("\n", 1)[1])
    assert payload["current_state"]["schema_version"] == "composer.bounded-view.v1"
    assert payload["current_state"]["content_manifest"]["values"]
    assert graph.nodes[0].options["prompt"] not in context


def test_advisor_materializes_only_explicit_complete_value_coverage(graph):
    graph = replace(graph, nodes=(replace(graph.nodes[0], options={"first": "a" * 3_000, "second": "b" * 3_000}),))
    scope = _scope(graph)
    summary = fit_advisor_checkpoint_summary(graph, {}, 0, content_scope=scope)
    assert summary.content_manifest is not None
    assert len(summary.content_manifest.values) == 2
    selected = (summary.content_manifest.values[0].reference_id,)
    assert materialize_advisor_complete_values(summary, graph, content_scope=scope, reference_ids=()) == ()
    result = materialize_advisor_complete_values(summary, graph, content_scope=scope, reference_ids=selected)
    assert len(result) == 1 and result[0].to_value() == "a" * 3_000
    assert len(summary.content_manifest.values) == 2
    with pytest.raises(AuditIntegrityError, match="unique exact reference"):
        materialize_advisor_complete_values(summary, graph, content_scope=scope, reference_ids=selected * 2)


def test_final_advisor_render_preserves_only_owned_reference_identity_positions(graph):
    identity = str(UUID(int=1))
    graph = replace(
        graph,
        sources={
            "source": SourceSpec(
                plugin="csv", on_success="input", on_validation_failure="discard", options={"path": "data.csv", "blob_ref": identity}
            )
        },
        nodes=(replace(graph.nodes[0], options={"prompt": "x" * 3_000, "note": identity}),),
    )
    summary = fit_advisor_checkpoint_summary(graph, {}, 0, content_scope=_scope(graph))
    rendered = build_advisor_user_message(
        {
            "trigger": "proactive_security_safety",
            "problem_summary": "Inspect the neutral fields.",
            "recent_errors": [],
            "attempted_actions": [],
            "schema_excerpt": "".join(part.text for part in summary.values),
            "_elspeth_advisor_summary_parts": summary,
        }
    )
    assert f'"session_id":"{identity}"' in rendered
    assert f'"blob_ref":"{identity}"' in rendered
    assert f'"note":"{identity}"' not in rendered
    assert summary.content_manifest is not None
    for reference in summary.content_manifest.values:
        assert reference.reference_id in rendered and reference.content_hash in rendered


@pytest.mark.parametrize("key", [str(UUID(int=1)), "person@example.com"])
def test_advisor_refuses_reference_paths_that_the_privacy_policy_would_change(graph, key):
    graph = replace(graph, nodes=(replace(graph.nodes[0], options={key: "x" * 3_000}),))
    with pytest.raises(AdvisorGraphCoverageUnavailable, match="path"):
        fit_advisor_checkpoint_summary(graph, {}, 0, content_scope=_scope(graph))


@pytest.mark.parametrize("reference", ["not-a-uuid", "ABCDEF01-2345-6789-ABCD-EF0123456789"])
def test_advisor_refuses_malformed_configured_source_identity(graph, reference):
    graph = replace(
        graph,
        sources={
            "source": SourceSpec(
                plugin="csv", on_success="input", on_validation_failure="discard", options={"path": "data.csv", "blob_ref": reference}
            )
        },
    )
    with pytest.raises(AdvisorGraphCoverageUnavailable, match="canonical identity"):
        fit_advisor_checkpoint_summary(graph, {}, 0, content_scope=_scope(graph))


def test_graph_identity_and_credentials_are_checked_before_hiding_values(graph):
    with pytest.raises(AuditIntegrityError, match="current graph scope"):
        build_composition_content_view(graph, content_scope=replace(_scope(graph), composition_content_hash="b" * 64))
    secret = replace(graph.nodes[0], options={"api_key": "private-value" * 1_000})
    unsafe = replace(graph, nodes=(secret,))
    with pytest.raises(CredentialMaterialRefused):
        build_composition_content_view(unsafe, content_scope=_scope(unsafe))


def test_complete_value_is_offered_as_noncacheable_hash_only_discovery(graph, live_context):
    definition = next(item for item in get_tool_definitions() if item["name"] == "get_complete_pipeline_value")
    assert set(definition["parameters"]["properties"]) == {"reference_id"}
    assert definition["parameters"]["additionalProperties"] is False
    assert _GET_COMPLETE_PIPELINE_VALUE_DECLARATION.cacheable is False
    view = prepare_bounded_pipeline_state(graph, live_context)
    reference = view.manifest.values[0]
    first, second = (_dispatch(reference.reference_id, graph, live_context) for _ in range(2))
    assert first.success and second.success
    contract = _GET_COMPLETE_PIPELINE_VALUE_DECLARATION.response_contract
    assert contract is not None
    admitted = contract.admit(first.data)
    wire = admitted.to_wire()
    assert wire["value"] == graph.nodes[0].options["prompt"]
    assert wire["reference_id"] == reference.reference_id
    assert set(wire) == {"session_id", "composition_content_hash", "reference_id", "path", "content_hash", "utf8_bytes", "value"}
    assert deep_thaw(first.data) == wire
    assert contract.admit(wire).to_wire() == wire
    assert admitted.readmit(contract).to_wire() == wire
    assert first.data is not second.data
    canonical = _GET_PIPELINE_STATE_DECLARATION.handler({}, graph, live_context)
    canonical_contract = _GET_PIPELINE_STATE_DECLARATION.response_contract
    assert canonical_contract is not None
    assert "content_manifest" not in canonical_contract.admit(canonical.data).to_wire()


@pytest.mark.parametrize(
    ("key", "replacement"),
    [
        ("session_id", str(UUID(int=2))),
        ("session_id", "00000000000000000000000000000001"),
        ("composition_content_hash", "b" * 64),
        ("reference_id", "b" * 64),
        ("path", ["foreign"]),
        ("path", frozenset({"nodes"})),
        ("path", [True]),
        ("content_hash", "b" * 64),
        ("utf8_bytes", 1),
        ("utf8_bytes", True),
        ("utf8_bytes", "9002"),
        ("value", "corrupt"),
        ("value", {"nested": "value"}),
    ],
)
def test_complete_value_response_refuses_corrupted_wire_binding(graph, key, replacement):
    contract = _GET_COMPLETE_PIPELINE_VALUE_DECLARATION.response_contract
    assert contract is not None
    value = MaterializedCompleteValue.from_value(_scope(graph), ("nodes", 0, "options", "prompt"), graph.nodes[0].options["prompt"])
    wire = contract.admit(value).to_wire()
    assert type(wire) is dict
    wire[key] = replacement
    with pytest.raises((AuditIntegrityError, FrameworkBugError)):
        contract.admit(wire)


def test_complete_value_response_refuses_private_or_incomplete_shapes(graph):
    contract = _GET_COMPLETE_PIPELINE_VALUE_DECLARATION.response_contract
    assert contract is not None
    value = MaterializedCompleteValue.from_value(_scope(graph), ("nodes", 0, "options", "prompt"), graph.nodes[0].options["prompt"])
    wire = contract.admit(value).to_wire()
    assert type(wire) is dict
    for payload in (
        {"content_manifest": {}, "json_text": value.json_text},
        {**wire, "extra": None},
        {k: v for k, v in wire.items() if k != "value"},
    ):
        with pytest.raises(FrameworkBugError):
            contract.admit(payload)
    container = MaterializedCompleteValue.from_value(_scope(graph), ("nodes",), ["complete"])
    with pytest.raises(FrameworkBugError):
        contract.admit(container)


def test_identical_reference_cannot_reuse_released_operation_authority(graph, live_context):
    view = prepare_bounded_pipeline_state(graph, live_context)
    reference = view.manifest.values[0].reference_id
    first = materialize_pipeline_state_value(reference, graph, live_context)
    second = materialize_pipeline_state_value(reference, graph, live_context)
    assert first is not second and first.reference == second.reference
    authority, operation = live_context.session_operation_authority, live_context.session_operation_context
    assert authority is not None and operation is not None
    authority.release(operation)
    with pytest.raises(SessionOperationFenceLost):
        materialize_pipeline_state_value(reference, graph, live_context)


def test_canonical_wire_keeps_full_value_while_public_audit_redacts_it(graph, live_context):
    reference = prepare_bounded_pipeline_state(graph, live_context).manifest.values[0].reference_id
    result = _dispatch(reference, graph, live_context)
    admitted = admit_discovery_result("get_complete_pipeline_value", result)
    serialized = serialize_admitted_discovery_result(admitted)
    payload = json.loads(serialized)
    assert payload["data"]["value"] == graph.nodes[0].options["prompt"]
    public = redact_tool_call_response("get_complete_pipeline_value", payload, telemetry=NoopRedactionTelemetry())
    assert public["data"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert graph.nodes[0].options["prompt"] not in json.dumps(public, ensure_ascii=False)
    assert result.success and public["success"] is True


@pytest.mark.parametrize("missing", ["user", "operation", "authority", "foreign_session"])
def test_materialization_refuses_missing_or_foreign_actual_context(graph, live_context, missing):
    reference = prepare_bounded_pipeline_state(graph, live_context).manifest.values[0].reference_id
    changes = {
        "user": {"user_id": None},
        "operation": {"session_operation_context": None},
        "authority": {"session_operation_authority": None},
        "foreign_session": {"session_id": str(UUID(int=2))},
    }
    with pytest.raises(AuditIntegrityError, match="authenticated compose operation"):
        materialize_pipeline_state_value(reference, graph, replace(live_context, **changes[missing]))


def test_stale_foreign_and_unsupported_references_never_fall_back_to_raw_graph(graph, live_context):
    reference = prepare_bounded_pipeline_state(graph, live_context).manifest.values[0].reference_id
    changed = replace(graph, nodes=(replace(graph.nodes[0], options={"prompt": "changed" * 1_000}),), version=graph.version + 1)
    result = _dispatch(reference, changed, live_context)
    assert not result.success
    assert result.validation.errors[0].error_code == "complete_value_unavailable"
    foreign = build_composition_content_view(graph, content_scope=_scope(graph)).manifest.values[0].reference_id
    assert not _dispatch(foreign, graph, live_context).success
    unsupported = replace(graph, nodes=(replace(graph.nodes[0], options={"k" * 3_000: "complete"}),))
    result = _dispatch(reference, unsupported, live_context)
    assert not result.success and result.validation.errors[0].error_code == "complete_value_unsupported"
    assert graph.nodes[0].options["prompt"] not in str(deep_thaw(result.data))
