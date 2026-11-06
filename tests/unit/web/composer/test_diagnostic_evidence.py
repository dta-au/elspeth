"""Private evidence retains authored meaning without credential/raw fallback."""

import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.diagnostic_evidence import (
    DIAGNOSTIC_EVIDENCE_KIND,
    ComposerDiagnosticEvidence,
    build_private_tool_evidence,
    is_diagnostic_evidence_envelope,
)


def build(arguments: object, *, wire: str | None = None, result: object = None, tool: str = "add_node") -> ComposerDiagnosticEvidence:
    return build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="call-a",
        tool_name=tool,
        status="success",
        arguments_canonical=canonical_json(arguments),
        result_canonical=canonical_json(result) if result is not None else None,
        raw_arguments=wire,
    )


@pytest.mark.parametrize("tool", ("upsert_node", "splice_transform", "set_pipeline", "emit_pipeline_proposal"))
@pytest.mark.parametrize("authoring_metadata", (False, True))
def test_private_evidence_preserves_owned_field_mapper_labels(tool: str, authoring_metadata: bool) -> None:
    node = {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
    }
    if authoring_metadata:
        node["options"]["interpretation_requirements"] = [
            {"id": "rename-review", "kind": "pipeline_decision", "user_term": "customer identifier", "status": "pending"}
        ]
    arguments = (
        node
        if tool == "upsert_node"
        else {"node": {"plugin": "field_mapper", "options": node["options"]}}
        if tool == "splice_transform"
        else {"nodes": [node]}
        if tool == "set_pipeline"
        else {"pipeline": {"nodes": [node]}}
    )
    evidence = build(arguments, tool=tool)
    if tool == "emit_pipeline_proposal":
        assert evidence.arguments.disposition == "filtered"
        assert "customer_id" not in evidence.arguments.content
        return
    assert evidence.arguments.disposition == "retained"
    assert json.loads(evidence.arguments.content) == arguments


def test_private_field_mapper_projection_retains_credential_refusal_and_unknown_patch_filtering() -> None:
    patch = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "customer_id"}}}
    evidence = build(patch, tool="patch_node_options")
    assert evidence.arguments.disposition == "filtered"
    assert "customer_id" not in evidence.arguments.content
    node = {
        "node_type": "transform",
        "plugin": "field_mapper",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}, "api_key": "low-entropy-secret"},
    }
    evidence = build(node, tool="upsert_node")
    assert evidence.arguments.disposition == "filtered"
    assert "low-entropy-secret" not in evidence.arguments.content
    del node["options"]["api_key"]
    node["options"]["interpretation_requirements"] = [{"draft": "sk-" + "a" * 24}]
    evidence = build(node, tool="upsert_node")
    assert evidence.arguments.disposition == "filtered"
    assert "sk-" + "a" * 24 not in evidence.arguments.content


@pytest.mark.parametrize("mismatch", ("operation", "request", "fence", "epoch"))
def test_operation_failure_capture_rejects_other_request_or_fence(mismatch: str) -> None:
    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
    from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts
    from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts

    context = SessionOperationContext(SessionOperationFence("session-a", "compose-fence", "lease", 4), SessionOperationKind.COMPOSE)
    capture = ComposerOperationFailureFacts("durable-operation", "request", context, capture_failure_facts(RuntimeError("Private fact")))
    capture.validate_for(durable_operation_id="durable-operation", request_id="request", context=context)
    altered = replace(
        context,
        fence=replace(
            context.fence,
            operation_id="other-fence" if mismatch == "fence" else "compose-fence",
            operation_epoch=5 if mismatch == "epoch" else 4,
        ),
    )
    with pytest.raises(AuditIntegrityError):
        capture.validate_for(
            durable_operation_id="another-operation" if mismatch == "operation" else "durable-operation",
            request_id="another-request" if mismatch == "request" else "request",
            context=altered,
        )


