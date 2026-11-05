"""Dormant carrier and actual finish-once hop controls; stage activation is separate."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
from itertools import permutations
from sys import getsizeof
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
    CONTINUATION_LEGACY_ADVISOR_TEXT_MEMO_MAX_RETAINED_BYTES,
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
    borrow_legacy_advisor_inflight_memo,
    borrow_v4_preparation_memo_scope,
    current_legacy_advisor_preparation_memo,
    current_retention_stage_memo,
    current_v4_preparation_memo,
    finish_legacy_advisor_preparation_memo,
    finish_v4_preparation_memo,
    legacy_advisor_preparation_memo_scope,
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
async def test_legacy_advisor_wider_shared_text_pool_keeps_individual_and_default_limits() -> None:
    clock = _advisor_clock()
    bound = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    retained_bound = CONTINUATION_LEGACY_ADVISOR_TEXT_MEMO_MAX_RETAINED_BYTES
    proof_weight = 250_000 * 2 + 64 + len(str(bound))
    fits = retained_bound // proof_weight
    assert fits == 10 and fits * proof_weight <= retained_bound < (fits + 1) * proof_weight
    keys = [CanonicalTextEncodingKey(chr(ord("a") + index) * 250_000, bound) for index in range(fits + 2)]
    calls: list[str] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(key.canonical[:1])
        return key.canonical.encode("utf-8")

    with legacy_advisor_preparation_memo_scope(clock) as parent:
        for key in keys[:fits]:
            parent.canonical_text_proof(key, encode, hashlib.sha256)
        assert len(parent._canonical_proofs) == fits
        assert parent._canonical_proof_bytes == fits * proof_weight
        for key in keys[:fits]:
            parent.canonical_text_proof(key, encode, hashlib.sha256)
        assert len(calls) == fits
        parent.canonical_text_proof(keys[fits], encode, hashlib.sha256)
        assert len(parent._canonical_proofs) == fits
        assert (encode, hashlib.sha256, keys[0]) not in parent._canonical_proofs
        parent.canonical_text_proof(keys[fits + 1], encode, hashlib.sha256)
        assert (encode, hashlib.sha256, keys[1]) not in parent._canonical_proofs
        assert parent._canonical_proof_bytes <= retained_bound
        with borrow_legacy_advisor_inflight_memo(parent, clock):
            child = current_retention_stage_memo()
            assert child is not None and child is not parent
            child.canonical_text_proof(keys[0], encode, hashlib.sha256)
            assert not child._canonical_proofs and len(parent._canonical_proofs) == fits
        assert not child.active
    assert parent.cached_entry_count == 0

    with retention_stage_memo_scope() as default:
        for key in keys[:fits]:
            default.canonical_text_proof(key, encode, hashlib.sha256)
        assert len(default._canonical_proofs) == bound // proof_weight == 2
        assert default._canonical_proof_bytes <= bound
        assert (encode, hashlib.sha256, keys[0]) not in default._canonical_proofs

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

    with legacy_advisor_preparation_memo_scope(_advisor_clock()) as parent:
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
async def test_legacy_advisor_four_fresh_sql_cuts_share_only_exact_immutable_text() -> None:
    clock = _advisor_clock()
    worker_only = ContextVar("legacy-advisor-sql-cut", default="fresh")
    calls = {"text": 0, "block": 0, "request": 0, "snapshot": 0, "chunk": 0, "scan": 0, "state": 0}
    text = '{"whole":"input"}'
    text_key = CanonicalTextEncodingKey(text, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    block_key = RecorderBlockHashKey(hashlib.sha256(text.encode()).hexdigest(), text)
    request_key = RecorderRequestTextKey(text, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
    snapshot_key = _snapshot_key()
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    chunk_key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    scan_key = _scan_key()
    state_key = StateCredentialStepKey(scan_key)

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls["text"] += 1
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

    with legacy_advisor_preparation_memo_scope(clock) as memo:

        def sql_cut(ordinal: int) -> int:
            assert worker_only.get() == "fresh"
            assert current_legacy_advisor_preparation_memo(clock) is memo
            if ordinal == 4:
                with borrow_legacy_advisor_inflight_memo(memo, clock):
                    child = current_retention_stage_memo()
                    assert child is not None and child is not memo
                    assert current_legacy_advisor_preparation_memo(clock) is None
            worker_only.set(f"cut-{ordinal}")
            assert memo.canonical_text_proof(text_key, encode, hashlib.sha256).digest == block_key.expected_digest
            memo.recorder_block_hash(block_key, verify_block)
            assert memo.recorder_request_text(request_key, prepare_request) == RecorderRequestTextProof("{}", ())
            memo.snapshot(snapshot_key, decode)
            memo.private_chunk(chunk_key, verify_chunk)
            memo.credential(scan_key, scan)
            memo.state_credentials(state_key, scan_state)
            return ordinal

        for ordinal in range(1, 5):
            outcome = await async_workers.run_required_sql_finish_once(publication_ticket(), lambda ordinal=ordinal: sql_cut(ordinal))
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value == ordinal
        assert calls == {"text": 1, "block": 1, "request": 1, "snapshot": 4, "chunk": 4, "scan": 4, "state": 4}
        assert memo.cached_entry_count == 3
        clock.finish_preparation()
        finish_legacy_advisor_preparation_memo(clock)
        assert current_retention_stage_memo() is None and memo.cached_entry_count == 0
        assert not memo.active
    assert current_retention_stage_memo() is None


@pytest.mark.asyncio
async def test_legacy_advisor_nested_cut_has_fresh_full_local_proofs() -> None:
    clock = _advisor_clock()
    calls = {"text": 0, "snapshot": 0, "chunk": 0, "scan": 0, "state": 0}
    text_key = CanonicalTextEncodingKey('{"whole":"same"}', 100)
    snapshot_key = _snapshot_key()
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    chunk_key = PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    scan_key = _scan_key()
    state_key = StateCredentialStepKey(scan_key)
    children: list[RetentionStageMemo] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls["text"] += 1
        return key.canonical.encode()

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

    with legacy_advisor_preparation_memo_scope(clock) as parent:
        for _cut in range(2):
            with borrow_legacy_advisor_inflight_memo(parent, clock):
                child = current_retention_stage_memo()
                assert child is not None and child is not parent
                children.append(child)
                for _repeat in range(2):
                    child.canonical_text_proof(text_key, encode, hashlib.sha256)
                    child.snapshot(snapshot_key, decode)
                    child.private_chunk(chunk_key, verify_chunk)
                    child.credential(scan_key, scan)
                    child.state_credentials(state_key, scan_state)
                assert child.cached_entry_count > 0
                assert parent.cached_entry_count == 1
            assert current_retention_stage_memo() is parent and not child.active
        assert calls == {"text": 1, "snapshot": 2, "chunk": 2, "scan": 2, "state": 2}
        assert children[0] is not children[1] and all(item.cached_entry_count == 0 for item in children)
    assert parent.cached_entry_count == 0 and current_retention_stage_memo() is None


@pytest.mark.asyncio
async def test_legacy_advisor_text_identity_nested_borrow_and_late_close() -> None:
    clock, foreign_clock = _advisor_clock(), _advisor_clock()
    calls: list[tuple[str, str]] = []

    def encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(("encode", key.canonical))
        return key.canonical.encode()

    def other_encode(key: CanonicalTextEncodingKey) -> bytes:
        calls.append(("other", key.canonical))
        return key.canonical.encode()

    first = CanonicalTextEncodingKey("whole", 100)
    changed = CanonicalTextEncodingKey("changed", 100)
    reference = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}').reference
    referred = replace(first, reference=reference)
    entered, release = threading.Event(), threading.Event()

    def delayed(key: CanonicalTextEncodingKey) -> bytes:
        entered.set()
        assert release.wait(5)
        return key.canonical.encode()

    with legacy_advisor_preparation_memo_scope(clock) as memo:
        assert memo.canonical_text_proof(first, encode, hashlib.sha256) is memo.canonical_text_proof(first, encode, hashlib.sha256)
        memo.canonical_text_proof(changed, encode, hashlib.sha256)
        memo.canonical_text_proof(referred, encode, hashlib.sha256)
        memo.canonical_text_proof(first, other_encode, hashlib.sha256)
        assert calls == [("encode", "whole"), ("encode", "changed"), ("encode", "whole"), ("other", "whole")]
        with retention_stage_memo_scope() as unrelated:
            with pytest.raises(AuditIntegrityError, match="foreign preparation memo"), borrow_legacy_advisor_inflight_memo(memo, clock):
                pass
            assert unrelated is current_retention_stage_memo()
        with pytest.raises(AuditIntegrityError, match="original action clock"):
            current_legacy_advisor_preparation_memo(foreign_clock)
        with borrow_legacy_advisor_inflight_memo(memo, clock):
            child = current_retention_stage_memo()
            assert child is not None and child is not memo
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(child.canonical_text_encoding, CanonicalTextEncodingKey("late", 100), delayed)
                assert entered.wait(5)
                memo.close()
                release.set()
                assert future.result(timeout=5) == b"late"
            assert child.active and child.cached_entry_count == 0
        assert memo.cached_entry_count == 0 and current_retention_stage_memo() is None
    assert not memo.active and not child.active


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
                assert entry.retained_bytes == 400_000 + len(str(bound)) + len("shape")

        for family in order:
            admit(family)
        assert calls == dict.fromkeys(order, 1)
        assert parent.cached_entry_count == 2
        for family in order:
            admit(family)
        admit("request", "z")
        assert max(calls.values()) > 1
    assert parent.cached_entry_count == parent._v4_recorder_request_bytes == 0


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
    with legacy_advisor_preparation_memo_scope(_advisor_clock()) as legacy:
        legacy.evidence_references(key, produce)
        legacy.evidence_references(key, produce)
        assert legacy.cached_entry_count == 0
    assert calls == 4
    clock = _advisor_clock()
    with v4_preparation_memo_scope(clock) as parent:
        for _ in range(2):
            with borrow_v4_preparation_memo_scope() as child:
                child.evidence_references(key, produce)
                assert child.cached_entry_count == 0 and parent.cached_entry_count == 1
            assert not child.active and parent.active
        assert calls == 5
        clock.finish_preparation()
        finish_v4_preparation_memo(clock)
        assert parent.cached_entry_count == parent._evidence_reference_bytes == 0
        parent.evidence_references(key, produce)
        assert calls == 6 and parent.cached_entry_count == 0
    with v4_preparation_memo_scope(_advisor_clock()) as other:
        other.evidence_references(key, produce)
        assert calls == 7


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
