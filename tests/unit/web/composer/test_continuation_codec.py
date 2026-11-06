import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web import async_workers, credential_guard
from elspeth.web.composer import continuation_codec as codec
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import encode_private_chunk, verify_private_chunk
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy
from elspeth.web.continuation_retention_memo import (
    borrow_v4_preparation_memo_scope,
    retention_stage_memo_scope,
    v4_preparation_memo_scope,
)
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from tests.unit.web.composer.test_continuation_frames import initial_stack
from tests.unit.web.test_credential_guard import _field_mapper_state
from tests.unit.web.test_required_sql_finish_once import publication_ticket


@pytest.mark.parametrize("noncanonical", [False, True])
def test_retention_malformed_unused_codec_never_compares_foreign_field_classes(noncanonical):
    callbacks = []

    class ForeignMeta(type):
        __hash__ = type.__hash__

        def __eq__(self, other):
            callbacks.append("foreign class equality")
            raise AssertionError("memo eligibility must not invoke the foreign class")

    class ForeignCodec(metaclass=ForeignMeta):
        pass

    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    reference = chunk.reference.model_copy(update={"codec": ForeignCodec()})
    canonical = '{"safe": true}' if noncanonical else chunk.canonical

    def inspect():
        if noncanonical:
            with pytest.raises(AuditIntegrityError, match="canonical strict JSON") as caught:
                verify_private_chunk(reference, canonical)
            return type(caught.value), str(caught.value)
        return verify_private_chunk(reference, canonical)

    ordinary = inspect()
    assert callbacks == []
    with retention_stage_memo_scope() as memo:
        assert inspect() == ordinary
        assert memo.cached_entry_count == 0
    assert callbacks == []


def test_exact_native_order_and_omitted_tools_are_distinct():
    one = canonical_json(
        {
            "messages": [
                {
                    "role": "assistant",
                    "thinking_blocks": [
                        {"type": "thinking", "thinking": "first", "signature": "s1"},
                        {"type": "redacted_thinking", "data": "second"},
                    ],
                }
            ],
            "tools": None,
        }
    )
    two = canonical_json(
        {
            "messages": [
                {
                    "role": "assistant",
                    "thinking_blocks": [
                        {"type": "redacted_thinking", "data": "second"},
                        {"type": "thinking", "thinking": "first", "signature": "s1"},
                    ],
                }
            ],
            "tools": [],
        }
    )
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=one)
    verify_private_chunk(chunk.reference, one)
    with pytest.raises(AuditIntegrityError):
        verify_private_chunk(chunk.reference, two)


def test_private_chunk_admission_inspects_the_complete_tail_before_retention():
    safe = canonical_json({"prefix": "x" * 8192, "tail": {"description": "synthetic value"}})
    assert encode_private_chunk(codec="composer.messages.v1", canonical=safe).canonical == safe
    unsafe = canonical_json({"prefix": "x" * 8192, "tail": {"api_key": "synthetic-literal-credential"}})
    with pytest.raises(CredentialMaterialRefused):
        encode_private_chunk(codec="composer.messages.v1", canonical=unsafe)


def test_private_chunk_stage_reuses_text_bytes_but_rechecks_each_credential_policy(monkeypatch):
    text = canonical_json({"text": "ordinary value"})
    proof_calls: list[str] = []
    credential_calls: list[frozenset[str]] = []
    original_proof = codec._private_chunk_canonical_text_bytes
    original_guard = credential_guard.require_no_credential_material

    def counted_proof(key):
        proof_calls.append(key.canonical)
        return original_proof(key)

    def counted_guard(value, *, surface, env_ref_names, max_string_bytes):
        credential_calls.append(env_ref_names)
        return original_guard(value, surface=surface, env_ref_names=env_ref_names, max_string_bytes=max_string_bytes)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", counted_proof)
    monkeypatch.setattr(credential_guard, "require_no_credential_material", counted_guard)
    with retention_stage_memo_scope() as memo:
        first = encode_private_chunk(codec="composer.messages.v1", canonical=text, env_ref_names=frozenset({"FIRST"}))
        second = encode_private_chunk(codec="composer.messages.v1", canonical=text, env_ref_names=frozenset({"SECOND"}))
        assert first == second and first is not second
        assert proof_calls == [text]
        assert credential_calls == [frozenset({"FIRST"}), frozenset({"SECOND"})]
    assert memo.cached_entry_count == 0


