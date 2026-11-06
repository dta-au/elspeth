"""TITLE admission cancellation readback proves one original pending V4 attempt."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.sessions.continuation_title_provider import (
    admit_v4_title_provider_on_connection,
    read_v4_title_admission_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_policy import continuation_test_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions import test_continuation_initialization as initialization_cases
from tests.unit.web.sessions.test_continuation_title_provider import title_case

operation_store = operations.operation_store


@pytest.mark.asyncio
async def test_v4_title_admission_readback_recovers_only_original_active_hold_and_model(operation_store, tmp_path, monkeypatch):
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_max_retained_payload_bytes=16 * 1_048_576))
    assert policy is not None
    authority = ComposerAsyncOperationAuthority(
        operation_store[0], owner_instance_id="test-owner", claim_lease_seconds=30, continuation_policy=policy
    )
    monkeypatch.setattr(initialization_cases, "minimal_continuation_policy", lambda: policy)
    store = (operation_store[0], operation_store[1], authority, operation_store[3], operation_store[4])
    running, _composer, _work, prepared, _guard = await title_case(store, tmp_path, monkeypatch)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    try:
        with locked_session_transaction(store[0], sid) as conn:
            assert read_v4_title_admission_on_connection(conn, prepared=prepared) is None
            admitted = admit_v4_title_provider_on_connection(conn, store[3], prepared, model=prepared.request.kwargs["model"])
        with locked_session_transaction(store[0], sid) as conn:
            recovered = read_v4_title_admission_on_connection(conn, prepared=prepared)
            assert recovered == admitted
            assert recovered is not admitted
            assert recovered is not None and recovered.selected == admitted.selected
            mutated = conn.begin_nested()
            conn.execute(
                update(effects)
                .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == "provider:3:0")
                .values(requested_model="foreign-model")
            )
            with pytest.raises(AuditIntegrityError, match="original physical request and actor"):
                read_v4_title_admission_on_connection(conn, prepared=prepared)
            mutated.rollback()
            assert read_v4_title_admission_on_connection(conn, prepared=prepared) == admitted
    finally:
        store[1].release(running.session_operation_context)