def test_operation_failure_draft_preserves_bounded_facts_and_final_support_identity() -> None:
    from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
    from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts, private_operation_failure_draft
    from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    context = SessionOperationContext(SessionOperationFence("session-a", "compose-fence", "lease", 4), SessionOperationKind.COMPOSE)
    original = ExceptionGroup("Private group", [RuntimeError("ordinary private detail " * 80) for _ in range(12)])
    facts = capture_failure_facts(original)
    capture = ComposerOperationFailureFacts("durable-operation", "request", context, facts)
    selected = project_composer_operation_error(original, request_id="request", diagnostic_id="11111111-1111-4111-8111-111111111111")
    draft = private_operation_failure_draft(capture, failure=selected)
    assert draft.role == "audit" and draft.inherit_composition_state is False
    assert draft.composition_state_id is None
    assert len(facts.canonical.encode("utf-8")) <= 8192
    evidence = ComposerDiagnosticEvidence.from_envelope(
        draft.tool_calls[0], expected_session_id="session-a", expected_operation_id="compose-fence"
    )
    assert evidence.result.content == facts.canonical
    assert evidence.tool_call_id == "operation:durable-operation"
    assert evidence.evidence_id.startswith("operation-failure:")
    assert evidence.status == "operation_failure"
    assert json.loads(evidence.arguments.content) == {
        "durable_operation_id": "durable-operation",
        "request_id": "request",
        "diagnostic_id": selected.diagnostic_id,
        "failure_code": "operation_failed",
        "correlation_kind": "internal_operation",
    }
    assert "ordinary private detail" not in selected.model_dump_json()


def test_complete_nested_authored_values_and_result_survive() -> None:
    arguments = {
        "options": {
            "prompt_template": "Classify {{ row.case_study }}",
            "references": [{"id": "complex", "text": "Long normal policy"}],
            "interpretation_requirements": [{"user_term": "service type", "review_id": "service type:classifier"}],
            "temperature": 0,
            "enabled": False,
            "optional": None,
            "api_key": {"secret_ref": "classification_provider", "secret_scope": "user"},
        }
    }
    result = {"success": False, "error": "Review reference missing", "candidate": arguments}
    evidence = build(arguments, wire=json.dumps(arguments, indent=2), result=result)
    assert json.loads(evidence.arguments.content) == arguments
    assert json.loads(evidence.result.content) == result
    assert evidence.wire_arguments.content == json.dumps(arguments, indent=2)
    assert evidence.arguments.disposition == "retained"
    restored = ComposerDiagnosticEvidence.from_envelope(evidence.to_envelope(), expected_session_id="session-a")
    assert restored == evidence


@pytest.mark.parametrize(
    "wire",
    [
        '{"api_key":"opaque-first","api_key":{"secret_ref":"provider"}}',
        '{"api_key":{"secret_ref":"provider"},"api_key":"opaque-last"}',
        '{"api_key":"opaque-first","api\\u005fkey":{"secret_ref":"provider"}}',
        '{"prompt":"ordinary", "api\\u005fkey":"opaque"',
        '{"prompt":"ordinary"} trailing',
    ],
)
def test_ambiguous_or_malformed_wire_never_falls_back_to_raw(wire: str) -> None:
    evidence = build({"prompt": "ordinary"}, wire=wire)
    assert evidence.wire_arguments.content is None
    assert evidence.wire_arguments.disposition == "unavailable"
    assert json.loads(evidence.arguments.content) == {"prompt": "ordinary"}


def test_escaped_credentials_are_filtered_and_wire_withheld() -> None:
    wire = '{"prompt":"ordinary", "api\\u005fkey":"opaque credential"}'
    evidence = build(json.loads(wire), wire=wire, result={"password": "opaque result", "explanation": "benign"})
    assert evidence.arguments.disposition == "filtered"
    assert json.loads(evidence.arguments.content)["prompt"] == "ordinary"
    assert "opaque" not in json.dumps(evidence.to_envelope())
    assert evidence.result.disposition == "filtered"
    assert evidence.wire_arguments.content is None


def test_filter_findings_not_output_equality_decide_raw_eligibility() -> None:
    wire = '{"api_key":"<redacted-secret>"}'
    evidence = build(json.loads(wire), wire=wire)
    assert evidence.wire_arguments.content is None
    assert evidence.arguments.disposition == "filtered"