def test_private_chunk_encoding_proof_reuse_still_rejects_each_changed_reference(monkeypatch):
    text = canonical_json({"text": "ordinary value"})
    proof_calls: list[str] = []
    original_proof = codec._private_chunk_canonical_text_bytes

    def counted_proof(key):
        proof_calls.append(key.canonical)
        return original_proof(key)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", counted_proof)
    with retention_stage_memo_scope() as memo:
        chunk = encode_private_chunk(codec="composer.messages.v1", canonical=text)
        verify_private_chunk(chunk.reference, text)
        for changed in (
            chunk.reference.model_copy(update={"digest": "0" * 64}),
            chunk.reference.model_copy(update={"byte_length": chunk.reference.byte_length + 1}),
        ):
            with pytest.raises(AuditIntegrityError, match="bytes differ from their binding"):
                verify_private_chunk(changed, text)
        verify_private_chunk(chunk.reference, text)
        assert proof_calls == [text]
    assert not memo.active and memo.cached_entry_count == 0
    with retention_stage_memo_scope():
        verify_private_chunk(chunk.reference, text)
        assert proof_calls == [text, text]


def test_private_chunk_stage_keeps_state_guard_outside_text_proof(monkeypatch):
    state_text = canonical_json(_field_mapper_state().to_dict())
    proof_calls: list[str] = []
    guard_calls: list[str] = []
    original_proof = codec._private_chunk_canonical_text_bytes
    original_state_guard = credential_guard.require_no_credential_material_in_state

    def counted_proof(key):
        proof_calls.append(key.canonical)
        return original_proof(key)

    def counted_state_guard(state, *, surface, env_ref_names):
        guard_calls.append(surface)
        return original_state_guard(state, surface=surface, env_ref_names=env_ref_names)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", counted_proof)
    monkeypatch.setattr(credential_guard, "require_no_credential_material_in_state", counted_state_guard)
    with retention_stage_memo_scope():
        generic = encode_private_chunk(codec="composer.messages.v1", canonical=state_text)
        first = encode_private_chunk(codec="composer.state.v1", canonical=state_text)
        second = encode_private_chunk(codec="composer.state.v1", canonical=state_text)
    assert generic.reference.codec == "composer.messages.v1"
    assert first.reference.codec == second.reference.codec == "composer.state.v1"
    assert proof_calls == [state_text]
    assert guard_calls == ["composer_continuation_state", "composer_continuation_state"]


@pytest.mark.parametrize("invalid", ['{"b":2,"a":1}', '{"a":1,"a":2}', "NaN", '{"a": 1}', '{"text":"' + chr(0xD800) + '"}'])
def test_private_chunk_stage_safe_hit_cannot_admit_invalid_changed_text(invalid):
    with retention_stage_memo_scope():
        encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
        with pytest.raises((AuditIntegrityError, UnicodeEncodeError)):
            encode_private_chunk(codec="composer.messages.v1", canonical=invalid)


