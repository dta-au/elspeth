"""Closed source registry, nominal authority and deterministic failure evidence."""

import asyncio
from concurrent.futures import Future
from dataclasses import replace
from itertools import permutations
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.required_work import (
    SOURCE_MAPPING,
    ComposerFailureReceipt,
    ComposerRequiredStage,
    OwnedCompletionWitness,
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkBinding,
    RequiredWorkCoordinator,
    RequiredWorkIncomplete,
    RequiredWorkRole,
    RequiredWorkSource,
    RequiredWorkSubphase,
    make_required_work_key,
    reduce_composer_failures,
    required_failure_leaves,
)


@pytest.fixture
def authority():
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "private-token", 1), SessionOperationKind.COMPOSE)
    return RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1)


def test_explicit_binding_validates_actual_context_without_task_local_lookup(authority):
    binding = RequiredWorkBinding(RequiredWorkCoordinator(authority), 0, 0, RequiredWorkRole.TURN)
    binding.validate_context(authority.context)
    with pytest.raises(AuditIntegrityError):
        binding.validate_context(None)
    foreign_context = SessionOperationContext(
        SessionOperationFence(authority.context.fence.session_id, str(uuid4()), "foreign-token", 2), SessionOperationKind.COMPOSE
    )
    with pytest.raises(AuditIntegrityError):
        binding.validate_context(foreign_context)


@pytest.mark.asyncio
async def test_explicit_binding_is_independent_across_concurrent_children(authority):
    parent = RequiredWorkCoordinator(authority)
    child = parent.for_proposal(str(uuid4()), "provider-tool-call")
    parent_binding = RequiredWorkBinding(parent, 0, 0, RequiredWorkRole.TURN)
    child_binding = RequiredWorkBinding(child, 0, 1, RequiredWorkRole.TURN)

    async def observe(binding):
        await asyncio.sleep(0)
        binding.validate_context(authority.context)
        return binding.coordinator

    parent_seen, child_seen = await asyncio.gather(observe(parent_binding), observe(child_binding))
    assert parent_seen is parent
    assert child_seen is child
    assert parent_binding.coordinator is parent


@pytest.mark.parametrize("source", tuple(RequiredWorkSource))
def test_each_source_has_exact_plan_mapping(authority, source):
    expected_stages = (
        0,
        0,
        1,
        1,
        2,
        2,
        2,
        3,
        3,
        3,
        4,
        4,
        5,
        5,
        5,
        5,
        5,
        5,
        5,
        6,
        6,
        6,
        7,
        7,
        8,
        8,
        9,
        9,
        10,
        10,
        11,
        11,
        11,
        12,
        12,
        12,
        12,
        12,
        12,
        13,
        13,
        14,
        14,
        0,
        6,
        6,
        6,
        11,
        11,
        11,
        11,
        7,
        7,
    )
    expected_phases = (
        2,
        5,
        2,
        5,
        1,
        2,
        5,
        2,
        5,
        2,
        2,
        5,
        2,
        5,
        2,
        5,
        2,
        5,
        2,
        2,
        5,
        0,
        2,
        5,
        2,
        5,
        2,
        5,
        5,
        2,
        0,
        0,
        0,
        2,
        5,
        4,
        5,
        2,
        5,
        2,
        2,
        0,
        2,
        1,
        2,
        5,
        2,
        7,
        6,
        6,
        6,
        2,
        5,
    )
    assert len(RequiredWorkSource) == len(SOURCE_MAPPING) == 53
    key = make_required_work_key(authority, source)
    assert (key.stage.value, key.subphase.value) == (expected_stages[source.value], expected_phases[source.value])
    assert key.invocation_ordinal == source.value * (source.value + 1) // 2
    with pytest.raises(AuditIntegrityError):
        replace(key, stage=ComposerRequiredStage((key.stage.value + 1) % 15))


