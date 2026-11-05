"""Native-string writer parity against the unchanged public serializer."""

from __future__ import annotations

import hashlib
import random
import struct
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from types import MappingProxyType

import pytest
import rfc8785

from elspeth.contracts import _canonical_json_native as candidate
from elspeth.contracts import hashing
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.contracts.readonly_json import ReadonlyJsonArray, ReadonlyJsonObject, ReadonlyJsonString


def _outcome(serialize, value):
    try:
        return "bytes", serialize(value).encode("utf-8")
    except Exception as error:
        cause = error.__cause__
        return (
            "error",
            type(error),
            error.args,
            error.__suppress_context__,
            None if cause is None else (type(cause), cause.args, type(cause.__context__)),
            error.__context__ is cause,
        )


def _assert_same(value):
    expected = _outcome(canonical_json, value)
    actual = _outcome(candidate.canonical_json_native_strings, value)
    assert actual == expected
    return actual


def test_comparator_detects_deliberately_wrong_escape(monkeypatch):
    value = {"text": 'quote " slash / backslash \\ newline\n'}
    assert _assert_same(value)[0] == "bytes"
    original = candidate._native_string_bytes

    def wrong_escape(text):
        encoded = original(text)
        return None if encoded is None else encoded.replace(b"\\n", b"\\u000a")

    monkeypatch.setattr(candidate, "_native_string_bytes", wrong_escape)
    with pytest.raises(AssertionError):
        _assert_same(value)


def test_every_unicode_scalar_in_bounded_chunks(record_property):
    covered = chunks = 0
    for start in range(0, 0x110000, 4096):
        value = "".join(chr(code) for code in range(start, min(start + 4096, 0x110000)) if not 0xD800 <= code <= 0xDFFF)
        assert _assert_same(value)[0] == "bytes"
        covered += len(value)
        chunks += 1
    assert covered == 0x110000 - (0xE000 - 0xD800) and chunks > 0
    record_property("native_writer_unicode_scalar_count", covered)
    record_property("native_writer_unicode_chunk_count", chunks)


def test_every_surrogate_preserves_value_and_key_refusal_details(record_property):
    values = keys = 0
    for code in range(0xD800, 0xE000):
        surrogate = chr(code)
        for value in (surrogate, '\n"' + surrogate + "\\", [surrogate], {"value": surrogate}):
            outcome = _assert_same(value)
            assert outcome[0] == "error" and outcome[1] is rfc8785.CanonicalizationError
            values += 1
        outcome = _assert_same({surrogate: "value"})
        assert outcome[0] == "error" and outcome[1] is UnicodeEncodeError
        keys += 1
    assert values == 4 * (0xE000 - 0xD800) and keys == 0xE000 - 0xD800
    record_property("native_writer_surrogate_value_cases", values)
    record_property("native_writer_surrogate_key_cases", keys)


@pytest.mark.parametrize("text", [chr(value) for value in range(32)] + ['"', "\\", "/", "\u2028", "\u2029", "e\u0301", "é", "\uffff"])
def test_control_and_escape_combinations_remain_exact(text):
    _assert_same({text: [text, text * 3, canonical_json({"embedded": text})]})


def test_utf16_key_order_and_raw_unescaped_keys():
    value = {"\ue000": 1, "\U00010000": 2, "\r": 3, "1": 4, "€": 5, "😀": 6, "\ufb33": 7, "\\": 8, '"': 9}
    result = _assert_same(value)
    assert result[0] == "bytes"
    assert result[1].index("\U00010000".encode()) < result[1].index("\ue000".encode())


# RFC 8785 Appendix B: https://www.rfc-editor.org/rfc/rfc8785.html#appendix-B
_NUMBER_VECTORS = (
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
)


@pytest.mark.parametrize(("bits", "expected"), _NUMBER_VECTORS)
def test_rfc_appendix_b_numbers_delegate_identically(bits, expected):
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    assert _assert_same(value) == ("bytes", expected.encode())
    _assert_same({"nested": [value, -value]})


