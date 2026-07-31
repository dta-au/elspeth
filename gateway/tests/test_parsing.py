import pytest
from elspeth_llm_gateway.core.parsing import StrictJsonError, parse_strict_json


def test_parses_plain_object():
    assert parse_strict_json(b'{"a": 1}', max_bytes=100) == {"a": 1}


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b'{"a":1,"a":2}', "duplicate_key"),
        (b'{"a": Infinity}', "non_finite"),
        (b'{"a": NaN}', "non_finite"),
        (b"\xff\xfe", "invalid_utf8"),
        (b'{"a"', "invalid_json"),
        (b"[1,2]" + b" " * 200, "too_large"),
    ],
)
def test_rejections(raw, reason):
    with pytest.raises(StrictJsonError) as exc:
        parse_strict_json(raw, max_bytes=100)
    assert exc.value.reason == reason


def test_nested_duplicate_key_rejected():
    with pytest.raises(StrictJsonError):
        parse_strict_json(b'{"x": {"b":1,"b":2}}', max_bytes=100)


def test_overflow_float_in_object_rejected():
    with pytest.raises(StrictJsonError) as exc:
        parse_strict_json(b'{"t": 1e400}', max_bytes=100)
    assert exc.value.reason == "non_finite"


def test_overflow_float_in_array_rejected():
    with pytest.raises(StrictJsonError) as exc:
        parse_strict_json(b"[-1e400]", max_bytes=100)
    assert exc.value.reason == "non_finite"


def test_normal_float_still_parses():
    assert parse_strict_json(b'{"t": 1.5}', max_bytes=100) == {"t": 1.5}


def test_deeply_nested_but_small_body_rejected_as_too_deep_not_recursion_error():
    """A deeply-nested-but-individually-tiny payload (well under max_bytes)
    must be rejected with StrictJsonError(reason="too_deep") -- never let a
    raw RecursionError escape parse_strict_json, since every caller in this
    codebase only catches StrictJsonError."""
    deeply_nested = b"[" * 2000 + b"]" * 2000
    with pytest.raises(StrictJsonError) as exc:
        parse_strict_json(deeply_nested, max_bytes=1_000_000)
    assert exc.value.reason == "too_deep"


def test_shallow_nesting_still_parses():
    shallow = b"[" * 10 + b"1" + b"]" * 10
    result = parse_strict_json(shallow, max_bytes=1000)
    depth = 0
    node = result
    while isinstance(node, list):
        depth += 1
        node = node[0]
    assert depth == 10