def test_authority_scope_nullability_and_nominal_kind(authority):
    with pytest.raises(AuditIntegrityError):
        replace(authority, claim_attempt=None)
    with pytest.raises(AuditIntegrityError):
        replace(authority, durable_operation_id=None)
    proposal = replace(authority.context, operation_kind=SessionOperationKind.PROPOSAL)
    manual = RequiredWorkAuthority(RequiredAuthorityKind.MANUAL_PROPOSAL, proposal, proposal_id=str(uuid4()), invocation_id=str(uuid4()))
    assert manual.durable_operation_id is manual.claim_attempt is None
    sync = RequiredWorkAuthority(
        RequiredAuthorityKind.SYNCHRONOUS_COMPOSE,
        authority.context,
        invocation_id=str(uuid4()),
        tool_call_id="backend_auto_surface:" + str(uuid4()),
    )
    assert sync.tool_call_id.startswith("backend_auto_surface:")
    with pytest.raises(AuditIntegrityError):
        replace(manual, context=authority.context)
    with pytest.raises(AuditIntegrityError):
        replace(sync, context=proposal)


def _receipt(authority, source, error):
    key = make_required_work_key(authority, source)
    return ComposerFailureReceipt(key, error, required_failure_leaves(error), OwnedCompletionWitness(key))


def test_collision_arrival_and_group_order_retain_originals(authority):
    audit = AuditIntegrityError("PRIVATE AUDIT")
    sql = OperationalError("PRIVATE SQL", {}, Exception("PRIVATE PARAM"))
    accounting = ComposerOwnedSettlementFailure()
    roots = (audit, sql, accounting)
    sources = (RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL, RequiredWorkSource.INGRESS_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_SQL)
    receipts = tuple(_receipt(authority, source, error) for source, error in zip(sources, roots, strict=True))
    expected_key = receipts[0].key
    for ordered in permutations(receipts):
        reduced = reduce_composer_failures(ordered)
        assert reduced.winner.key == expected_key
        assert reduced.winner.original_root is audit
        assert {id(root) for root in reduced.original_roots} == {id(root) for root in roots}
        assert reduced.project(request_id="r", timeout_seconds=10).http_status == 500
    for ordered in permutations((audit, sql)):
        group = ExceptionGroup("ORIGINAL", list(ordered))
        reduced = reduce_composer_failures((_receipt(authority, sources[0], group),))
        assert reduced.winner.original_root is group
        assert reduced.witnesses == (audit,)


def test_same_category_stage_tie_and_public_body_conflict(authority):
    first, second = HTTPException(409, "first"), HTTPException(422, "second")
    a = _receipt(authority, RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER, first)
    b = _receipt(authority, RequiredWorkSource.OPERATION_TERMINAL_PROJECTION, second)
    for receipts in ((a, b), (b, a)):
        assert reduce_composer_failures(receipts).winner is a
    for leaves in ((first, second), (second, first)):
        root = ExceptionGroup("bodies", list(leaves))
        reduction = reduce_composer_failures((_receipt(authority, RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER, root),))
        assert reduction.project(request_id="r", timeout_seconds=10).http_status == 500


def test_duplicates_mixed_scopes_and_actual_completion_barrier(authority):
    coordinator = RequiredWorkCoordinator(authority)
    ticket = coordinator.reserve(RequiredWorkSource.INGRESS_SQL)
    with pytest.raises(AuditIntegrityError):
        coordinator.reserve(RequiredWorkSource.INGRESS_SQL)
    future = Future()
    ticket.bind_future(future)
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.prepare_lease_release()
    with pytest.raises(RequiredWorkIncomplete):
        ticket.observe_actual_outcome()
    error = OperationalError("sql", {}, Exception())
    future.set_exception(error)
    ticket.observe_actual_outcome()
    ticket.observe_actual_outcome()
    assert ticket.receipts()[0].original_root is error
    release = coordinator.prepare_lease_release()
    assert release.key.stage is ComposerRequiredStage.LEASE_CLOSE
    with pytest.raises(AuditIntegrityError):
        coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL)
    receipt = ticket.receipts()[0]
    with pytest.raises(AuditIntegrityError):
        reduce_composer_failures((receipt, receipt))
    foreign = _receipt(replace(authority, durable_operation_id=str(uuid4())), RequiredWorkSource.INGRESS_SQL, error)
    with pytest.raises(AuditIntegrityError):
        reduce_composer_failures((receipt, foreign))


