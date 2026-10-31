"""Retained P4 metadata uses its own credential and closed-value owners."""

import json
from hashlib import sha256

import pytest

from elspeth.contracts.composer_mutation import ComposerMutationCeiling
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.continuation_prepared_p4 import (
    capture_prepared_p4,
    decode_prepared_p4,
    encode_prepared_p4,
    p4_semantic_input,
    prepared_p4_members,
)
from elspeth.web.composer.diagnostic_evidence import (
    ComposerDiagnosticEvidence,
    build_private_tool_evidence,
    diagnostic_evidence_identity,
)
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from tests.unit.web.composer.test_continuation_prepared_p4 import prepared_input

CREDENTIAL_URI = "postgresql://user:password@db.example/data"  # secret-scan: allow-this-line


def _empty_input(
    *,
    session_id="session",
    creator_operation_id="creator",
    parent_composition_state_id=None,
    expected_current_state_id=None,
):
    context = SessionOperationContext(
        SessionOperationFence(session_id, creator_operation_id, "fixture-live-lease", 7),
        SessionOperationKind.COMPOSE,
    )
    rows = ()
    return capture_prepared_p4(
        context=context,
        assistant_content="retained prose",
        raw_content=None,
        redacted_calls=(),
        redacted_rows=rows,
        revoked_rows=rows,
        mutation_ceiling=None,
        authority_revocation=None,
        private_tool_evidence=(),
        private_tool_failures=(),
        rejection_records=(),
        withheld_replies=(),
        invocations=(),
        authorings=(),
        parent_composition_state_id=parent_composition_state_id,
        expected_current_state_id=expected_current_state_id,
        assistant_row_uses_current_dispatch=False,
        crash_pending=False,
    )


def _roundtrip(value, *, env_ref_names=frozenset()):
    encoded = encode_prepared_p4(value, env_ref_names=env_ref_names)
    restored = decode_prepared_p4(encoded, env_ref_names=env_ref_names)
    assert restored == value
    assert encode_prepared_p4(restored, env_ref_names=env_ref_names) == encoded
    assert p4_semantic_input(restored, env_ref_names=env_ref_names) == p4_semantic_input(value, env_ref_names=env_ref_names)
    members = prepared_p4_members(restored, env_ref_names=env_ref_names)
    if value.revoked_rows_are_original:
        assert members.revoked_rows is members.redacted_rows
    assert "fixture-live-lease" not in encoded
    return members


def _refuses_credentials(value):
    with pytest.raises(CredentialMaterialRefused):
        prepared_p4_members(value)
    with pytest.raises(CredentialMaterialRefused):
        encode_prepared_p4(value)
    with pytest.raises(CredentialMaterialRefused):
        decode_prepared_p4(canonical_json(value.model_dump(mode="json", warnings="error")))
    with pytest.raises(CredentialMaterialRefused):
        p4_semantic_input(value)


@pytest.mark.parametrize("member", ["session_id", "creator_operation_id", "parent_composition_state_id", "expected_current_state_id"])
def test_empty_cohort_identity_metadata_is_admitted_without_a_private_evidence_backstop(member):
    clean = _empty_input(**{member: "ordinary-metadata-café"})
    _roundtrip(clean)
    assert clean.model_dump()[member] == "ordinary-metadata-café"
    _refuses_credentials(clean.model_copy(update={member: CREDENTIAL_URI}))
    with pytest.raises(CredentialMaterialRefused):
        _empty_input(**{member: CREDENTIAL_URI})


@pytest.mark.parametrize("member", ["parent_composition_state_id", "expected_current_state_id"])
def test_nonempty_p4_standalone_lineage_metadata_requires_credential_admission(member):
    value, *_ = prepared_input(graph=False)
    _roundtrip(value)
    for replacement in (None, "another-parent-café"):
        changed = value.model_copy(update={member: replacement})
        _roundtrip(changed)
        assert changed.model_dump()[member] == replacement
    # No cohort or digest needs changing: this scalar otherwise admits exactly.
    _refuses_credentials(value.model_copy(update={member: CREDENTIAL_URI}))


def test_lineage_environment_reference_is_literal_metadata_without_retaining_a_lease():
    value = _empty_input().model_copy(update={"parent_composition_state_id": "${DB_URL}"})
    # The actual policy checks inventory only under a credential field.
    require_no_credential_material(
        {"parent_composition_state_id": "${DB_URL}"},
        surface="test lineage metadata",
    )
    _roundtrip(value)
    _roundtrip(value, env_ref_names=frozenset({"DB_URL"}))
    assert value.parent_composition_state_id == "${DB_URL}"
    require_no_credential_material(
        {"password": "${DB_URL}"},
        surface="test credential slot",
        env_ref_names=frozenset({"DB_URL"}),
    )
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material({"password": "${DB_URL}"}, surface="test credential slot")
    _refuses_credentials(value.model_copy(update={"parent_composition_state_id": CREDENTIAL_URI}))


