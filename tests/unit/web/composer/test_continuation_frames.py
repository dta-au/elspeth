import json
from uuid import UUID

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import (
    ContinuationFrameStack,
    ContinuationProviderCostPolicy,
    EffectFrontierEntry,
    InitializeFrame,
    LogicalRequestBinding,
    MutationCeilingSnapshot,
    PrivateChunkRef,
    prepare_composer_continuation_admission,
)
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot,
    encode_continuation_snapshot,
)
from tests.helpers.continuation_policy import minimal_continuation_policy


def initial_stack():
    policy = minimal_continuation_policy()
    return ContinuationFrameStack(
        logical=LogicalRequestBinding(
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000002",
            original_user_message_id="original",
            request_hash="a" * 64,
            kind="compose_message",
            base_state_id=None,
        ),
        mutation_ceiling=MutationCeilingSnapshot(
            session_id="00000000-0000-4000-8000-000000000001",
            request_id="original",
            initial_trust_mode="auto_commit",
        ),
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        cursor=0,
        frames=(
            InitializeFrame(
                request_json='{"content":"café"}',
                ingress_phase="ready",
                title_attempt_id=None,
                bootstrap=PrivateChunkRef(codec="composer.bootstrap.v1", digest="b" * 64, byte_length=2),
            ),
        ),
        effect_frontier=(),
        pending_settlement_ids=(),
        semantic_frontier=None,
    )


def test_roundtrip_preserves_bytes_order_and_private_phase():
    original = initial_stack()
    encoded = encode_continuation_snapshot(original)
    assert decode_continuation_snapshot(encoded) == original
    assert encode_continuation_snapshot(decode_continuation_snapshot(encoded)) == encoded


def test_unknown_dispatch_is_not_a_handoff_boundary():
    stack = initial_stack()
    assert stack.is_handoff_safe
    unknown = stack.model_copy(
        update={"effect_frontier": (EffectFrontierEntry(effect_id="provider:1", disposition="unknown", receipt_id=None),)}
    )
    assert not unknown.is_handoff_safe
    assert not stack.model_copy(update={"pending_settlement_ids": ("attempt:1",)}).is_handoff_safe


@pytest.mark.parametrize("field,value", [("schema_version", "future"), ("cursor", True), ("extra", "ignored")])
def test_unknown_or_mistyped_storage_is_integrity_failure(field, value):
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    data[field] = value
    with pytest.raises(AuditIntegrityError):
        decode_continuation_snapshot(json.dumps(data))


def test_reserved_id_and_initializer_are_bound_before_ingress():
    sid = UUID("00000000-0000-4000-8000-000000000001")
    operation_id = "00000000-0000-4000-8000-000000000002"
    policy = minimal_continuation_policy()
    kwargs = {
        "session_id": sid,
        "operation_id": operation_id,
        "kind": "compose_message",
        "request_json": '{"content":"Build"}',
        "request_hash": "a" * 64,
        "base_state_id": None,
        "policy_hash": policy.policy_hash,
        "admission_policy_json": policy.model_dump_json(),
        "initial_action_json": '{"kind":"ingress"}',
        "initial_trust_mode": "auto_commit",
    }
    first = prepare_composer_continuation_admission(**kwargs)
    assert first == prepare_composer_continuation_admission(**kwargs)
    first.validate_binding(
        session_id=sid,
        operation_id=operation_id,
        kind="compose_message",
        request_json=kwargs["request_json"],
        request_hash="a" * 64,
        base_state_id=None,
        reserved_user_message_id=first.reserved_user_message_id,
    )
    with pytest.raises(AuditIntegrityError):
        first.validate_binding(
            session_id=sid,
            operation_id=operation_id,
            kind="compose_message",
            request_json='{"content":"changed"}',
            request_hash="a" * 64,
            base_state_id=None,
            reserved_user_message_id=first.reserved_user_message_id,
        )


def test_v2_provider_policy_is_explicit_and_survives_cold_codec():
    original = initial_stack()
    restored = decode_continuation_snapshot(encode_continuation_snapshot(original))
    assert restored.provider_cost_policy.applicability == "not_applicable"
    assert restored.provider_cost_policy.lifecycle_version == 2
    assert restored.provider_cost_policy.accounting_owner == "physical_audit_and_token_ledger"
    assert restored.provider_cost_policy == original.provider_cost_policy


