"""Checkpoint Future/handoff custody and actual P4 publication ownership."""

import asyncio
import json
import threading
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web import async_workers
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation_codec import PrivateChunk
from elspeth.web.composer.continuation_live_p4 import ContinuationLiveP4
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkSource,
    RequiredWorkTicket,
)
from elspeth.web.sessions._persist_payload import AuditOutcome
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.continuation_p4_application_store import CommittedContinuationP4
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


@pytest.fixture
def coordinator():
    context = SessionOperationContext(SessionOperationFence(str(uuid4()), str(uuid4()), "private-token", 1), SessionOperationKind.COMPOSE)
    return RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.DURABLE_COMPOSE, context, str(uuid4()), 1))


def _checkpoint_pair(coordinator):
    return coordinator.reserve_pair(
        RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
        RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION,
        transition_ordinal=0,
        semantic_ordinal=0,
    )


@pytest.mark.asyncio
async def test_checkpoint_validator_requires_actual_registered_future_and_exact_handoff(coordinator):
    ticket, projection = _checkpoint_pair(coordinator)
    value = ["actual physical callback value"]
    outcome = await run_required_sql_finish_once(ticket, lambda: value)
    assert type(outcome) is RequiredSQLReturned and outcome.value is value
    assert ticket._finish_once_handoff is outcome and ticket._future.done() and ticket._future.result() is value
    coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
    with pytest.raises(AuditIntegrityError, match="unsupported source"):
        coordinator.validate_continuation_sql_outcome(
            ticket=ticket, expected_source=RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, actual_outcome=outcome
        )
    for counterfeit in (RequiredSQLReturned(value, ()), RequiredSQLReturned(list(value), ())):
        with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
            coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=counterfeit)
    other, other_projection = _checkpoint_pair(coordinator)
    other_outcome = await run_required_sql_finish_once(other, lambda: value)
    coordinator.validate_compose_checkpoint_sql_outcome(ticket=other, actual_outcome=other_outcome)
    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_compose_checkpoint_sql_outcome(ticket=other, actual_outcome=outcome)
    copied = RequiredWorkTicket(ticket.key, coordinator=coordinator)
    foreign = RequiredWorkCoordinator(coordinator.authority)
    for selected_coordinator, selected_ticket in ((coordinator, copied), (foreign, ticket)):
        with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
            selected_coordinator.validate_compose_checkpoint_sql_outcome(ticket=selected_ticket, actual_outcome=outcome)
    wrong_source = coordinator.reserve(RequiredWorkSource.CONTINUATION_READ_SQL)
    wrong_outcome = await run_required_sql_finish_once(wrong_source, lambda: value)
    with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
        coordinator.validate_compose_checkpoint_sql_outcome(ticket=wrong_source, actual_outcome=wrong_outcome)
    projection.complete_without_submission()
    other_projection.complete_without_submission()
    coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("no_submission", (False, True))
async def test_checkpoint_validator_preserves_actual_raised_or_no_submit_failure(coordinator, monkeypatch, no_submission):
    ticket, projection = _checkpoint_pair(coordinator)
    error = RuntimeError("original checkpoint physical/setup failure")
    calls = []

    def fail():
        calls.append("entered")
        raise error

    if no_submission:
        monkeypatch.setattr(async_workers, "_get_shared_executor", fail)
        outcome = await run_required_sql_finish_once(ticket, lambda: calls.append("must not execute"))
    else:
        outcome = await run_required_sql_finish_once(ticket, fail)
    assert type(outcome) is RequiredSQLRaised and outcome.error is error
    assert calls == ["entered"] and ticket._finish_once_handoff is outcome
    assert ticket._no_submission is no_submission
    coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
    for counterfeit in (RequiredSQLRaised(error, ()), RequiredSQLRaised(RuntimeError(str(error)), ())):
        with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
            coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=counterfeit)
    assert outcome.error is error and ticket.errors == (error,)
    projection.complete_without_submission()
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_checkpoint_validator_cannot_drop_actual_deferred_cancellation(coordinator):
    ticket, projection = _checkpoint_pair(coordinator)
    entered, release = threading.Event(), threading.Event()
    value = object()

    def physical():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("checkpoint cancellation control was not released")
        return value

    task = asyncio.create_task(run_required_sql_finish_once(ticket, physical))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel("checkpoint after physical entry")
        await asyncio.sleep(0)
        release.set()
        outcome = await task
        assert type(outcome) is RequiredSQLReturned and outcome.value is value
        assert len(outcome.deferred_cancellations) == 1 and ticket._finish_once_handoff is outcome
        coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=outcome)
        for cancellations in ((), outcome.deferred_cancellations):
            with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
                coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLReturned(value, cancellations))
        assert outcome.deferred_cancellations[0].args == ("checkpoint after physical entry",)
    finally:
        release.set()
        if not task.done():
            await task
        projection.complete_without_submission()
    coordinator.assert_completed()