@pytest.mark.parametrize("left,right", [(left, right) for left in range(11) for right in range(left + 1, 11)])
def test_every_closed_category_pair_in_both_arrival_orders(authority, left, right):
    import asyncio
    import errno

    from sqlalchemy.exc import SQLAlchemyError

    from elspeth.web.sessions.composer_operations import COMPOSER_CANCEL_REQUESTED, COMPOSER_DEADLINE, COMPOSER_SHUTDOWN

    errors = (
        AuditIntegrityError("private"),
        OperationalError("private", {}, Exception("private")),
        OSError(errno.ENOSPC, "private"),
        SQLAlchemyError("private"),
        ComposerOwnedSettlementFailure(),
        ComposerOwnedSettlementFailure(),
        asyncio.CancelledError(COMPOSER_CANCEL_REQUESTED),
        asyncio.CancelledError(COMPOSER_DEADLINE),
        asyncio.CancelledError(COMPOSER_SHUTDOWN),
        HTTPException(422, {"error_type": "typed", "detail": "safe"}),
        ValueError("private"),
    )
    sources = (
        RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL,
        RequiredWorkSource.INGRESS_SQL,
        RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
        RequiredWorkSource.RECOVERY_PARTIAL_STATE_SQL,
        RequiredWorkSource.PROVIDER_SETTLEMENT_SQL,
        RequiredWorkSource.LEASE_CLOSE,
        RequiredWorkSource.DURABLE_STOP_SIGNAL,
        RequiredWorkSource.DEADLINE_SIGNAL,
        RequiredWorkSource.LOCAL_CANCELLATION_SIGNAL,
        RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER,
        RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER,
    )
    ranks = (10, 20, 30, 40, 50, 51, 60, 70, 80, 90, 100)
    a, b = (_receipt(authority, sources[index], errors[index]) for index in (left, right))
    forward = reduce_composer_failures((a, b))
    reverse = reduce_composer_failures((b, a))
    assert forward.winner is reverse.winner is a
    assert forward.category_rank == ranks[left]
    assert forward.project(request_id="r", timeout_seconds=10) == reverse.project(request_id="r", timeout_seconds=10)
    assert {id(root) for root in forward.original_roots} == {id(errors[left]), id(errors[right])}


def test_audit_metadata_group_conflict_is_order_independent(authority):
    from elspeth.contracts.errors import FailedTurnMetadata

    first = AuditIntegrityError("private", failed_turn=FailedTurnMetadata(str(uuid4()), 1, 0))
    second = AuditIntegrityError("private", failed_turn=FailedTurnMetadata(str(uuid4()), 2, 1))
    results = []
    for leaves in ((first, second), (second, first)):
        root = ExceptionGroup("original", list(leaves))
        receipt = _receipt(authority, RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL, root)
        reduced = reduce_composer_failures((receipt,))
        assert reduced.winner.original_root is root
        assert {id(leaf) for leaf in reduced.witnesses} == {id(first), id(second)}
        results.append(reduced.project(request_id="r", timeout_seconds=10))
    assert results[0] == results[1]
    assert results[0].body["diagnostic"] == "failed_turn_metadata_conflict"
    assert "failed_turn" not in results[0].body


def test_submission_setup_provenance_refuses_foreign_and_nonsql(authority):
    from elspeth.web.required_work import RequiredWorkKey

    origin = make_required_work_key(
        authority, RequiredWorkSource.INGRESS_SQL, transition_ordinal=1, semantic_ordinal=2, recurrence_ordinal=3
    )
    key = RequiredWorkKey(
        authority,
        ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD,
        RequiredWorkSubphase.PRODUCER,
        1,
        2,
        origin.invocation_ordinal,
        0,
        origin,
    )
    assert key.source is RequiredWorkSource.CUSTODY_SUBMISSION_SETUP
    for bad in (
        replace(origin, authority=replace(authority, durable_operation_id=str(uuid4()))),
        make_required_work_key(authority, RequiredWorkSource.INGRESS_PROJECTION),
    ):
        with pytest.raises(AuditIntegrityError):
            replace(key, originating_key=bad)
    with pytest.raises(AuditIntegrityError):
        replace(key, semantic_ordinal=3)
    with pytest.raises(AuditIntegrityError):
        replace(key, originating_key=object())