@pytest.mark.parametrize("mutation", ["missing", "legacy", "lifecycle", "invented_total"])
def test_v2_policy_codec_refuses_scope_substitution(mutation):
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    if mutation == "missing":
        del data["provider_cost_policy"]
    elif mutation == "legacy":
        data["provider_cost_policy"]["applicability"] = "legacy_empty_v1"
    elif mutation == "lifecycle":
        data["provider_cost_policy"]["lifecycle_version"] = 1
    else:
        data["provider_cost_policy"]["total_cost"] = "0"
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_continuation_snapshot(json.dumps(data))


@pytest.mark.parametrize("mutation", ["missing", "session", "request", "mode"])
def test_ceiling_codec_requires_original_identity_and_explicit_mode(mutation):
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    if mutation == "missing":
        del data["mutation_ceiling"]
    elif mutation == "session":
        data["mutation_ceiling"]["session_id"] = "another-session"
    elif mutation == "request":
        data["mutation_ceiling"]["request_id"] = "another-message"
    else:
        data["mutation_ceiling"]["initial_trust_mode"] = "default-auto"
    with pytest.raises(AuditIntegrityError):
        decode_continuation_snapshot(json.dumps(data))


def test_ceiling_and_revocation_capture_require_nominal_values_and_roundtrip():
    from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
    from elspeth.web.composer.continuation import MutationRevocationSnapshot

    original = ComposerMutationCeiling(initial_stack().logical.session_id, "original", "explicit_approve")
    snapshot = MutationCeilingSnapshot.capture(original)
    assert snapshot.materialize() == original
    snapshot.require_logical(initial_stack().logical)
    assert not snapshot.materialize().permits_auto("auto_commit")
    revoked = ComposerMutationRevocation(original, "auto_commit")
    assert MutationRevocationSnapshot.capture(revoked).materialize() == revoked
    with pytest.raises(TypeError, match="owned mutation ceiling"):
        MutationCeilingSnapshot.capture(object())
    with pytest.raises(TypeError, match="actual nominal value"):
        MutationRevocationSnapshot.capture(object())


def test_original_explicit_ceiling_survives_actual_cold_json():
    original = initial_stack()
    original = original.model_copy(
        update={"mutation_ceiling": original.mutation_ceiling.model_copy(update={"initial_trust_mode": "explicit_approve"})}
    )
    restored = decode_continuation_snapshot(encode_continuation_snapshot(original))
    assert restored.mutation_ceiling.initial_trust_mode == "explicit_approve"
    assert not restored.mutation_ceiling.materialize().permits_auto("auto_commit")


def test_prepared_initializer_requires_its_pre_title_bootstrap():
    from pydantic import ValidationError

    for phase in ("title_pending", "ready"):
        with pytest.raises(ValidationError, match="pre-title bootstrap"):
            InitializeFrame(request_json="{}", ingress_phase=phase, title_attempt_id=None)


def test_duplicate_effects_and_uncommitted_pending_results_refuse_at_codec():
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    effect = {"effect_id": "effect", "disposition": "prepared", "receipt_id": None}
    data["effect_frontier"] = [effect, effect]
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_continuation_snapshot(json.dumps(data))
    data["effect_frontier"] = [effect]
    data["pending_owner_results"] = [
        {"effect_key": "effect", "kind": "read", "result": {"codec": "composer.owner-result.v1", "digest": "a" * 64, "byte_length": 2}}
    ]
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_continuation_snapshot(json.dumps(data))
    effect.update(disposition="committed", receipt_id="actual-receipt")
    restored = decode_continuation_snapshot(json.dumps(data))
    assert not restored.is_handoff_safe


def test_active_action_requires_paired_actual_source_and_strict_owned_action():
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    data["active_action_json"] = '{"kind":"invented"}'
    with pytest.raises(AuditIntegrityError):
        decode_continuation_snapshot(json.dumps(data))
    data["active_action_source"] = {
        "effect_key": "provider:1:0",
        "input_sha256": "a" * 64,
        "response": {"codec": "composer.response.v1", "digest": "b" * 64, "byte_length": 2},
    }
    with pytest.raises(AuditIntegrityError, match="prescribed continuation action"):
        decode_continuation_snapshot(json.dumps(data))