def test_deterministic_float_bit_corpus_and_integer_boundaries(record_property):
    rng = random.Random(8785)
    for _ in range(2048):
        value = struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0]
        _assert_same(value)
    for value in (-(2**53 - 1), 2**53 - 1, -(2**53), 2**53, 10**5000, -(10**5000)):
        _assert_same({"value": value})
    for value in (float("nan"), float("inf"), -float("inf")):
        _assert_same({"early": (value,), "later": frozenset()})
    record_property("native_writer_random_float_patterns", 2048)


def test_frozen_containers_enum_subclasses_and_unsupported_inputs():
    # Exercise the legacy str/Enum mixin: its __str__ differs from StrEnum.
    Text = Enum("Text", {"VALUE": "underlying content"}, type=str)

    class NativeText(StrEnum):
        VALUE = "native string enum"

    class Number(IntEnum):
        VALUE = 7

    class MisleadingString(str):
        def __str__(self):
            raise AssertionError("string content must not be coerced")

    class FailingKey(str):
        def encode(self, encoding="utf-8", errors="strict"):
            raise AttributeError("original key encoding failure")

    class Float(float):
        pass

    class Integer(int):
        pass

    class BorrowedArray(ReadonlyJsonArray):
        def __len__(self):
            return 1

        def __iter__(self):
            yield "one"

    class BorrowedObject(ReadonlyJsonObject):
        def __len__(self):
            return 1

        def __iter__(self):
            yield "key"

        def __getitem__(self, key):
            return "value"

    class BorrowedString(ReadonlyJsonString):
        def canonical_string_bytes(self, *, limit):
            return 5

    for value in (
        {},
        [],
        (),
        None,
        True,
        False,
        MappingProxyType({"nested": (1, MappingProxyType({"text": Text.VALUE}))}),
        deep_freeze({"array": [1, {"text": "frozen"}]}),
        [Text.VALUE, NativeText.VALUE, Number.VALUE, MisleadingString("real"), Float(1.5), Integer(42)],
        {Text.VALUE: "enum key", MisleadingString("key"): "subclass key"},
        {FailingKey("bad"): "key"},
        {1: "non-string key"},
        {b"bytes": "key"},
        {"early": 2**53, 4: "invalid key wins before value encoding"},
        {"invalid": frozenset({1})},
        {"invalid": {1, 2}},
        {"invalid": b"bytes"},
        {"invalid": Decimal("1.5")},
        {"invalid": datetime(2026, 1, 1, tzinfo=UTC)},
        object(),
        BorrowedArray(),
        BorrowedObject(),
        BorrowedString(),
    ):
        _assert_same(value)


def test_nested_containers_and_cycle_refusal():
    value = {"empty": ({}, [], ()), "leaf": ['quote"', 0.0]}
    for depth in range(96):
        value = {str(depth): [value]}
    assert _assert_same(value)[0] == "bytes"
    cycle = []
    cycle.append(cycle)
    for serialize in (canonical_json, candidate.canonical_json_native_strings):
        with pytest.raises(RecursionError):
            serialize(cycle)


@pytest.mark.parametrize("count", (0, 1, 3))
def test_actual_recorder_projection_corpus_and_reference_digest_without_wiring(count):
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
    from tests.unit.web.composer.test_continuation_physical_outcomes import private_record

    snapshot = _snapshot(count)
    if count:
        record = private_record()
        snapshot = replace(snapshot, llm_calls=(*snapshot.llm_calls, replace(record, call_id="00000000-0000-4000-8000-000000000099")))
    chunk = encode_recorder_snapshot(snapshot)
    projected = canonical_json_loads(chunk.canonical)
    encoded = candidate.canonical_json_native_strings(projected).encode()
    assert encoded == canonical_json(projected).encode()
    assert encoded == chunk.canonical.encode()
    assert len(encoded) == chunk.reference.byte_length
    assert hashlib.sha256(encoded).hexdigest() == chunk.reference.digest


def test_private_writer_does_not_replace_public_canonical_or_rfc_entrypoints():
    public, dumps, dump = hashing.canonical_json, rfc8785.dumps, rfc8785.dump
    _assert_same({"nested": ['quotes"\\', 1.0, -0.0]})
    assert hashing.canonical_json is public and rfc8785.dumps is dumps and rfc8785.dump is dump
