"""Actual committed evidence metadata: identifier labels versus credential values."""

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_evidence import (
    RetainedHistoricalEffect,
    TurnEvidenceSnapshot,
    _require_evidence_metadata,
    admit_turn_evidence,
    decode_turn_evidence,
    encode_turn_evidence,
)
from elspeth.web.composer.continuation_evidence_application_data import build_portable_p4_application
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.composer.test_portable_evidence_application import portable_input
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


@pytest.mark.asyncio
async def test_actual_committed_effect_locator_is_metadata_not_a_credential_slot(operation_store):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        snapshot = bundle.evidence_after.snapshot
        assert len(snapshot.effect_locators) == 1
        actual = snapshot.effect_locators[0]
        assert actual.effect_key == values["result"].receipt.effect_key
        assert actual.effect_key.startswith("batch:") and actual.effect_key.endswith(":p4")
        assert actual.input_sha256 == values["result"].receipt.input_sha256
        assert actual.creator_operation_id == values["producer"].creator_operation_id
        assert actual.inserted_state_ids == values["result"].receipt.inserted_state_ids
        assert actual.authored_state_hash == values["result"].receipt.authored_state_hash
        assert decode_turn_evidence(encode_turn_evidence(bundle.evidence_after), env_ref_names=frozenset()).snapshot == snapshot
        _require_evidence_metadata(snapshot, env_ref_names=frozenset())
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("effect_key", "creator_operation_id", "receipt_id"))
async def test_owned_locator_values_still_reject_actual_credentials(operation_store, field):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        snapshot = bundle.evidence_after.snapshot
        assert decode_turn_evidence(encode_turn_evidence(bundle.evidence_after), env_ref_names=frozenset()).snapshot == snapshot
        _require_evidence_metadata(snapshot, env_ref_names=frozenset())
        locator_fields = snapshot.effect_locators[0].model_dump(mode="python")
        locator_fields[field] = "https://" + "name:synthetic-private-value@example.test/"
        locator = RetainedHistoricalEffect.model_validate(locator_fields)
        changed = TurnEvidenceSnapshot.model_validate({**snapshot.model_dump(mode="python"), "effect_locators": (locator,)})
        # Exercise the exact metadata admission owner independently of the
        # separate receipt-link mismatch, which must also remain a refusal.
        with pytest.raises(CredentialMaterialRefused) as refused:
            _require_evidence_metadata(changed, env_ref_names=frozenset())
        assert refused.value.finding.category == "credential_url"
        with pytest.raises((AuditIntegrityError, CredentialMaterialRefused)):
            admit_turn_evidence(changed, env_ref_names=frozenset())
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_other_evidence_metadata_remains_under_original_guard(operation_store):
    running, _args, values, _audit, _messages = await portable_input(operation_store)
    try:
        bundle = build_portable_p4_application(**values)
        assert decode_turn_evidence(bundle.evidence_chunk, env_ref_names=frozenset()).snapshot == bundle.evidence_after.snapshot
        snapshot = TurnEvidenceSnapshot.model_validate(
            {**bundle.evidence_after.snapshot.model_dump(mode="python"), "actor_user_id": "sk-" + "a" * 24}
        )
        with pytest.raises(CredentialMaterialRefused):
            admit_turn_evidence(snapshot, env_ref_names=frozenset())
    finally:
        operation_store[1].release(running.session_operation_context)