def test_custody_failure_and_actual_sql_result_have_separate_receipts(authority):
    coordinator = RequiredWorkCoordinator(authority)
    ticket = coordinator.reserve(RequiredWorkSource.INGRESS_SQL)
    submission = RuntimeError("original submission")
    ticket.observe_submission_unknown(submission)
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.prepare_lease_release()
    actual = Future()
    ticket.bind_future(actual)
    sql = OperationalError("original SQL", {}, Exception())
    actual.set_exception(sql)
    ticket.observe_actual_outcome()
    receipts = ticket.receipts()
    assert len(receipts) == 2
    assert receipts[0].original_root is sql
    assert receipts[1].original_root is submission
    assert receipts[1].key.originating_key is ticket.key
    assert reduce_composer_failures(receipts).winner is receipts[0]


def test_proposal_child_scope_retains_exact_job_and_blocks_parent_release(authority):
    parent = RequiredWorkCoordinator(authority)
    proposal_id = str(uuid4())
    child = parent.for_proposal(proposal_id, "vendor_tool-string")
    assert child.authority.context is authority.context
    assert child.authority.durable_operation_id == authority.durable_operation_id
    assert child.authority.claim_attempt == authority.claim_attempt
    assert child.authority.proposal_id == proposal_id
    assert child.authority.tool_call_id == "vendor_tool-string"
    assert parent.for_proposal(proposal_id, "vendor_tool-string") is child
    with pytest.raises(AuditIntegrityError):
        parent.for_proposal(proposal_id, "other")
    ticket = child.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
    actual = Future()
    ticket.bind_future(actual)
    with pytest.raises(RequiredWorkIncomplete):
        parent.prepare_lease_release()
    actual.set_result(object())
    ticket.observe_actual_outcome()
    parent.prepare_lease_release()
    with pytest.raises(AuditIntegrityError):
        parent.for_proposal(str(uuid4()), "later")


def test_registered_child_accounting_rank_survives_parent_scope_and_order(authority):
    parent = RequiredWorkCoordinator(authority)
    child, producer = parent.begin_proposal_child(str(uuid4()), "vendor-call", transition_ordinal=0, semantic_ordinal=0)
    fault = ComposerOwnedSettlementFailure()
    accounting = child.reserve(RequiredWorkSource.PROVIDER_SETTLEMENT_SQL)
    accounting.complete_without_submission(fault)
    stop = parent.reserve(RequiredWorkSource.DURABLE_STOP_SIGNAL)
    stop.complete_owned(asyncio.CancelledError())
    parent.complete_proposal_child(child, producer, fault)
    receipts = parent.failure_receipts()
    for ordered in (receipts, tuple(reversed(receipts))):
        result = reduce_composer_failures(ordered)
        assert result.category_rank == 50
        assert result.winner.key is producer.key
        assert result.winner.original_root is fault
        assert result.winner.child_outcome.child_receipts[0].key is accounting.key
        assert result.witnesses == (fault,)


