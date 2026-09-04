"""Parity pins for the four hand-copied ``semantic_contracts[*]`` wire shapes.

``SemanticEdgeContract`` (contracts/plugin_semantics.py) is serialized to
the same eight-key payload by four independent copies, each marked only by a
"mirrors" comment (systems-seat finding S2 on elspeth-e405ad7cd2):

- ``tools/_common._SemanticEdgeContractPayload`` — the composer ToolResult
  envelope; the authority here because the tool-result envelope gate
  (``test_tool_result_envelope_gate.py``) derives the shipped vocabulary from
  it and the redaction shadow is pinned against it.
- ``composer_mcp.server._SemanticEdgeContractPayload`` — the MCP surface.
- ``web/execution/schemas.SemanticEdgeContractResponse`` — the HTTP surface.
- ``redaction._SemanticEdgeContractShadowModel`` — the persist-time shadow.
- ``web/frontend/src/types/index.ts`` ``SemanticEdgeContract`` — the UI.

A key added to one and not the others either ships unadmitted (the gate
catches the envelope side) or renders as ``undefined`` in the UI with no
test going red. Python-side parity is measured from the live objects;
the TS side is parsed with a Prettier-stable regex, with a smoke test so an
empty match cannot pass.
"""

from __future__ import annotations

import re
import types
import typing
from dataclasses import fields
from pathlib import Path
from typing import get_type_hints

import pytest

import elspeth
from elspeth.composer_mcp import server as mcp_server
from elspeth.contracts.plugin_semantics import SemanticEdgeContract, SemanticOutcome
from elspeth.web.composer import redaction
from elspeth.web.composer.tools import _common
from elspeth.web.execution.schemas import SemanticEdgeContractResponse

_TS_TYPES_PATH = Path(elspeth.__file__).parent / "web" / "frontend" / "src" / "types" / "index.ts"

# The exported interface block, then one `name: type;` record per line
# (Prettier-stable). Comments inside the block are not records.
_TS_INTERFACE_RE = re.compile(r"^export interface SemanticEdgeContract \{\n(?P<body>.*?)^\}", re.MULTILINE | re.DOTALL)
_TS_FIELD_RE = re.compile(r"^\s*(?P<name>\w+):\s*(?P<type>[^;]+);\s*$", re.MULTILINE)
# A closed vocabulary spelled as a union of double-quoted string literals: `"a" | "b"`.
_TS_STRING_LITERAL_RE = re.compile(r'^"(?P<value>[^"]*)"$')

# Python scalar -> the TS spelling the interface uses for it.
_TS_SCALARS: dict[type, str] = {str: "string", bool: "boolean", int: "number", float: "number"}


def _authority() -> dict[str, object]:
    return get_type_hints(_common._SemanticEdgeContractPayload)


def _nullable(annotation: object) -> bool:
    return "None" in str(annotation)


def _expected_ts_type(hint: object) -> str:
    """The TS spelling the interface must use for a Python hint of the payload.

    ``str`` -> ``string``, ``bool`` -> ``boolean``, ``int`` / ``float`` ->
    ``number``, ``list[X]`` -> ``X[]``, and ``X | None`` -> ``X | null`` (the
    interface's own spelling: ``producer_plugin: string | null``). Anything
    else is a refusal, so a hint this table does not know cannot pass by
    accident.
    """
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        members = [arg for arg in typing.get_args(hint) if arg is not type(None)]
        assert len(members) == len(typing.get_args(hint)) - 1, f"{hint!r}: expected exactly one None member"
        assert len(members) == 1, f"{hint!r}: TS parity knows only ``X | None`` unions"
        return f"{_expected_ts_type(members[0])} | null"
    if origin is list:
        (item,) = typing.get_args(hint)
        return f"{_expected_ts_type(item)}[]"
    assert isinstance(hint, type) and hint in _TS_SCALARS, f"{hint!r}: no TS spelling known for this hint"
    return _TS_SCALARS[hint]


def _string_literal_members(ts_type: str) -> tuple[str, ...] | None:
    """The values of a ``"a" | "b"`` literal union, or None when ``ts_type`` is not one."""
    members = [_TS_STRING_LITERAL_RE.match(part.strip()) for part in ts_type.split("|")]
    if not members or any(m is None for m in members):
        return None
    return tuple(m.group("value") for m in members if m is not None)


def _normalize_ts_type(ts_type: str) -> str:
    """Collapse a string-literal union to ``string``: a closed vocabulary is still a string on the wire.

    Nothing else is rewritten, so ``string | null`` and ``number`` compare as spelled.
    """
    return "string" if _string_literal_members(ts_type) is not None else ts_type


def _ts_fields() -> list[tuple[str, str]]:
    match = _TS_INTERFACE_RE.search(_TS_TYPES_PATH.read_text(encoding="utf-8"))
    assert match is not None, f"no `export interface SemanticEdgeContract {{` block in {_TS_TYPES_PATH}"
    return [(m.group("name"), m.group("type").strip()) for m in _TS_FIELD_RE.finditer(match.group("body"))]


