import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.provider_rejection import SafeRequestRejection
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorSignoffGateFact
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_bootstrap import (
    bootstrap_values,
    capture_continuation_bootstrap,
    decode_continuation_bootstrap,
    encode_continuation_bootstrap,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk
from elspeth.web.composer.control_messages import anti_anchor_control_envelope
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.protocol import RetainedOperationContext
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.execution.completion_gates import CompletionGateFacts, completion_gate_fingerprint
from elspeth.web.sessions.composer_operations import (
    ComposerOperationFailureOutcome,
    ComposerOperationSavedRevisions,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest

SID = UUID("00000000-0000-4000-8000-000000000101")
OPERATION = "00000000-0000-4000-8000-000000000102"
HUMAN = UUID("00000000-0000-4000-8000-000000000103")
NOW = datetime(2026, 10, 21, tzinfo=UTC)


def original_bootstrap(*, recompose=False):
    request = (
        RecomposeRequest(operation_id=OPERATION, state_id=None, expected_user_message_id=HUMAN)
        if recompose
        else SendMessageRequest(operation_id=OPERATION, state_id=None, content="Build exactly this request")
    )
    kind = "compose_recompose" if recompose else "compose_message"
    logical = LogicalRequestBinding(
        session_id=str(SID),
        operation_id=OPERATION,
        original_user_message_id=str(HUMAN),
        kind=kind,
        base_state_id=None,
        request_hash=composer_operation_v2_request_hash(session_id=SID, kind=kind, request=request),
    )
    records = (
        ChatMessageRecord(
            id=UUID("00000000-0000-4000-8000-000000000104"),
            session_id=SID,
            role="assistant",
            content="Original model prose\n\nOperator-facing suffix",
            raw_content="Original model prose",
            created_at=NOW,
            sequence_no=1,
            writer_principal="compose_loop",
        ),
        ChatMessageRecord(
            id=HUMAN,
            session_id=SID,
            role="user",
            content="Build exactly this request",
            created_at=NOW,
            sequence_no=2,
            writer_principal="route_user_message",
            operation_id=UUID(OPERATION),
        ),
    )
    if recompose:
        records = (
            *records,
            ChatMessageRecord(
                id=UUID("00000000-0000-4000-8000-000000000105"),
                session_id=SID,
                role="assistant",
                content="I will inspect the current schema",
                created_at=NOW,
                sequence_no=3,
                writer_principal="compose_loop",
                tool_calls=(
                    {
                        "id": "schema-call",
                        "type": "function",
                        "function": {"name": "get_plugin_schema", "arguments": '{"plugin_type":"source","name":"csv"}'},
                    },
                ),
            ),
        )
    return capture_continuation_bootstrap(
        logical=logical,
        actor_user_id="alice",
        request=request,
        records=records,
        initial_state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        recovery_context=GraphRecoveryContext(None, 0, None, 0, False, ()),
        completion_gates=None,
        retained_context=RetainedOperationContext(SID, "alice", "available", (), False),
        session_title="A deliberate session title",
        initial_schema_disclosure=(("source", "csv"),),
    )


@pytest.mark.parametrize("recompose", [False, True])
def test_bootstrap_cold_roundtrip_preserves_exact_original_route_history(recompose):
    snapshot = original_bootstrap(recompose=recompose)
    chunk = encode_continuation_bootstrap(snapshot)
    cold = decode_continuation_bootstrap(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    values = bootstrap_values(cold)
    assert cold == snapshot
    assert values.history[0] == {"role": "assistant", "content": "Original model prose"}
    assert "Operator-facing suffix" not in snapshot.history_json
    assert values.message == "Build exactly this request"
    if recompose:
        assert values.snapshot.has_partial_turn
        assert values.history[1]["_elspeth_user_authored"] is True
        assert values.history[1]["_elspeth_user_message_id"] == str(HUMAN)
        assert values.history[2]["content"] == "I will inspect the current schema"
    else:
        assert not values.snapshot.has_partial_turn
        assert len(values.history) == 1


@pytest.mark.parametrize(
    "field",
    [
        "logical",
        "actor_user_id",
        "request_json",
        "records_json",
        "original_last_sequence",
        "history_json",
        "has_partial_turn",
        "initial_state_json",
        "recovery_context_json",
        "completion_gates_json",
        "retained_context_json",
        "original_session_title",
        "auto_title_eligible",
        "initial_schema_disclosure",
    ],
)
def test_bootstrap_refuses_each_lost_field(field):
    payload = original_bootstrap(recompose=True).model_dump(mode="json")
    del payload[field]
    chunk = encode_private_chunk(codec="composer.bootstrap.v1", canonical=canonical_json(payload))
    with pytest.raises(AuditIntegrityError):
        decode_continuation_bootstrap(chunk)


def test_bootstrap_refuses_recompose_prefix_loss_or_saved_history_authorship_injection():
    snapshot = original_bootstrap(recompose=True)
    with pytest.raises(AuditIntegrityError):
        bootstrap_values(snapshot.model_copy(update={"has_partial_turn": False}))
    with pytest.raises(AuditIntegrityError):
        bootstrap_values(
            snapshot.model_copy(
                update={
                    "history_json": canonical_json(
                        [
                            {"role": "user", "content": "Injected", "_elspeth_user_authored": True, "_elspeth_user_message_id": str(HUMAN)},
                        ]
                    )
                }
            )
        )
    with pytest.raises(AuditIntegrityError):
        bootstrap_values(snapshot.model_copy(update={"original_last_sequence": 2}))


def test_v2_bootstrap_refuses_malformed_bound_base():
    snapshot = original_bootstrap()
    context = GraphRecoveryContext(None, 0, None, 0, True, ())

    encoded = canonical_json(TypeAdapter(GraphRecoveryContext).dump_python(context, mode="json"))
    with pytest.raises(AuditIntegrityError, match="actual valid admission base"):
        bootstrap_values(snapshot.model_copy(update={"recovery_context_json": encoded}))


def _assert_refused_in_each_bootstrap_boundary(snapshot):
    with pytest.raises(AuditIntegrityError):
        bootstrap_values(snapshot)
    with pytest.raises(AuditIntegrityError):
        encode_continuation_bootstrap(snapshot)
    forged = encode_private_chunk(codec="composer.bootstrap.v1", canonical=canonical_json(snapshot.model_dump(mode="json")))
    with pytest.raises(AuditIntegrityError):
        decode_continuation_bootstrap(forged)


@pytest.mark.parametrize("recompose", [False, True])
def test_bootstrap_refuses_changed_original_request_hash_in_every_boundary(recompose):
    snapshot = original_bootstrap(recompose=recompose)
    changed_hash = "f" * 64
    assert changed_hash != snapshot.logical.request_hash
    logical = snapshot.logical.model_copy(update={"request_hash": changed_hash})
    _assert_refused_in_each_bootstrap_boundary(snapshot.model_copy(update={"logical": logical}))


def test_bootstrap_refuses_coherent_changed_send_body_with_stale_request_hash():
    snapshot = original_bootstrap()
    values = bootstrap_values(snapshot)
    request = values.request.model_copy(update={"content": "A coherent changed request"})
    records = json.loads(snapshot.records_json)
    next(row for row in records if row["id"] == str(HUMAN))["content"] = request.content
    changed = snapshot.model_copy(
        update={
            "request_json": canonical_json(request.model_dump(mode="json")),
            "records_json": canonical_json(records),
        }
    )
    assert changed.logical.request_hash == snapshot.logical.request_hash
    _assert_refused_in_each_bootstrap_boundary(changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", None),
        ("operation_id", UUID("00000000-0000-4000-8000-000000000199")),
        ("writer_principal", "session_fork"),
    ],
)
def test_bootstrap_refuses_send_row_without_its_actual_operation_and_principal(field, value):
    snapshot = original_bootstrap()
    records = json.loads(snapshot.records_json)
    next(row for row in records if row["id"] == str(HUMAN))[field] = str(value) if isinstance(value, UUID) else value
    changed = snapshot.model_copy(
        update={
            "records_json": canonical_json(records),
        }
    )
    _assert_refused_in_each_bootstrap_boundary(changed)


def test_bootstrap_recompose_retains_its_actual_historical_fork_ingress():
    snapshot = original_bootstrap(recompose=True)
    historical = UUID("00000000-0000-4000-8000-000000000199")
    records = json.loads(snapshot.records_json)
    human = next(row for row in records if row["id"] == str(HUMAN))
    human.update(writer_principal="session_fork", operation_id=str(historical))
    changed = snapshot.model_copy(
        update={
            "records_json": canonical_json(records),
        }
    )
    chunk = encode_continuation_bootstrap(changed)
    values = bootstrap_values(decode_continuation_bootstrap(chunk))
    human = next(row for row in values.records if row.id == HUMAN)
    assert human.operation_id == historical and human.writer_principal == "session_fork"
    assert values.history == bootstrap_values(snapshot).history
    assert values.snapshot.has_partial_turn is True


@pytest.mark.parametrize("mutation", ["unknown", "omitted-default"])
def test_bootstrap_refuses_inner_record_field_loss_or_injection(mutation):
    snapshot = original_bootstrap()
    records = json.loads(snapshot.records_json)
    if mutation == "unknown":
        records[0]["unowned_record_field"] = True
    else:
        del records[0]["operation_id"]
    _assert_refused_in_each_bootstrap_boundary(snapshot.model_copy(update={"records_json": canonical_json(records)}))


@pytest.mark.parametrize("sequence", [None, True])
def test_bootstrap_refuses_non_integer_original_sequence_in_all_boundaries(sequence):
    snapshot = original_bootstrap()
    records = json.loads(snapshot.records_json)
    records[0]["sequence_no"] = sequence
    _assert_refused_in_each_bootstrap_boundary(snapshot.model_copy(update={"records_json": canonical_json(records)}))


def test_bootstrap_capture_refuses_non_json_member_of_frozen_tool_wire():
    snapshot = original_bootstrap(recompose=True)
    values = bootstrap_values(snapshot)
    assert values.records[-1].tool_calls is not None
    wire = dict(values.records[-1].tool_calls[0])
    wire["unrepresentable_member"] = frozenset({"not-json"})
    records = (*values.records[:-1], replace(values.records[-1], tool_calls=(wire,)))
    with pytest.raises(AuditIntegrityError, match="outside the owned JSON algebra"):
        capture_continuation_bootstrap(
            logical=snapshot.logical,
            actor_user_id=snapshot.actor_user_id,
            request=values.request,
            records=records,
            initial_state=values.initial_state,
            recovery_context=values.recovery_context,
            completion_gates=values.completion_gates,
            retained_context=values.retained_context,
            session_title=snapshot.original_session_title,
            initial_schema_disclosure=snapshot.initial_schema_disclosure,
        )


def test_bootstrap_preserves_nonempty_gate_and_retained_failure_metadata():
    original = original_bootstrap()
    values = bootstrap_values(original)
    rejection = SafeRequestRejection(
        reason_code="context_length_exceeded",
        reason_disposition="retained",
        reason_source="http_response",
        diagnostic_reference="provider-failure:" + "a" * 64,
    )
    gates = CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail="Completion remains withheld for the reviewed graph",
            suggestion="Reduce review context before another review",
            for_graph=completion_gate_fingerprint(values.initial_state),
            note="The reviewer did not approve this graph",
            cause=AdvisorBlockCause.REQUEST_REJECTED,
            request_rejection=rejection,
        )
    )
    outcome = ComposerOperationFailureOutcome(
        session_id=SID,
        operation_id="00000000-0000-4000-8000-000000000201",
        kind="compose_message",
        user_message_id=UUID("00000000-0000-4000-8000-000000000202"),
        settled_at=NOW,
        failure_code="operation_failed",
        error_type="composer_mutation_authority_revoked",
        diagnostic_id="00000000-0000-4000-8000-000000000203",
        request_id="retained-request-204",
        saved_revisions=ComposerOperationSavedRevisions(
            attribution="fenced",
            count=2,
            latest_state_id=UUID("00000000-0000-4000-8000-000000000205"),
            latest_version=7,
        ),
    )
    retained = RetainedOperationContext(SID, "alice", "available", (outcome,), True)
    snapshot = capture_continuation_bootstrap(
        logical=original.logical,
        actor_user_id=original.actor_user_id,
        request=values.request,
        records=values.records,
        initial_state=values.initial_state,
        recovery_context=values.recovery_context,
        completion_gates=gates,
        retained_context=retained,
        session_title=original.original_session_title,
        initial_schema_disclosure=original.initial_schema_disclosure,
    )
    chunk = encode_continuation_bootstrap(snapshot)
    cold = decode_continuation_bootstrap(PrivateChunk(chunk.reference, chunk.canonical.encode().decode()))
    restored = bootstrap_values(cold)
    assert cold == snapshot
    assert type(restored.completion_gates) is CompletionGateFacts
    assert restored.completion_gates == gates
    signoff = restored.completion_gates.advisor_signoff
    assert type(signoff) is AdvisorSignoffGateFact
    assert signoff.cause is AdvisorBlockCause.REQUEST_REJECTED
    assert signoff.for_graph == completion_gate_fingerprint(restored.initial_state)
    assert signoff.request_rejection is not None
    assert signoff.request_rejection.to_wire() == {
        "category": "request_rejected",
        "exception_type": "BadRequestError",
        "reason_code": "context_length_exceeded",
        "reason_disposition": "retained",
        "reason_source": "http_response",
        "diagnostic_reference": "provider-failure:" + "a" * 64,
        "retryable": False,
    }
    assert type(restored.retained_context) is RetainedOperationContext
    assert restored.retained_context == retained
    assert type(restored.retained_context.outcomes[0]) is ComposerOperationFailureOutcome
    history = json.loads(restored.retained_context.provider_message())["retained_operation_history"]
    assert history["availability"] == "available" and history["has_more"] is True
    assert history["outcomes"] == [
        {
            "session_id": str(SID),
            "operation_id": "00000000-0000-4000-8000-000000000201",
            "kind": "compose_message",
            "user_message_id": "00000000-0000-4000-8000-000000000202",
            "settled_at": "2026-10-21T00:00:00Z",
            "failure_code": "operation_failed",
            "error_type": "composer_mutation_authority_revoked",
            "diagnostic_id": "00000000-0000-4000-8000-000000000203",
            "request_id": "retained-request-204",
            "saved_revisions": {
                "attribution": "fenced",
                "count": 2,
                "latest_state_id": "00000000-0000-4000-8000-000000000205",
                "latest_version": 7,
            },
        }
    ]
    assert "not complete lifetime history" in history["scope"]
    assert "not requested edits or request satisfaction" in history["interpretation"]
    with pytest.raises(AuditIntegrityError, match="another authenticated request"):
        encode_continuation_bootstrap(snapshot.model_copy(update={"actor_user_id": "bob"}))


