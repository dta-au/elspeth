"""The actual payload-schema key owner refuses non-string annotation keys."""

from collections.abc import Mapping
from typing import NotRequired, Required, TypedDict, is_typeddict

import pytest

from elspeth.contracts.declaration_contracts import resolve_payload_schema_key_sets


class _InheritedPayload(TypedDict):
    stable: int
    optional: NotRequired[str]


class _PartialPayload(_InheritedPayload, total=False):
    added_optional: bool
    explicit_required: Required[int]


class _StringKey(str):
    pass


def test_payload_schema_key_owner_preserves_inherited_totality_and_wrappers():
    assert resolve_payload_schema_key_sets(_PartialPayload) == (
        frozenset({"stable", "explicit_required"}),
        frozenset({"optional", "added_optional"}),
    )
    names = TypedDict("UnicodePayload", {"customer field": str, "名前": int})
    assert resolve_payload_schema_key_sets(names) == (frozenset({"customer field", "名前"}), frozenset())


@pytest.mark.parametrize("key", [17, Mapping, _StringKey("customer")])
def test_payload_schema_key_owner_refuses_non_exact_string_keys_after_real_typeddict_admission(key):
    # The stdlib checks annotation values; it preserves these keys unchanged.
    schema = TypedDict("InvalidKeyPayload", {key: int})
    assert is_typeddict(schema)
    assert tuple(schema.__annotations__) == (key,)
    assert next(iter(schema.__annotations__)) is key
    assert resolve_payload_schema_key_sets(_PartialPayload) == (
        frozenset({"stable", "explicit_required"}),
        frozenset({"optional", "added_optional"}),
    )
    with pytest.raises(TypeError, match="Payload schema annotation keys must be exact strings"):
        resolve_payload_schema_key_sets(schema)
