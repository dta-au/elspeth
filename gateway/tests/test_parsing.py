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