@pytest.mark.parametrize(
    "marker",
    [{"secret_ref": "not-a-valid-name"}, {"secret_ref": "safe", "extra": "ordinary"}, {"secret_ref": "safe", "secret_scope": "invalid"}],
)
def test_invalid_secret_reference_is_removed(marker: object) -> None:
    evidence = build({"api_key": marker})
    assert json.loads(evidence.arguments.content) == {"api_key": "<redacted-secret>"}


def test_plugin_declared_credential_and_structural_option_name() -> None:
    evidence = build({"plugin": "database", "options": {"url": "opaque credential", "table": "output"}}, tool="set_output")
    assert json.loads(evidence.arguments.content)["options"] == {"url": "<redacted-secret>", "table": "output"}
    evidence = build({"name": "provider", "option_key": "api_key", "target": "node", "target_id": "classifier"}, tool="wire_secret_ref")
    assert json.loads(evidence.arguments.content)["option_key"] == "api_key"


def test_nested_reference_data_does_not_acquire_plugin_credential_semantics() -> None:
    arguments = {"options": {"reference": {"plugin": "database", "options": {"url": "ordinary reference text"}}}}
    assert json.loads(build(arguments).arguments.content) == arguments


def test_unrelated_safe_wire_is_not_bound_to_other_arguments() -> None:
    evidence = build({"prompt": "actual arguments"}, wire='{"prompt":"different arguments"}')
    assert evidence.wire_arguments.content is None
    assert evidence.wire_arguments.reasons == ("semantic_mismatch",)


def test_safe_wire_is_not_invented_for_alternate_cohort() -> None:
    evidence = build({"prompt": "ordinary"})
    assert evidence.wire_arguments.reasons == ("not_recorded",)
    assert evidence.result.disposition == "not_returned"
    assert evidence.result.content is None


def test_filtered_content_tampering_and_cross_bindings_fail_closed() -> None:
    evidence = build({"prompt": "ordinary"})
    envelope = evidence.to_envelope()
    envelope["evidence"]["arguments"]["content"] = '{"prompt":"changed"}'
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id="session-a")
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_envelope(evidence.to_envelope(), expected_session_id="other-session")
    envelope = evidence.to_envelope()
    envelope["tool_call_id"] = "different-call"
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id="session-a")
    assert is_diagnostic_evidence_envelope({"_kind": DIAGNOSTIC_EVIDENCE_KIND, "malformed": True})


def test_payload_cannot_claim_absence_with_retained_content() -> None:
    evidence = build({"prompt": "ordinary"})
    with pytest.raises(AuditIntegrityError):
        replace(evidence.arguments, disposition="unavailable")


def test_planner_evidence_identity_is_separate_from_tool_call_identity() -> None:
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=2,
        tool_call_id="reused-call",
        evidence_id="provider-b:reused-call:received",
        tool_name="set_pipeline",
        status="received",
        arguments_canonical='{"prompt":"ordinary"}',
        result_canonical=None,
    )
    assert evidence.tool_call_id == "reused-call"
    assert evidence.evidence_id == "provider-b:reused-call:received"
    envelope = evidence.to_envelope()
    envelope["evidence_id"] = "different-evidence"
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id="session-a")


def test_ordered_pipeline_authority_survives_filtering() -> None:
    from elspeth.web.composer.authority_hashing import composer_authority_canonical_json, restore_composer_authority_payload

    arguments = {
        "sources": {
            "second": {"plugin": "csv", "options": {"path": "second.csv"}},
            "first": {"plugin": "csv", "options": {"path": "first.csv"}},
        },
        "nodes": [{"node_type": "row_union", "branches": {"z": "z-edge", "a": "a-edge"}}],
    }
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="call-a",
        tool_name="set_pipeline",
        status="success",
        arguments_canonical=canonical_json(arguments),
        result_canonical=None,
        authority_arguments_canonical=composer_authority_canonical_json(arguments),
    )
    restored = restore_composer_authority_payload(json.loads(evidence.authority_arguments.content))
    assert list(restored["sources"]) == ["second", "first"]
    assert list(restored["nodes"][0]["branches"]) == ["z", "a"]


