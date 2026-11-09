"""Fix 2C: the short-lived validate_state memo collapses /validate + /audit-readiness.

Inner-validation counts come from a spy on
``ExecutionServiceImpl._authoritative_state_preflight_sync`` (the worker-side
graph build), never from ``validate_state`` itself. Each memo-hit assertion
has a negative control that runs the same calls with the memo replaced by a
pass-through (``_NoMemo``) and must see the pre-fix count.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import SecretBytes

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.secrets import SecretInventoryItem
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.audit_readiness.service import ReadinessService
from elspeth.web.blobs.protocol import BlobContentMissingError, BlobNotFoundError
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.execution.progress import ProgressBroadcaster
from elspeth.web.execution.schemas import ValidationCheck, ValidationError, ValidationReadiness, ValidationResult
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation_memo import (
    BlobReadLog,
    BlobReads,
    ValidationMemoKey,
    ValidationResultMemo,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.helpers import execution_custody
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.helpers.session_fences import make_blob_read_context

execution_fixture = execution_custody.execution_fixture

_STATE_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ── Fixtures and doubles ──────────────────────────────────────────────────────


class _NoMemo:
    """Pass-through stand-in for the memo: the pre-fix behaviour (negative control)."""

    async def get_or_compute(
        self,
        key: ValidationMemoKey,
        *,
        compute: Callable[[BlobReadLog], Awaitable[ValidationResult]],
        blob_reads_current: Callable[[BlobReads], Awaitable[bool]],
    ) -> ValidationResult:
        del key, blob_reads_current
        return await compute(BlobReadLog())


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _UnusedSessionService:
    pass


class _StateRecord:
    def __init__(self) -> None:
        self.id = _STATE_ID
        self.composer_meta = None


class _ReadinessSessionService:
    def __init__(self) -> None:
        self.record = _StateRecord()

    async def get_current_state(self, session_id: UUID) -> _StateRecord:
        del session_id
        return self.record

    async def list_interpretation_events(self, *args: object, **kwargs: object) -> list[object]:
        raise AssertionError("the memo test state has no LLM transform")


class _Inventory:
    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        del user_id
        return []


class _RetentionSettings:
    payload_store_retention_days = 90


def _state(tmp_path: Path, session_id: UUID, *, name: str = "memo") -> CompositionState:
    blobs_dir = tmp_path / "blobs" / str(session_id)
    outputs_dir = tmp_path / "outputs" / str(session_id)
    blobs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    (blobs_dir / "input.csv").write_text("product\nhats\n", encoding="utf-8")
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": str(blobs_dir / "input.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="copy",
                node_type="transform",
                plugin="passthrough",
                input="rows",
                on_success="results",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={"path": str(outputs_dir / "results.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(name=name),
        version=1,
    )


def _service(tmp_path: Path, custody: ExecutionTestCustody) -> ExecutionServiceImpl:
    loop = asyncio.get_running_loop()
    return custody.bind(
        ExecutionServiceImpl.for_trained_operator(
            loop=loop,
            broadcaster=ProgressBroadcaster(loop),
            settings=WebSettings(
                data_dir=tmp_path,
                composer_max_composition_turns=10,
                composer_max_discovery_turns=5,
                composer_timeout_seconds=30.0,
                composer_rate_limit_per_minute=60,
                shareable_link_signing_key=SecretBytes(b"\x00" * 32),
            ),
            session_service=cast(Any, _UnusedSessionService()),
            yaml_generator=composer_yaml_generator,
            telemetry=build_sessions_telemetry(),
            execution_lease_release_registry=custody.registry(custody.loop),
        )
    )


def _readiness(service: ExecutionServiceImpl, state: CompositionState) -> ReadinessService:
    return ReadinessService(
        execution_service=service,
        session_service=cast(Any, _ReadinessSessionService()),
        scoped_secret_resolver=cast(Any, _Inventory()),
        settings=cast(Any, _RetentionSettings()),
        state_from_record=lambda _record: state,
    )


def _snapshot_without_csv(base: PluginAvailabilitySnapshot) -> PluginAvailabilitySnapshot:
    """A second principal's availability: same policy, csv source not available."""
    available = frozenset(plugin_id for plugin_id in base.available if not (plugin_id.kind == "source" and plugin_id.name == "csv"))
    assert available != base.available
    return PluginAvailabilitySnapshot.create(
        policy_hash=base.policy_hash,
        principal_scope=base.principal_scope,
        available=available,
        unavailable=base.unavailable,
        selected=base.selected,
        usable_profile_aliases=base.usable_profile_aliases,
        selected_profile_aliases=base.selected_profile_aliases,
        binding_generation_fingerprint=base.binding_generation_fingerprint,
        control_modes=base.control_modes,
        authority=base.authority,
        power_automate_allowed_origins=base.power_automate_allowed_origins,
    )