def test_authority_is_the_eight_key_payload_the_envelope_gate_trusts() -> None:
    assert list(_authority()) == [
        "from_id",
        "to_id",
        "consumer_plugin",
        "producer_plugin",
        "producer_field",
        "consumer_field",
        "outcome",
        "requirement_code",
    ]


def test_mcp_payload_matches_the_envelope_payload_key_for_key() -> None:
    """Order AND hints (red-team R8): dict equality is order-blind, so the key list is pinned first."""
    mcp_hints = get_type_hints(mcp_server._SemanticEdgeContractPayload)
    authority = _authority()
    assert list(mcp_hints) == list(authority)
    assert [(name, hint) for name, hint in mcp_hints.items()] == [(name, hint) for name, hint in authority.items()]


def test_http_response_matches_the_envelope_payload_key_for_key() -> None:
    authority = _authority()
    http_fields = SemanticEdgeContractResponse.model_fields
    assert list(http_fields) == list(authority)
    assert {name: field.annotation for name, field in http_fields.items()} == authority


def test_redaction_shadow_matches_the_envelope_payload_key_for_key() -> None:
    authority = _authority()
    shadow_fields = redaction._SemanticEdgeContractShadowModel.model_fields
    assert list(shadow_fields) == list(authority)
    assert {name: field.annotation for name, field in shadow_fields.items()} == authority


def test_frontend_interface_matches_the_envelope_payload_in_order_nullability_and_type() -> None:
    authority = _authority()
    ts = _ts_fields()
    assert [name for name, _ in ts] == list(authority)
    # ``string | null`` on the TS side is exactly ``str | None`` on the
    # Python side; a literal union such as the ``outcome`` values is still a
    # non-null string.
    assert {name: "| null" in ts_type for name, ts_type in ts} == {name: _nullable(hint) for name, hint in authority.items()}
    # Scalar types (red-team R8 / mutation RM8b): ``string`` -> ``number`` on one
    # field must go red. The expected spelling is derived from the authority's
    # Python hint, never listed by hand.
    assert {name: _normalize_ts_type(ts_type) for name, ts_type in ts} == {
        name: _expected_ts_type(hint) for name, hint in authority.items()
    }


def test_frontend_outcome_literal_union_is_the_semantic_outcome_vocabulary() -> None:
    """``outcome`` is a closed vocabulary on the TS side; its members are the enum the serializer projects.

    ``tools/_common`` ships ``outcome=sc.outcome.value`` from ``SemanticOutcome``, so a
    member added to or dropped from either side is a wire decision this pin makes visible.
    """
    ts_types = dict(_ts_fields())
    members = _string_literal_members(ts_types["outcome"])
    assert members is not None, f"outcome is not a string-literal union: {ts_types['outcome']!r}"
    assert sorted(members) == sorted(member.value for member in SemanticOutcome)


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        (str, "string"),
        (bool, "boolean"),
        (int, "number"),
        (float, "number"),
        (str | None, "string | null"),
        (list[str], "string[]"),
        (list[int] | None, "number[] | null"),
    ],
    ids=["str", "bool", "int", "float", "optional-str", "list-str", "optional-list-int"],
)
def test_expected_ts_type_mapping(hint: object, expected: str) -> None:
    assert _expected_ts_type(hint) == expected


@pytest.mark.parametrize("hint", [dict[str, str], str | int, bytes], ids=["dict", "two-member-union", "bytes"])
def test_expected_ts_type_refuses_hints_it_does_not_know(hint: object) -> None:
    with pytest.raises(AssertionError):
        _expected_ts_type(hint)


def test_normalize_ts_type_collapses_only_string_literal_unions() -> None:
    assert _normalize_ts_type('"satisfied" | "conflict"') == "string"
    assert _normalize_ts_type('"only"') == "string"
    assert _normalize_ts_type("string | null") == "string | null"
    assert _normalize_ts_type("number") == "number"
    assert _string_literal_members('"a" | b') is None


def test_frontend_interface_is_actually_parsed() -> None:
    """Smoke: the anchor path resolves and the regex matched real records.

    Guards the parity test against passing because the file moved or
    Prettier changed the record shape and the regex matched nothing.
    """
    assert _TS_TYPES_PATH.is_file(), f"expected the frontend types at {_TS_TYPES_PATH}"
    assert len(_ts_fields()) >= 8


def test_dataclass_projects_onto_the_payload_with_one_derived_key() -> None:
    """Every payload key is a dataclass field except ``requirement_code``.

    ``requirement_code`` is projected from ``requirement.requirement_code``
    by both serializers (``composer_mcp.server._semantic_edge_contract_to_payload``
    and ``tools/_common``); the dataclass's ``producer_facts`` and
    ``requirement`` objects never ship. Pinning the projection here means a
    new dataclass field is a deliberate wire decision, not a silent omission.
    """
    dataclass_fields = {field.name for field in fields(SemanticEdgeContract)}
    payload_keys = set(_authority())
    assert payload_keys - dataclass_fields == {"requirement_code"}
    assert dataclass_fields - payload_keys == {"producer_facts", "requirement"}
