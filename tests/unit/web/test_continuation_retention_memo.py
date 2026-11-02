"""Dormant carrier and actual finish-once hop controls; stage activation is separate."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from typing import cast

import pytest
from pydantic import ValidationError

from elspeth.contracts.credential_material import CredentialMaterialFinding, CredentialTraversalPolicy, find_credential_material
from elspeth.web import async_workers
from elspeth.web.composer import prompt_guard
from elspeth.web.composer.continuation import ContinuationFrameStack, InitializeFrame
from elspeth.web.composer.continuation_codec import (
    _decode_continuation_snapshot_uncached,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.continuation_retention_memo import (
    CanonicalTextEncodingKey,
    CredentialInputToken,
    CredentialPolicyKey,
    CredentialScanKey,
    PrivateChunkVerificationKey,
    RecorderBlockHashKey,
    RecorderRequestTextKey,
    RecorderRequestTextProof,
    RetentionStageMemo,
    SnapshotDecodeKey,
    StateCredentialStepKey,
    current_retention_stage_memo,
    prepare_retention_sql_callable,
    retention_stage_memo_scope,
)
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.sessions import service
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.test_required_sql_finish_once import publication_ticket


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
