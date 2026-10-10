"""Owned-coordinator entry integrity, before dispatch or child allocation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkCoordinator
from elspeth.web.sessions import composer_turn
from elspeth.web.sessions.composer_operations import ComposerOperationClaim, ComposerOperationRunning
from elspeth.web.sessions.composer_turn import ComposerBudgetAnchor, ComposerTurnInput, ComposerTurnObservation
from elspeth.web.sessions.routes.composer import pipeline_settlement
from elspeth.web.sessions.schemas import SendMessageRequest


class _CoercingImpostor:
    def __init__(self) -> None:
        self.reads = 0
        self.dispatches = 0
        self.coercions = 0

    @property
    def authority(self) -> object:
        self.reads += 1
        raise AssertionError("Foreign authority property was read")

    def reserve(self, *args: object, **kwargs: object) -> object:
        self.dispatches += 1
        raise AssertionError("Foreign reserve was dispatched")

    def begin_proposal_child(self, *args: object, **kwargs: object) -> object:
        self.dispatches += 1
        raise AssertionError("Foreign proposal child was dispatched")

    def __bool__(self) -> bool:
        self.coercions += 1
        return False

    def __eq__(self, other: object) -> bool:
        self.coercions += 1
        return True


class _Authority:
    def __init__(self) -> None:
        self.cas: list[SessionOperationContext] = []
        self.releases: list[SessionOperationContext] = []
        self.release_error: BaseException | None = None

    def compare_and_swap(self, context: SessionOperationContext) -> None:
        self.cas.append(context)

    def release(self, context: SessionOperationContext) -> None:
        self.releases.append(context)
        if self.release_error is not None:
            raise self.release_error

    def renew(self, context: SessionOperationContext, *, lease_seconds: int) -> SessionOperationContext:
        return context


def _context() -> SessionOperationContext:
    return SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "owned-test-lease", 1), SessionOperationKind.COMPOSE)


def _coordinator(context: SessionOperationContext) -> RequiredWorkCoordinator:
    return RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))


def _no_task_allocations(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []

    def refuse(coroutine: object, *args: object, **kwargs: object) -> None:
        calls.append(coroutine)
        # The assertion detects the allocation attempt; no fake completed task
        # or SQL completion witness is supplied to the code under test.
        raise AssertionError("Unexpected task allocation")

    monkeypatch.setattr(asyncio, "create_task", refuse)
    return calls


def _assert_untouched(impostor: _CoercingImpostor) -> None:
    assert impostor.reads == 0
    assert impostor.dispatches == 0
    assert impostor.coercions == 0


@pytest.mark.parametrize("value_kind", ["impostor", "plain-object"])
def test_lease_constructor_refuses_foreign_owner_before_task(monkeypatch: pytest.MonkeyPatch, value_kind: str) -> None:
    impostor = _CoercingImpostor()
    value = impostor if value_kind == "impostor" else object()
    authority = _Authority()
    allocations = _no_task_allocations(monkeypatch)
    with pytest.raises(AuditIntegrityError, match="owned required-work coordinator"):
        SessionOperationLease(authority, _context(), lease_seconds=30, renew_interval_seconds=10, required_work=value)
    assert allocations == []
    assert authority.cas == authority.releases == []
    _assert_untouched(impostor)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_context", [False, True])
async def test_adopt_foreign_or_wrong_scope_releases_owned_context_without_foreign_dispatch(
    monkeypatch: pytest.MonkeyPatch, wrong_context: bool
) -> None:
    context = _context()
    impostor = _CoercingImpostor()
    value = _coordinator(_context()) if wrong_context else impostor
    authority = _Authority()
    production_create_task = asyncio.create_task
    renewal_allocations: list[object] = []

    def record_task(coroutine, *args, **kwargs):
        if kwargs.get("name") == "session-operation-renewal":
            renewal_allocations.append(coroutine)
        return production_create_task(coroutine, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", record_task)
    with pytest.raises(AuditIntegrityError):
        await SessionOperationLease.adopt(authority, context, lease_seconds=30, required_work=value)
    assert renewal_allocations == []
    assert authority.cas == []
    assert authority.releases == [context]
    _assert_untouched(impostor)


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fails", [False, True])
async def test_adopt_bad_owner_preserves_original_timing_failure_and_owned_release(
    monkeypatch: pytest.MonkeyPatch, release_fails: bool
) -> None:
    from elspeth.web.coordination import lifecycle

    context = _context()
    impostor = _CoercingImpostor()
    authority = _Authority()
    original = ValueError("controlled original timing failure")
    release_error = AuditIntegrityError("controlled original release failure")
    if release_fails:
        authority.release_error = release_error

    def invalid_timing(*, lease_seconds: int, renew_interval_seconds: float | None) -> float:
        raise original

    monkeypatch.setattr(lifecycle, "_validate_lifecycle_timing", invalid_timing)
    with pytest.raises(BaseException) as caught:
        await SessionOperationLease.adopt(authority, context, lease_seconds=0, required_work=impostor)
    if release_fails:
        assert isinstance(caught.value, BaseExceptionGroup)
        assert caught.value.exceptions[0] is original
        assert caught.value.exceptions[1] is release_error
    else:
        assert caught.value is original
    assert authority.cas == []
    assert authority.releases == [context]
    _assert_untouched(impostor)


def test_constructor_refuses_actual_wrong_context_before_task(monkeypatch: pytest.MonkeyPatch) -> None:
    allocations = _no_task_allocations(monkeypatch)
    with pytest.raises(AuditIntegrityError, match="scope disagrees"):
        SessionOperationLease(_Authority(), _context(), lease_seconds=30, renew_interval_seconds=10, required_work=_coordinator(_context()))
    assert allocations == []


@pytest.mark.asyncio
async def test_actual_coordinator_adoption_and_same_identity_binding_complete() -> None:
    context = _context()
    coordinator = _coordinator(context)
    authority = _Authority()
    lease = await SessionOperationLease.adopt(authority, context, lease_seconds=30, required_work=coordinator)
    try:
        assert lease.required_work is coordinator
        lease.bind_required_work(coordinator)
        lease.bind_required_work(coordinator)
        assert authority.cas == [context]
        replacement = _coordinator(context)
        with pytest.raises(AuditIntegrityError, match="changed immutable authority"):
            lease.bind_required_work(replacement)
        with pytest.raises(AuditIntegrityError, match="changed immutable authority"):
            lease.bind_required_work(_coordinator(_context()))
        impostor = _CoercingImpostor()
        with pytest.raises(AuditIntegrityError, match="owned required-work coordinator"):
            lease.bind_required_work(impostor)
        _assert_untouched(impostor)
        assert lease.required_work is coordinator
    finally:
        await lease.close()
    assert authority.releases == [context]
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_binding_refuses_coercive_corrupted_retained_owner() -> None:
    context = _context()
    lease = SessionOperationLease(_Authority(), context, lease_seconds=30, renew_interval_seconds=10)
    impostor = _CoercingImpostor()
    try:
        lease._required_work = impostor
        with pytest.raises(AuditIntegrityError, match="changed immutable authority"):
            lease.bind_required_work(_coordinator(context))
        _assert_untouched(impostor)
    finally:
        lease._required_work = None
        await lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_turn_foreign_owner_refused_before_binding_or_provider(monkeypatch: pytest.MonkeyPatch, public: bool) -> None:
    context = _context()
    lease = SessionOperationLease(_Authority(), context, lease_seconds=30, renew_interval_seconds=10)
    impostor = _CoercingImpostor()
    operation_id = str(uuid4())
    running = ComposerOperationRunning(ComposerOperationClaim(UUID(context.fence.session_id), operation_id, "claim", 1), context)
    turn = ComposerTurnInput(
        UUID(context.fence.session_id),
        operation_id,
        "compose_message",
        "actor",
        SendMessageRequest(content="request", operation_id=operation_id, state_id=None),
        None,
        30.0,
    )
    observation = ComposerTurnObservation(required_work=impostor)
    try:
        with monkeypatch.context() as patch:
            allocations = _no_task_allocations(patch)
            entry = composer_turn.run_composer_turn if public else composer_turn._run_composer_turn
            with pytest.raises(AuditIntegrityError, match="owned required-work coordinator"):
                await entry(
                    object(),
                    turn,
                    lease=lease,
                    running=running,
                    request_lifecycle=object(),
                    observation=observation,
                    budget_anchor=ComposerBudgetAnchor(30.0, 0.0),
                )
            assert allocations == []
            _assert_untouched(impostor)
    finally:
        await lease.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_public_turn_keeps_actual_owner_and_original_body_failure(monkeypatch: pytest.MonkeyPatch, grouped: bool) -> None:
    context = _context()
    coordinator = _coordinator(context)
    lease = SessionOperationLease(_Authority(), context, lease_seconds=30, renew_interval_seconds=10)
    body_error = RuntimeError("controlled body failure")
    cancellation = asyncio.CancelledError("controlled original cancellation")
    original = BaseExceptionGroup("controlled originals", [body_error, cancellation]) if grouped else body_error
    observation = ComposerTurnObservation(required_work=coordinator)

    async def body(*args: object, **kwargs: object) -> None:
        assert lease.required_work is coordinator
        assert observation.required_work is coordinator
        raise original

    monkeypatch.setattr(composer_turn, "_run_composer_turn", body)
    try:
        with pytest.raises(BaseException) as caught:
            await composer_turn.run_composer_turn(
                object(),
                object(),
                lease=lease,
                running=object(),
                request_lifecycle=object(),
                observation=observation,
                budget_anchor=ComposerBudgetAnchor(30.0, 0.0),
            )
        assert caught.value is original
    finally:
        await lease.close()
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_kind", ["inner", "wrapper", "auto"])
@pytest.mark.parametrize("wrong_context", [False, True])
async def test_pipeline_foreign_or_wrong_context_refused_before_authority_or_dispatch(
    monkeypatch: pytest.MonkeyPatch, entry_kind: str, wrong_context: bool
) -> None:
    context = _context()
    impostor = _CoercingImpostor()
    coordinator = _coordinator(_context()) if wrong_context else impostor
    allocations = _no_task_allocations(monkeypatch)
    common = {
        "services": object(),
        "user_id": "actor",
        "session_operation_context": context,
        "commit_timeout_seconds": 30.0,
        "required_work": coordinator,
    }
    with pytest.raises(AuditIntegrityError):
        if entry_kind == "auto":
            await pipeline_settlement.settle_auto_commit_intent(
                **common, service=object(), session_id=uuid4(), intent=object(), composer_meta=None, telemetry_source="compose"
            )
        else:
            entry = (
                pipeline_settlement._settle_pipeline_proposal_under_compose_lock
                if entry_kind == "inner"
                else pipeline_settlement.settle_pipeline_proposal_under_compose_lock
            )
            await entry(**common, authority=object(), draft_hash="draft")
    assert allocations == []
    _assert_untouched(impostor)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [False, True])
async def test_inner_proposal_actual_owner_preserves_existing_proposal_and_stale_hash_refusals(mismatch: bool) -> None:
    context = _context()
    proposal_id = uuid4()
    coordinator = RequiredWorkCoordinator(
        RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE,
            context,
            str(uuid4()),
            1,
            proposal_id=str(uuid4()) if mismatch else str(proposal_id),
            tool_call_id="tool",
        )
    )
    authority = SimpleNamespace(row=SimpleNamespace(id=proposal_id, tool_call_id="tool"), proposal=SimpleNamespace(draft_hash="bound"))
    if mismatch:
        with pytest.raises(RuntimeError, match="not bound to the exact proposal"):
            await pipeline_settlement._settle_pipeline_proposal_under_compose_lock(
                services=SimpleNamespace(session_service=object()),
                user_id="actor",
                authority=authority,
                draft_hash="stale",
                session_operation_context=context,
                commit_timeout_seconds=30.0,
                required_work=coordinator,
            )
    else:
        with pytest.raises(HTTPException) as caught:
            await pipeline_settlement._settle_pipeline_proposal_under_compose_lock(
                services=SimpleNamespace(session_service=object()),
                user_id="actor",
                authority=authority,
                draft_hash="stale",
                session_operation_context=context,
                commit_timeout_seconds=30.0,
                required_work=coordinator,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == "The pipeline proposal draft hash is stale or mismatched."
    coordinator.assert_completed()


def test_supported_telemetry_installation_subclass_keeps_inherited_reserve_abi() -> None:
    from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner
    from elspeth.web.operator_telemetry_installation import OwnedTestTelemetryInstallation, TelemetryInstallation

    class SupportedInstallation(OwnedTestTelemetryInstallation):
        def get_provider(self) -> object:
            return self.provider_slot

    installation = SupportedInstallation()
    owner = OperatorTelemetryCleanupOwner(installation=installation)
    assert owner.installation is installation
    assert installation.reserve.__func__ is TelemetryInstallation.reserve
    assert installation.reserve(owner) is None
    assert installation.record is not None and installation.record.owner is owner
    installation.finish_cleanup(owner, successful=True)
    assert installation.record.state == "closed"