def test_child_outcome_refuses_incomplete_foreign_root_and_forged_receipt(authority):
    parent = RequiredWorkCoordinator(authority)
    child, producer = parent.begin_proposal_child(str(uuid4()), "vendor-call", transition_ordinal=0, semantic_ordinal=0)
    ticket = child.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL)
    with pytest.raises(RequiredWorkIncomplete):
        parent.complete_proposal_child(child, producer)
    fault = OperationalError("private SQL", {}, RuntimeError("private cause"))
    ticket.complete_without_submission(fault)
    with pytest.raises(AuditIntegrityError):
        parent.complete_proposal_child(child, producer, RuntimeError("dropped child"))
    with pytest.raises(AuditIntegrityError):
        parent.complete_proposal_child(RequiredWorkCoordinator(authority), producer, fault)
    parent.complete_proposal_child(child, producer, fault)
    receipt = parent.failure_receipts()[0]
    forged = replace(receipt, child_outcome=replace(receipt.child_outcome, original_root=RuntimeError("replacement")))
    with pytest.raises(AuditIntegrityError):
        reduce_composer_failures((forged,))
    forged = replace(receipt, original_root=RuntimeError("replacement"))
    with pytest.raises(AuditIntegrityError):
        reduce_composer_failures((forged,))


def test_child_unowned_sdk_cause_is_not_promoted(authority):
    parent = RequiredWorkCoordinator(authority)
    child, producer = parent.begin_proposal_child(str(uuid4()), "vendor-call", transition_ordinal=0, semantic_ordinal=0)
    sdk = RuntimeError("SDK weather")
    sdk.__cause__ = OperationalError("hidden SQL", {}, RuntimeError())
    child.reserve(RequiredWorkSource.PIPELINE_PUBLICATION_SQL).complete_without_submission(sdk)
    parent.complete_proposal_child(child, producer, sdk)
    stop = parent.reserve(RequiredWorkSource.DURABLE_STOP_SIGNAL)
    stop.complete_owned(asyncio.CancelledError())
    assert reduce_composer_failures(parent.failure_receipts()).category_rank == 60


@pytest.mark.parametrize("source", tuple(RequiredWorkSource))
def test_only_actual_bound_healthy_renewal_may_remain_pending_at_settlement(authority, source):
    coordinator = RequiredWorkCoordinator(authority)
    ticket = coordinator.reserve(source)
    actual = Future()
    ticket.bind_future(actual)
    if source is RequiredWorkSource.LEASE_RENEWAL:
        assert coordinator.settlement_failure_receipts() == ()
    else:
        with pytest.raises(RequiredWorkIncomplete):
            coordinator.settlement_failure_receipts()
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.prepare_lease_release()
    actual.set_result(None)
    ticket.observe_actual_outcome()
    assert coordinator.settlement_failure_receipts() == ()


def test_unknown_renewal_is_not_healthy_and_observed_fault_remains_evidence(authority):
    coordinator = RequiredWorkCoordinator(authority)
    unknown = coordinator.reserve(RequiredWorkSource.LEASE_RENEWAL)
    fault = OperationalError("renewal SQL", {}, RuntimeError())
    unknown.observe_submission_unknown(fault)
    with pytest.raises(RequiredWorkIncomplete):
        coordinator.settlement_failure_receipts()
    unknown.complete_generation_joined(fault)
    receipts = coordinator.settlement_failure_receipts()
    assert receipts[0].original_root is fault
    assert reduce_composer_failures(receipts).category_rank == 20


def test_distinct_same_category_receipts_preserve_metadata_conflict(authority):
    from elspeth.contracts.errors import FailedTurnMetadata

    coordinator = RequiredWorkCoordinator(authority)
    first = AuditIntegrityError("first", failed_turn=FailedTurnMetadata(assistant_message_id=None, tool_calls_attempted=1))
    second = AuditIntegrityError("second", failed_turn=FailedTurnMetadata(assistant_message_id=None, tool_calls_attempted=2))
    coordinator.reserve(RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL).complete_without_submission(first)
    coordinator.reserve(RequiredWorkSource.TITLE_ACCOUNTING_SQL).complete_without_submission(second)
    for receipts in (coordinator.failure_receipts(), tuple(reversed(coordinator.failure_receipts()))):
        error = reduce_composer_failures(receipts).project(request_id="request", timeout_seconds=10)
        assert error.body["diagnostic"] == "failed_turn_metadata_conflict"