def test_recognized_key_credentials_are_removed_without_key_collision() -> None:
    secret_a = "sk-or-v1-" + "a" * 30
    secret_b = "sk-or-v1-" + "b" * 30
    evidence = build({secret_a: "one", secret_b: "two", "prompt": "ordinary"}, result={"ordinary": secret_a})
    assert json.loads(evidence.arguments.content) == {"prompt": "ordinary"}
    assert secret_a not in json.dumps(evidence.to_envelope())
    assert secret_b not in json.dumps(evidence.to_envelope())


def test_large_admitted_candidate_is_retained_beyond_old_scrubber_width() -> None:
    arguments = {"references": [f"ordinary-reference-{index}" for index in range(700)]}
    assert json.loads(build(arguments).arguments.content) == arguments


def test_malformed_wrapper_is_not_misrepresented_as_authored_json() -> None:
    evidence = build({"_unparseable_arguments": '{"api\\u005fkey":"opaque"', "_truncated": False})
    assert evidence.arguments.content is None
    assert evidence.arguments.reasons == ("canonical_sentinel",)


def test_oversized_original_wire_is_explicitly_unavailable() -> None:
    evidence = build({"ordinary": True}, wire='"' + "a" * 1_048_576 + '"')
    assert evidence.wire_arguments.content is None
    assert evidence.wire_arguments.reasons == ("bounds",)


def test_outer_epoch_requires_exact_integer() -> None:
    envelope = build({"prompt": "ordinary"}).to_envelope()
    envelope["operation_epoch"] = True
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_envelope(envelope, expected_session_id="session-a")


@pytest.mark.parametrize("field", ["schema_version", "detector_version", "operation_epoch", "tool_name", "evidence_id"])
def test_stored_metadata_requires_exact_primitive_types(field: str) -> None:
    data = build({"prompt": "ordinary"}).to_dict()
    data[field] = True
    with pytest.raises(AuditIntegrityError):
        ComposerDiagnosticEvidence.from_dict(data)


def test_valid_reference_in_plugin_declared_slot_is_retained() -> None:
    arguments = {"plugin": "database", "options": {"url": {"secret_ref": "database_connection", "secret_scope": "org"}}}
    assert json.loads(build(arguments, tool="set_output").arguments.content) == arguments


def test_pipeline_output_declared_slot_is_filtered_in_candidate_and_result() -> None:
    arguments = {"outputs": {"warehouse": {"plugin": "database", "options": {"url": "opaque credential", "table": "benign"}}}}
    evidence = build(arguments, tool="set_pipeline", result={"data": {"pipeline": arguments}})
    assert "opaque credential" not in json.dumps(evidence.to_envelope())
    assert evidence.arguments.disposition == evidence.result.disposition == "filtered"


@pytest.mark.parametrize("content", ['{"x":1,"x":2}', '{ "x": 1 }', "NaN"])
def test_corrupt_owned_canonical_source_fails_closed(content: str) -> None:
    with pytest.raises(AuditIntegrityError):
        build_private_tool_evidence(
            session_id="session-a",
            operation_id="operation-a",
            operation_epoch=1,
            tool_call_id="call-a",
            tool_name="add_node",
            status="success",
            arguments_canonical=content,
            result_canonical=None,
        )


def test_pipeline_envelope_wire_is_eligible_for_same_semantic_candidate() -> None:
    arguments = {"sources": {}, "nodes": [], "outputs": []}
    wire = json.dumps({"pipeline": arguments}, indent=2)
    evidence = build(arguments, wire=wire, tool="set_pipeline")
    assert evidence.wire_arguments.content == wire


def test_explicit_additional_credential_fields_cover_existing_output_edits() -> None:
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="call-a",
        tool_name="patch_output_options",
        status="success",
        arguments_canonical=canonical_json({"sink_name": "database_output", "patch": {"url": "opaque credential", "table": "benign"}}),
        result_canonical=None,
        additional_credential_fields=("url",),
    )
    assert json.loads(evidence.arguments.content)["patch"] == {"url": "<redacted-secret>", "table": "benign"}


