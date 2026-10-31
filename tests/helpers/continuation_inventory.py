"""Real names-only owner for SQL handoff tests; no provider or fabricated inventory."""

from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.chargeable_admission import ComposerChargeableAdmission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.config import WebSettings
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkRole,
)


def inventory_advisor(sessions, *, secret_service=None):
    return AdvisorCheckpointOwner(
        settings=WebSettings(
            composer_max_composition_turns=15,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=85.0,
            composer_rate_limit_per_minute=100,
            shareable_link_signing_key=b"\x00" * 32,
        ),
        composer_skill_text="inventory-test",
        advisor_endpoint_base_url=None,
        advisor_endpoint_api_key=None,
        sessions_service=sessions,
        chargeable_admission=ComposerChargeableAdmission(sessions),
        secret_service=secret_service,
    )


def inventory_work(running):
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context, running.claim.operation_id, running.claim.attempt
        )
    )
    return RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)


async def inventory_for_handoff_test(store, running, stored):
    _engine, _repository, authority, sessions, _sid = store
    root = decode_continuation_snapshot(stored.payload_json)
    chunks = authority.load_continuation_chunks(running, stored)
    selected = SelectedContinuationState(stored.checkpoint_id, root, chunks)
    work = inventory_work(running)
    try:
        return await materialize_continuation_env_inventory(
            advisor=inventory_advisor(sessions), authority=authority, selected=selected, required_work=work
        )
    finally:
        work.coordinator.assert_completed()