def test_snapshot_refuses_duplicate_json_keys_before_owned_decoder():
    encoded = encode_continuation_snapshot(initial_stack())
    duplicate = encoded[:-1] + ',"cursor":0}'
    with pytest.raises(AuditIntegrityError, match="strict JSON"):
        decode_continuation_snapshot(duplicate)


def test_snapshot_encoder_requires_exact_owned_stack_and_bound_policy():
    with pytest.raises(TypeError, match="owned frame stack"):
        encode_continuation_snapshot(object())
    stack = initial_stack().model_copy(update={"policy_hash": "wrong"})
    with pytest.raises(AuditIntegrityError):
        encode_continuation_snapshot(stack)


def test_seed_rejects_original_identity_replacement_in_actual_json():
    from dataclasses import replace

    policy = minimal_continuation_policy()
    sid = UUID("00000000-0000-4000-8000-000000000001")
    operation_id = "00000000-0000-4000-8000-000000000002"
    seed = prepare_composer_continuation_admission(
        session_id=sid,
        operation_id=operation_id,
        kind="compose_message",
        request_json="{}",
        request_hash="a" * 64,
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json="{}",
        initial_trust_mode="auto_commit",
    )
    data = json.loads(seed.snapshot_json)
    data["logical"]["original_user_message_id"] = "replacement"
    data["mutation_ceiling"]["request_id"] = "replacement"
    forged = replace(seed, snapshot_json=json.dumps(data))
    with pytest.raises(AuditIntegrityError, match="differs from admission"):
        forged.validate_binding(
            session_id=sid,
            operation_id=operation_id,
            kind="compose_message",
            request_json="{}",
            request_hash="a" * 64,
            base_state_id=None,
            reserved_user_message_id=seed.reserved_user_message_id,
        )


def test_recompose_reuses_original_message_and_required_ceiling_before_any_action():
    policy = minimal_continuation_policy()
    kwargs = {
        "session_id": UUID("00000000-0000-4000-8000-000000000001"),
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "kind": "compose_recompose",
        "request_json": "{}",
        "request_hash": "a" * 64,
        "base_state_id": None,
        "policy_hash": policy.policy_hash,
        "admission_policy_json": policy.model_dump_json(),
        "initial_action_json": "{}",
        "initial_trust_mode": "explicit_approve",
    }
    with pytest.raises(AuditIntegrityError, match="verified original user ingress"):
        prepare_composer_continuation_admission(**kwargs)
    seed = prepare_composer_continuation_admission(**kwargs, original_user_message_id="original-human")
    stack = decode_continuation_snapshot(seed.snapshot_json)
    assert stack.logical.original_user_message_id == "original-human"
    assert stack.mutation_ceiling.request_id == "original-human"
    assert stack.mutation_ceiling.initial_trust_mode == "explicit_approve"
    assert stack.frames[0].ingress_phase == "reserved"


