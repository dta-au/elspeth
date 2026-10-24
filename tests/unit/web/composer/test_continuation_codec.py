import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import encode_private_chunk, verify_private_chunk
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.test_credential_guard import _field_mapper_state


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
