"""The worker's bounded startup/periodic V2 recovery sweep.

Discovery is advisory. The repository revalidates the complete original owner
and chooses recovery or terminal Stop inside its own session transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.sessions.composer_app_services import composer_app_services

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_async_worker import ComposerAsyncWorker


async def reap_continuations_once(worker: ComposerAsyncWorker) -> int:
    from elspeth.web.sessions.composer_async_worker import _owned

    services = composer_app_services(worker._app)
    repository = services.session_service.session_operation_authority
    count = 0
    candidates = await _owned(run_sync_in_worker(worker._authority.list_recoverable_segments, limit=worker._concurrency))
    for candidate in candidates:
        key = candidate.session_id, candidate.operation_id
        if key in worker._jobs:
            continue
        if key in worker._adoption_recovery_pending:
            worker._process_recovery.request_shutdown()
            continue
        disposition = await _owned(
            run_sync_in_worker(
                repository.reconcile_unavailable_composer_segment,
                candidate,
                owner_instance_id=worker._owner_instance_id,
                lease_seconds=services.session_service.session_operation_lease_seconds,
                auth_provider_type=services.settings.auth_provider,
            )
        )
        if disposition in ("recovered", "settled"):
            count += 1
        elif disposition != "unchanged":
            raise AuditIntegrityError("continuation recovery returned an unsupported disposition")
    inactive = await _owned(run_sync_in_worker(worker._authority.list_inactive_continuations, limit=worker._concurrency))
    for inactive_candidate in inactive:
        key = inactive_candidate.session_id, inactive_candidate.operation_id
        if key in worker._jobs:
            continue
        if key in worker._adoption_recovery_pending:
            worker._process_recovery.request_shutdown()
            continue
        settled = await _owned(
            run_sync_in_worker(
                repository.settle_lost_inactive_continuation,
                inactive_candidate,
                auth_provider_type=services.settings.auth_provider,
            )
        )
        if type(settled) is not bool:
            raise AuditIntegrityError("inactive continuation settlement returned an unsupported result")
        count += int(settled)
    return count