@pytest.mark.parametrize("entrypoint", ("prepare", "validate"))
@pytest.mark.parametrize("violation", ("credential_tail", "byte_bound"))
def test_seed_entrypoints_admit_complete_original_before_snapshot_work(monkeypatch, entrypoint, violation):
    from dataclasses import replace

    import elspeth.web.composer.continuation_codec as codec_module
    from elspeth.web.credential_guard import CredentialMaterialRefused
    from elspeth.web.sessions.composer_operations import (
        COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH,
        composer_operation_v2_request_hash,
    )
    from elspeth.web.sessions.schemas import SendMessageRequest

    sid = UUID("00000000-0000-4000-8000-000000000001")
    operation_id = "00000000-0000-4000-8000-000000000002"
    policy = minimal_continuation_policy()
    clean_request = SendMessageRequest(operation_id=operation_id, content="café" * 15_000)
    clean_json = clean_request.model_dump_json()
    clean_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=clean_request)
    assert 65_536 < len(clean_json.encode("utf-8")) < COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH
    kwargs = {
        "session_id": sid,
        "operation_id": operation_id,
        "kind": "compose_message",
        "request_json": clean_json,
        "request_hash": clean_hash,
        "base_state_id": None,
        "policy_hash": policy.policy_hash,
        "admission_policy_json": policy.model_dump_json(),
        "initial_action_json": policy.actions.session_sql.model_dump_json(),
        "initial_trust_mode": "explicit_approve",
    }
    clean_seed = prepare_composer_continuation_admission(**kwargs)
    before = clean_seed.snapshot_json
    clean_binding = {
        "session_id": sid,
        "operation_id": operation_id,
        "kind": "compose_message",
        "request_json": clean_json,
        "request_hash": clean_hash,
        "base_state_id": None,
        "reserved_user_message_id": clean_seed.reserved_user_message_id,
    }
    clean_seed.validate_binding(**clean_binding)
    actual = decode_continuation_snapshot(before)
    assert actual.logical.request_hash == clean_hash
    assert actual.mutation_ceiling.initial_trust_mode == "explicit_approve"
    assert actual.provider_cost_policy.applicability == "not_applicable"
    assert actual.frames[0].request_json == clean_json
    assert actual.frames[0].ingress_phase == "reserved"
    assert clean_seed.action_json == policy.actions.session_sql.model_dump_json()

    if violation == "credential_tail":
        # Deliberately fake credential assignment, beyond the long valid DTO prefix.
        bad_request = SendMessageRequest(
            operation_id=operation_id,
            content=clean_request.content + "\npassword=example-value",  # secret-scan: allow-this-line
        )
        bad_json = bad_request.model_dump_json()
        bad_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=bad_request)
        assert bad_json.encode("utf-8").index(b"password=") > 65_536
        expected_error = CredentialMaterialRefused
        expected_message = None
    else:
        # A valid JSON representation of the same DTO, exceeding the exact raw UTF-8 bound.
        bad_json = clean_json + " " * (COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH - len(clean_json.encode("utf-8")) + 1)
        bad_hash = clean_hash
        assert json.loads(bad_json) == json.loads(clean_json)
        assert len(bad_json.encode("utf-8")) == COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH + 1
        expected_error = AuditIntegrityError
        expected_message = "request exceeds its admitted byte bound"

    # A controlled replacement of a real clean seed, not a new admission authority.
    # Keep every structural binding valid so omitting the validation call can reach acceptance.
    retained = json.loads(before)
    retained["frames"][0]["request_json"] = bad_json
    retained["logical"]["request_hash"] = bad_hash
    forged_seed = replace(clean_seed, snapshot_json=json.dumps(retained, ensure_ascii=False))
    shaped = decode_continuation_snapshot(forged_seed.snapshot_json)
    assert shaped.frames[0].request_json == bad_json and shaped.logical.request_hash == bad_hash
    bad_binding = clean_binding | {"request_json": bad_json, "request_hash": bad_hash}
    entered = []

    with monkeypatch.context() as observation:
        if entrypoint == "prepare":
            actual_encode = codec_module.encode_continuation_snapshot

            def observe_encode(stack):
                entered.append("snapshot")
                return actual_encode(stack)

            observation.setattr(codec_module, "encode_continuation_snapshot", observe_encode)
            assert prepare_composer_continuation_admission(**kwargs) == clean_seed
        else:
            actual_decode = codec_module.decode_continuation_snapshot

            def observe_decode(encoded):
                entered.append("snapshot")
                return actual_decode(encoded)

            observation.setattr(codec_module, "decode_continuation_snapshot", observe_decode)
            clean_seed.validate_binding(**clean_binding)
        assert entered == ["snapshot"]
        entered.clear()
        with pytest.raises(expected_error, match=expected_message):
            if entrypoint == "prepare":
                prepare_composer_continuation_admission(**(kwargs | {"request_json": bad_json, "request_hash": bad_hash}))
            else:
                forged_seed.validate_binding(**bad_binding)
        assert entered == []
        assert clean_seed.snapshot_json == before
        if entrypoint == "prepare":
            assert prepare_composer_continuation_admission(**kwargs) == clean_seed
        else:
            clean_seed.validate_binding(**clean_binding)
        assert entered == ["snapshot"]