def test_checkpoint_validator_does_not_accept_unjoined_unknown_or_incomplete_work(coordinator):
    ticket, projection = _checkpoint_pair(coordinator)
    original = RuntimeError("actual unresolved submission custody")
    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLRaised(original, ()))
    ticket.observe_submission_unknown(original)
    with pytest.raises(AuditIntegrityError, match="actual finish-once carrier"):
        coordinator.validate_compose_checkpoint_sql_outcome(ticket=ticket, actual_outcome=RequiredSQLRaised(original, ()))
    assert not ticket.complete and ticket._submission_unknown and not ticket._generation_joined
    assert ticket._finish_once_handoff is None and ticket._custody_errors == [original]
    # No success or no-submit witness is invented; this deliberately unresolved
    # unit coordinator remains blocked from release and owns no physical Future.
    with pytest.raises(AuditIntegrityError):
        coordinator.prepare_lease_release()
    projection.complete_without_submission()


@pytest.mark.asyncio
@pytest.mark.parametrize("opposed", (False, True))
async def test_actual_p4_binds_current_checkpoint_and_refuses_a_coherent_prior_recurrence(operation_store, tmp_path, monkeypatch, opposed):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        earlier, earlier_projection = owner.work.reserve_pair(
            RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        # This is an actual finish-once Future control, not an AuditOutcome or
        # a fabricated business checkpoint. Actual P4 SQL below stays real.
        earlier_outcome = await run_required_sql_finish_once(earlier, object)
        owner.work.coordinator.validate_compose_checkpoint_sql_outcome(ticket=earlier, actual_outcome=earlier_outcome)
        earlier_projection.complete_without_submission()
        actual_bind, actual_accept = ContinuationLiveP4.bind_checkpoint_ticket, ContinuationLiveP4.accept_physical
        sessions = owner.service._require_sessions_service()
        actual_persist = type(sessions).persist_continuation_compose_turn
        bound, observed = [], []

        def bind(self, ticket):
            assert self._checkpoint_ticket is None
            with pytest.raises(AuditIntegrityError, match="precede physical submission"):
                actual_bind(self, earlier)
            copied = RequiredWorkTicket(ticket.key, coordinator=self.work.coordinator)
            with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
                actual_bind(self, copied)
            foreign_coordinator = RequiredWorkCoordinator(self.work.coordinator.authority)
            foreign = foreign_coordinator.reserve(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
            wrong_source, wrong_source_projection = self.work.reserve_pair(
                RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
            )
            wrong_scope = self.work.coordinator.reserve(
                RequiredWorkSource.COMPOSE_CHECKPOINT_SQL,
                transition_ordinal=self.work.transition_ordinal + 1,
                semantic_ordinal=self.work.semantic_ordinal,
            )
            try:
                for foreign_ticket in (foreign, wrong_source):
                    with pytest.raises(AuditIntegrityError, match="exact registered source authority"):
                        actual_bind(self, foreign_ticket)
                    assert not foreign_ticket.complete and foreign_ticket._future is None
                with pytest.raises(AuditIntegrityError, match="actual prepared scope"):
                    actual_bind(self, wrong_scope)
                assert not wrong_scope.complete and wrong_scope._future is None
            finally:
                foreign.complete_without_submission()
                wrong_source.complete_without_submission()
                wrong_source_projection.complete_without_submission()
                wrong_scope.complete_without_submission()
            actual_bind(self, ticket)
            assert self._checkpoint_ticket is ticket
            with pytest.raises(AuditIntegrityError, match="already bound or observed"):
                actual_bind(self, ticket)
            bound.append((self, ticket))

        async def persist(self, **kwargs):
            assert self is sessions and len(bound) == 1
            phase, ticket = bound[0]
            assert kwargs["required_work"] is ticket and kwargs["continuation"] is phase.commit
            assert kwargs["continuation_observation"] is phase.writer_observation
            assert ticket._future is None and not ticket.complete
            physical = await actual_persist(self, **kwargs)
            assert type(physical) is RequiredSQLReturned and type(physical.value) is CommittedContinuationP4
            assert type(physical.value.audit_outcome) is AuditOutcome
            assert ticket._finish_once_handoff is physical and ticket._result is physical.value
            observed.append(physical)
            return physical

        def accept(self, ticket, physical):
            assert bound == [(self, ticket)] and observed == [physical]
            if opposed:
                with pytest.raises(AuditIntegrityError, match="actual bound checkpoint ticket"):
                    actual_accept(self, earlier, earlier_outcome)
                assert self.physical is earlier_outcome and self._checkpoint_ticket is ticket
                assert ticket._finish_once_handoff is physical and ticket._result is physical.value
                # Preserve the original rejected input; this owner cannot
                # overwrite it with a favorable second admission.
                with pytest.raises(AuditIntegrityError, match="cannot replace"):
                    actual_accept(self, ticket, physical)
                raise AuditIntegrityError("opposed earlier checkpoint pair refused after actual P4 SQL")
            result = actual_accept(self, ticket, physical)
            assert result is physical.value.audit_outcome and self.physical is physical
            with pytest.raises(AuditIntegrityError, match="cannot replace"):
                actual_accept(self, ticket, physical)
            return result

        monkeypatch.setattr(ContinuationLiveP4, "bind_checkpoint_ticket", bind)
        monkeypatch.setattr(ContinuationLiveP4, "accept_physical", accept)
        monkeypatch.setattr(type(sessions), "persist_continuation_compose_turn", persist)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        selected = owner.runtime.source.selected
        if opposed:
            with pytest.raises(AuditIntegrityError, match="opposed earlier checkpoint pair refused"):
                await owner.finish_owed_prefix(selected)
        else:
            await owner.finish_owed_prefix(selected)
            assert owner.p4.publication is not None and owner.p4.application is not None
        assert len(bound) == len(observed) == 1 and len(sdk) == 1
        assert earlier.key.invocation_ordinal != bound[0][1].key.invocation_ordinal


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", (False, True))
async def test_actual_p4_bind_refusal_closes_only_its_unsubmitted_local_pair(operation_store, tmp_path, monkeypatch, cleanup_fails):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        earlier, earlier_projection = owner.work.reserve_pair(
            RequiredWorkSource.COMPOSE_CHECKPOINT_SQL, RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        earlier_outcome = await run_required_sql_finish_once(earlier, object)
        earlier_projection.complete_without_submission()
        foreign_coordinator = RequiredWorkCoordinator(owner.work.coordinator.authority)
        foreign = foreign_coordinator.reserve(RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        original = RuntimeError("original pre-await checkpoint bind refusal")
        cleanup = RuntimeError("opposed no-submission cleanup failure")
        actual_complete = RequiredWorkTicket.complete_without_submission
        reserved, physical_calls, cleanup_attempts = [], [], []

        def refuse_bind(self, ticket):
            projections = tuple(
                item
                for item in self.work.coordinator.tickets
                if item.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION and item is not earlier_projection
            )
            assert len(projections) == 1 and ticket._future is None and not ticket.complete
            reserved.append((self, ticket, projections[0]))
            raise original

        def complete(self, error=None):
            if reserved and self is reserved[0][1]:
                cleanup_attempts.append(error)
                if cleanup_fails:
                    raise cleanup
            return actual_complete(self, error)

        async def must_not_submit(self, **kwargs):
            physical_calls.append(kwargs)
            raise AssertionError("bind refusal reached physical checkpoint SQL")

        sessions = owner.service._require_sessions_service()
        monkeypatch.setattr(ContinuationLiveP4, "bind_checkpoint_ticket", refuse_bind)
        monkeypatch.setattr(RequiredWorkTicket, "complete_without_submission", complete)
        monkeypatch.setattr(type(sessions), "persist_continuation_compose_turn", must_not_submit)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        try:
            if cleanup_fails:
                with pytest.raises(BaseExceptionGroup) as caught:
                    await owner.finish_owed_prefix(owner.runtime.source.selected)
                assert caught.value.exceptions == (original, cleanup)
            else:
                with pytest.raises(RuntimeError) as caught:
                    await owner.finish_owed_prefix(owner.runtime.source.selected)
                assert caught.value is original
            assert len(reserved) == 1 and cleanup_attempts == [original] and not physical_calls
            phase, ticket, projection = reserved[0]
            assert ticket._future is None and ticket._finish_once_handoff is None and not ticket._submission_unknown
            assert phase.physical is None and phase.publication is None and phase.application is None
            if cleanup_fails:
                assert not ticket.complete and not projection.complete
            else:
                assert ticket.complete and ticket._no_submission and ticket.errors == (original,)
                assert projection.complete
            assert earlier._finish_once_handoff is earlier_outcome and earlier.complete
            assert not foreign.complete and foreign._future is None and foreign._finish_once_handoff is None
            assert len(sdk) == 1
        finally:
            # Keep the negative cleanup-failure control's private fixture tidy
            # without pretending the product established that closure.
            for _phase, ticket, projection in reserved:
                if not ticket.complete:
                    actual_complete(ticket, original)
                if not projection.complete:
                    actual_complete(projection)
            actual_complete(foreign)


def _canonical_object_size(canonical):
    """Measure exact serialized members, never return their retained values."""
    value = json.loads(canonical)
    if type(value) is not dict or canonical_json(value) != canonical:
        raise ValueError("size measurement requires the original canonical object")
    encoded = canonical.encode("utf-8")
    members = {}
    for key, item in value.items():
        encoded_value = canonical_json(item).encode("utf-8")
        members[key] = {
            "value_bytes": len(encoded_value),
            "member_bytes": len(canonical_json(key).encode("utf-8")) + 1 + len(encoded_value),
        }
    return {
        "bytes": len(encoded),
        "sha256": sha256(encoded).hexdigest(),
        "punctuation_bytes": 2 + max(0, len(members) - 1),
        "members": members,
    }


def _p4_evidence_size(canonical, source_chunks):
    """Decompose the actual produced envelope; no payload leaves this helper."""
    root = json.loads(canonical)
    entry = root["application_entry"]
    originals = {(chunk.reference.codec, chunk.reference.digest): chunk for chunk in source_chunks}
    return {
        "evidence": _canonical_object_size(canonical),
        "entry": _canonical_object_size(canonical_json(entry)),
        # Raw graph bytes differ from its escaped string value contribution in
        # entry.members.graph_json. Both measurements retain their real context.
        "graph": _canonical_object_size(entry["graph_json"]),
        "records": [_canonical_object_size(canonical_json(record)) for record in entry["applications"]],
        "original_dependencies": _original_dependency_sizes(
            tuple(
                (index, role, record[role], originals[(record[role]["codec"], record[role]["digest"])].canonical)
                for index, record in enumerate(entry["applications"])
                for role in ("response", "pending_batch", "prepared_input", "result")
            )
        ),
        "graph_syntax": _graph_syntax_counts(entry["graph_json"]),
        "diagnostic_work": "STDLIB_PARSE_UTF8_HASH_AND_DECOMPOSITION_EXCLUDED_FROM_PRODUCTION_COUNTERS",
    }


def _original_dependency_sizes(occurrences):
    """Diagnostic verification of existing inline bytes, never source admission."""
    unique = {}
    occurrence_bytes = 0
    for index, role, reference, original in occurrences:
        if role not in ("response", "pending_batch", "prepared_input", "result"):
            raise ValueError("dependency measurement lost its closed role")
        if type(reference) is not dict or set(reference) != {"codec", "digest", "byte_length"}:
            raise ValueError("dependency measurement lost its exact reference")
        codec, digest, bound = reference["codec"], reference["digest"], reference["byte_length"]
        if (
            codec not in ("composer.response.v1", "composer.p4-pending-batch.v1", "composer.prepared-p4.v1", "composer.owner-result.v1")
            or type(digest) is not str
            or type(bound) is not int
            or bound < 0
            or type(original) is not str
        ):
            raise ValueError("dependency measurement lost its nominal scalar bounds")
        key = codec, digest
        # A conflicting declared length is refused before any deduplication.
        if key in unique and unique[key]["byte_length"] != bound:
            raise ValueError("dependency measurement has conflicting lengths")
        encoded = original.encode("utf-8")
        actual_digest = sha256(encoded).hexdigest()
        if len(encoded) != bound or actual_digest != digest:
            raise ValueError("dependency measurement differs from original bytes")
        occurrence_bytes += len(encoded)
        if key not in unique:
            unique[key] = {
                "codec": codec,
                "digest": digest,
                "byte_length": bound,
                "actual_utf8_bytes": len(encoded),
                "actual_sha256": actual_digest,
                "roles": [],
            }
        unique[key]["roles"].append({"record_ordinal": index, "role": role})
    for item in unique.values():
        item["occurrences"] = len(item["roles"])
    return {
        "scope": "FOUR_DECLARED_RECORD_ROLES_ONLY_NOT_TOTAL_CLOSURE",
        "verified_originals": list(unique.values()),
        "unique_count": len(unique),
        "occurrence_count": len(occurrences),
        "role_occurrences": {
            role: sum(1 for _index, actual_role, _reference, _original in occurrences if actual_role == role)
            for role in ("response", "pending_batch", "prepared_input", "result")
        },
        "unique_original_bytes": sum(item["byte_length"] for item in unique.values()),
        "occurrence_original_bytes": occurrence_bytes,
        "diagnostic_hash_calls": len(occurrences),
        "diagnostic_utf8_bytes_hashed": occurrence_bytes,
    }


def _graph_syntax_counts(canonical):
    """Count only parsed source syntax; do not construct another owned graph."""
    root = json.loads(canonical)
    pending = [root]
    references = scalars = string_characters = 0
    while pending:
        value = pending.pop()
        if type(value) is dict:
            if set(value) == {"ref"}:
                references += 1
            elif set(value) == {"scalar"}:
                scalars += 1
                if type(value["scalar"]) is str:
                    string_characters += len(value["scalar"])
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)
    return {
        "nodes": len(root["nodes"]),
        "roots": len(root["roots"]),
        "reference_occurrences": references,
        "scalar_occurrences": scalars,
        "scalar_string_characters": string_characters,
        "scalar_string_utf8_bytes": "UNKNOWN_NOT_ENCODED_FOR_PRODUCTION_WORK",
    }


class _P4DiagnosticThreadState(threading.local):
    def __init__(self):
        self.depth = 0


class _P4WorkLedger:
    """Test-local counters around fixed real calls; no authority or DATA cache."""

    def __init__(self):
        self._lock = threading.Lock()
        self._diagnostic = _P4DiagnosticThreadState()
        self._enabled = False
        self._counts = {}

    def start(self):
        self._enabled = True

    def stop(self):
        self._enabled = False

    @contextmanager
    def diagnostic(self):
        self._diagnostic.depth += 1
        try:
            yield
        finally:
            self._diagnostic.depth -= 1

    def _add(self, role, field, amount=1):
        with self._lock:
            counts = self._counts[role]
            counts[field] = counts.get(field, 0) + amount

    def wrap(self, role, original, *, input_position=None, input_name=None, output_kind=None):
        if role in self._counts:
            raise ValueError("work measurement cannot replace an installed alias")
        self._counts[role] = {"entered": 0, "returned": 0, "raised": 0}

        def observed(*args, **kwargs):
            if not self._enabled or self._diagnostic.depth:
                return original(*args, **kwargs)
            self._add(role, "entered")
            if input_position is not None:
                text = args[input_position] if len(args) > input_position else kwargs.get(input_name)
                if type(text) is str:
                    self._add(role, "input_characters", len(text))
                else:
                    self._add(role, "unknown_input_extent")
            try:
                value = original(*args, **kwargs)
            except BaseException:
                self._add(role, "raised")
                raise
            self._add(role, "returned")
            if output_kind == "bytes" and type(value) is bytes:
                self._add(role, "actual_returned_buffer_bytes", len(value))
            elif output_kind == "text" and type(value) is str:
                self._add(role, "output_characters", len(value))
            elif output_kind == "chunk" and type(value) is PrivateChunk:
                # The original function returned only after its own verification.
                # This is its verified bound, not a new observer hash/UTF8 pass.
                self._add(role, "verified_returned_chunk_bytes", value.reference.byte_length)
            elif output_kind is not None:
                self._add(role, "unknown_output_extent")
            return value

        return observed

    def report(self):
        with self._lock:
            counts = {role: dict(value) for role, value in self._counts.items()}
        return {
            "fixed_aliases": counts,
            "scope": "ONLY_NAMED_ALIASES_DURING_ACTUAL_AWAIT_OVERLAPPING_LEVELS_NOT_ADDITIVE",
            "unknowns": (
                "GLOBAL_W_AND_ALL_SQL",
                "UTF8_EXTENT_OF_STR_ONLY_PARSE_GRAPH_CALLS",
                "OTHER_HASHES_CREDENTIAL_TRAVERSAL_AND_PYDANTIC_PARSERS",
                "HASH_INVOCATIONS_ARE_NOT_VERIFICATION_ALIAS_CALL_COUNTS",
                "CPU_TIME_AND_PRODUCTION_PERFORMANCE",
            ),
            "diagnostic_work_excluded": True,
        }


@pytest.mark.parametrize(
    ("canonical", "expected_bytes", "expected_members"),
    (
        ('{"a":1,"b":2}', 13, {"a": 5, "b": 5}),
        ('{"é":"雪"}', 12, {"é": 10}),
        ('{"a":"\\"\\\\\\n"}', 14, {"a": 12}),
    ),
)
def test_p4_evidence_size_instrument_counts_complete_utf8_and_escaping(canonical, expected_bytes, expected_members):
    measured = _canonical_object_size(canonical)
    assert measured["bytes"] == expected_bytes
    assert {key: item["member_bytes"] for key, item in measured["members"].items()} == expected_members
    assert measured["bytes"] == measured["punctuation_bytes"] + sum(item["member_bytes"] for item in measured["members"].values())
    assert measured["sha256"] == sha256(canonical.encode("utf-8")).hexdigest()
    # A value-only, character-only or unescaped measurement cannot satisfy the
    # whole-object accounting, including the actual UTF8 and quote/backslash case.
    assert measured["bytes"] > sum(item["value_bytes"] for item in measured["members"].values())
    with pytest.raises(ValueError, match="original canonical object"):
        _canonical_object_size('{"a": 1}')

    # These are byte-ledger calibrations, not produced business chunks/receipts.
    reference = {"codec": "composer.response.v1", "digest": sha256(canonical.encode("utf-8")).hexdigest(), "byte_length": expected_bytes}
    occurrences = ((0, "response", reference, canonical), (0, "result", reference, canonical))
    dependencies = _original_dependency_sizes(occurrences)
    assert dependencies["unique_count"] == 1 and dependencies["occurrence_count"] == 2
    assert dependencies["unique_original_bytes"] == expected_bytes
    assert dependencies["occurrence_original_bytes"] == 2 * expected_bytes
    assert dependencies["role_occurrences"] == {"response": 1, "pending_batch": 0, "prepared_input": 0, "result": 1}
    assert dependencies["verified_originals"][0]["occurrences"] == 2
    assert dependencies["verified_originals"][0]["roles"] == [
        {"record_ordinal": 0, "role": "response"},
        {"record_ordinal": 0, "role": "result"},
    ]
    different_codec = {**reference, "codec": "composer.owner-result.v1"}
    separate = _original_dependency_sizes((occurrences[0], (0, "result", different_codec, canonical)))
    assert separate["unique_count"] == 2 and separate["unique_original_bytes"] == 2 * expected_bytes
    for counterfeit in ({**reference, "digest": "0" * 64}, {**reference, "byte_length": expected_bytes + 1}):
        with pytest.raises(ValueError, match="differs from original bytes"):
            _original_dependency_sizes(((0, "response", counterfeit, canonical),))
    with pytest.raises(ValueError, match="differs from original bytes"):
        _original_dependency_sizes(((0, "response", reference, canonical + " "),))
    with pytest.raises(ValueError, match="conflicting lengths"):
        _original_dependency_sizes((occurrences[0], (0, "result", {**reference, "byte_length": expected_bytes + 1}, canonical)))

    # The repeated identity has the same declared and actual length: only
    # verification of this second occurrence can detect its changed digest.
    corrupt_second = canonical.replace("{", "[", 1)
    assert len(corrupt_second.encode("utf-8")) == expected_bytes
    assert sha256(corrupt_second.encode("utf-8")).hexdigest() != reference["digest"]
    with pytest.raises(ValueError, match=r"^dependency measurement differs from original bytes$"):
        _original_dependency_sizes((occurrences[0], (0, "result", reference, corrupt_second)))

    # Fixed source syntax only, never an owned business graph or DATA grant.
    # Three roots reference 0, 0, 1; node 1 references node 0 twice. Node 0
    # contains one two-character/five-UTF8-byte string and three nonstrings.
    graph_literal = (
        '{"nodes":[{"kind":"list","value":[{"scalar":"é雪"},{"scalar":7},{"scalar":false},{"scalar":null}]},'
        '{"kind":"tuple","value":[{"ref":0},{"ref":0}]}],"roots":[{"ref":0},{"ref":0},{"ref":1}]}'
    )
    expected_graph = {
        "nodes": 2,
        "roots": 3,
        "reference_occurrences": 5,
        "scalar_occurrences": 4,
        "scalar_string_characters": 2,
        "scalar_string_utf8_bytes": "UNKNOWN_NOT_ENCODED_FOR_PRODUCTION_WORK",
    }

    def require_graph_counts(measurement):
        assert measurement == expected_graph

    graph_counts = _graph_syntax_counts(graph_literal)
    require_graph_counts(graph_counts)
    missing_ref = graph_literal.replace('[{"ref":0},{"ref":0}]}', '[{"ref":0}]}', 1)
    doubled_ref = graph_literal.replace('[{"ref":0},{"ref":0}]}', '[{"ref":0},{"ref":0},{"ref":0}]}', 1)
    assert missing_ref != graph_literal and doubled_ref != graph_literal
    for opposed_graph in (missing_ref, doubled_ref):
        with pytest.raises(AssertionError):
            require_graph_counts(_graph_syntax_counts(opposed_graph))
    for field in ("nodes", "roots", "reference_occurrences", "scalar_occurrences", "scalar_string_characters"):
        for opposed_count in (0, 2 * expected_graph[field]):
            with pytest.raises(AssertionError):
                require_graph_counts({**graph_counts, field: opposed_count})
    with pytest.raises(AssertionError):
        require_graph_counts({**graph_counts, "scalar_string_characters": 5})

    def require_exact_delegation(factory, error=None):
        value, marker = object(), object()
        calls = []

        def original(text, *, marker):
            calls.append((text, marker))
            if error is not None:
                raise error
            return value

        wrapped = factory(original)
        returned = failure = None
        try:
            returned = wrapped("é", marker=marker)
        except BaseException as caught:
            failure = caught
        assert calls == [("é", marker)]
        if error is None:
            assert failure is None and returned is value
        else:
            assert failure is error

    ledger = _P4WorkLedger()
    ledger.start()
    require_exact_delegation(lambda original: ledger.wrap("returned", original))
    for ordinal, error in enumerate(
        (RuntimeError("original instrument error"), asyncio.CancelledError("original cancellation"), KeyboardInterrupt())
    ):
        require_exact_delegation(lambda original, role=f"raised_{ordinal}": ledger.wrap(role, original), error)
    ledger.stop()
    counts = ledger.report()["fixed_aliases"]
    assert counts["returned"] == {"entered": 1, "returned": 1, "raised": 0}
    assert all(counts[f"raised_{ordinal}"] == {"entered": 1, "returned": 0, "raised": 1} for ordinal in range(3))

    def skipped(original):
        return lambda *args, **kwargs: None

    def doubled(original):
        def invoke(*args, **kwargs):
            original(*args, **kwargs)
            return original(*args, **kwargs)

        return invoke

    def discarded(original):
        def invoke(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except BaseException:
                return None

        return invoke

    def replaced_cancellation(original):
        def invoke(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except asyncio.CancelledError:
                raise asyncio.CancelledError("original cancellation") from None

        return invoke

    for opposed in (skipped, doubled):
        with pytest.raises(AssertionError):
            require_exact_delegation(opposed)
    for error in (RuntimeError("original instrument error"), asyncio.CancelledError("original cancellation")):
        with pytest.raises(AssertionError):
            require_exact_delegation(discarded, error)
    with pytest.raises(AssertionError):
        require_exact_delegation(replaced_cancellation, asyncio.CancelledError("original cancellation"))

    # String input measures characters, not invented UTF8 bytes. The output
    # bytes are the actual buffer returned by this controlled original once.
    extent = _P4WorkLedger()
    returned_buffer = b"\xc3\xa9"
    buffer_calls = []

    def buffer_source(text):
        buffer_calls.append(text)
        return returned_buffer

    buffer_observer = extent.wrap("extent", buffer_source, input_position=0, input_name="text", output_kind="bytes")
    extent.start()
    assert buffer_observer("é") is returned_buffer
    with extent.diagnostic():
        assert buffer_observer("é") is returned_buffer
    extent.stop()
    assert buffer_calls == ["é", "é"]
    assert extent.report()["fixed_aliases"]["extent"] == {
        "entered": 1,
        "returned": 1,
        "raised": 0,
        "input_characters": 1,
        "actual_returned_buffer_bytes": 2,
    }

    # Nominal fixture values calibrate output bookkeeping only. Their private
    # construction supplies no business admission, verification or authority.
    from elspeth.web.composer.continuation import PrivateChunkRef

    returned_text = "é雪"
    returned_chunk = PrivateChunk(
        PrivateChunkRef.model_validate({"codec": "composer.response.v1", "digest": sha256(b'{"a":1}').hexdigest(), "byte_length": 7}),
        '{"a":1}',
    )
    output_calls = []

    def text_source():
        output_calls.append("text")
        return returned_text

    def chunk_source():
        output_calls.append("chunk")
        return returned_chunk

    output_extent = _P4WorkLedger()
    text_observer = output_extent.wrap("text", text_source, output_kind="text")
    chunk_observer = output_extent.wrap("chunk", chunk_source, output_kind="chunk")
    output_extent.start()
    assert text_observer() is returned_text
    assert chunk_observer() is returned_chunk
    output_extent.stop()
    assert output_calls == ["text", "chunk"]
    assert output_extent.report()["fixed_aliases"] == {
        "text": {"entered": 1, "returned": 1, "raised": 0, "output_characters": 2},
        "chunk": {"entered": 1, "returned": 1, "raised": 0, "verified_returned_chunk_bytes": 7},
    }
    unavailable = _P4WorkLedger()
    original_error = RuntimeError("original without an exposed string input")

    def no_string_source():
        raise original_error

    unmeasured = unavailable.wrap("unknown", no_string_source, input_position=0, input_name="text")
    unavailable.start()
    with pytest.raises(RuntimeError) as caught:
        unmeasured()
    unavailable.stop()
    assert caught.value is original_error
    assert unavailable.report()["fixed_aliases"]["unknown"] == {"entered": 1, "returned": 0, "raised": 1, "unknown_input_extent": 1}


@pytest.mark.asyncio
async def test_measure_actual_stop_p4_application_evidence_excess(operation_store, tmp_path, monkeypatch, record_property):
    """Measure the real new-layout success; propagate any remaining refusal."""
    from elspeth.web.composer import (
        continuation_batch_data,
        continuation_codec,
        continuation_evidence,
        continuation_evidence_application_data,
    )
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.sessions import continuation_p4_application_store

    actual_encode = continuation_evidence._encode_turn_evidence_source
    observations = []
    work = _P4WorkLedger()

    monkeypatch.setattr(
        continuation_p4_application_store,
        "_private_chunk_on_connection",
        work.wrap(
            "application_store.verified_chunk_load", continuation_p4_application_store._private_chunk_on_connection, output_kind="chunk"
        ),
    )
    monkeypatch.setattr(
        continuation_p4_application_store,
        "read_chunk",
        work.wrap("application_store.read_chunk", continuation_p4_application_store.read_chunk, output_kind="bytes"),
    )
    monkeypatch.setattr(
        continuation_p4_application_store,
        "verify_private_chunk",
        work.wrap(
            "application_store.verify_private_chunk",
            continuation_p4_application_store.verify_private_chunk,
            input_position=1,
            input_name="canonical",
        ),
    )
    monkeypatch.setattr(
        continuation_evidence_application_data,
        "verify_private_chunk",
        work.wrap(
            "application_data.verify_private_chunk",
            continuation_evidence_application_data.verify_private_chunk,
            input_position=1,
            input_name="canonical",
        ),
    )
    monkeypatch.setattr(
        continuation_codec,
        "parse_json_strict",
        work.wrap("codec.parse_json_strict", continuation_codec.parse_json_strict, input_position=0, input_name="text"),
    )
    monkeypatch.setattr(
        continuation_batch_data,
        "_encode",
        work.wrap("batch_data.graph_encode", continuation_batch_data._encode, output_kind="text"),
    )
    monkeypatch.setattr(
        continuation_batch_data,
        "_decode_graph_values",
        work.wrap("batch_data.graph_decode", continuation_batch_data._decode_graph_values, input_position=0, input_name="encoded"),
    )
    monkeypatch.setattr(
        continuation_evidence_application_data,
        "_project",
        work.wrap("application_data.historical_projection", continuation_evidence_application_data._project),
    )

    def observe(snapshot, **kwargs):
        value = actual_encode(snapshot, **kwargs)
        if snapshot.application_entry is not None:
            # Memory only inside the physical worker. These extra diagnostic
            # parses/hashes do not enter the production call counters.
            with work.diagnostic():
                observations.append((value[0].reference, _p4_evidence_size(value[0].canonical, kwargs["sources"].chunks)))
        return value

    monkeypatch.setattr(continuation_evidence, "_encode_turn_evidence_source", observe)
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        work.start()
        try:
            await owner.finish_owed_prefix(owner.runtime.source.selected)
        finally:
            work.stop()
        assert owner.p4.publication is not None and owner.p4.application is not None
        physical = owner.p4.physical
        assert type(physical) is RequiredSQLReturned and physical.value.application is owner.p4.application
        assert owner.p4.application_sql is None, "one physical publication/application transaction, no second application writer"
        selected_measurements = [
            measurement for reference, measurement in observations if reference == owner.p4.application.bundle.evidence_chunk.reference
        ]
        assert len(selected_measurements) == 1
        assert len(sdk) == 1
        measurement = selected_measurements[0]
        assert measurement["evidence"]["bytes"] <= JSON_MAX_TOTAL_UTF8_BYTES
        assert measurement["graph"]["bytes"] <= JSON_MAX_TOTAL_UTF8_BYTES
        assert len(measurement["records"]) == 1
        for part in (measurement["evidence"], measurement["entry"], measurement["graph"], *measurement["records"]):
            assert part["bytes"] == part["punctuation_bytes"] + sum(item["member_bytes"] for item in part["members"].values())
        dependencies = measurement["original_dependencies"]
        assert dependencies["occurrence_count"] == 4 * len(measurement["records"])
        assert sum(dependencies["role_occurrences"].values()) == dependencies["occurrence_count"]
        assert dependencies["unique_original_bytes"] <= dependencies["occurrence_original_bytes"]
        measurement["production_call_observations"] = work.report()
        measurement["production_call_observations"]["unknowns"] += ("DIRECTIONAL_WIRE_EMISSION_OUTSIDE_NAMED_ENCODE_ALIAS",)
        assert set(work.report()["fixed_aliases"]) == {
            "application_store.verified_chunk_load",
            "application_store.read_chunk",
            "application_store.verify_private_chunk",
            "application_data.verify_private_chunk",
            "codec.parse_json_strict",
            "batch_data.graph_encode",
            "batch_data.graph_decode",
            "application_data.historical_projection",
        }
        assert all(row["entered"] > 0 for row in work.report()["fixed_aliases"].values())
        assert all(row["entered"] == row["returned"] + row["raised"] for row in work.report()["fixed_aliases"].values())
        report = canonical_json(measurement)
        assert len(report.encode("utf-8")) < 65_536
        # Only hashes, counts and closed model field labels are exported, after
        # the real owner's await has completed. This is one supported case,
        # never a global bound, production latency or whole-worker acceptance.
        record_property("measurement_only_no_global_resource_claim", True)
        record_property("p4_evidence_size_decomposition", report)