def test_private_chunk_stage_rechecks_changed_bound_subclass_and_mutable_source(monkeypatch):
    text = canonical_json({"text": "ordinary"})
    original_bound = codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES

    class ForeignText(str):
        def encode(self, encoding="utf-8", errors="strict"):
            raise AssertionError("foreign string must not use a cached proof")

    source = {"text": "ordinary"}
    with retention_stage_memo_scope():
        encode_private_chunk(codec="composer.messages.v1", canonical=text)
        with pytest.raises(AssertionError, match="foreign string"):
            encode_private_chunk(codec="composer.messages.v1", canonical=ForeignText(text))
        monkeypatch.setattr(codec, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", len(text.encode("utf-8")) - 1)
        with pytest.raises(AuditIntegrityError, match="complete JSON admission bound"):
            encode_private_chunk(codec="composer.messages.v1", canonical=text)
        monkeypatch.setattr(codec, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", original_bound)
        source["api_key"] = "synthetic-literal-credential"
        with pytest.raises(CredentialMaterialRefused):
            encode_private_chunk(codec="composer.messages.v1", canonical=canonical_json(source))


def test_state_chunks_keep_field_mapper_labels_under_the_real_state_projection():
    encoded = canonical_json(_field_mapper_state().to_dict())
    actual = encode_private_chunk(codec="composer.state.v1", canonical=encoded)
    verify_private_chunk(actual.reference, encoded)


def test_equal_length_changed_bytes_are_rejected_by_digest():
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"text":"alpha"}')
    with pytest.raises(AuditIntegrityError, match="bytes differ"):
        verify_private_chunk(chunk.reference, '{"text":"bravo"}')


def test_stage_reuses_exact_interleaved_chunk_sha_but_rechecks_reference_and_text(monkeypatch):
    original_producer = codec._private_chunk_canonical_text_bytes
    produced: list[tuple[str, object]] = []

    def counted_producer(key):
        produced.append((key.canonical, key.reference))
        return original_producer(key)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", counted_producer)
    first_text = '{"text":"first"}'
    second_text = '{"text":"second"}'
    with retention_stage_memo_scope():
        first = encode_private_chunk(codec="composer.messages.v1", canonical=first_text)
        second = encode_private_chunk(codec="composer.messages.v1", canonical=second_text)
        verify_private_chunk(first.reference, first_text)
        verify_private_chunk(second.reference, second_text)
        assert encode_private_chunk(codec="composer.messages.v1", canonical=first_text) == first
        assert produced == [
            (first_text, None),
            (second_text, None),
        ]
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(first.reference, second_text)
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(first.reference.model_copy(update={"digest": "0" * 64}), first_text)
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(first.reference.model_copy(update={"byte_length": None}), first_text)
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(first.reference.model_copy(update={"byte_length": len(first_text) + 1}), first_text)
    earlier_proofs = len(produced)
    with retention_stage_memo_scope():
        encode_private_chunk(codec="composer.messages.v1", canonical=first_text)
    assert len(produced) == earlier_proofs + 1 and produced[-1] == (first_text, None)


def test_stage_sha_hit_still_runs_changed_credential_policy(monkeypatch):
    from elspeth.web import credential_guard

    original_guard = credential_guard.require_no_credential_material
    seen: list[frozenset[str]] = []

    def guarded(value, *, surface, env_ref_names, max_string_bytes):
        seen.append(env_ref_names)
        if env_ref_names == frozenset({"DENY"}):
            raise ValueError("changed credential policy")
        return original_guard(value, surface=surface, env_ref_names=env_ref_names, max_string_bytes=max_string_bytes)

    monkeypatch.setattr(credential_guard, "require_no_credential_material", guarded)
    text = '{"safe":true}'
    with retention_stage_memo_scope():
        encode_private_chunk(codec="composer.messages.v1", canonical=text, env_ref_names=frozenset({"ALLOW"}))
        with pytest.raises(ValueError, match="changed credential policy"):
            encode_private_chunk(codec="composer.messages.v1", canonical=text, env_ref_names=frozenset({"DENY"}))
    assert seen == [frozenset({"ALLOW"}), frozenset({"DENY"})]


@pytest.mark.parametrize("canonical", ['{"b":2,"a":1}', '{"a":1,"a":2}', "NaN", '{"a": 1}'])
def test_chunk_codec_requires_canonical_strict_json(canonical):
    with pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
        encode_private_chunk(codec="composer.messages.v1", canonical=canonical)


def test_utf8_length_is_bytes_and_is_restored_exactly():
    import hashlib

    encoded = canonical_json({"text": "café"})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=encoded)
    assert chunk.reference.byte_length == len(encoded.encode("utf-8"))
    assert chunk.reference.digest == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    verify_private_chunk(chunk.reference, encoded)


def test_owned_json_copies_complete_nested_json_from_immutable_mappings():
    from types import MappingProxyType

    from elspeth.web.composer.continuation_codec import owned_json

    source = {"name": "café", "values": [None, True, False, 7, 1.25, {"nested": ("tail", 2)}]}
    actual = owned_json(MappingProxyType(source))
    expected = {"name": "café", "values": [None, True, False, 7, 1.25, {"nested": ["tail", 2]}]}
    assert actual == expected
    assert canonical_json(actual) == canonical_json(expected)
    source["values"][-1]["nested"] = ("changed",)
    assert actual == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_owned_json_refuses_nonfinite_values_in_the_complete_nested_tail(value):
    from elspeth.web.composer.continuation_codec import owned_json

    with pytest.raises(AuditIntegrityError, match="not finite JSON"):
        owned_json({"safe": [1, {"tail": value}]})


def test_owned_json_refuses_nested_nonstring_keys():
    from elspeth.web.composer.continuation_codec import owned_json

    with pytest.raises(AuditIntegrityError, match="non-string key"):
        owned_json({"safe": [1, {2: "tail"}]})


@pytest.mark.parametrize("value", [object(), b"bytes", complex(1, 2), lambda: None])
def test_owned_json_refuses_arbitrary_objects_in_the_complete_nested_tail(value):
    from elspeth.web.composer.continuation_codec import owned_json

    with pytest.raises(AuditIntegrityError, match="outside the owned JSON algebra"):
        owned_json({"safe": [1, {"tail": value}]})


def test_snapshot_decoder_reuses_only_the_exact_input_inside_one_stage(monkeypatch):
    original = codec._decode_continuation_snapshot_uncached
    work = []

    def decode(encoded):
        work.append(encoded)
        return original(encoded)

    monkeypatch.setattr(codec, "_decode_continuation_snapshot_uncached", decode)
    encoded = codec.encode_continuation_snapshot(initial_stack())
    changed = codec.encode_continuation_snapshot(initial_stack().model_copy(update={"cursor": 1}))
    with retention_stage_memo_scope() as memo:
        first = codec.decode_continuation_snapshot(encoded)
        assert codec.decode_continuation_snapshot(encoded) is first
        assert codec.decode_continuation_snapshot(changed).cursor == 1
        assert work == [encoded, changed]
    assert not memo.active and memo.cached_entry_count == 0
    assert codec.decode_continuation_snapshot(encoded) == first
    assert codec.decode_continuation_snapshot(encoded) is not first
    assert work == [encoded, changed, encoded, encoded]