@pytest.fixture
def inner_spy() -> Iterator[Callable[[ExecutionServiceImpl], MagicMock]]:
    patches: list[Any] = []

    def _install(service: ExecutionServiceImpl) -> MagicMock:
        patcher = patch.object(service, "_authoritative_state_preflight_sync", wraps=service._authoritative_state_preflight_sync)
        patches.append(patcher)
        return cast(MagicMock, patcher.start())

    yield _install
    for patcher in patches:
        patcher.stop()


async def _validate_then_readiness(
    service: ExecutionServiceImpl, state: CompositionState, session_id: UUID, context: SessionOperationContext
) -> tuple[ValidationResult, ValidationResult]:
    """POST /validate's call, then GET /audit-readiness's compute_snapshot, for one state."""
    posted = await service.validate_state(
        state,
        session_operation_context=context,
        user_id="alice",
        session_id=session_id,
        completion_gates=None,
    )
    snapshot = await _readiness(service, state).compute_snapshot(
        session_id=session_id,
        user_id="alice",
        session_operation_context=context,
    )
    return posted, snapshot.validation_result


# ── Service-level: the POST + GET pair ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(("memoized", "expected_inner_runs"), [(True, 1), (False, 2)])
async def test_validate_then_readiness_runs_inner_validation_once(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    memoized: bool,
    expected_inner_runs: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        if not memoized:
            monkeypatch.setattr(service, "_validation_memo", _NoMemo())
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        posted, readiness_result = await _validate_then_readiness(service, state, session_id, context)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == expected_inner_runs
    assert posted.is_valid is True
    assert readiness_result == posted
    assert readiness_result is not posted


@pytest.mark.asyncio
@pytest.mark.parametrize(("memoized", "expected_inner_runs"), [(True, 1), (False, 2)])
async def test_concurrent_validate_and_readiness_share_one_inner_validation(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    memoized: bool,
    expected_inner_runs: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontend fires both on a version change; they overlap in flight."""
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        if not memoized:
            monkeypatch.setattr(service, "_validation_memo", _NoMemo())
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        posted, snapshot = await asyncio.gather(
            service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id),
            _readiness(service, state).compute_snapshot(session_id=session_id, user_id="alice", session_operation_context=context),
        )
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == expected_inner_runs
    assert snapshot.validation_result == posted


@pytest.mark.asyncio
async def test_different_user_is_computed_separately_and_sees_its_own_availability(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        base = service._plugin_snapshot_for_user("alice", operation="test")
        bob_snapshot = _snapshot_without_csv(base)
        monkeypatch.setattr(service, "_plugin_snapshot_factory", lambda user_id: bob_snapshot if user_id == "bob" else base)
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        alice = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        bob = await service.validate_state(state, session_operation_context=context, user_id="bob", session_id=session_id)
        alice_again = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 2  # alice, bob; alice_again is a hit
    assert alice.is_valid is True
    assert bob.is_valid is False  # csv is not available to bob: never alice's verdict
    assert alice_again == alice


@pytest.mark.asyncio
async def test_same_availability_different_user_still_computes_separately(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
) -> None:
    """user_id is a key component on its own, not only via the snapshot hash."""
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        await service.validate_state(state, session_operation_context=context, user_id="bob", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_different_state_content_or_version_is_computed_separately(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        spy = inner_spy(service)
        first = _state(tmp_path, session_id, name="first")
        other_content = _state(tmp_path, session_id, name="second")
        other_version = replace(first, version=2)
        for state in (first, other_content, other_version, first):
            await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 3  # the final ``first`` is a hit


@pytest.mark.asyncio
async def test_changed_availability_for_same_user_and_state_recomputes(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        base = service._plugin_snapshot_for_user("alice", operation="test")
        rotated = _snapshot_without_csv(base)
        current = {"snapshot": base}
        monkeypatch.setattr(service, "_plugin_snapshot_factory", lambda _user_id: current["snapshot"])
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        before = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        current["snapshot"] = rotated
        after = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 2
    assert before.is_valid is True
    assert after.is_valid is False


@pytest.mark.asyncio
async def test_secret_inventory_change_recomputes(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    inventory: list[SecretInventoryItem] = []

    class _Secrets:
        def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
            del user_id
            return list(inventory)

    try:
        monkeypatch.setattr(service, "_secret_service", _Secrets())
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        inventory.append(SecretInventoryItem(name="NEW_KEY", scope="user", available=True))
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 2  # second call hits; inventory change misses


@pytest.mark.asyncio
async def test_state_with_authored_secret_ref_bypasses_memo(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
) -> None:
    """A wired secret's value is resolved by validation and is not keyable."""
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        spy = inner_spy(service)
        plain = _state(tmp_path, session_id)
        output = plain.outputs[0]
        wired = replace(
            plain,
            outputs=(replace(output, options={**dict(output.options), "api_key": {"secret_ref": "SINK_KEY"}}),),
        )
        for _ in range(2):
            await service.validate_state(wired, session_operation_context=context, user_id="alice", session_id=session_id)
        for _ in range(2):
            await service.validate_state(plain, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == 3  # wired twice (bypass) + plain once (second is a hit)


@pytest.mark.asyncio
async def test_entry_expiry_recomputes(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    clock = _FakeClock()
    try:
        monkeypatch.setattr(service, "_validation_memo", ValidationResultMemo(ttl_seconds=10.0, clock=clock))
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        clock.now += 9.9
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        hits_before_expiry = spy.call_count
        clock.now += 0.2
        await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert hits_before_expiry == 1
    assert spy.call_count == 2


@pytest.mark.asyncio
async def test_inner_failure_is_not_memoized(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        real = service._authoritative_state_preflight_sync
        calls: list[int] = []

        def _flaky(*args: Any, **kwargs: Any) -> ValidationResult:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("transient worker failure")
            return real(*args, **kwargs)

        with patch.object(service, "_authoritative_state_preflight_sync", side_effect=_flaky):
            state = _state(tmp_path, session_id)
            with pytest.raises(RuntimeError, match="transient worker failure"):
                await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
            recovered = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
            await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert recovered.is_valid is True
    assert len(calls) == 2  # failure, then one success that the third call reuses


@pytest.mark.asyncio
async def test_consumers_cannot_see_each_others_mutations(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        state = _state(tmp_path, session_id)
        first = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        pristine = first.model_copy(deep=True)
        first.checks.append(ValidationCheck(name="state_exists", passed=False, detail="mutated", affected_nodes=(), outcome_code=None))
        first.readiness.blockers.clear()
        second = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        second.errors.append(_any_error())
        third = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert second.checks == pristine.checks
    assert third == pristine
    assert second is not third


def _any_error() -> ValidationError:
    return ValidationError(component_id=None, component_type=None, message="mutated", suggestion=None, error_code=None)


# ── Blob dependency revalidation ──────────────────────────────────────────────


def _blob(session_id: UUID, *, content_hash: str = "a" * 64, status: str = "ready") -> BlobRecord:
    return BlobRecord(
        id=UUID("5b7a4e0e-9e4a-4f0b-8d3e-2c0e1f0d3a4b"),
        session_id=session_id,
        filename="input.csv",
        mime_type="text/csv",
        size_bytes=12,
        content_hash=content_hash,
        storage_path="/data/blobs/input.csv",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="user",
        source_description=None,
        status=cast(Any, status),
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


class _BlobRows:
    def __init__(self, record: BlobRecord | None) -> None:
        self.record = record
        self.contexts: list[SessionOperationContext] = []

    async def get_blob(self, blob_id: UUID, *, session_operation_context: SessionOperationContext) -> BlobRecord:
        self.contexts.append(session_operation_context)
        if self.record is None or self.record.id != blob_id:
            raise BlobNotFoundError(str(blob_id))
        return self.record


@pytest.mark.asyncio
async def test_blob_reads_still_current_rechecks_each_row_under_callers_context(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    recorded = _blob(session_id)
    rows = _BlobRows(recorded)
    try:
        monkeypatch.setattr(service, "_blob_service", rows)
        unchanged = await service._blob_reads_still_current(((recorded.id, recorded),), session_operation_context=context)
        rows.record = _blob(session_id, content_hash="b" * 64)
        replaced = await service._blob_reads_still_current(((recorded.id, recorded),), session_operation_context=context)
        rows.record = None
        deleted = await service._blob_reads_still_current(((recorded.id, recorded),), session_operation_context=context)
        still_absent = await service._blob_reads_still_current(((recorded.id, None),), session_operation_context=context)
    finally:
        await execution_fixture.shutdown_service(service)

    assert (unchanged, replaced, deleted, still_absent) == (True, False, False, True)
    assert rows.contexts and all(seen is context for seen in rows.contexts)


def _result(tag: str) -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[ValidationCheck(name="graph_structure", passed=True, detail=tag, affected_nodes=(), outcome_code=None)],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


def _key(user_id: str = "alice") -> ValidationMemoKey:
    return ValidationMemoKey(
        user_id=user_id,
        session_id=UUID(int=1),
        state_content_hash="c" * 64,
        state_version=1,
        plugin_snapshot_hash="d" * 64,
        secret_inventory=frozenset(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("current", "expected_runs"), [(True, 1), (False, 2)])
async def test_hit_is_served_only_when_consulted_blob_rows_are_unchanged(current: bool, expected_runs: int) -> None:
    memo = ValidationResultMemo()
    record = _blob(UUID(int=1))
    runs: list[int] = []
    seen: list[BlobReads] = []

    async def _compute(log: BlobReadLog) -> ValidationResult:
        runs.append(1)
        log.record(record.id, record)
        return _result("v")

    async def _current(blob_reads: BlobReads) -> bool:
        seen.append(blob_reads)
        return current

    await memo.get_or_compute(_key(), compute=_compute, blob_reads_current=_current)
    await memo.get_or_compute(_key(), compute=_compute, blob_reads_current=_current)

    assert len(runs) == expected_runs
    assert seen == [((record.id, record),)]


@pytest.mark.asyncio
async def test_failed_content_read_or_conflicting_rows_are_not_stored() -> None:
    memo = ValidationResultMemo()
    record = _blob(UUID(int=1))
    runs: list[str] = []

    async def _content_failed(log: BlobReadLog) -> ValidationResult:
        runs.append("content")
        log.record(record.id, record)
        log.mark_uncacheable()
        return _result("v")

    async def _conflict(log: BlobReadLog) -> ValidationResult:
        runs.append("conflict")
        log.record(record.id, record)
        log.record(record.id, None)
        return _result("v")

    async def _always(_reads: BlobReads) -> bool:
        return True

    for compute in (_content_failed, _content_failed, _conflict, _conflict):
        await memo.get_or_compute(_key(), compute=compute, blob_reads_current=_always)

    assert runs == ["content", "content", "conflict", "conflict"]
    assert len(memo) == 0


@pytest.mark.asyncio
async def test_waiter_on_a_failed_in_flight_computation_computes_for_itself() -> None:
    memo = ValidationResultMemo()
    release = asyncio.Event()
    runs: list[str] = []

    async def _failing(_log: BlobReadLog) -> ValidationResult:
        runs.append("owner")
        await release.wait()
        raise RuntimeError("owner failed")

    async def _ok(_log: BlobReadLog) -> ValidationResult:
        runs.append("waiter")
        return _result("waiter")

    async def _always(_reads: BlobReads) -> bool:
        return True

    owner = asyncio.create_task(memo.get_or_compute(_key(), compute=_failing, blob_reads_current=_always))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(memo.get_or_compute(_key(), compute=_ok, blob_reads_current=_always))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError, match="owner failed"):
        await owner
    waited = await waiter

    assert runs == ["owner", "waiter"]
    assert waited.checks[0].detail == "waiter"


@pytest.mark.asyncio
async def test_cancelled_owner_releases_waiters() -> None:
    memo = ValidationResultMemo()
    started = asyncio.Event()

    async def _hang(_log: BlobReadLog) -> ValidationResult:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def _ok(_log: BlobReadLog) -> ValidationResult:
        return _result("waiter")

    async def _always(_reads: BlobReads) -> bool:
        return True

    owner = asyncio.create_task(memo.get_or_compute(_key(), compute=_hang, blob_reads_current=_always))
    await started.wait()
    waiter = asyncio.create_task(memo.get_or_compute(_key(), compute=_ok, blob_reads_current=_always))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    waited = await asyncio.wait_for(waiter, timeout=5)

    assert waited.checks[0].detail == "waiter"
    assert len(memo) == 1  # the waiter's own success is stored


@pytest.mark.asyncio
async def test_memo_is_bounded() -> None:
    memo = ValidationResultMemo(max_entries=3)

    async def _compute(_log: BlobReadLog) -> ValidationResult:
        return _result("v")

    async def _always(_reads: BlobReads) -> bool:
        return True

    for index in range(5):
        await memo.get_or_compute(_key(f"user-{index}"), compute=_compute, blob_reads_current=_always)

    assert len(memo) == 3


class _SourceBlobService(_BlobRows):
    """Serves one session-owned ready source blob for the bounded source proof."""

    def __init__(self, record: BlobRecord, content: bytes) -> None:
        super().__init__(record)
        self.content = content
        self.prefix_reads = 0
        self.fail_prefix = False

    async def read_blob_content_prefix_verified(
        self, blob_id: UUID, *, prefix_bytes: int, session_operation_context: SessionOperationContext
    ) -> tuple[bytes, str, int]:
        del session_operation_context
        self.prefix_reads += 1
        if self.fail_prefix:
            raise BlobContentMissingError(str(blob_id), storage_path="gone")
        return self.content[:prefix_bytes], hashlib.sha256(self.content).hexdigest(), len(self.content)


def _blob_backed_state(tmp_path: Path, session_id: UUID, record: BlobRecord) -> CompositionState:
    state = _state(tmp_path, session_id)
    source = state.sources["source"]
    return replace(
        state,
        sources={"source": replace(source, options={**dict(source.options), "blob_ref": str(record.id), "path": record.storage_path})},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "row_replaced", "row_deleted", "content_read_failed"])
async def test_service_memo_rechecks_the_source_blob_row_it_consulted(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    content = b"product\nhats\n"
    csv_path = tmp_path / "blobs" / str(session_id) / "input.csv"
    record = replace(
        _blob(session_id, content_hash=hashlib.sha256(content).hexdigest()),
        storage_path=str(csv_path),
        size_bytes=len(content),
    )
    blobs = _SourceBlobService(record, content)
    service = _service(tmp_path, execution_fixture)
    try:
        monkeypatch.setattr(service, "_blob_service", blobs)
        state = _blob_backed_state(tmp_path, session_id, record)
        spy = inner_spy(service)
        if change == "content_read_failed":
            blobs.fail_prefix = True
        first = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        if change == "row_replaced":
            blobs.record = replace(record, status=cast(Any, "pending"))
        elif change == "row_deleted":
            blobs.record = None
        second = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert blobs.contexts and all(seen is context for seen in blobs.contexts)
    if change == "none":
        assert first.is_valid is True
        assert blobs.prefix_reads == 1  # the source proof actually ran and read the blob
        assert spy.call_count == 1
        assert second == first
    else:
        assert spy.call_count == 2


class _InlineBlobService(_BlobRows):
    """Serves one inline-content blob (a reference table) to the validator."""

    def __init__(self, record: BlobRecord, content: bytes) -> None:
        super().__init__(record)
        self.content = content
        self.content_reads = 0
        self.fail_content = False

    async def read_blob_content(self, blob_id: UUID, *, session_operation_context: SessionOperationContext) -> bytes:
        self.contexts.append(session_operation_context)
        self.content_reads += 1
        if self.fail_content:
            raise BlobNotFoundError(str(blob_id))
        return self.content


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "row_replaced", "content_read_failed"])
async def test_service_memo_rechecks_the_inline_content_blob_row_it_consulted(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    table = b"sku,description\nhats,A fine hat\n"
    digest = hashlib.sha256(table).hexdigest()
    record = replace(_blob(session_id, content_hash=digest), size_bytes=len(table))
    blobs = _InlineBlobService(record, table)
    service = _service(tmp_path, execution_fixture)
    try:
        monkeypatch.setattr(service, "_blob_service", blobs)
        plain = _state(tmp_path, session_id)
        state = replace(
            plain,
            nodes=(
                replace(
                    plain.nodes[0],
                    id="enrich",
                    plugin="reference_join",
                    options={
                        "reference_content": {"blob_ref": str(record.id), "mode": "inline_content", "sha256": digest},
                        "reference_format": "csv",
                        "key_field": "product",
                        "reference_key_name": "sku",
                        "output": {"description": "ref['description']"},
                        "schema": {"mode": "observed"},
                    },
                ),
            ),
        )
        spy = inner_spy(service)
        if change == "content_read_failed":
            blobs.fail_content = True
        first = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        if change == "row_replaced":
            blobs.record = replace(record, filename="renamed.csv")
        second = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
    finally:
        await execution_fixture.shutdown_service(service)

    assert blobs.contexts and all(seen is context for seen in blobs.contexts)
    if change == "none":
        assert first.is_valid is True, first.errors
        assert blobs.content_reads == 1
        assert spy.call_count == 1
        assert second == first
    else:
        assert spy.call_count == 2


def test_in_flight_entry_on_another_loop_is_not_joined_but_stored_results_cross_loops() -> None:
    """The service is reachable from several loops (one per test-client request)."""
    import threading

    memo = ValidationResultMemo()
    owner_started = threading.Event()
    release_owner = threading.Event()
    runs: list[str] = []

    async def _always(_reads: BlobReads) -> bool:
        return True

    async def _slow(_log: BlobReadLog) -> ValidationResult:
        runs.append("owner")
        owner_started.set()
        while not release_owner.is_set():
            await asyncio.sleep(0.001)
        return _result("owner")

    async def _other(_log: BlobReadLog) -> ValidationResult:
        runs.append("other-loop")
        return _result("other-loop")

    owner = threading.Thread(target=lambda: asyncio.run(memo.get_or_compute(_key(), compute=_slow, blob_reads_current=_always)))
    owner.start()
    assert owner_started.wait(timeout=5)
    # In flight on the owner's loop: a second loop must compute for itself, not await a foreign future.
    try:
        other = asyncio.run(
            asyncio.wait_for(memo.get_or_compute(_key(), compute=_other, blob_reads_current=_always), timeout=5),
        )
    finally:
        release_owner.set()
        owner.join(timeout=5)

    async def _unexpected(_log: BlobReadLog) -> ValidationResult:
        raise AssertionError("a stored result must be served across loops")

    stored = asyncio.run(memo.get_or_compute(_key(), compute=_unexpected, blob_reads_current=_always))

    assert other.checks[0].detail == "other-loop"
    assert runs == ["owner", "other-loop"]
    assert stored.checks[0].detail in {"owner", "other-loop"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("fresh", "expected_inner_runs"), [(True, 2), (False, 1)])
async def test_fresh_call_bypasses_a_warm_memo_entry(
    tmp_path: Path,
    execution_fixture: ExecutionTestCustody,
    inner_spy: Callable[[ExecutionServiceImpl], MagicMock],
    fresh: bool,
    expected_inner_runs: int,
) -> None:
    """The shareable-review mark passes fresh=True: its verdict is written into a
    durable review record and must not come from the advisory memo. The
    ``fresh=False`` row is the control: the same second call would have hit."""
    session_id = uuid4()
    context = make_blob_read_context(session_id)
    service = _service(tmp_path, execution_fixture)
    try:
        spy = inner_spy(service)
        state = _state(tmp_path, session_id)
        warmed = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id)
        second = await service.validate_state(state, session_operation_context=context, user_id="alice", session_id=session_id, fresh=fresh)
        entries_after = len(service._validation_memo)
    finally:
        await execution_fixture.shutdown_service(service)

    assert spy.call_count == expected_inner_runs
    assert second == warmed
    assert entries_after == 1  # a fresh call neither evicts nor overwrites the advisory entry