@pytest.mark.parametrize("owned", [None, asyncio.CancelledError, ComposerOwnedSettlementFailure])
def test_generation_operational_rank_and_exact_safe_envelope(authority, owned):
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    leaf = RequiredGenerationUnavailable("private generation diagnostic")
    root = leaf if owned is None else owned()
    if root is not leaf:
        root.__cause__ = leaf
    receipt = _receipt(authority, RequiredWorkSource.INGRESS_SQL, root)
    reduction = reduce_composer_failures((receipt,))
    assert reduction.category_rank == 20
    assert reduction.winner.original_root is root
    assert reduction.witnesses == (leaf,)
    error = reduction.project(request_id="correlation", timeout_seconds=10)
    assert error.http_status == 503
    assert error.body == {
        "detail": "Database is currently unavailable. Please retry in a moment.",
        "error_type": "database_unavailable",
        "request_id": "correlation",
    }


@pytest.mark.parametrize("unowned", [RuntimeError, TimeoutError, ValueError])
def test_generation_unowned_cause_and_message_never_promote(authority, unowned):
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    root = unowned("generation shutdown database timeout")
    root.__cause__ = RequiredGenerationUnavailable("private")
    reduction = reduce_composer_failures((_receipt(authority, RequiredWorkSource.INGRESS_SQL, root),))
    assert reduction.category_rank == 100
    assert reduction.project(request_id=None, timeout_seconds=10).http_status == 500
    assert reduction.original_roots == (root,)


@pytest.mark.parametrize(
    "other_source,other,expected",
    [
        (RequiredWorkSource.REQUIRED_UNWIND_AUDIT_SQL, AuditIntegrityError("private"), 10),
        (RequiredWorkSource.TITLE_ACCOUNTING_SQL, ComposerOwnedSettlementFailure(), 20),
        (RequiredWorkSource.DURABLE_STOP_SIGNAL, asyncio.CancelledError(), 20),
        (RequiredWorkSource.DEADLINE_SIGNAL, TimeoutError(), 20),
        (RequiredWorkSource.LOCAL_CANCELLATION_SIGNAL, asyncio.CancelledError(), 20),
        (RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER, RuntimeError("generation database"), 20),
    ],
)
def test_generation_collisions_keep_original_groups_and_reverse_order(authority, other_source, other, expected):
    from elspeth.web.required_executor import RequiredGenerationUnavailable

    nominal = RequiredGenerationUnavailable("private")
    group = BaseExceptionGroup("original group", [nominal, ValueError("private")])
    generation = _receipt(authority, RequiredWorkSource.INGRESS_SQL, group)
    secondary = _receipt(authority, other_source, other)
    forward = reduce_composer_failures((generation, secondary))
    reverse = reduce_composer_failures((secondary, generation))
    assert forward.category_rank == reverse.category_rank == expected
    assert forward.project(request_id="r", timeout_seconds=10) == reverse.project(request_id="r", timeout_seconds=10)
    assert any(root is group for root in forward.original_roots)
    assert group.exceptions[0] is nominal
    assert forward.secondary_receipts == reverse.secondary_receipts


def test_generation_negative_controls_detect_overbroad_category_mutation(authority, monkeypatch):
    from elspeth.web import required_work

    original = required_work._category

    def broad(leaf, key):
        return (20, 0) if isinstance(leaf, RuntimeError) else original(leaf, key)

    monkeypatch.setattr(required_work, "_category", broad)
    with pytest.raises(AssertionError):
        test_generation_unowned_cause_and_message_never_promote(authority, RuntimeError)


def test_generation_negative_controls_detect_unowned_cause_traversal_mutation(authority, monkeypatch):
    from elspeth.web import required_work

    original = required_work.required_failure_leaves

    def broad(root):
        return original(root.__cause__) if root.__cause__ is not None else original(root)

    monkeypatch.setattr(required_work, "required_failure_leaves", broad)
    # The receipt provenance guard can reject the mutation before the public
    # category assertion sees the incorrectly promoted cause.
    with pytest.raises((AssertionError, AuditIntegrityError)):
        test_generation_unowned_cause_and_message_never_promote(authority, RuntimeError)