def test_snapshot_decoder_does_not_retain_refusals(monkeypatch):
    original = codec._decode_continuation_snapshot_uncached
    work = []

    def decode(encoded):
        work.append(encoded)
        return original(encoded)

    monkeypatch.setattr(codec, "_decode_continuation_snapshot_uncached", decode)
    with retention_stage_memo_scope() as memo:
        for _ in range(2):
            with pytest.raises(AuditIntegrityError, match="not strict JSON"):
                codec.decode_continuation_snapshot('{"cursor":')
        assert memo.cached_entry_count == 0
    assert work == ['{"cursor":', '{"cursor":']


@pytest.mark.asyncio
async def test_public_snapshot_decoder_hits_across_actual_required_sql_hops(monkeypatch):
    original = codec._decode_continuation_snapshot_uncached
    work = []
    requests = []

    def decode(encoded):
        work.append(threading.get_ident())
        return original(encoded)

    def read(encoded):
        requests.append(threading.get_ident())
        return codec.decode_continuation_snapshot(encoded)

    monkeypatch.setattr(codec, "_decode_continuation_snapshot_uncached", decode)
    encoded = codec.encode_continuation_snapshot(initial_stack())
    tickets = [publication_ticket(), publication_ticket()]
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(async_workers, "_SHARED_EXECUTOR", pool)
        with retention_stage_memo_scope() as memo:
            first = await async_workers.run_required_sql_finish_once(tickets[0], read, encoded)
            second = await async_workers.run_required_sql_finish_once(tickets[1], read, encoded)
            assert isinstance(first, RequiredSQLReturned) and isinstance(second, RequiredSQLReturned)
            assert first.value is second.value
            assert len(requests) == 2 and work == requests[:1]
            assert requests[0] == requests[1] != threading.get_ident()
        assert memo.cached_entry_count == 0
    assert all(ticket.complete for ticket in tickets)


def test_chunk_warm_hit_rechecks_the_actual_digest(monkeypatch):
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    original_parse = codec.parse_json_strict
    original_hash = codec.sha256
    parses = []
    hashes = []

    def parse(encoded):
        parses.append(encoded)
        return original_parse(encoded)

    def digest(encoded):
        hashes.append(encoded)
        return original_hash(encoded if len(hashes) == 1 else b"changed actual bytes")

    monkeypatch.setattr(codec, "parse_json_strict", parse)
    monkeypatch.setattr(codec, "sha256", digest)
    with retention_stage_memo_scope():
        verify_private_chunk(chunk.reference, chunk.canonical)
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(chunk.reference, chunk.canonical)
    assert parses == [chunk.canonical]
    assert hashes == [chunk.canonical.encode("utf-8")] * 2


