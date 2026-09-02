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
from dataclasses import fields
from pathlib import Path
from typing import get_type_hints

import elspeth
from elspeth.composer_mcp import server as mcp_server
from elspeth.contracts.plugin_semantics import SemanticEdgeContract
from elspeth.web.composer import redaction
from elspeth.web.composer.tools import _common
from elspeth.web.execution.schemas import SemanticEdgeContractResponse

_TS_TYPES_PATH = Path(elspeth.__file__).parent / "web" / "frontend" / "src" / "types" / "index.ts"

# The exported interface block, then one `name: type;` record per line
# (Prettier-stable). Comments inside the block are not records.
_TS_INTERFACE_RE = re.compile(r"^export interface SemanticEdgeContract \{\n(?P<body>.*?)^\}", re.MULTILINE | re.DOTALL)
_TS_FIELD_RE = re.compile(r"^\s*(?P<name>\w+):\s*(?P<type>[^;]+);\s*$", re.MULTILINE)


def _authority() -> dict[str, object]:
    return get_type_hints(_common._SemanticEdgeContractPayload)


def _nullable(annotation: object) -> bool:
    return "None" in str(annotation)


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
    assert get_type_hints(mcp_server._SemanticEdgeContractPayload) == _authority()


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


def test_frontend_interface_matches_the_envelope_payload_in_order_and_nullability() -> None:
    authority = _authority()
    ts = _ts_fields()
    assert [name for name, _ in ts] == list(authority)
    # ``string | null`` on the TS side is exactly ``str | None`` on the
    # Python side; a literal union such as the ``outcome`` values is still a
    # non-null string.
    assert {name: "| null" in ts_type for name, ts_type in ts} == {name: _nullable(hint) for name, hint in authority.items()}


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