def _with_ceiling_request(value, request_id):
    ceiling = json.loads(value.mutation_ceiling_json)
    revocation = json.loads(value.authority_revocation_json)
    ceiling["request_id"] = request_id
    revocation["ceiling"] = ceiling.copy()
    # The real nominal owner admits the string; P4 owns credential admission.
    assert ComposerMutationCeiling(ceiling["session_id"], request_id, ceiling["initial_trust_mode"]).request_id == request_id
    return value.model_copy(
        update={
            "mutation_ceiling_json": canonical_json(ceiling),
            "authority_revocation_json": canonical_json(revocation),
        }
    )


def test_mutation_request_metadata_is_checked_with_coherent_ceiling_and_revocation():
    value, *_ = prepared_input(graph=False)
    clean = _with_ceiling_request(value, "request-café")
    members = _roundtrip(clean)
    assert members.mutation_ceiling.request_id == "request-café"
    assert members.authority_revocation.ceiling == members.mutation_ceiling
    _refuses_credentials(_with_ceiling_request(value, CREDENTIAL_URI))


def _with_visible_envelope(value, envelope):
    rows = json.loads(value.redacted_rows_json)
    rows[0]["visible_event_json"] = canonical_json(envelope)
    encoded_rows = canonical_json(rows)
    assert value.revoked_rows_are_original
    return value.model_copy(update={"redacted_rows_json": encoded_rows, "revoked_rows_json": encoded_rows})


def _with_actual_visible_event(value, *, request_id="request-café", missing_received=False):
    members = prepared_p4_members(value)
    row = members.redacted_rows[0]
    event = row.visible_event
    assert event is not None
    payload = json.loads(event.payload_json)
    function = members.redacted_calls[0]["function"]
    produced = VisibleToolEvent.capture(
        context=SessionOperationContext(
            SessionOperationFence(value.session_id, value.creator_operation_id, "fixture-live-lease", value.creator_operation_epoch),
            SessionOperationKind.COMPOSE,
        ),
        request_id=request_id,
        tool_call_id=row.tool_call_id,
        tool_name=function["name"],
        received_arguments=None if missing_received else payload["received_arguments"],
        effective_arguments=payload["effective_arguments"],
        selected_result=payload["selected_result"],
        display_arguments=function["arguments"],
        display_result=row.content,
    )
    return _with_visible_envelope(value, produced.to_envelope())


@pytest.mark.parametrize("request_id", [None, "request-café"])
def test_visible_producer_request_metadata_is_preserved_and_credential_checked(request_id):
    value, *_ = prepared_input(graph=False)
    clean = _with_actual_visible_event(value, request_id=request_id)
    members = _roundtrip(clean)
    assert members.redacted_rows[0].visible_event.producer_declared_request_id == request_id
    # The actual producer accepts correlation metadata without credential admission.
    bad = _with_actual_visible_event(value, request_id=CREDENTIAL_URI)
    _refuses_credentials(bad)


def test_visible_availability_metadata_is_checked_with_an_actual_gap_and_matching_body_hash():
    value, *_ = prepared_input(graph=False)
    clean = _with_actual_visible_event(value, missing_received=True)
    members = _roundtrip(clean)
    event = members.redacted_rows[0].visible_event
    assert event is not None
    payload = json.loads(event.payload_json)
    assert payload["received_arguments"] is None
    assert payload["availability_gaps"] == ["received_arguments_unavailable"]
    envelope = event.to_envelope()
    payload["availability_gaps"] = [CREDENTIAL_URI]
    envelope["payload_json"] = canonical_json(payload)
    envelope["payload_hash"] = sha256(envelope["payload_json"].encode("utf-8")).hexdigest()
    # Historical body/type/hash admission succeeds; this is not stale-hash evidence.
    admitted = VisibleToolEvent.from_envelope(envelope)
    admitted.validate_recorded_link(
        session_id=clean.session_id,
        operation_id=clean.creator_operation_id,
        tool_call_id=members.redacted_rows[0].tool_call_id,
        tool_name=members.redacted_calls[0]["function"]["name"],
        display_arguments=members.redacted_calls[0]["function"]["arguments"],
        display_result=members.redacted_rows[0].content,
    )
    _refuses_credentials(_with_visible_envelope(clean, admitted.to_envelope()))


