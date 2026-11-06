"""Dormant carrier and actual finish-once hop controls; stage activation is separate."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from itertools import permutations
from sys import getsizeof
from types import MappingProxyType
from typing import cast

import pytest
from pydantic import ValidationError

from elspeth.contracts.credential_material import CredentialMaterialFinding, CredentialTraversalPolicy, find_credential_material
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web import async_workers
from elspeth.web import continuation_retention_memo as memo_module
from elspeth.web.composer import prompt_guard
from elspeth.web.composer.continuation import ContinuationFrameStack, InitializeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    _decode_continuation_snapshot_uncached,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_limits import (
    CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
    CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES,
)
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
from elspeth.web.continuation_retention_memo import (
    CanonicalTextEncodingKey,
    CanonicalTextVerificationDependencies,
    CanonicalTextVerificationKey,
    CredentialInputToken,
    CredentialPolicyKey,
    CredentialScanKey,
    PrivateChunkVerificationKey,
    RecorderBlockHashKey,
    RecorderRequestTextDependencies,
    RecorderRequestTextKey,
    RecorderRequestTextProof,
    RetentionStageMemo,
    SnapshotDecodeKey,
    StateCredentialStepKey,
    borrow_v4_preparation_memo_scope,
    current_retention_stage_memo,
    current_v4_preparation_memo,
    finish_v4_preparation_memo,
    prepare_retention_sql_callable,
    retention_stage_memo_scope,
    v4_preparation_memo_scope,
)
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.sessions import service
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.test_required_sql_finish_once import publication_ticket


def _advisor_clock() -> ContinuationActionClock:
    return ContinuationActionClock(ContinuationActionPolicy(preparation=10, guard=1, quota=1, physical=1, retention=1))


def _snapshot_key() -> SnapshotDecodeKey:
    return SnapshotDecodeKey(encode_continuation_snapshot(initial_stack()))


def _scan_key() -> CredentialScanKey:
    return CredentialScanKey(
        "generic",
        "memo-control",
        (CredentialInputToken("string", "ordinary content"),),
        CredentialPolicyKey.capture(CredentialTraversalPolicy()),
    )


def _scan(key: CredentialScanKey) -> CredentialMaterialFinding | None:
    policy = key.policy
    return find_credential_material(
        key.content[0].value,
        CredentialTraversalPolicy(
            env_ref_names=policy.env_ref_names,
            additional_credential_fields=policy.additional_credential_fields,
            max_depth=policy.max_depth,
            max_nodes=policy.max_nodes,
            max_width=policy.max_width,
            max_string_bytes=policy.max_string_bytes,
        ),
    )


def test_canonical_text_proof_is_one_entry_exact_producer_and_stage_bounded() -> None:
    calls: list[tuple[str, int]] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append((key.canonical, key.max_utf8_bytes))
        return key.canonical.encode("utf-8")

    def other_encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(("other:" + key.canonical, key.max_utf8_bytes))
        return key.canonical.encode("utf-8")

    first = CanonicalTextEncodingKey('{"text":"first"}', 1_048_576)
    second = CanonicalTextEncodingKey('{"text":"other"}', 1_048_576)
    changed_bound = CanonicalTextEncodingKey(first.canonical, 1_048_575)
    with retention_stage_memo_scope() as memo:
        assert memo.canonical_text_encoding(first, encode) == first.canonical.encode()
        assert memo.canonical_text_encoding(first, encode) == first.canonical.encode()
        assert calls == [(first.canonical, first.max_utf8_bytes)]
        assert memo.cached_entry_count == 1
        assert memo.canonical_text_encoding(changed_bound, encode) == first.canonical.encode()
        assert memo.canonical_text_encoding(first, other_encode) == first.canonical.encode()
        assert memo.canonical_text_encoding(second, encode) == second.canonical.encode()
        assert memo.cached_entry_count == 1
        assert memo.canonical_text_encoding(first, encode) == first.canonical.encode()
        assert calls == [
            (first.canonical, first.max_utf8_bytes),
            (first.canonical, changed_bound.max_utf8_bytes),
            ("other:" + first.canonical, first.max_utf8_bytes),
            (second.canonical, second.max_utf8_bytes),
            (first.canonical, first.max_utf8_bytes),
        ]
    assert memo.cached_entry_count == 0 and not memo.active
    memo.canonical_text_encoding(first, encode)
    assert memo.cached_entry_count == 0


def test_canonical_text_proof_does_not_cache_failed_or_foreign_producers() -> None:
    memo = RetentionStageMemo()
    key = CanonicalTextEncodingKey('{"safe":true}', 1_048_576)

    def fails(_key: CanonicalTextEncodingKey) -> bytes:
        raise ValueError("original failure")

    def foreign(_key: CanonicalTextEncodingKey) -> str:
        return "not bytes"

    with pytest.raises(ValueError, match="original failure"):
        memo.canonical_text_encoding(key, fails)
    with pytest.raises(TypeError, match="immutable bytes"):
        memo.canonical_text_encoding(key, foreign)
    assert memo.cached_entry_count == 0
    memo.close()


def test_canonical_text_proof_never_retains_more_than_existing_private_document_bound() -> None:
    memo = RetentionStageMemo()
    oversized = CanonicalTextEncodingKey(
        "x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1), CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1
    )

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        return key.canonical.encode("utf-8")

    assert len(memo.canonical_text_encoding(oversized, encode)) == CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1
    assert memo.cached_entry_count == 0
    memo.close()


def test_canonical_sha_proofs_are_exact_and_share_one_existing_byte_budget() -> None:
    calls: list[str] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(key.canonical[:1])
        return key.canonical.encode("utf-8")

    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    first = CanonicalTextEncodingKey("a" * 250_000, bound)
    second = CanonicalTextEncodingKey("b" * 250_000, bound)
    third = CanonicalTextEncodingKey("c" * 250_000, bound)
    with retention_stage_memo_scope() as memo:
        first_proof = memo.canonical_text_proof(first, encode, hashlib.sha256)
        assert first_proof.digest == hashlib.sha256(first.canonical.encode()).hexdigest()
        memo.canonical_text_proof(second, encode, hashlib.sha256)
        assert memo.canonical_text_proof(first, encode, hashlib.sha256) is first_proof
        assert calls == ["a", "b"] and memo.cached_entry_count == 2
        memo.canonical_text_proof(third, encode, hashlib.sha256)
        assert memo.cached_entry_count == 2
        memo.canonical_text_proof(second, encode, hashlib.sha256)
        assert calls == ["a", "b", "c", "b"]
    assert memo.cached_entry_count == 0


@pytest.mark.asyncio
async def test_v4_preparation_keeps_individual_text_entry_limits() -> None:
    clock = _advisor_clock()
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    oversized = CanonicalTextEncodingKey("x" * 600_000, bound)
    block_text = "z" * (bound - 63)
    block = RecorderBlockHashKey(hashlib.sha256(block_text.encode()).hexdigest(), block_text)
    oversized_calls: list[str] = []

    def oversized_encode(key: CanonicalTextEncodingKey) -> bytes:
        oversized_calls.append("proof")
        return key.canonical.encode("utf-8")

    def verify_block(key: RecorderBlockHashKey) -> None:
        oversized_calls.append("block")
        assert hashlib.sha256(key.canonical.encode()).hexdigest() == key.expected_digest

    with v4_preparation_memo_scope(clock) as parent:
        for _ in range(2):
            parent.canonical_text_proof(oversized, oversized_encode, hashlib.sha256)
            parent.recorder_block_hash(block, verify_block)
        assert oversized_calls == ["proof", "block", "proof", "block"]
        assert not parent._canonical_proofs and not parent._recorder_block_hashes


def test_canonical_sha_proof_does_not_cache_oversize_or_foreign_producer() -> None:
    memo = RetentionStageMemo()
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    oversized = CanonicalTextEncodingKey("x" * 600_000, bound)

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        return key.canonical.encode("utf-8")

    def foreign(_key: CanonicalTextEncodingKey) -> str:
        return "not bytes"

    assert len(memo.canonical_text_proof(oversized, encode, hashlib.sha256).encoded) == 600_000
    assert memo.cached_entry_count == 0
    with pytest.raises(TypeError, match="exact immutable bytes"):
        memo.canonical_text_proof(CanonicalTextEncodingKey("safe", bound), foreign, hashlib.sha256)
    memo.close()


def test_recorder_block_hash_memo_keeps_exact_key_producer_and_stage() -> None:
    calls: list[str] = []

    def verify(key: RecorderBlockHashKey) -> None:
        calls.append(key.canonical)
        if hashlib.sha256(key.canonical.encode()).hexdigest() != key.expected_digest:
            raise ValueError("changed block address")

    text = '{"safe":true}'
    key = RecorderBlockHashKey(hashlib.sha256(text.encode()).hexdigest(), text)
    with retention_stage_memo_scope() as memo:
        memo.recorder_block_hash(key, verify)
        memo.recorder_block_hash(key, verify)
        assert calls == [text] and memo.cached_entry_count == 1
        with pytest.raises(ValueError, match="changed block address"):
            memo.recorder_block_hash(replace(key, expected_digest="0" * 64), verify)
        assert calls == [text, text] and memo.cached_entry_count == 1
    with retention_stage_memo_scope() as second_memo:
        second_memo.recorder_block_hash(key, verify)
    assert calls == [text, text, text]


def test_recorder_request_proof_counts_complete_aggregate_and_one_entry() -> None:
    calls: list[str] = []

    def produce(key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls.append(key.canonical)
        return RecorderRequestTextProof('{"messages":[]}', (("a" * 64, "block-one"), ("b" * 64, "block-two")))

    key_text = "request"
    proof = produce(RecorderRequestTextKey(key_text, 1_048_576))
    calls.clear()
    exact = (
        len(key_text.encode())
        + len(proof.ref_document_json.encode())
        + sum(len(digest.encode()) + len(text.encode()) for digest, text in proof.blocks)
    )
    with retention_stage_memo_scope() as memo:
        key = RecorderRequestTextKey(key_text, exact)
        assert memo.recorder_request_text(key, produce) == proof
        assert memo.recorder_request_text(key, produce) == proof
        assert calls == [key_text] and memo.cached_entry_count == 1
        short = RecorderRequestTextKey(key_text, exact - 1)
        assert memo.recorder_request_text(short, produce) == proof
        assert memo.cached_entry_count == 1
        assert memo.recorder_request_text(key, produce) == proof
        assert calls == [key_text, key_text]
    assert memo.cached_entry_count == 0


def test_recorder_request_proof_fallback_producer_identity_nested_scope_and_close() -> None:
    calls: list[str] = []
    key = RecorderRequestTextKey("prepared", 1_048_576)

    def produce(actual: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls.append(actual.canonical)
        return RecorderRequestTextProof("{}", ())

    def other_produce(actual: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls.append("other:" + actual.canonical)
        return RecorderRequestTextProof("{}", ())

    def fallback(_actual: RecorderRequestTextKey) -> None:
        return None

    with retention_stage_memo_scope() as outer:
        assert outer.recorder_request_text(key, produce) == RecorderRequestTextProof("{}", ())
        assert outer.recorder_request_text(key, fallback) is None
        assert outer.recorder_request_text(key, other_produce) == RecorderRequestTextProof("{}", ())
        assert outer.cached_entry_count == 1
        with retention_stage_memo_scope() as inner:
            assert inner.recorder_request_text(key, produce) == RecorderRequestTextProof("{}", ())
            assert inner is not outer and inner.cached_entry_count == 1
        assert outer.recorder_request_text(key, other_produce) == RecorderRequestTextProof("{}", ())
        assert calls == ["prepared", "other:prepared", "prepared"]
    assert outer.cached_entry_count == 0 and inner.cached_entry_count == 0


def test_recorder_request_proof_close_during_miss_cannot_publish() -> None:
    memo = RetentionStageMemo()
    entered = threading.Event()
    release = threading.Event()
    key = RecorderRequestTextKey("prepared", 1_048_576)

    def produce(_actual: RecorderRequestTextKey) -> RecorderRequestTextProof:
        entered.set()
        assert release.wait(timeout=5)
        return RecorderRequestTextProof("{}", ())

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memo.recorder_request_text, key, produce)
        assert entered.wait(timeout=5)
        memo.close()
        release.set()
        assert future.result(timeout=5) == RecorderRequestTextProof("{}", ())
    assert memo.cached_entry_count == 0 and not memo.active


def test_closed_families_keep_none_and_exact_inputs_separate() -> None:
    counts = {"snapshot": 0, "chunk": 0, "scan": 0, "state": 0}

    def decode(encoded: str) -> ContinuationFrameStack:
        counts["snapshot"] += 1
        return _decode_continuation_snapshot_uncached(encoded)

    def canonical(encoded: str) -> None:
        counts["chunk"] += 1
        assert encoded == '{"safe":true}'

    def scan(key: CredentialScanKey) -> CredentialMaterialFinding | None:
        counts["scan"] += 1
        return _scan(key)

    def state(key: StateCredentialStepKey) -> CredentialMaterialFinding | None:
        counts["state"] += 1
        return _scan(key.scan)

    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    with retention_stage_memo_scope() as memo:
        key = _snapshot_key()
        first = memo.snapshot(key, decode)
        assert memo.snapshot(key, decode) is first
        assert first == _decode_continuation_snapshot_uncached(key.encoded)
        changed = replace(key, encoded=encode_continuation_snapshot(initial_stack().model_copy(update={"cursor": 1})))
        assert memo.snapshot(changed, decode).cursor == 1
        chunk_key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
        assert memo.private_chunk(chunk_key, canonical) is None
        assert memo.private_chunk(chunk_key, canonical) is None
        scan_key = _scan_key()
        assert memo.credential(scan_key, scan) is None
        assert memo.credential(scan_key, scan) is None
        state_key = StateCredentialStepKey(scan_key)
        assert memo.state_credentials(state_key, state) is None
        assert memo.state_credentials(state_key, state) is None
        assert counts == {"snapshot": 2, "chunk": 1, "scan": 1, "state": 1}
        assert memo.cached_entry_count == 5
        frame = first.frames[0]
        assert isinstance(frame, InitializeFrame)
        with pytest.raises(ValidationError):
            frame.request_json = '{"changed":true}'
        with pytest.raises(ValidationError):
            first.cursor = 4
    assert memo.cached_entry_count == 0 and not memo.active


def test_producer_identity_is_part_of_the_exact_family_key() -> None:
    key = _scan_key()
    finding = CredentialMaterialFinding("traversal_limit", "root")

    def allowed(_key: CredentialScanKey) -> CredentialMaterialFinding | None:
        return None

    def refused(_key: CredentialScanKey) -> CredentialMaterialFinding | None:
        return finding

    with retention_stage_memo_scope() as memo:
        assert memo.credential(key, allowed) is None
        assert memo.credential(key, refused) is finding
        assert memo.credential(key, allowed) is None


def test_foreign_result_and_original_compute_error_are_never_retained() -> None:
    original = RuntimeError("controlled pure computation failure")

    def foreign(_encoded: str) -> ContinuationFrameStack:
        return cast(ContinuationFrameStack, {"mutable": []})

    def fails(_encoded: str) -> ContinuationFrameStack:
        raise original

    with retention_stage_memo_scope() as memo:
        with pytest.raises(TypeError, match="foreign result"):
            memo.snapshot(_snapshot_key(), foreign)
        with pytest.raises(RuntimeError) as raised:
            memo.snapshot(_snapshot_key(), fails)
        assert raised.value is original and memo.cached_entry_count == 0


@pytest.mark.parametrize(
    "field",
    ("env_ref_names", "additional_credential_fields", "max_depth", "max_nodes", "max_width", "max_string_bytes", "detector_version"),
)
def test_every_policy_input_changes_scan_identity(field: str) -> None:
    names = {"FIRST_REF"}
    fields = {"secret_option"}
    policy = CredentialTraversalPolicy(env_ref_names=names, additional_credential_fields=fields)
    before = CredentialPolicyKey.capture(policy)
    names.add("SECOND_REF")
    fields.add("another_secret_option")
    assert before.env_ref_names == frozenset({"FIRST_REF"})
    assert before.additional_credential_fields == frozenset({"secret_option"})
    changes: dict[str, frozenset[str] | int | str] = {
        "env_ref_names": frozenset({"SECOND_REF"}),
        "additional_credential_fields": frozenset(),
        "max_depth": 1,
        "max_nodes": 2,
        "max_width": 3,
        "max_string_bytes": 4,
        "detector_version": "next",
    }
    assert replace(before, **{field: changes[field]}) != before


def test_nested_success_and_error_reset_by_token_and_close_values() -> None:
    original = RuntimeError("controlled stage failure")
    assert current_retention_stage_memo() is None
    with retention_stage_memo_scope() as outer:
        with pytest.raises(RuntimeError) as raised, retention_stage_memo_scope() as inner:
            assert inner is not outer and current_retention_stage_memo() is inner
            inner.snapshot(_snapshot_key(), decode_continuation_snapshot)
            raise original
        assert raised.value is original and not inner.active and inner.cached_entry_count == 0
        assert current_retention_stage_memo() is outer
    assert current_retention_stage_memo() is None and not outer.active


def test_inflight_completion_cannot_repopulate_after_close() -> None:
    entered, release = threading.Event(), threading.Event()
    key = _snapshot_key()
    memo = RetentionStageMemo()

    def decode(encoded: str) -> ContinuationFrameStack:
        entered.set()
        assert release.wait(5)
        return decode_continuation_snapshot(encoded)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(memo.snapshot, key, decode)
        try:
            assert entered.wait(5)
            memo.close()
        finally:
            release.set()
        assert future.result(timeout=5) == decode_continuation_snapshot(key.encoded)
    assert memo.cached_entry_count == 0


def test_overlapping_misses_publish_one_winning_immutable_value() -> None:
    barrier = threading.Barrier(2)
    key = _snapshot_key()

    def decode(encoded: str) -> ContinuationFrameStack:
        value = decode_continuation_snapshot(encoded)
        barrier.wait(timeout=5)
        return value

    with retention_stage_memo_scope() as memo, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(memo.snapshot, key, decode)
        second = pool.submit(memo.snapshot, key, decode)
        assert first.result(timeout=5) is second.result(timeout=5)
        assert memo.cached_entry_count == 1


@pytest.mark.asyncio
async def test_concurrent_independent_stage_scopes_never_share_values() -> None:
    ready = asyncio.Event()
    entered = 0
    key = _snapshot_key()

    async def stage() -> tuple[RetentionStageMemo, ContinuationFrameStack]:
        nonlocal entered
        with retention_stage_memo_scope() as memo:
            entered += 1
            if entered == 2:
                ready.set()
            await ready.wait()
            value = prepare_retention_sql_callable(memo.snapshot, key, decode_continuation_snapshot)()
            return memo, value

    (first, one), (second, two) = await asyncio.gather(stage(), stage())
    assert first is not second and one == two and one is not two
    assert first.cached_entry_count == second.cached_entry_count == 0
    assert current_retention_stage_memo() is None


@pytest.mark.asyncio
async def test_real_finish_once_hops_hit_only_stage_memo_and_keep_caller_context_out(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    caller = ContextVar("memo-caller-control", default="default")
    worker_only = ContextVar("memo-worker-control", default="default")
    caller_token = caller.set("caller")
    lock_token = service._SESSION_WRITE_LOCK_HELD.set(frozenset({(1, "caller-session")}))
    loop_thread = threading.get_ident()
    key = _snapshot_key()
    requests: list[int] = []
    work: list[int] = []

    def decode(encoded: str) -> ContinuationFrameStack:
        work.append(threading.get_ident())
        return decode_continuation_snapshot(encoded)

    def sql() -> ContinuationFrameStack:
        assert threading.get_ident() != loop_thread
        assert caller.get() == "default" and worker_only.get() == "default"
        assert prompt_guard._USER_PROMPT.get() is None and service._SESSION_WRITE_LOCK_HELD.get() == frozenset()
        assert async_workers.outstanding_admissions() == 1
        requests.append(threading.get_ident())
        worker_only.set("must not survive worker Context exit")
        active = current_retention_stage_memo()
        assert active is memo
        return active.snapshot(key, decode)

    try:
        with prompt_guard.prompt_guard_user_scope("caller provenance"), retention_stage_memo_scope() as memo:
            first_ticket, second_ticket = publication_ticket(), publication_ticket()
            first = await async_workers.run_required_sql_finish_once(first_ticket, sql)
            second = await async_workers.run_required_sql_finish_once(second_ticket, sql)
            assert isinstance(first, RequiredSQLReturned) and isinstance(second, RequiredSQLReturned)
            assert first.value is second.value
            assert len(requests) == 2 and len(work) == 1 and work[0] != loop_thread
            assert first_ticket.complete and second_ticket.complete and async_workers.outstanding_admissions() == 0
        assert memo.cached_entry_count == 0
        assert executor.submit(lambda: (current_retention_stage_memo(), worker_only.get(), service._SESSION_WRITE_LOCK_HELD.get())).result(
            timeout=5
        ) == (None, "default", frozenset())
    finally:
        service._SESSION_WRITE_LOCK_HELD.reset(lock_token)
        caller.reset(caller_token)
        await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_no_carrier_preserves_existing_worker_context_without_copying_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    value = ContextVar("memo-absent-worker-context", default="fresh-context")
    worker_token = executor.submit(value.set, "existing-worker-context").result(timeout=5)
    caller_token = value.set("caller-context")
    try:
        assert current_retention_stage_memo() is None
        outcome = await async_workers.run_required_sql_finish_once(
            publication_ticket(), lambda: (value.get(), current_retention_stage_memo())
        )
        assert isinstance(outcome, RequiredSQLReturned)
        assert outcome.value == ("existing-worker-context", None)
    finally:
        value.reset(caller_token)
        executor.submit(value.reset, worker_token).result(timeout=5)
        await async_workers.shutdown_async_workers()


def test_captured_stage_closed_before_invocation_still_uses_fresh_context() -> None:
    value = ContextVar("memo-closed-before-entry", default="fresh-context")
    caller_token = value.set("caller-context")
    try:
        with retention_stage_memo_scope() as memo:
            prepared = prepare_retention_sql_callable(lambda: (value.get(), current_retention_stage_memo()))
        assert not memo.active
        assert prepared() == ("fresh-context", None)
    finally:
        value.reset(caller_token)


@pytest.mark.asyncio
async def test_closed_stage_in_escaped_task_and_no_carrier_compute_normally() -> None:
    key = _snapshot_key()
    release = asyncio.Event()

    async def escaped() -> ContinuationFrameStack:
        await release.wait()
        assert current_retention_stage_memo() is None
        return prepare_retention_sql_callable(decode_continuation_snapshot, key.encoded)()

    with retention_stage_memo_scope() as memo:
        memo.snapshot(key, decode_continuation_snapshot)
        task = asyncio.create_task(escaped())
    release.set()
    assert await task == decode_continuation_snapshot(key.encoded)
    assert memo.cached_entry_count == 0
    first = prepare_retention_sql_callable(decode_continuation_snapshot, key.encoded)()
    second = prepare_retention_sql_callable(decode_continuation_snapshot, key.encoded)()
    assert first == second and first is not second


@pytest.mark.asyncio
async def test_deferred_cancellation_joins_worker_before_scope_closes_and_preserves_error(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", executor)
    entered, release = threading.Event(), threading.Event()
    original = RuntimeError("controlled SQL original")
    ticket = publication_ticket()

    def sql() -> None:
        assert current_retention_stage_memo() is memo
        entered.set()
        assert release.wait(5)
        raise original

    try:
        with retention_stage_memo_scope() as memo:
            task = asyncio.create_task(async_workers.run_required_sql_finish_once(ticket, sql))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            task.cancel("stage caller cancellation")
            await asyncio.sleep(0)
            assert not task.done() and memo.active and not ticket.complete
            release.set()
            outcome = await task
            assert isinstance(outcome, RequiredSQLRaised) and outcome.error is original
            assert len(outcome.deferred_cancellations) == 1 and ticket.complete
        assert current_retention_stage_memo() is None and not memo.active
    finally:
        release.set()
        await async_workers.shutdown_async_workers()


@pytest.mark.asyncio
async def test_v4_preparation_sql_cuts_share_only_canonical_and_block_proofs() -> None:
    clock = _advisor_clock()
    calls = {"text": 0, "block": 0, "encoding": 0, "request": 0, "snapshot": 0, "chunk": 0, "scan": 0, "state": 0}
    worker_only = ContextVar("v4-preparation-cut", default="fresh")
    text = '{"whole":"input"}'
    key = CanonicalTextEncodingKey(text, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    block = RecorderBlockHashKey(hashlib.sha256(text.encode()).hexdigest(), text)
    request = RecorderRequestTextKey(text, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    snapshot_key = _snapshot_key()
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    chunk_key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    scan_key = _scan_key()
    state_key = StateCredentialStepKey(scan_key)
    children: list[RetentionStageMemo] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls["text"] += 1
        return key.canonical.encode()

    def encode_locally(key: CanonicalTextEncodingKey) -> bytes:
        calls["encoding"] += 1
        return key.canonical.encode()

    def verify_block(key: RecorderBlockHashKey) -> None:
        calls["block"] += 1
        assert hashlib.sha256(key.canonical.encode()).hexdigest() == key.expected_digest

    def prepare_request(_key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls["request"] += 1
        return RecorderRequestTextProof("{}", ())

    def decode(_encoded: str) -> ContinuationFrameStack:
        calls["snapshot"] += 1
        return initial_stack()

    def verify_chunk(_canonical: str) -> None:
        calls["chunk"] += 1

    def scan(key: CredentialScanKey) -> CredentialMaterialFinding | None:
        calls["scan"] += 1
        return _scan(key)

    def scan_state(key: StateCredentialStepKey) -> CredentialMaterialFinding | None:
        calls["state"] += 1
        return _scan(key.scan)

    with v4_preparation_memo_scope(clock) as parent:

        def sql_cut() -> int:
            assert worker_only.get() == "fresh"
            assert current_v4_preparation_memo(clock) is parent
            worker_only.set("inside-original-worker")
            with borrow_v4_preparation_memo_scope(clock) as child:
                assert child is current_retention_stage_memo() and child is not parent
                assert current_v4_preparation_memo(clock) is parent
                children.append(child)
                for _repeat in range(2):
                    child.canonical_text_proof(key, encode, hashlib.sha256)
                    child.recorder_block_hash(block, verify_block)
                    child.canonical_text_encoding(key, encode_locally)
                    child.recorder_request_text(request, prepare_request)
                    child.snapshot(snapshot_key, decode)
                    child.private_chunk(chunk_key, verify_chunk)
                    child.credential(scan_key, scan)
                    child.state_credentials(state_key, scan_state)
                assert parent.cached_entry_count == 2
            assert not child.active and child.cached_entry_count == 0
            return len(children)

        for ordinal in range(1, 4):
            outcome = await async_workers.run_required_sql_finish_once(publication_ticket(), sql_cut)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == ordinal
        assert calls == {"text": 1, "block": 1, "encoding": 3, "request": 3, "snapshot": 3, "chunk": 3, "scan": 3, "state": 3}
        assert len({id(child) for child in children}) == 3
        assert parent._shared_text_budget_bytes == CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES == 33_554_432
        with v4_preparation_memo_scope(clock) as nested:
            assert nested is parent
        assert parent.active
        clock.finish_preparation()
        finish_v4_preparation_memo(clock)
        assert not parent.active and parent.cached_entry_count == 0
        with pytest.raises(AuditIntegrityError, match="reopen"), v4_preparation_memo_scope(clock):
            pass
    assert current_retention_stage_memo() is None
    with pytest.raises(AuditIntegrityError, match="reopen"), v4_preparation_memo_scope(clock):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_mib", (1, 5))
async def test_v4_preparation_text_pressure_respects_selected_transient_budget(monkeypatch: pytest.MonkeyPatch, budget_mib: int) -> None:
    budget_bytes = budget_mib * 1_048_576
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", budget_bytes)
    calls: list[str] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(key.canonical[:1])
        return key.canonical.encode()

    def verify_block(key: RecorderBlockHashKey) -> None:
        calls.append("block")
        assert hashlib.sha256(key.canonical.encode()).hexdigest() == key.expected_digest

    first = CanonicalTextEncodingKey("a" * 300_000, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    second = CanonicalTextEncodingKey("b" * 300_000, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    block_text = "c" * 500_000
    block = RecorderBlockHashKey(hashlib.sha256(block_text.encode()).hexdigest(), block_text)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        with borrow_v4_preparation_memo_scope() as child:
            child.canonical_text_proof(first, encode, hashlib.sha256)
            child.canonical_text_proof(second, encode, hashlib.sha256)
            assert parent.cached_entry_count == (1 if budget_mib == 1 else 2)
            child.recorder_block_hash(block, verify_block)
            assert parent.cached_entry_count == (1 if budget_mib == 1 else 3)
            child.canonical_text_proof(second, encode, hashlib.sha256)
            assert parent.cached_entry_count == (1 if budget_mib == 1 else 3)
            child.canonical_text_proof(second, encode, hashlib.sha256)
            assert parent._canonical_proof_bytes + parent._recorder_block_hash_bytes <= budget_bytes
        assert calls == (["a", "b", "block", "b"] if budget_mib == 1 else ["a", "b", "block"])


@pytest.mark.asyncio
async def test_v4_preparation_clock_error_and_late_publication_do_not_reopen_parent() -> None:
    clock, foreign = _advisor_clock(), _advisor_clock()
    original = RuntimeError("original V4 preparation error")
    entered, release = threading.Event(), threading.Event()

    def delayed(key: CanonicalTextEncodingKey) -> bytes:
        entered.set()
        assert release.wait(5)
        return key.canonical.encode()

    with pytest.raises(RuntimeError) as raised, v4_preparation_memo_scope(clock) as parent:
        with pytest.raises(AuditIntegrityError, match="original action clock"), v4_preparation_memo_scope(foreign):
            pass
        with pytest.raises(AuditIntegrityError, match="original action clock"), borrow_v4_preparation_memo_scope(foreign):
            pass
        with borrow_v4_preparation_memo_scope(clock) as child:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(child.canonical_text_proof, CanonicalTextEncodingKey("late", 100), delayed, hashlib.sha256)
                assert entered.wait(5)
                finish_v4_preparation_memo(clock)
                release.set()
                assert future.result(timeout=5).encoded == b"late"
            assert not parent.active and parent.cached_entry_count == 0
            assert child.cached_entry_count == 0
        raise original
    assert raised.value is original and current_retention_stage_memo() is None
    assert not child.active
    with v4_preparation_memo_scope(_advisor_clock()) as following:
        assert following is not parent and following.cached_entry_count == 0


@pytest.mark.asyncio
async def test_v4_preparation_original_error_closes_creator_and_complete_keys_remain_distinct() -> None:
    clock = _advisor_clock()
    original = RuntimeError("original preparation failed before finishing")
    calls: list[tuple[str, str, int, object]] = []
    reference = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}').reference
    first = CanonicalTextEncodingKey("whole", 100)

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(("original", key.canonical, key.max_utf8_bytes, key.reference))
        return key.canonical.encode()

    def replacement(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(("replacement", key.canonical, key.max_utf8_bytes, key.reference))
        return key.canonical.encode()

    with pytest.raises(RuntimeError) as raised, v4_preparation_memo_scope(clock) as parent:
        for key, producer in (
            (first, encode),
            (replace(first, canonical="changed"), encode),
            (replace(first, max_utf8_bytes=101), encode),
            (replace(first, reference=reference), encode),
            (first, replacement),
        ):
            with borrow_v4_preparation_memo_scope(clock) as child:
                child.canonical_text_proof(key, producer, hashlib.sha256)
                child.canonical_text_proof(key, producer, hashlib.sha256)
        assert calls == [
            ("original", "whole", 100, None),
            ("original", "changed", 100, None),
            ("original", "whole", 101, None),
            ("original", "whole", 100, reference),
            ("replacement", "whole", 100, None),
        ]
        raise original
    assert raised.value is original and not parent.active and parent.cached_entry_count == 0
    assert not child.active and current_retention_stage_memo() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("order", tuple(permutations(("full", "canonical", "block"))))
@pytest.mark.parametrize("budget_mib", (1, 5))
async def test_v4_three_text_families_enforce_combined_budget_in_every_insertion_order(
    monkeypatch: pytest.MonkeyPatch, order: tuple[str, ...], budget_mib: int
) -> None:
    budget_bytes = budget_mib * 1_048_576
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", budget_bytes)
    calls = {"full": 0, "canonical": 0, "block": 0}
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    full = CanonicalTextEncodingKey("f" * 200_000, bound)
    canonical = CanonicalTextVerificationKey("c" * 400_000, bound)
    block_text = "b" * 400_000
    block = RecorderBlockHashKey(hashlib.sha256(block_text.encode()).hexdigest(), block_text)

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls["full"] += 1
        return key.canonical.encode()

    def verify(_text: str) -> None:
        calls["canonical"] += 1

    def verify_block(key: RecorderBlockHashKey) -> None:
        calls["block"] += 1
        assert hashlib.sha256(key.canonical.encode()).hexdigest() == key.expected_digest

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for family in order:
            with borrow_v4_preparation_memo_scope() as child:
                if family == "full":
                    child.canonical_text_proof(full, encode, hashlib.sha256)
                elif family == "canonical":
                    child.canonical_text_verification(canonical, verify)
                else:
                    child.recorder_block_hash(block, verify_block)
            assert parent._canonical_proof_bytes + parent._canonical_verification_bytes + parent._recorder_block_hash_bytes <= budget_bytes
        assert calls == {"full": 1, "canonical": 1, "block": 1} and parent.cached_entry_count == (2 if budget_mib == 1 else 3)
        # The 1 MiB case proves existing eviction preferences; 5 MiB retains
        # the same complete entries and must hit on the selected revisit.
        revisited = "full" if order[-1] == "canonical" else "canonical"
        with borrow_v4_preparation_memo_scope() as child:
            if revisited == "full":
                child.canonical_text_proof(full, encode, hashlib.sha256)
            else:
                child.canonical_text_verification(canonical, verify)
        assert calls[revisited] == (2 if budget_mib == 1 else 1)
        assert parent._canonical_proof_bytes + parent._canonical_verification_bytes + parent._recorder_block_hash_bytes <= budget_bytes
    assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_mib", (1, 5))
async def test_v4_canonical_only_lru_uses_actual_utf8_and_complete_producer_text_bound_keys(
    monkeypatch: pytest.MonkeyPatch, budget_mib: int
) -> None:
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", budget_mib * 1_048_576)
    calls: list[str] = []

    def verify(text: str) -> None:
        calls.append(text[:1])

    def replacement(text: str) -> None:
        calls.append("replacement:" + text[:1])

    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    first, second, third = (CanonicalTextVerificationKey(char * 350_000, bound) for char in "abc")
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for key in (first, second, first, third, first, second):
            with borrow_v4_preparation_memo_scope() as child:
                child.canonical_text_verification(key, verify)
        assert calls == (["a", "b", "c", "b"] if budget_mib == 1 else ["a", "b", "c"])
        assert parent.cached_entry_count == (2 if budget_mib == 1 else 3)
        parent.canonical_text_verification(CanonicalTextVerificationKey("😀" * 20, 100), verify)
        assert parent._canonical_verifications[(verify, CanonicalTextVerificationKey("😀" * 20, 100))] == 83
        parent.canonical_text_verification(CanonicalTextVerificationKey("😀" * 20, 101), verify)
        parent.canonical_text_verification(CanonicalTextVerificationKey("😀" * 20, 100), replacement)
        assert calls[-3:] == ["😀", "😀", "replacement:😀"]


@pytest.mark.asyncio
async def test_v4_canonical_only_oversize_failure_and_late_completion_never_publish() -> None:
    calls: list[str] = []
    original = AuditIntegrityError("original canonical refusal")

    def verify(text: str) -> None:
        calls.append(text[:1])

    def fails(_text: str) -> None:
        raise original

    def foreign(_text: str) -> None:
        return cast(None, [])

    entered, release = threading.Event(), threading.Event()

    def delayed(_text: str) -> None:
        entered.set()
        assert release.wait(5)

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for key in (
            CanonicalTextVerificationKey("large" * 250_000, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES),
            CanonicalTextVerificationKey("small", 2),
        ):
            for _ in range(2):
                parent.canonical_text_verification(key, verify)
        assert calls == ["l", "l", "s", "s"] and parent.cached_entry_count == 0
        with pytest.raises(AuditIntegrityError) as raised:
            parent.canonical_text_verification(CanonicalTextVerificationKey("bad", 1), fails)
        assert raised.value is original
        with pytest.raises(TypeError, match="no mutable value"):
            parent.canonical_text_verification(CanonicalTextVerificationKey("foreign", 100), foreign)
        assert parent.cached_entry_count == 0

        def current() -> bool:
            return True

        delayed_dependencies = CanonicalTextVerificationDependencies((delayed,) * 17, current)
        with borrow_v4_preparation_memo_scope() as child, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                child.canonical_text_verification, CanonicalTextVerificationKey("late", 100, delayed_dependencies), delayed
            )
            assert entered.wait(5)
            parent.close()
            release.set()
            assert future.result(timeout=5) is None
        assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0
        parent.canonical_text_verification(CanonicalTextVerificationKey("closed", 100), verify)
        assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


def test_canonical_encoding_replacement_clears_canonical_only_family() -> None:
    def verify(_text: str) -> None:
        pass

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        return key.canonical.encode()

    with retention_stage_memo_scope() as memo:
        memo.canonical_text_verification(CanonicalTextVerificationKey("first", 100), verify)
        assert memo.cached_entry_count == 1
        memo.canonical_text_encoding(CanonicalTextEncodingKey("replacement", 100), encode)
        assert memo.cached_entry_count == 1 and memo._canonical_verification_bytes == 0


@pytest.mark.asyncio
async def test_canonical_dependency_charge_affects_retention_only_at_exact_entry_bound() -> None:
    calls: list[int] = []

    def current() -> bool:
        return True

    def verify(text: str) -> None:
        calls.append(len(text))

    dependencies = CanonicalTextVerificationDependencies((verify,) * 17, current)
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    text = "x" * (bound - len(str(bound)) - dependencies.retained_bytes)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for candidate in (text, text, text + "x", text + "x"):
            parent.canonical_text_verification(CanonicalTextVerificationKey(candidate, bound, dependencies), verify)
        assert calls == [len(text), len(text) + 1, len(text) + 1]
        assert len(parent._canonical_verifications) == 1 and parent._canonical_verification_bytes == bound


@pytest.mark.asyncio
async def test_canonical_dependency_metadata_is_charged_and_observer_counts_exact_membership(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.helpers.continuation_end_profile import EndTextMemoObservation

    calls: list[str] = []

    def current() -> bool:
        return True

    def verify(text: str) -> None:
        calls.append(text)

    dependencies = CanonicalTextVerificationDependencies((verify,) * 17, current)
    key = CanonicalTextVerificationKey("😀", 100, dependencies)
    weight = 4 + len(str(key.cache_bound_bytes)) + getsizeof(dependencies) + getsizeof(dependencies.functions)
    assert dependencies.retained_bytes == getsizeof(dependencies) + getsizeof(dependencies.functions)
    observation = EndTextMemoObservation()
    with observation.install(monkeypatch), v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            with borrow_v4_preparation_memo_scope() as child:
                child.canonical_text_verification(key, verify)
        assert calls == ["😀"] and parent._canonical_verification_bytes == weight
        assert parent._canonical_verifications[(verify, key)] == weight
    result = observation.snapshot()
    assert result["families"]["canonical"]["hits"] == 1 and result["families"]["canonical"]["misses"] == 1
    assert result["families"]["canonical"]["admissions"] == 1
    assert result["peak_retained_bytes"] == weight
    assert result["parents"][0]["closed_empty"] is True
    assert result["observer_errors"] == result["budget_violations"] == result["fingerprint_drops"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change_at", ("producer", "size"))
async def test_canonical_dependency_changes_before_publication_never_publish(monkeypatch: pytest.MonkeyPatch, change_at: str) -> None:
    active = True
    original_size = memo_module.utf8_size
    calls: list[str] = []

    def current() -> bool:
        return active

    def verify(text: str) -> None:
        nonlocal active
        calls.append(text)
        if change_at == "producer":
            active = False

    def size(text: str) -> int:
        nonlocal active
        assert change_at == "size"
        active = False
        return original_size(text)

    dependencies = CanonicalTextVerificationDependencies((verify,) * 17, current)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        monkeypatch.setattr(memo_module, "utf8_size", size)
        parent.canonical_text_verification(CanonicalTextVerificationKey("value", 100, dependencies), verify)
        assert calls == ["value"] and parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("order", tuple(permutations(("full", "canonical", "block", "request"))))
async def test_four_family_insertions_account_every_retained_entry(monkeypatch: pytest.MonkeyPatch, order: tuple[str, ...]) -> None:
    bound = 1_048_576
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", bound)
    calls = dict.fromkeys(order, 0)

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls["full"] += 1
        return key.canonical.encode()

    def canonical(_text: str) -> None:
        calls["canonical"] += 1

    def block(_key: RecorderBlockHashKey) -> None:
        calls["block"] += 1

    def request(_key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls["request"] += 1
        return RecorderRequestTextProof("r" * 200_000, ())

    dependencies = RecorderRequestTextDependencies(encode, encode, hashlib.sha256, frozenset({"shape"}))

    def current() -> bool:
        return True

    canonical_dependencies = CanonicalTextVerificationDependencies((canonical,) * 17, current)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:

        def admit(family: str, request_text: str = "q") -> None:
            with borrow_v4_preparation_memo_scope() as child:
                if family == "full":
                    child.canonical_text_proof(CanonicalTextEncodingKey("f" * 200_000, bound), encode, hashlib.sha256)
                elif family == "canonical":
                    child.canonical_text_verification(CanonicalTextVerificationKey("c" * 400_000, bound, canonical_dependencies), canonical)
                elif family == "block":
                    child.recorder_block_hash(RecorderBlockHashKey("0" * 64, "b" * 400_000), block)
                else:
                    child.v4_recorder_request_text(RecorderRequestTextKey(request_text * 200_000, bound), dependencies, request)
            actual = (
                sum(value.retained_bytes for value in parent._canonical_proofs.values())
                + sum(parent._canonical_verifications.values())
                + sum(parent._recorder_block_hashes.values())
                + sum(value.retained_bytes for value in parent._v4_recorder_requests.values())
            )
            charged = (
                parent._canonical_proof_bytes
                + parent._canonical_verification_bytes
                + parent._recorder_block_hash_bytes
                + parent._v4_recorder_request_bytes
            )
            assert actual == charged <= bound
            for entry in parent._v4_recorder_requests.values():
                forward = parent._v4_recorder_request_reverse[entry.reverse_key]
                assert forward in parent._v4_recorder_requests
                assert entry.retained_bytes == (
                    400_000
                    + len(str(bound))
                    + len("shape")
                    + getsizeof(entry.reverse_key)
                    + getsizeof({entry.reverse_key: forward})
                    + getsizeof(entry)
                )
            assert all(key in parent._v4_recorder_requests for key in parent._v4_recorder_request_reverse.values())

        for family in order:
            admit(family)
        assert calls == dict.fromkeys(order, 1)
        assert parent.cached_entry_count == 2
        for family in order:
            admit(family)
        admit("request", "z")
        assert max(calls.values()) > 1
    assert parent.cached_entry_count == parent._v4_recorder_request_bytes == 0
    assert not parent._v4_recorder_request_reverse


@pytest.mark.asyncio
async def test_v4_request_lru_key_bound_producer_oversize_and_late_close(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 1_048_576)
    calls = []

    def produce(key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls.append(key.canonical[:1])
        return RecorderRequestTextProof("r" * 200_000, ())

    def replacement(key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls.append("replacement")
        return RecorderRequestTextProof("{}", ())

    dependencies = RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset({"shape"}))
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        keys = [RecorderRequestTextKey(char * 200_000, 1_048_576) for char in "abc"]
        for key in (keys[0], keys[1], keys[0], keys[2], keys[0], keys[1]):
            parent.v4_recorder_request_text(key, dependencies, produce)
        assert calls == ["a", "b", "c", "b"]
        parent.v4_recorder_request_text(keys[0], dependencies, replacement)
        parent.v4_recorder_request_text(replace(keys[0], cache_bound_bytes=100), dependencies, produce)
        parent.v4_recorder_request_text(replace(keys[0], cache_bound_bytes=100), dependencies, produce)
        assert calls[-3:] == ["replacement", "a", "a"]
        unsupported = replace(dependencies, context_keys=[])
        parent.v4_recorder_request_text(keys[0], unsupported, produce)
        parent.v4_recorder_request_text(keys[0], unsupported, produce)
        assert calls[-2:] == ["a", "a"]
        entered, release = threading.Event(), threading.Event()

        def delayed(_key: RecorderRequestTextKey) -> RecorderRequestTextProof:
            entered.set()
            assert release.wait(5)
            return RecorderRequestTextProof("{}", ())

        with borrow_v4_preparation_memo_scope() as child, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(child.v4_recorder_request_text, RecorderRequestTextKey("late", 100), dependencies, delayed)
            assert entered.wait(5)
            parent.close()
            release.set()
            assert future.result(timeout=5) == RecorderRequestTextProof("{}", ())
        assert parent.cached_entry_count == parent._v4_recorder_request_bytes == 0


@pytest.mark.asyncio
async def test_request_inverse_uses_same_entry_namespace_lru_and_charge(monkeypatch):
    def produce(key):
        return RecorderRequestTextProof(key.canonical + "-refs", ())

    def other(key):
        return produce(key)

    deps = RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset({"shape"}))
    first, second, third = (RecorderRequestTextKey(text, 4096) for text in ("first", "second", "third"))
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        with borrow_v4_preparation_memo_scope() as child:
            proof = child.v4_recorder_request_text(first, deps, produce)
            assert proof is not None
            assert child.v4_restored_recorder_request_text(proof.ref_document_json, 4096, deps, produce) == "first"
            assert not child._v4_recorder_requests and not child._v4_recorder_request_reverse
        weight = parent._v4_recorder_request_bytes
        assert weight > len("firstfirst-refsshape4096")
        for text, bound, dependencies, producer in (
            ("second-refs", 4096, deps, produce),
            ("first-refs", 4095, deps, produce),
            ("first-refs", 4096, replace(deps, renderer=other), produce),
            ("first-refs", 4096, replace(deps, context_keys=[]), produce),
            ("first-refs", 4096, deps, other),
        ):
            assert parent.v4_restored_recorder_request_text(text, bound, dependencies, producer) is None
        # Two entries fit; a reverse hit must rescue the first from eviction.
        parent._shared_text_budget_bytes = weight * 2 + 4
        parent.v4_recorder_request_text(second, deps, produce)
        assert parent.v4_restored_recorder_request_text("first-refs", 4096, deps, produce) == "first"
        parent.v4_recorder_request_text(third, deps, produce)
        assert parent.v4_restored_recorder_request_text("second-refs", 4096, deps, produce) is None
        assert parent.v4_restored_recorder_request_text("first-refs", 4096, deps, produce) == "first"
    assert not parent._v4_recorder_request_reverse and parent._v4_recorder_request_bytes == 0
    assert parent.v4_restored_recorder_request_text("first-refs", 4096, deps, produce) is None

    # This fits the original payload charge but cannot fit the added index.
    # It detects an omitted bookkeeping charge without reproducing its math.
    with v4_preparation_memo_scope(_advisor_clock()) as small:
        small.v4_recorder_request_text(replace(first, cache_bound_bytes=100), deps, produce)
        assert not small._v4_recorder_requests and not small._v4_recorder_request_reverse
        small._shared_text_budget_bytes = 100
        small.v4_recorder_request_text(first, deps, produce)
        assert not small._v4_recorder_requests and small._v4_recorder_request_bytes == 0

    # Ordinary stages never expose the V4 inverse lookup.
    with retention_stage_memo_scope() as ordinary:
        ordinary.v4_recorder_request_text(first, deps, produce)
        assert ordinary.v4_restored_recorder_request_text("first-refs", 4096, deps, produce) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("close_during_compute", (False, True))
async def test_request_inverse_competing_publication_and_close(close_during_compute):
    barrier = threading.Barrier(3)
    release = threading.Event()

    def produce(key):
        barrier.wait(timeout=5)
        assert release.wait(5)
        return RecorderRequestTextProof("refs", ())

    deps = RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset())
    key = RecorderRequestTextKey("original", 4096)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(parent.v4_recorder_request_text, key, deps, produce) for _ in range(2)]
            barrier.wait(timeout=5)
            if close_during_compute:
                parent.close()
            release.set()
            assert all(future.result(timeout=5) == RecorderRequestTextProof("refs", ()) for future in futures)
        assert len(parent._v4_recorder_request_reverse) == len(parent._v4_recorder_requests) == (0 if close_during_compute else 1)
        if not close_during_compute:
            assert parent._v4_recorder_request_bytes == next(iter(parent._v4_recorder_requests.values())).retained_bytes
            assert parent.v4_restored_recorder_request_text("refs", 4096, deps, produce) == "original"
    assert not parent._v4_recorder_request_reverse


@pytest.mark.asyncio
@pytest.mark.parametrize("other_family", ("category", "evidence"))
@pytest.mark.parametrize("request_first", (False, True))
async def test_request_inverse_mixed_pressure_removes_only_live_aliases(monkeypatch, other_family, request_first):
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 1_048_576)

    def produce(_key):
        return RecorderRequestTextProof("r" * 325_000, ())

    def category(_text):
        return None

    def evidence(_text):
        return ()

    deps = RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset())
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for request in (request_first, not request_first, request_first):
            if request:
                parent.v4_recorder_request_text(RecorderRequestTextKey("q" * 325_000, 1_048_576), deps, produce)
            elif other_family == "category":
                parent.credential_string_category("s" * 650_000, category)
            else:
                parent.evidence_references(_evidence_key('"' + "e" * 650_000 + '"'), evidence)
            assert parent.cached_entry_count == 1
            assert bool(parent._v4_recorder_request_reverse) is request
            assert all(key in parent._v4_recorder_requests for key in parent._v4_recorder_request_reverse.values())
    assert not parent._v4_recorder_request_reverse


@pytest.mark.asyncio
async def test_request_inverse_rebound_alias_and_singleton_reset():
    def produce(_key):
        return RecorderRequestTextProof("shared-reference", ())

    def encode(key):
        return key.canonical.encode()

    deps = RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset())
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for text in ("first", "second"):
            parent.v4_recorder_request_text(RecorderRequestTextKey(text, 4096), deps, produce)
        with parent._lock:
            parent._discard_oldest_recorder_request()
        assert parent.v4_restored_recorder_request_text("shared-reference", 4096, deps, produce) == "second"
        # Deliberately transplant a real charged entry into an ordinary stage
        # to exercise its singleton-reset cleanup. Normal V4 scopes cannot
        # acquire that ordinary-stage state through their public API.
        ordinary = RetentionStageMemo()
        ordinary._v4_recorder_requests.update(parent._v4_recorder_requests)
        ordinary._v4_recorder_request_reverse.update(parent._v4_recorder_request_reverse)
        ordinary._v4_recorder_request_bytes = parent._v4_recorder_request_bytes
        ordinary.canonical_text_encoding(CanonicalTextEncodingKey("reset", 100), encode)
        assert not ordinary._v4_recorder_requests and not ordinary._v4_recorder_request_reverse
        assert ordinary._v4_recorder_request_bytes == 0
        assert parent.v4_restored_recorder_request_text("shared-reference", 4096, deps, produce) == "second"
        ordinary.close()


def _evidence_key(canonical: str = '{"source":true}') -> PrivateChunkVerificationKey:
    chunk = encode_private_chunk(codec="composer.evidence.v2", canonical=canonical)
    return PrivateChunkVerificationKey(chunk.reference, chunk.canonical)


@pytest.mark.asyncio
async def test_evidence_reference_memo_keys_complete_reference_text_and_producer() -> None:
    key = _evidence_key()
    reference = encode_private_chunk(codec="composer.messages.v1", canonical="[]").reference
    result = (reference, reference)
    calls: list[str] = []

    def produce(text: str) -> tuple[PrivateChunkRef, ...]:
        calls.append(text)
        return result

    def replacement(text: str) -> tuple[PrivateChunkRef, ...]:
        calls.append("replacement:" + text)
        return ()

    variants = (
        key,
        replace(key, canonical='{"source":false}'),
        replace(key, reference=key.reference.model_copy(update={"codec": "composer.messages.v1"})),
        replace(key, reference=key.reference.model_copy(update={"digest": "f" * 64})),
        replace(key, reference=key.reference.model_copy(update={"byte_length": key.reference.byte_length + 1})),
    )
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for variant in variants:
            assert parent.evidence_references(variant, produce) is result
            assert parent.evidence_references(variant, produce) is result
        assert calls == [variant.canonical for variant in variants]
        assert parent.evidence_references(key, replacement) == ()
        assert parent.evidence_references(key, replacement) == ()
        assert len(calls) == len(variants) + 1
        assert parent.cached_entry_count == len(variants) + 1
        assert parent._evidence_references[(produce, key)].references == (reference, reference)
        with pytest.raises(ValidationError):
            reference.byte_length = 99
    assert parent.cached_entry_count == parent._evidence_reference_bytes == 0


@pytest.mark.asyncio
async def test_evidence_reference_failures_and_oversize_successes_do_not_publish() -> None:
    key = _evidence_key()
    marker = AuditIntegrityError("original evidence parser failure")
    failures = 0
    successes = 0

    def fail(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal failures
        failures += 1
        raise marker

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal successes
        successes += 1
        return (key.reference,)

    def large_result(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal successes
        successes += 1
        return (key.reference,) * 20_000

    oversized_text = replace(key, canonical="x" * (CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1))
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            with pytest.raises(AuditIntegrityError) as raised:
                parent.evidence_references(key, fail)
            assert raised.value is marker
            assert parent.evidence_references(oversized_text, produce) == (key.reference,)
            assert len(parent.evidence_references(key, large_result)) == 20_000
            assert parent.cached_entry_count == parent._evidence_reference_bytes == 0
        assert failures == 2 and successes == 4
        assert parent.evidence_references(key, produce) == (key.reference,)
        assert parent.evidence_references(key, produce) == (key.reference,)
        assert successes == 5 and parent.cached_entry_count == 1


@pytest.mark.asyncio
async def test_evidence_reference_foreign_results_never_enter_the_cache() -> None:
    key = _evidence_key()
    calls = 0

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal calls
        calls += 1
        return cast(tuple[PrivateChunkRef, ...], ({"mutable": "graph"},))

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            with pytest.raises(TypeError, match="immutable reference tuple"):
                parent.evidence_references(key, produce)
            assert parent.cached_entry_count == parent._evidence_reference_bytes == 0
        assert calls == 2


@pytest.mark.asyncio
async def test_evidence_reference_results_borrow_only_original_v4_parent() -> None:
    key = _evidence_key()
    calls = 0

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal calls
        calls += 1
        return (key.reference,)

    with retention_stage_memo_scope() as ordinary:
        ordinary.evidence_references(key, produce)
        ordinary.evidence_references(key, produce)
        assert ordinary.cached_entry_count == 0
    assert calls == 2
    clock = _advisor_clock()
    with v4_preparation_memo_scope(clock) as parent:
        for _ in range(2):
            with borrow_v4_preparation_memo_scope() as child:
                child.evidence_references(key, produce)
                assert child.cached_entry_count == 0 and parent.cached_entry_count == 1
            assert not child.active and parent.active
        assert calls == 3
        clock.finish_preparation()
        finish_v4_preparation_memo(clock)
        assert parent.cached_entry_count == parent._evidence_reference_bytes == 0
        parent.evidence_references(key, produce)
        assert calls == 4 and parent.cached_entry_count == 0
    with v4_preparation_memo_scope(_advisor_clock()) as other:
        other.evidence_references(key, produce)
        assert calls == 5


@pytest.mark.asyncio
async def test_evidence_reference_entry_larger_than_selected_pool_returns_uncached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 131_072)
    key = _evidence_key('"' + "e" * 650_000 + '"')
    calls = 0

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        nonlocal calls
        calls += 1
        return (key.reference,)

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            assert parent.evidence_references(key, produce) == (key.reference,)
            assert parent.cached_entry_count == parent._evidence_reference_bytes == 0
        assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("full", "canonical", "block", "request", "category"))
@pytest.mark.parametrize("evidence_first", (False, True))
async def test_evidence_reference_pressure_evicts_across_every_existing_family(
    monkeypatch: pytest.MonkeyPatch, family: str, evidence_first: bool
) -> None:
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", bound)
    key = _evidence_key('"' + "e" * 650_000 + '"')
    calls = {"evidence": 0, "other": 0}

    def evidence(_text: str) -> tuple[PrivateChunkRef, ...]:
        calls["evidence"] += 1
        return (key.reference,)

    def full(other: CanonicalTextEncodingKey) -> bytes:
        calls["other"] += 1
        return other.canonical.encode()

    def canonical(_text: str) -> None:
        calls["other"] += 1

    def block(_key: RecorderBlockHashKey) -> None:
        calls["other"] += 1

    def request(_key: RecorderRequestTextKey) -> RecorderRequestTextProof:
        calls["other"] += 1
        return RecorderRequestTextProof("r" * 325_000, ())

    def category(_text: str) -> None:
        calls["other"] += 1

    dependencies = RecorderRequestTextDependencies(full, full, hashlib.sha256, frozenset({"shape"}))
    with v4_preparation_memo_scope(_advisor_clock()) as parent:

        def admit(is_evidence: bool) -> None:
            with borrow_v4_preparation_memo_scope() as child:
                if is_evidence:
                    child.evidence_references(key, evidence)
                elif family == "full":
                    child.canonical_text_proof(CanonicalTextEncodingKey("f" * 350_000, bound), full, hashlib.sha256)
                elif family == "canonical":
                    child.canonical_text_verification(CanonicalTextVerificationKey("c" * 650_000, bound), canonical)
                elif family == "block":
                    child.recorder_block_hash(RecorderBlockHashKey("0" * 64, "b" * 650_000), block)
                elif family == "request":
                    child.v4_recorder_request_text(RecorderRequestTextKey("q" * 325_000, bound), dependencies, request)
                else:
                    child.credential_string_category("s" * 650_000, category)
            # Each successful entry retains over half the pool in payload alone.
            # Two entries therefore demonstrate missing cross-family accounting,
            # independently of the implementation's metadata-charge arithmetic.
            assert parent.cached_entry_count == 1

        admit(evidence_first)
        admit(not evidence_first)
        assert calls == {"evidence": 1, "other": 1}
        admit(not evidence_first)
        assert calls == {"evidence": 1, "other": 1}
        admit(evidence_first)
        assert calls["evidence" if evidence_first else "other"] == 2
    assert parent.cached_entry_count == parent._evidence_reference_bytes == 0


@pytest.mark.asyncio
async def test_evidence_reference_pressure_control_detects_omitted_new_family_charge(monkeypatch: pytest.MonkeyPatch) -> None:
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", bound)
    key = _evidence_key('"' + "e" * 650_000 + '"')

    def evidence(_text: str) -> tuple[PrivateChunkRef, ...]:
        return (key.reference,)

    def category(_text: str) -> None:
        return None

    for omit_charge in (False, True):
        with v4_preparation_memo_scope(_advisor_clock()) as parent:
            parent.evidence_references(key, evidence)
            assert parent._evidence_reference_bytes > 650_000
            if omit_charge:
                monkeypatch.setattr(parent, "_evidence_reference_bytes", 0)
            parent.credential_string_category("s" * 650_000, category)

            def require_one_entry() -> None:
                assert parent.cached_entry_count == 1

            if omit_charge:
                with pytest.raises(AssertionError):
                    require_one_entry()
            else:
                require_one_entry()


@pytest.mark.asyncio
async def test_evidence_reference_overlapping_misses_publish_one_result() -> None:
    key = _evidence_key()
    barrier = threading.Barrier(2)

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        result = (key.reference,)
        barrier.wait(timeout=5)
        return result

    with v4_preparation_memo_scope(_advisor_clock()) as parent, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(parent.evidence_references, key, produce)
        second = executor.submit(parent.evidence_references, key, produce)
        assert first.result(timeout=5) is second.result(timeout=5)
        assert parent.cached_entry_count == 1
        assert parent._evidence_reference_bytes == parent._evidence_references[(produce, key)].retained_bytes


@pytest.mark.asyncio
async def test_evidence_reference_close_during_borrowed_miss_cannot_publish() -> None:
    key = _evidence_key()
    entered, release = threading.Event(), threading.Event()

    def produce(_text: str) -> tuple[PrivateChunkRef, ...]:
        entered.set()
        assert release.wait(5)
        return (key.reference,)

    with (
        v4_preparation_memo_scope(_advisor_clock()) as parent,
        borrow_v4_preparation_memo_scope() as child,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(child.evidence_references, key, produce)
        try:
            assert entered.wait(5)
            parent.close()
        finally:
            release.set()
        assert future.result(timeout=5) == (key.reference,)
        assert parent.cached_entry_count == child.cached_entry_count == 0
        assert parent._evidence_reference_bytes == 0


@pytest.mark.asyncio
async def test_semantic_preparation_borrows_v4_text_but_keeps_fresh_local_proofs() -> None:
    clock = _advisor_clock()
    key = _evidence_key()
    changed_reference = replace(key, reference=key.reference.model_copy(update={"digest": "1" * 64}))
    changed_text = replace(key, canonical='{"changed":true}')
    calls: list[str] = []
    local_calls: list[str] = []
    children: list[RetentionStageMemo] = []

    def references(text: str) -> tuple[PrivateChunkRef, ...]:
        calls.append(text)
        return (key.reference,)

    def verify_local(text: str) -> None:
        local_calls.append(text)

    with v4_preparation_memo_scope(clock) as parent:

        def actual_cut() -> int:
            assert current_v4_preparation_memo(clock) is parent
            with borrow_v4_preparation_memo_scope():
                child = current_retention_stage_memo()
                assert child is not None and child is not parent
                assert current_v4_preparation_memo(clock) is parent
                children.append(child)
                for candidate in (key, key, changed_reference, changed_text):
                    assert child.evidence_references(candidate, references) == (key.reference,)
                child.private_chunk(key, verify_local)
                child.private_chunk(key, verify_local)
            assert not child.active and child.cached_entry_count == 0
            return len(children)

        for expected in (1, 2):
            outcome = await async_workers.run_required_sql_finish_once(publication_ticket(), actual_cut)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == expected
        assert children[0] is not children[1]
        assert calls == [key.canonical, key.canonical, changed_text.canonical]
        assert local_calls == [key.canonical, key.canonical]
        assert parent.cached_entry_count == 3
    assert not parent.active and parent.cached_entry_count == 0
    with borrow_v4_preparation_memo_scope():
        ordinary = current_retention_stage_memo()
        assert ordinary is not None and current_v4_preparation_memo() is None
        ordinary.evidence_references(key, references)
        ordinary.evidence_references(key, references)
        assert ordinary.cached_entry_count == 0
    assert not ordinary.active and len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_kind", ("success", "timeout", "failure", "cancel"))
async def test_semantic_inflight_v4_parent_closure_preserves_original_outcome(exit_kind: str) -> None:
    clock = _advisor_clock()
    marker = asyncio.CancelledError("original cancellation") if exit_kind == "cancel" else RuntimeError("original failure")
    observed: BaseException | None = None
    try:
        with v4_preparation_memo_scope(clock) as parent, borrow_v4_preparation_memo_scope():
            child = current_retention_stage_memo()
            assert child is not None and current_v4_preparation_memo(clock) is parent
            child.canonical_text_proof(CanonicalTextEncodingKey("{}", 100), _semantic_text_bytes, hashlib.sha256)
            assert parent.cached_entry_count == 1
            if exit_kind in {"failure", "cancel"}:
                raise marker
            if exit_kind == "timeout":
                clock.preparation_deadline_monotonic = clock.started_monotonic - 1
            try:
                clock.finish_preparation()
            finally:
                finish_v4_preparation_memo(clock)
            assert not parent.active and parent.cached_entry_count == 0
    except BaseException as error:
        observed = error
    if exit_kind == "success":
        assert observed is None and clock._seen == {"preparation"}
    elif exit_kind == "timeout":
        assert type(observed) is TimeoutError and clock._seen == set()
    else:
        assert observed is marker and clock._seen == set()
    assert not parent.active and not child.active
    assert parent.cached_entry_count == child.cached_entry_count == 0
    assert current_retention_stage_memo() is None
    with v4_preparation_memo_scope(_advisor_clock()) as following:
        assert following is not parent and following.cached_entry_count == 0


def _semantic_text_bytes(key: CanonicalTextEncodingKey) -> bytes:
    return key.canonical.encode("utf-8")


@pytest.mark.asyncio
async def test_semantic_inflight_v4_late_result_cannot_repopulate_finished_parent() -> None:
    entered, release = threading.Event(), threading.Event()
    clock = _advisor_clock()
    key = _evidence_key()

    def delayed(_text: str) -> tuple[PrivateChunkRef, ...]:
        entered.set()
        assert release.wait(5)
        return (key.reference,)

    with v4_preparation_memo_scope(clock) as parent:
        with borrow_v4_preparation_memo_scope(), ThreadPoolExecutor(max_workers=1) as executor:
            child = current_retention_stage_memo()
            assert child is not None
            future = executor.submit(child.evidence_references, key, delayed)
            try:
                assert entered.wait(5)
                clock.finish_preparation()
                finish_v4_preparation_memo(clock)
            finally:
                release.set()
            assert future.result(timeout=5) == (key.reference,)
            assert not parent.active and parent.cached_entry_count == child.cached_entry_count == 0
        assert not child.active


def _recorder_data_chunk(text="recorder data"):
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from elspeth.web.composer.withheld_replies import WithheldReply
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot

    return encode_recorder_snapshot(replace(_snapshot(0), withheld_replies=(WithheldReply("planner_prose_unadmitted", text),)))


def _produce_uncached_recorder_data(text):
    from elspeth.web.composer.continuation_audit_buffer import _construct_recorder_document

    with memo_module.without_retention_stage_memo():
        return _construct_recorder_document(text)


def _recorder_factory(key):
    def construct():
        return _produce_uncached_recorder_data(key.canonical)

    return construct


@pytest.mark.asyncio
async def test_public_recorder_retains_admitted_canonical_identity_and_charges_actual_storage_key(monkeypatch):
    from sys import getsizeof

    from elspeth.web.composer import continuation_audit_buffer as audit_buffer
    from elspeth.web.composer.continuation_codec import PrivateChunk

    chunk = _recorder_data_chunk("café☃" * 1_000)
    original_construct = audit_buffer._construct_recorder_document
    admissions = []

    def construct(source):
        data = original_construct(source)
        admissions.append(data)
        return data

    monkeypatch.setattr(audit_buffer, "_construct_recorder_document", construct)
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        first = audit_buffer.decode_recorder_snapshot(chunk)
        ((stored_key, entry),) = parent._recorder_data.items()
        actual_data = admissions[0]
        assert stored_key.reference is actual_data.chunk.reference
        assert stored_key.canonical is actual_data.chunk.canonical
        assert stored_key.canonical == chunk.canonical and stored_key.canonical is not chunk.canonical
        assert entry.data is actual_data and first is actual_data.snapshot
        actual_charge = memo_module.recorder_data_retained_bytes(stored_key, actual_data, limit=parent._shared_text_budget_bytes)
        duplicate_charge = memo_module.recorder_data_retained_bytes(
            PrivateChunkVerificationKey(stored_key.reference, chunk.canonical), actual_data, limit=parent._shared_text_budget_bytes
        )
        assert type(actual_charge) is type(duplicate_charge) is int
        assert entry.retained_bytes == parent._recorder_data_bytes == actual_charge
        assert duplicate_charge - actual_charge == getsizeof(chunk.canonical)
        incoming = chunk.canonical.encode("utf-8").decode("utf-8")
        assert incoming == chunk.canonical and incoming is not chunk.canonical
        assert audit_buffer.decode_recorder_snapshot(PrivateChunk(chunk.reference, incoming)) is first
        assert admissions == [actual_data]
        assert len(parent._recorder_data) == 1 and parent._recorder_data_bytes == actual_charge
    assert not parent._recorder_data and parent._recorder_data_bytes == 0


@pytest.mark.asyncio
async def test_recorder_document_factory_must_return_the_actual_complete_source():
    chunk = _recorder_data_chunk()
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    other = _recorder_data_chunk("another valid document")
    actual_data = _produce_uncached_recorder_data(other.canonical)

    def produce():
        return actual_data

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        with pytest.raises(AuditIntegrityError, match="actual source"):
            parent.recorder_data(key, produce)
        assert not parent._recorder_data and parent._recorder_data_bytes == 0
        accepted = parent.recorder_data(key, _recorder_factory(key))
        assert accepted.chunk == chunk


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ("full", "canonical", "block", "request", "credential", "evidence"))
@pytest.mark.parametrize("recorder_first", (False, True))
async def test_recorder_data_pressure_evicts_each_family_in_both_directions(monkeypatch, family, recorder_first):
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", bound)
    chunk = _recorder_data_chunk("r" * 300_000)
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    evidence_key = _evidence_key('"' + "e" * 650_000 + '"')
    calls = {"recorder": 0, "other": 0}

    def recorder():
        calls["recorder"] += 1
        return _produce_uncached_recorder_data(key.canonical)

    def full(other):
        calls["other"] += 1
        return other.canonical.encode()

    def verify(_key):
        calls["other"] += 1

    def request(_key):
        calls["other"] += 1
        return RecorderRequestTextProof("r" * 325_000, ())

    def evidence(_text):
        calls["other"] += 1
        return (evidence_key.reference,)

    dependencies = RecorderRequestTextDependencies(full, full, hashlib.sha256, frozenset({"shape"}))
    with v4_preparation_memo_scope(_advisor_clock()) as parent:

        def insert(is_recorder):
            with borrow_v4_preparation_memo_scope() as child:
                if is_recorder:
                    child.recorder_data(key, recorder)
                    assert parent._recorder_data_bytes > bound // 2
                elif family == "full":
                    child.canonical_text_proof(CanonicalTextEncodingKey("f" * 350_000, bound), full, hashlib.sha256)
                elif family == "canonical":
                    child.canonical_text_verification(CanonicalTextVerificationKey("c" * 650_000, bound), verify)
                elif family == "block":
                    child.recorder_block_hash(RecorderBlockHashKey("0" * 64, "b" * 650_000), verify)
                elif family == "request":
                    child.v4_recorder_request_text(RecorderRequestTextKey("q" * 325_000, bound), dependencies, request)
                elif family == "credential":
                    child.credential_string_category("s" * 650_000, verify)
                else:
                    child.evidence_references(evidence_key, evidence)
                assert not child._recorder_data
            assert parent.cached_entry_count == 1
            assert bool(parent._v4_recorder_requests) == bool(parent._v4_recorder_request_reverse)

        insert(recorder_first)
        insert(not recorder_first)
        insert(not recorder_first)
        assert calls == {"recorder": 1, "other": 1}
        insert(recorder_first)
        assert calls["recorder" if recorder_first else "other"] == 2
    assert parent.cached_entry_count == parent._recorder_data_bytes == 0


@pytest.mark.asyncio
async def test_recorder_data_counter_omission_is_detected_by_actual_entry_weights(monkeypatch):
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", bound)
    chunk = _recorder_data_chunk("r" * 300_000)
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)

    def verify(_text):
        return None

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        parent.recorder_data(key, _recorder_factory(key))
        actual_charge = parent._recorder_data_bytes
        assert actual_charge > bound // 2
        monkeypatch.setattr(parent, "_recorder_data_bytes", 0)
        parent.canonical_text_verification(CanonicalTextVerificationKey("c" * 650_000, bound), verify)
        with pytest.raises(AssertionError):
            assert (
                sum(entry.retained_bytes for entry in parent._recorder_data.values()) + sum(parent._canonical_verifications.values())
                <= bound
            )


@pytest.mark.asyncio
async def test_recorder_data_lru_and_singleton_reset_keep_fixed_charges(monkeypatch):
    chunks = [_recorder_data_chunk(letter * 100) for letter in ("a", "b", "c")]
    keys = [PrivateChunkVerificationKey(chunk.reference, chunk.canonical) for chunk in chunks]
    data = [_produce_uncached_recorder_data(key.canonical) for key in keys]
    weights = [
        memo_module.recorder_data_retained_bytes(
            PrivateChunkVerificationKey(value.chunk.reference, value.chunk.canonical), value, limit=32 * 1_048_576
        )
        for key, value in zip(keys, data, strict=True)
    ]
    assert all(type(weight) is int for weight in weights)
    monkeypatch.setattr(memo_module, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 2 * max(weights))

    def encode(key):
        return key.canonical.encode()

    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        for key in (keys[0], keys[1], keys[0], keys[2]):
            parent.recorder_data(key, _recorder_factory(key))
        assert list(parent._recorder_data) == [keys[0], keys[2]]
        fixed = parent._recorder_data_bytes
        parent.recorder_data(keys[2], _recorder_factory(keys[2]))
        assert parent._recorder_data_bytes == fixed == sum(entry.retained_bytes for entry in parent._recorder_data.values())
        # A real entry transplanted into the ordinary carrier opposes its
        # legacy singleton reset; the public ordinary path never populates it.
        ordinary = RetentionStageMemo()
        ordinary._recorder_data.update(parent._recorder_data)
        ordinary._recorder_data_bytes = fixed
        ordinary.canonical_text_encoding(CanonicalTextEncodingKey("reset", 100), encode)
        assert not ordinary._recorder_data and ordinary._recorder_data_bytes == 0
        assert parent._recorder_data_bytes == fixed
        ordinary.close()


def test_recorder_weight_counts_unicode_proxy_backing_metadata_and_stops_before_expansion(monkeypatch):
    from elspeth.web.composer import continuation_audit_buffer as audit_buffer
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot

    snapshot = _snapshot(1)
    call = replace(snapshot.llm_calls[0], reasoning_details={"large": "𐀀" * 2_000, "nested": [{"value": "café"}]})
    chunk = audit_buffer.encode_recorder_snapshot(replace(snapshot, llm_calls=(call,)))
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    data = audit_buffer._construct_recorder_document(chunk)
    charge = memo_module.recorder_data_retained_bytes(key, data, limit=32 * 1_048_576)
    nested = data.snapshot.llm_calls[0].reasoning_details
    assert type(nested) is MappingProxyType
    minimum = (
        getsizeof(key.reference)
        + getsizeof(key.reference.__dict__)
        + getsizeof(key.reference.__pydantic_fields_set__)
        + getsizeof(key.canonical)
        + getsizeof(data.chunk)
        + getsizeof(data.chunk.canonical)
        + getsizeof(nested)
        + getsizeof(dict(nested))
        + getsizeof(nested["large"])
        + getsizeof(data.snapshot.llm_calls[0].private_request.canonical)
    )
    assert charge is not None and charge > minimum
    visited = []
    original = audit_buffer._recorder_data_children

    def children(value):
        visited.append(value)
        return original(value)

    monkeypatch.setattr(audit_buffer, "_recorder_data_children", children)
    assert memo_module.recorder_data_retained_bytes(key, data, limit=1_024) == 1_025
    assert not visited  # Source storage crosses cutoff before expanding DTOs.
    foreign = audit_buffer.RecorderDocumentData(data.chunk, replace(data.snapshot, llm_calls=(replace(call, reasoning_details=object()),)))
    assert memo_module.recorder_data_retained_bytes(key, foreign, limit=32 * 1_048_576) is None


def test_recorder_proxy_and_index_allowances_dominate_actual_container_allocations():
    # Small, resize-boundary and larger containers oppose the fixed arithmetic
    # allowance using this runtime's actual allocations, not a copy of its sum.
    for count in (0, 1, 5, 6, 21, 22, 85, 86, 341, 342, 4_096):
        mapping = {f"key-{index}": None for index in range(count)}
        assert getsizeof({}) + count * getsizeof({None: None}) >= getsizeof(mapping)
        if count:
            index = OrderedDict(mapping)
            assert count * getsizeof(OrderedDict(((None, None),))) >= getsizeof(index)


@pytest.mark.asyncio
async def test_recorder_data_admission_failure_ordinary_and_closed_paths_do_not_retain():
    chunk = _recorder_data_chunk()
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    produced = []
    marker = RuntimeError("actual constructor failure")

    def produce():
        data = _produce_uncached_recorder_data(key.canonical)
        produced.append(data)
        return data

    def refuse():
        produce()
        raise marker

    ordinary = RetentionStageMemo()
    for _ in range(2):
        ordinary.recorder_data(key, produce)
    assert ordinary.cached_entry_count == 0 and len(produced) == 2
    with v4_preparation_memo_scope(_advisor_clock()) as parent:
        with pytest.raises(RuntimeError) as caught:
            parent.recorder_data(key, refuse)
        assert caught.value is marker and not parent._recorder_data and parent._recorder_data_bytes == 0
        with borrow_v4_preparation_memo_scope() as child:
            retained = child.recorder_data(key, produce)
        assert not child.active and parent.active
        assert parent.recorder_data(key, produce) is retained
        # A hit consumes no new constructor, irrespective of closure identity.
        assert parent.recorder_data(key, refuse) is retained
        assert len(produced) == 4 and parent.cached_entry_count == 1
        child.recorder_data(key, produce)
        assert not child._recorder_data and parent.cached_entry_count == 1
    assert parent.cached_entry_count == parent._recorder_data_bytes == 0
    parent.recorder_data(key, produce)
    assert not parent._recorder_data and parent._recorder_data_bytes == 0
    assert len(produced) == 6


@pytest.mark.asyncio
async def test_recorder_data_concurrent_misses_return_each_admitted_value_but_publish_one():
    chunk = _recorder_data_chunk()
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    rendezvous = threading.Barrier(2)
    admitted = []

    def produce():
        data = _produce_uncached_recorder_data(key.canonical)
        admitted.append(data)
        rendezvous.wait(timeout=5)
        return data

    with v4_preparation_memo_scope(_advisor_clock()) as parent, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(parent.recorder_data, key, produce)
        second = executor.submit(parent.recorder_data, key, produce)
        left, right = first.result(timeout=5), second.result(timeout=5)
        assert left == right and left is not right
        assert {id(value) for value in admitted} == {id(left), id(right)}
        assert len(parent._recorder_data) == 1
        ((storage_key, entry),) = parent._recorder_data.items()
        assert entry.data is left or entry.data is right
        assert storage_key.canonical is entry.data.chunk.canonical and storage_key.reference is entry.data.chunk.reference
        assert (
            parent._recorder_data_bytes
            == entry.retained_bytes
            == memo_module.recorder_data_retained_bytes(storage_key, entry.data, limit=parent._shared_text_budget_bytes)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ("before_construction", "after_construction"))
async def test_recorder_data_parent_close_during_borrowed_work_cannot_repopulate(pause_at):
    chunk = _recorder_data_chunk()
    key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    entered, release = threading.Event(), threading.Event()
    admitted = []

    def pause():
        entered.set()
        assert release.wait(5)

    def produce():
        if pause_at == "before_construction":
            pause()
        data = _produce_uncached_recorder_data(key.canonical)
        if pause_at == "after_construction":
            pause()
        admitted.append(data)
        return data

    with (
        v4_preparation_memo_scope(_advisor_clock()) as parent,
        borrow_v4_preparation_memo_scope() as child,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        future = executor.submit(child.recorder_data, key, produce)
        try:
            assert entered.wait(5)
            parent.close()
        finally:
            release.set()
        result = future.result(timeout=5)
        assert admitted == [result]
        assert parent.cached_entry_count == parent._recorder_data_bytes == 0
        assert not parent._recorder_data and not child._recorder_data