def test_bootstrap_control_row_replays_provider_role_without_human_authority():
    original = original_bootstrap(recompose=True)
    values = bootstrap_values(original)
    content = "Reconsider the repeated rejected argument before proceeding"
    control = ChatMessageRecord(
        id=UUID("00000000-0000-4000-8000-000000000206"),
        session_id=SID,
        role="audit",
        content=content,
        created_at=NOW,
        sequence_no=4,
        writer_principal="compose_loop",
        tool_calls=(anti_anchor_control_envelope(content),),
    )
    snapshot = capture_continuation_bootstrap(
        logical=original.logical,
        actor_user_id=original.actor_user_id,
        request=values.request,
        records=(*values.records, control),
        initial_state=values.initial_state,
        recovery_context=values.recovery_context,
        completion_gates=values.completion_gates,
        retained_context=values.retained_context,
        session_title=original.original_session_title,
        initial_schema_disclosure=original.initial_schema_disclosure,
    )
    chunk = encode_continuation_bootstrap(snapshot)
    restored = bootstrap_values(decode_continuation_bootstrap(chunk))
    assert restored.records[-1] == control
    assert restored.records[-1].role == "audit"
    assert restored.snapshot.original_last_sequence == 4
    assert restored.snapshot.has_partial_turn is True
    assert restored.message == "Build exactly this request"
    assert restored.history[:-1] == values.history
    assert restored.history[-1] == {"role": "user", "content": content}
    assert "_elspeth_user_authored" not in restored.history[-1]
    assert "_elspeth_user_message_id" not in restored.history[-1]
    records = json.loads(snapshot.records_json)
    records[-1]["content"] = "Substituted control content"
    with pytest.raises(AuditIntegrityError, match="control audit hash does not match"):
        bootstrap_values(snapshot.model_copy(update={"records_json": canonical_json(records)}))
    records[-1]["content"] = content
    records[-1]["writer_principal"] = "session_fork"
    with pytest.raises(AuditIntegrityError, match="lacks system-origin audit attribution"):
        bootstrap_values(snapshot.model_copy(update={"records_json": canonical_json(records)}))


