import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web import async_workers
from elspeth.web.composer import continuation_codec as codec
from elspeth.web.composer.continuation_codec import encode_private_chunk, verify_private_chunk
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
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


def test_state_chunks_keep_field_mapper_labels_under_the_real_state_projection():
    encoded = canonical_json(_field_mapper_state().to_dict())
    actual = encode_private_chunk(codec="composer.state.v1", canonical=encoded)
    verify_private_chunk(actual.reference, encoded)


def test_equal_length_changed_bytes_are_rejected_by_digest():
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"text":"alpha"}')
    with pytest.raises(AuditIntegrityError, match="bytes differ"):
        verify_private_chunk(chunk.reference, '{"text":"bravo"}')


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


def test_chunk_reference_fields_are_part_of_the_pure_verification_key(monkeypatch):
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical='{"safe":true}')
    original = codec._verify_private_chunk_canonical
    parses = []

    def verify(encoded):
        parses.append(encoded)
        original(encoded)

    monkeypatch.setattr(codec, "_verify_private_chunk_canonical", verify)
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
    assert parses == [chunk.canonical] * 4


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