def test_large_valid_chunk_keeps_canonical_only_stage_proof_without_raising_bound(monkeypatch):
    canonical = canonical_json({"text": "x" * (codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES // 2 + 1024)})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original = codec._verify_private_chunk_canonical
    parses: list[str] = []

    def counted(text):
        parses.append(text)
        return original(text)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", counted)
    with retention_stage_memo_scope() as memo:
        verify_private_chunk(chunk.reference, canonical)
        verify_private_chunk(chunk.reference, canonical)
        assert memo.cached_entry_count == 1
    assert parses == [canonical]


def test_chunk_canonical_refusal_precedes_binding_refusal_in_a_stage(monkeypatch):
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    original_hash = codec.sha256
    hashes = []

    def digest(encoded):
        hashes.append(encoded)
        return original_hash(encoded)

    monkeypatch.setattr(codec, "sha256", digest)
    with retention_stage_memo_scope() as memo:
        for _ in range(2):
            with pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
                verify_private_chunk(chunk.reference, '{"safe": true}')
        assert memo.cached_entry_count == 0
    assert hashes == []


def test_chunk_reference_fields_are_rechecked_after_shared_pure_verification(monkeypatch):
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    original = codec._private_chunk_canonical_text_bytes
    parses = []

    def verify(key):
        parses.append(key.canonical)
        return original(key)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", verify)
    changed_codec = chunk.reference.model_copy(update={"codec": "composer.state.v1"})
    changed_digest = chunk.reference.model_copy(update={"digest": "0" * 64})
    changed_length = chunk.reference.model_copy(update={"byte_length": chunk.reference.byte_length + 1})
    with retention_stage_memo_scope():
        verify_private_chunk(chunk.reference, chunk.canonical)
        verify_private_chunk(chunk.reference, chunk.canonical)
        verify_private_chunk(changed_codec, chunk.canonical)
        for reference in (changed_digest, changed_length):
            with pytest.raises(AuditIntegrityError, match="bytes differ"):
                verify_private_chunk(reference, chunk.canonical)
    assert parses == [chunk.canonical]


@pytest.mark.parametrize("update", [{"digest": []}, {"byte_length": []}])
def test_malformed_nominal_chunk_reference_keeps_canonical_then_binding_refusal(update):
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    reference = chunk.reference.model_copy(update=update)
    with retention_stage_memo_scope():
        with pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
            verify_private_chunk(reference, '{"safe": true}')
        with pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(reference, chunk.canonical)


def test_malformed_unused_chunk_codec_keeps_ordinary_success():
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    reference = chunk.reference.model_copy(update={"codec": []})
    with retention_stage_memo_scope() as memo:
        verify_private_chunk(reference, chunk.canonical)
        assert memo.cached_entry_count == 0


def test_missing_nominal_chunk_field_keeps_canonical_refusal():
    reference = codec.PrivateChunkRef.model_construct(codec="composer.messages.v1", byte_length=13)
    with retention_stage_memo_scope(), pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
        verify_private_chunk(reference, '{"safe": true}')


def _canonical_only_clock():
    return ContinuationActionClock(ContinuationActionPolicy(preparation=10, guard=1, quota=1, physical=1, retention=1))


@pytest.mark.parametrize("extra_byte", (0, 1))
@pytest.mark.asyncio
async def test_v4_encoder_exact_full_proof_threshold_keeps_actual_utf8_and_digest(monkeypatch, extra_byte):
    bound = codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    fitting_bytes = (bound - 64 - len(str(bound))) // 2
    empty = canonical_json({"text": "é"})
    canonical = canonical_json({"text": "é" + "x" * (fitting_bytes + extra_byte - len(empty.encode("utf-8")))})
    assert len(canonical.encode("utf-8")) == fitting_bytes + extra_byte
    original_full = codec._private_chunk_canonical_text_bytes
    original_parse = codec.parse_json_strict
    full_calls = []
    parses = []

    def full(key):
        full_calls.append(key.canonical)
        assert extra_byte == 0, "unretainable full proof must use the canonical-only family"
        return original_full(key)

    def parse(text):
        parses.append(text)
        return original_parse(text)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", full)
    monkeypatch.setattr(codec, "parse_json_strict", parse)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        first = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        second = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        assert first == second and first is not second
        assert parses == [canonical]
        assert full_calls == ([] if extra_byte else [canonical])
        assert len(parent._canonical_proofs) == (0 if extra_byte else 1)
        assert len(parent._canonical_verifications) == extra_byte
        assert first.reference.byte_length == len(canonical.encode("utf-8"))
        assert first.reference.digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        changed = canonical.replace("x", "y")
        third = encode_private_chunk(codec="composer.messages.v1", canonical=changed)
        assert third.reference.byte_length == first.reference.byte_length
        assert third.reference.digest == hashlib.sha256(changed.encode("utf-8")).hexdigest() != first.reference.digest
        assert parses == [canonical, changed]
    assert parent.cached_entry_count == 0


@pytest.mark.asyncio
async def test_v4_large_encoder_reuses_only_canonical_success_across_sql_children_and_close(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original_parse = codec.parse_json_strict
    parses = []

    def parse(text):
        parses.append(text)
        return original_parse(text)

    monkeypatch.setattr(codec, "parse_json_strict", parse)
    for _ in range(2):
        before = len(parses)
        with v4_preparation_memo_scope(_canonical_only_clock()) as parent:

            def cut():
                with borrow_v4_preparation_memo_scope():
                    verify_private_chunk(chunk.reference, canonical)
                    return encode_private_chunk(codec="composer.messages.v1", canonical=canonical)

            for _ in range(2):
                outcome = await async_workers.run_required_sql_finish_once(publication_ticket(), cut)
                assert isinstance(outcome, RequiredSQLReturned) and outcome.value == chunk
            # Verification and encoding have distinct refusal messages/producers.
            assert parses[before:] == [canonical, canonical]
            assert len(parent._canonical_verifications) == 2 and not parent._canonical_proofs
            assert parent._canonical_verification_bytes == 2 * (len(canonical.encode("utf-8")) + len(str(1_048_576)))
            for reference in (
                chunk.reference.model_copy(update={"digest": "0" * 64}),
                chunk.reference.model_copy(update={"byte_length": chunk.reference.byte_length + 1}),
            ):
                with pytest.raises(AuditIntegrityError, match="bytes differ from their binding"):
                    verify_private_chunk(reference, canonical)
            assert len(parses) == before + 2
        assert parent.cached_entry_count == 0 and parent._canonical_verification_bytes == 0


@pytest.mark.parametrize("invalid_kind", ("malformed", "noncanonical"))
@pytest.mark.asyncio
async def test_v4_large_encoder_refusals_remain_cold_and_keep_original_message(monkeypatch, invalid_kind):
    canonical = canonical_json({"text": "x" * 600_000})
    invalid = canonical[:-1] if invalid_kind == "malformed" else canonical.replace(":", ": ", 1)
    original_parse = codec.parse_json_strict
    original_guard = credential_guard.require_no_credential_material
    parses = []
    guards = []

    def parse(text):
        parses.append(text)
        return original_parse(text)

    def guard(value, **kwargs):
        guards.append(value)
        return original_guard(value, **kwargs)

    monkeypatch.setattr(codec, "parse_json_strict", parse)
    monkeypatch.setattr(credential_guard, "require_no_credential_material", guard)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        for _ in range(2):
            with pytest.raises(AuditIntegrityError) as caught:
                encode_private_chunk(codec="composer.messages.v1", canonical=invalid)
            assert str(caught.value) == "private chunk must retain canonical strict JSON"
            assert caught.value.__cause__ is None
        assert parses == [canonical, invalid, invalid] and guards == [canonical]
        assert len(parent._canonical_verifications) == 1 and not parent._canonical_proofs


@pytest.mark.asyncio
async def test_v4_large_encoder_preserves_original_canonical_error_identity(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    original_error = AuditIntegrityError("injected original canonical failure")
    calls = []

    def fail(text):
        calls.append(text)
        raise original_error

    monkeypatch.setattr(codec, "_require_private_chunk_encoding_canonical", fail)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        for _ in range(2):
            with pytest.raises(AuditIntegrityError) as caught:
                encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
            assert caught.value is original_error
        assert calls == [canonical, canonical] and parent.cached_entry_count == 0


@pytest.mark.asyncio
async def test_v4_large_encoder_rechecks_bound_before_warm_lookup_with_exact_overflow(monkeypatch):
    canonical = canonical_json({"text": "é" * 300_000})
    byte_length = len(canonical.encode("utf-8"))
    original_parse = codec.parse_json_strict
    parses = []

    def parse(text):
        parses.append(text)
        return original_parse(text)

    monkeypatch.setattr(codec, "parse_json_strict", parse)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        monkeypatch.setattr(codec, "CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES", byte_length - 1)
        with pytest.raises(AuditIntegrityError, match="complete JSON admission bound") as caught:
            encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        overflow = caught.value.__cause__
        assert type(overflow) is codec.ContinuationMandatoryCaptureOverflow
        assert overflow.witness == codec.ContinuationCaptureOverflowWitness(
            unit="private_chunk", codec="composer.messages.v1", observed_bytes=byte_length, limit_bytes=byte_length - 1, size_is_exact=True
        )
        assert parses == [canonical] and len(parent._canonical_verifications) == 1


@pytest.mark.parametrize("memo_used", (False, True))
@pytest.mark.asyncio
async def test_large_encoder_substituted_sha_preserves_original_credential_failure_order(monkeypatch, memo_used):
    from contextlib import nullcontext

    canonical = canonical_json({"text": "x" * 600_000})
    crypto_error = RuntimeError("original crypto failure")
    credential_error = ValueError("original credential failure")
    calls = []

    def digest(_encoded):
        calls.append("sha")
        raise crypto_error

    def guard(_value, **_kwargs):
        calls.append("credential")
        raise credential_error

    with v4_preparation_memo_scope(_canonical_only_clock()) if memo_used else nullcontext():
        # Prime the actual optimized branch before selecting the existing
        # replacement-crypto fallback. This does not instrument trusted SHA.
        encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        monkeypatch.setattr(codec, "sha256", digest)
        monkeypatch.setattr(credential_guard, "require_no_credential_material", guard)
        with pytest.raises((RuntimeError, ValueError)) as caught:
            encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    assert caught.value is (crypto_error if memo_used else credential_error)
    assert calls == (["sha"] if memo_used else ["credential"])


def test_large_encoder_without_v4_keeps_existing_full_producer_path(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    original = codec._private_chunk_canonical_text_bytes
    calls = []

    def full(key):
        calls.append(key.canonical)
        return original(key)

    monkeypatch.setattr(codec, "_private_chunk_canonical_text_bytes", full)
    ordinary = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    with retention_stage_memo_scope() as memo:
        for _ in range(2):
            assert encode_private_chunk(codec="composer.messages.v1", canonical=canonical) == ordinary
        assert not memo._canonical_verifications and not memo._canonical_proofs
    assert calls == [canonical] * 3


@pytest.mark.parametrize("replace_validator", (False, True))
def test_factored_encoder_parser_keeps_exact_native_byte_provenance(monkeypatch, request, replace_validator):
    from elspeth.web.composer import continuation_clock
    from tests.helpers.continuation_memo import RetentionMemoTrace
    from tests.unit.web.test_continuation_memo_accounting import _late_chunk_budget

    canonical = canonical_json({"text": "café☃"})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original = codec._require_private_chunk_encoding_canonical

    def forwarded(text):
        return original(text)

    if replace_validator:
        monkeypatch.setattr(codec, "_require_private_chunk_encoding_canonical", forwarded)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True, require_chunk_artifacts=True)
    # The observer registers stages through this actual clock-owned scope.
    # A pre-imported memo-scope alias bypasses its carrier witness.
    with continuation_clock.retention_stage_memo_scope():
        for _ in range(2):
            assert verify_private_chunk(chunk.reference, canonical) is None
    assert all(record.chunk_reference is chunk.reference for record in trace.records)
    assert all(record.key.reference is None for record in trace.records)
    assert trace.records[0].chunk_proof is trace.records[1].chunk_proof
    assert all(record.chunk_binding_returned for record in trace.records)
    if replace_validator:
        with pytest.raises(AssertionError, match="native byte provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, chunk_artifact_budget=True)
        assert trace.chunk_artifact_errors and not trace._chunk_artifact_verified
    else:
        trace.finish(
            stages=1,
            require_snapshot=False,
            chunk_artifact_budget=True,
            native_byte_limits=(_late_chunk_budget(chunk, chunk.reference.byte_length),),
        )
        assert [record.attempts["chunk_work"] for record in trace.records] == [1, 0]
        assert [record.byte_calls["strict_parse"] for record in trace.records] == [1, 0]
        assert sum(record.byte_counts["strict_parse"] for record in trace.records) == chunk.reference.byte_length


@pytest.mark.asyncio
async def test_v4_large_encoder_observation_attributes_its_actual_canonical_producer(monkeypatch):
    from tests.helpers.continuation_end_profile import EndTextMemoObservation

    canonical = canonical_json({"text": "x" * 600_000})
    observation = EndTextMemoObservation()
    with observation.install(monkeypatch), v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        first = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        assert encode_private_chunk(codec="composer.messages.v1", canonical=canonical) == first
        assert tuple(key[0] for key in parent._canonical_verifications) == (codec._require_private_chunk_encoding_canonical,)
    result = observation.snapshot()
    assert result["families"]["full"]["requests"] == 0
    assert result["families"]["canonical"]["requests"] == 2
    assert result["families"]["canonical"]["hits"] == 1
    assert result["families"]["canonical"]["misses"] == 1
    assert result["observer_errors"] == result["overlapping_calls"] == result["budget_violations"] == 0
    assert all(parent["closed_empty"] for parent in result["parents"])


@pytest.mark.asyncio
async def test_v4_large_state_encoder_keeps_fresh_state_construction_and_environment_guard(monkeypatch):
    state = _field_mapper_state().to_dict()
    state["metadata"] = {"name": "n" * 200_000, "description": "d" * 200_000}
    state["nodes"][0]["description"] = "x" * 200_000
    state["nodes"][0]["options"]["mapping"] = {"customer": "customer_id"}
    state["nodes"][0]["options"]["api_key"] = "${CURRENT_KEY}"
    canonical = canonical_json(state)
    assert 600_000 < len(canonical.encode("utf-8")) < codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    allowed = frozenset({"CURRENT_KEY"})
    expected = encode_private_chunk(codec="composer.state.v1", canonical=canonical, env_ref_names=allowed)
    with pytest.raises(CredentialMaterialRefused) as ordinary_refusal:
        encode_private_chunk(codec="composer.state.v1", canonical=canonical)
    original_parse = codec.parse_json_strict
    original_guard = credential_guard.require_no_credential_material_in_state
    parses = []
    guarded_states = []
    environments = []

    def parse(text):
        parses.append(text)
        return original_parse(text)

    def guard(value, *, surface, env_ref_names):
        guarded_states.append(value)
        environments.append(env_ref_names)
        return original_guard(value, surface=surface, env_ref_names=env_ref_names)

    monkeypatch.setattr(codec, "parse_json_strict", parse)
    monkeypatch.setattr(credential_guard, "require_no_credential_material_in_state", guard)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
        for _ in range(2):
            assert encode_private_chunk(codec="composer.state.v1", canonical=canonical, env_ref_names=allowed) == expected
        with pytest.raises(CredentialMaterialRefused) as warm_refusal:
            encode_private_chunk(codec="composer.state.v1", canonical=canonical)
        assert str(warm_refusal.value) == str(ordinary_refusal.value)
        assert parses == [canonical] * 4  # One canonical success, then each state parse.
        assert len(guarded_states) == 3 and len({id(value) for value in guarded_states}) == 3
        assert environments == [allowed, allowed, frozenset()]
        assert len(parent._canonical_verifications) == 1 and not parent._canonical_proofs


@pytest.mark.asyncio
async def test_v4_large_canonical_only_proof_crosses_real_sql_children_and_rechecks_binding(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original = codec._verify_private_chunk_canonical
    canonical_calls = []

    def verify(text):
        canonical_calls.append(text)
        return original(text)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", verify)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:

        def cut():
            with borrow_v4_preparation_memo_scope():
                verify_private_chunk(chunk.reference, canonical)
            return True

        for _ in range(2):
            outcome = await async_workers.run_required_sql_finish_once(publication_ticket(), cut)
            assert isinstance(outcome, RequiredSQLReturned) and outcome.value is True
        assert canonical_calls == [canonical]
        assert len(parent._canonical_verifications) == 1 and not parent._canonical_proofs
        for reference in (
            chunk.reference.model_copy(update={"digest": "0" * 64}),
            chunk.reference.model_copy(update={"byte_length": chunk.reference.byte_length + 1}),
        ):
            with borrow_v4_preparation_memo_scope(), pytest.raises(AuditIntegrityError, match="bytes differ"):
                verify_private_chunk(reference, canonical)
        assert canonical_calls == [canonical]
        with borrow_v4_preparation_memo_scope(), pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(chunk.reference, canonical.replace("x", "y"))
        assert len(canonical_calls) == 2


@pytest.mark.asyncio
async def test_v4_warm_canonical_only_proof_keeps_foreign_sha_fresh_and_producer_change_cold(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original = codec._verify_private_chunk_canonical
    canonical_calls = []
    hashes = []

    def first(text):
        canonical_calls.append("first")
        return original(text)

    def replacement(text):
        canonical_calls.append("replacement")
        return original(text)

    def digest(encoded):
        hashes.append(len(encoded))
        return hashlib.sha256(encoded if len(hashes) == 1 else b"changed crypto result")

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", first)
    with v4_preparation_memo_scope(_canonical_only_clock()):
        with borrow_v4_preparation_memo_scope():
            verify_private_chunk(chunk.reference, canonical)
        monkeypatch.setattr(codec, "sha256", digest)
        with borrow_v4_preparation_memo_scope():
            verify_private_chunk(chunk.reference, canonical)
        with borrow_v4_preparation_memo_scope(), pytest.raises(AuditIntegrityError, match="bytes differ"):
            verify_private_chunk(chunk.reference, canonical)
        assert canonical_calls == ["first"] and hashes == [chunk.reference.byte_length] * 2
        monkeypatch.setattr(codec, "sha256", hashlib.sha256)
        monkeypatch.setattr(codec, "_verify_private_chunk_canonical", replacement)
        with borrow_v4_preparation_memo_scope():
            verify_private_chunk(chunk.reference, canonical)
        assert canonical_calls == ["first", "replacement"]


@pytest.mark.asyncio
async def test_v4_canonical_only_oversize_recomputes_and_malformed_refusal_precedes_hash(monkeypatch):
    canonical = canonical_json({"text": "x" * (codec.CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES + 1)})
    encoded = canonical.encode()
    reference = codec.PrivateChunkRef(codec="composer.messages.v1", digest=hashlib.sha256(encoded).hexdigest(), byte_length=len(encoded))
    original = codec._verify_private_chunk_canonical
    calls = []
    hashes = []

    def verify(text):
        calls.append(len(text))
        return original(text)

    def digest(data):
        hashes.append(len(data))
        return hashlib.sha256(data)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", verify)
    monkeypatch.setattr(codec, "sha256", digest)
    with v4_preparation_memo_scope(_canonical_only_clock()) as parent:
        for _ in range(2):
            with borrow_v4_preparation_memo_scope():
                verify_private_chunk(reference, canonical)
        assert calls == [len(canonical)] * 2 and hashes == [len(encoded)] * 2
        assert parent.cached_entry_count == 0
        corrupt = reference.model_copy(update={"digest": "0" * 64})
        for malformed in ('{"text": "x"}', canonical.replace(":", ": ", 1)):
            with borrow_v4_preparation_memo_scope(), pytest.raises(AuditIntegrityError, match="canonical strict JSON"):
                verify_private_chunk(corrupt, malformed)
        assert hashes == [len(encoded)] * 2 and parent.cached_entry_count == 0


@pytest.mark.asyncio
async def test_large_canonical_fallback_keeps_ordinary_proofs_child_local(monkeypatch):
    canonical = canonical_json({"text": "x" * 600_000})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=canonical)
    original = codec._verify_private_chunk_canonical
    calls = []

    def verify(text):
        calls.append(len(text))
        return original(text)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", verify)
    for _ in range(2):
        with retention_stage_memo_scope():
            verify_private_chunk(chunk.reference, canonical)
            verify_private_chunk(chunk.reference, canonical)
    assert calls == [len(canonical)] * 2