def test_pluginless_output_patch_filters_only_its_declared_url_slot() -> None:
    arguments = {
        "sink_name": "database_output",
        "url": "ordinary outer URL",
        "patch": {
            "url": "opaque database credential",
            "table": "output",
            "reference": {"url": "ordinary reference URL", "options": {"url": "ordinary nested options URL"}},
        },
    }
    evidence = build(arguments, tool="patch_output_options", wire=json.dumps(arguments))
    projected = json.loads(evidence.arguments.content)
    assert projected["patch"]["url"] == "<redacted-secret>"
    assert projected["url"] == arguments["url"]
    assert projected["patch"]["reference"] == arguments["patch"]["reference"]
    assert evidence.wire_arguments.content is None


def test_plugin_declared_slot_does_not_spread_to_nested_reference_urls() -> None:
    arguments = {"plugin": "database", "options": {"url": "opaque credential", "reference": {"url": "ordinary reference URL"}}}
    projected = json.loads(build(arguments, tool="set_output").arguments.content)
    assert projected["options"]["url"] == "<redacted-secret>"
    assert projected["options"]["reference"] == arguments["options"]["reference"]


def test_planner_emit_authority_preserves_wrapped_pipeline_order() -> None:
    """Retain the native control on live flat set_pipeline and its wrapped wire."""
    from elspeth.web.composer.authority_hashing import project_composer_authority_payload, restore_composer_authority_payload

    pipeline = {"sources": {"z": {"plugin": "csv"}, "a": {"plugin": "csv"}}, "nodes": []}
    arguments = pipeline
    authority = project_composer_authority_payload(pipeline)
    wire = json.dumps({"pipeline": pipeline})
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="pipeline-a",
        tool_name="set_pipeline",
        status="success",
        arguments_canonical=canonical_json(arguments),
        authority_arguments_canonical=canonical_json(authority),
        result_canonical=None,
        raw_arguments=wire,
    )
    retained_authority = json.loads(evidence.authority_arguments.content)
    assert list(restore_composer_authority_payload(retained_authority)["sources"]) == ["z", "a"]
    assert evidence.wire_arguments.content == wire
    assert ComposerDiagnosticEvidence.from_envelope(evidence.to_envelope(), expected_session_id="session-a") == evidence


def test_authority_projection_bound_does_not_lose_admitted_generic_arguments() -> None:
    from elspeth.web.composer.authority_hashing import composer_authority_canonical_json

    arguments = {"sources": {f"source_{index}": None for index in range(3400)}}
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="call-a",
        tool_name="set_pipeline",
        status="argument_error",
        arguments_canonical=canonical_json(arguments),
        authority_arguments_canonical=composer_authority_canonical_json(arguments),
        result_canonical=None,
    )
    assert json.loads(evidence.arguments.content) == arguments
    assert evidence.authority_arguments.disposition == "unavailable"
    assert evidence.authority_arguments.reasons == ("bounds",)


def test_raw_number_outside_canonical_domain_is_explicitly_unavailable() -> None:
    evidence = build_private_tool_evidence(
        session_id="session-a",
        operation_id="operation-a",
        operation_epoch=1,
        tool_call_id="call-a",
        tool_name="set_metadata",
        status="argument_error",
        arguments_canonical=canonical_json({"_canonicalization_error": "unsupported numeric domain"}),
        result_canonical=None,
        raw_arguments='{"value":1152921504606846976}',
        error_category="canonicalization",
    )
    assert evidence.arguments.disposition == "unavailable"
    assert evidence.wire_arguments.disposition == "unavailable"
    assert evidence.wire_arguments.reasons == ("unsupported",)
    assert evidence.wire_arguments.content is None


def test_owned_canonical_number_outside_canonical_domain_is_corruption() -> None:
    with pytest.raises(AuditIntegrityError):
        build_private_tool_evidence(
            session_id="session-a",
            operation_id="operation-a",
            operation_epoch=1,
            tool_call_id="call-a",
            tool_name="set_metadata",
            status="success",
            arguments_canonical='{"value":1152921504606846976}',
            result_canonical=None,
        )


def test_owned_integral_float_canonical_roundtrip_is_preserved() -> None:
    evidence = build({"threshold": 1e17}, result={"threshold": 1e17})
    assert evidence.arguments.content == canonical_json({"threshold": 1e17})
    assert evidence.result.disposition == "retained"
    assert ComposerDiagnosticEvidence.from_envelope(evidence.to_envelope(), expected_session_id="session-a") == evidence