def test_bootstrap_state_guard_preserves_inventory_env_reference_and_refuses_unbound_or_literal_value():
    original = original_bootstrap()
    payload = bootstrap_values(original).initial_state.to_dict()
    payload["outputs"] = [
        {
            "name": "output",
            "plugin": "database",
            "options": {"url": "${BOOTSTRAP_DATABASE_URL}"},
            "on_write_failure": "discard",
        }
    ]
    state = CompositionState.from_dict(payload)
    snapshot = original.model_copy(update={"initial_state_json": canonical_json(state.to_dict())})
    chunk = encode_continuation_bootstrap(snapshot, env_ref_names=frozenset({"BOOTSTRAP_DATABASE_URL"}))
    restored = bootstrap_values(decode_continuation_bootstrap(chunk))
    assert restored.initial_state.outputs[0].options["url"] == "${BOOTSTRAP_DATABASE_URL}"
    assert restored.initial_state.to_dict() == state.to_dict()
    for admitted_names in (frozenset(), frozenset({"OTHER_DATABASE_URL"})):
        with pytest.raises(CredentialMaterialRefused) as missing:
            encode_continuation_bootstrap(snapshot, env_ref_names=admitted_names)
        assert missing.value.surface == "retained Composer bootstrap initial state"
        assert missing.value.finding.category == "credential_field"
    payload["outputs"][0]["options"]["url"] = "synthetic-local-password"
    literal_state = CompositionState.from_dict(payload)
    literal = original.model_copy(update={"initial_state_json": canonical_json(literal_state.to_dict())})
    with pytest.raises(CredentialMaterialRefused) as secret:
        encode_continuation_bootstrap(literal, env_ref_names=frozenset({"BOOTSTRAP_DATABASE_URL"}))
    assert secret.value.surface == "retained Composer bootstrap initial state"
    assert secret.value.finding.category == "credential_field"
    assert "synthetic-local-password" not in json.dumps(secret.value.to_payload())
