"""The live phase owner carries one process-local progress callback across restore."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import cast

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.required_work import RequiredWorkBinding
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import initialized_primary_input

operation_store = _operation_store


@pytest.mark.asyncio
async def test_initial_binding_and_cold_restore_keep_the_exact_segment_progress_sink(operation_store, tmp_path, monkeypatch) -> None:
    initialize = cast(Callable[..., Awaitable[tuple[RestoredContinuationRuntime, RequiredWorkBinding, object]]], initialized_primary_input)
    runtime, work, _requests = await initialize(operation_store, tmp_path, monkeypatch)
    service, authority = runtime.materialization.service, operation_store[2]
    running = work.running
    assert running is not None
    provider = runtime.bindings.provider_owner
    assert type(provider) is ProviderInvocationOwner
    lease = await ContinuationLeaseAdoption(
        operation_store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator
    ).join()

    async def progress(_event) -> None:
        pass

    async def foreign_progress(_event) -> None:
        pass

    try:
        bound_runtime: RestoredContinuationRuntime = replace(runtime, bindings=replace(runtime.bindings, progress=progress))
        warm = ContinuationLivePhases(service, authority, work, provider, lease, progress=progress)
        warm.bind_initial(bound_runtime)
        assert warm.runtime is bound_runtime and warm.runtime.bindings.progress is progress

        foreign = ContinuationLivePhases(service, authority, work, provider, lease, progress=foreign_progress)
        with pytest.raises(AuditIntegrityError, match="live phase initialization changed its actual runtime owners"):
            foreign.bind_initial(bound_runtime)
        assert foreign.runtime is None

        cold = ContinuationLivePhases(service, authority, work, provider, lease, progress=progress)
        restored, _inventory = await cold.current(runtime.source.selected)
        assert cold.runtime is restored
        assert restored.bindings.progress is progress
        assert restored.source.selected is runtime.source.selected
        assert restored.bindings.session_operation_context is running.session_operation_context
    finally:
        await lease.close()