@pytest.mark.parametrize("cohort", ["private_tool_evidence_json", "private_tool_failures_json"])
@pytest.mark.parametrize("member", ["session_id", "operation_id", "tool_call_id", "tool_name", "status", "evidence_id"])
def test_actual_private_owner_already_refuses_credential_correlation_metadata(cohort, member):
    value, *_ = prepared_input(graph=False)
    _roundtrip(value)
    envelopes = json.loads(value.model_dump()[cohort])
    original = envelopes[0]
    assert (
        ComposerDiagnosticEvidence.from_envelope(
            original,
            expected_session_id=value.session_id,
            expected_operation_id=value.creator_operation_id,
        ).to_envelope()
        == original
    )
    original["evidence"][member] = CREDENTIAL_URI
    if member in {"session_id", "operation_id", "tool_call_id", "evidence_id"}:
        original[member] = CREDENTIAL_URI
    original["evidence_sha256"] = sha256(canonical_json(original["evidence"]).encode("utf-8")).hexdigest()
    with pytest.raises(AuditIntegrityError, match="correlation metadata"):
        ComposerDiagnosticEvidence.from_envelope(
            original,
            expected_session_id=value.session_id,
            expected_operation_id=value.creator_operation_id,
        )
    with pytest.raises(AuditIntegrityError, match="correlation metadata"):
        encode_prepared_p4(value.model_copy(update={cohort: canonical_json(envelopes)}))


@pytest.mark.parametrize("cohort", ["private_tool_evidence_json", "private_tool_failures_json"])
def test_actual_private_owner_closes_diagnostic_reason_metadata(cohort):
    value, *_ = prepared_input(graph=False)
    _roundtrip(value)
    envelopes = json.loads(value.model_dump()[cohort])
    envelopes[0]["evidence"]["result"]["reasons"] = [CREDENTIAL_URI]
    envelopes[0]["evidence_sha256"] = sha256(canonical_json(envelopes[0]["evidence"]).encode("utf-8")).hexdigest()
    with pytest.raises(AuditIntegrityError, match="reasons are invalid"):
        encode_prepared_p4(value.model_copy(update={cohort: canonical_json(envelopes)}))


def test_withheld_origin_uses_its_closed_owner_and_cannot_retain_arbitrary_metadata():
    value, *_ = prepared_input(graph=False)
    members = _roundtrip(value)
    assert members.withheld_replies[0].origin == "planner_prose_unadmitted"
    replies = json.loads(value.withheld_replies_json)
    replies[0]["origin"] = CREDENTIAL_URI
    bad = value.model_copy(update={"withheld_replies_json": canonical_json(replies)})
    with pytest.raises(AuditIntegrityError, match="violates its owned contract"):
        encode_prepared_p4(bad)
    with pytest.raises(AuditIntegrityError, match="violates its owned contract"):
        decode_prepared_p4(canonical_json(bad.model_dump(mode="json", warnings="error")))


@pytest.mark.parametrize("member", ["actor", "error_class", "error_message"])
def test_existing_source_invocation_metadata_scan_remains_required(member):
    value, *_ = prepared_input(graph=False)
    audit = json.loads(value.source_audit_json)
    audit["invocations"][1][member] = "ordinary-diagnostic-café"
    clean = value.model_copy(update={"source_audit_json": canonical_json(audit)})
    _roundtrip(clean)
    audit["invocations"][1][member] = CREDENTIAL_URI
    _refuses_credentials(value.model_copy(update={"source_audit_json": canonical_json(audit)}))


def test_filtered_failure_evidence_does_not_admit_credential_bearing_original_diagnostic_metadata():
    value, *_ = prepared_input(graph=False)
    members = _roundtrip(value)
    invocation = members.invocations[1]
    assert invocation.private_failure is not None
    facts = json.loads(invocation.private_failure.canonical)
    facts["error_class"] = CREDENTIAL_URI
    encoded_facts = canonical_json(facts)
    filtered = build_private_tool_evidence(
        session_id=value.session_id,
        operation_id=value.creator_operation_id,
        operation_epoch=value.creator_operation_epoch,
        tool_call_id=invocation.tool_call_id,
        tool_name="composer_failure",
        status="tool_failure",
        arguments_canonical=None,
        result_canonical=encoded_facts,
        evidence_id=diagnostic_evidence_identity("tool-failure", invocation.tool_call_id),
    )
    assert filtered.result.disposition == "filtered"
    assert (
        ComposerDiagnosticEvidence.from_envelope(
            filtered.to_envelope(),
            expected_session_id=value.session_id,
            expected_operation_id=value.creator_operation_id,
        )
        == filtered
    )
    audit = json.loads(value.source_audit_json)
    audit["invocations"][1]["private_failure"]["canonical"] = encoded_facts
    bad = value.model_copy(
        update={
            "source_audit_json": canonical_json(audit),
            "private_tool_failures_json": canonical_json([filtered.to_envelope()]),
        }
    )
    _refuses_credentials(bad)
