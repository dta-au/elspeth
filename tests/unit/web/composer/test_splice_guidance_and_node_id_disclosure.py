"""splice_transform planner surfaces: closed-code guidance and node.id disclosure.

* ``splice_validation_failed`` is the leading entry of a splice rejection, so
  the planner's redacted repair feedback resolves it through
  ``explain_validation_code``; an unresolved code attaches no guidance at all.
* ``splice_transform``'s advertised ``node.id`` must declare the runtime's
  reserved labels with ``not``, exactly as the canonical ``set_pipeline``
  schema declares them on ``nodes[].id``. Acceptance is unchanged: the
  runtime model already refuses those ids.
"""

from __future__ import annotations

from typing import Any

import pytest
from jsonschema import Draft202012Validator

from elspeth.core.config import _RESERVED_EDGE_LABELS
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema


def _splice_node_id_schema() -> dict[str, Any]:
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}
    schema: dict[str, Any] = definitions["splice_transform"]["parameters"]["properties"]["node"]["properties"]["id"]
    return schema


def test_splice_validation_failed_resolves_to_direct_guidance() -> None:
    assert explain_validation_code("splice_validation_failed") == (
        "The inserted transform passed its own option checks, but the pipeline that results from inserting it fails "
        "context-aware validation. The rejected_mutation entries that follow this one name each new error, attributed "
        "to the component it concerns in rejected_component. Nothing was mutated.",
        "Repair the inserted node's options against the entries that follow (for a schema_contract_violation, remove "
        "or satisfy the fields it names), or splice at a position whose producer supplies them, then retry "
        "splice_transform.",
    )


def test_splice_node_id_declares_the_reserved_labels_like_set_pipeline_node_id() -> None:
    canonical_node_id = canonical_set_pipeline_schema()["properties"]["nodes"]["items"]["properties"]["id"]
    splice_node_id = _splice_node_id_schema()

    assert splice_node_id["not"] == canonical_node_id["not"]
    assert splice_node_id["not"] == {"enum": sorted(_RESERVED_EDGE_LABELS)}
    assert splice_node_id["pattern"] == canonical_node_id["pattern"]
    assert splice_node_id["maxLength"] == canonical_node_id["maxLength"]


@pytest.mark.parametrize("reserved", sorted(_RESERVED_EDGE_LABELS))
def test_advertised_splice_node_id_schema_rejects_each_reserved_label(reserved: str) -> None:
    validator = Draft202012Validator(_splice_node_id_schema())

    assert [error.validator for error in validator.iter_errors(reserved)] == ["not"]


@pytest.mark.parametrize("node_id", ("a", "valid_id-1", "Z9", "forked", "continue_2"))
def test_advertised_splice_node_id_schema_still_admits_runtime_valid_ids(node_id: str) -> None:
    assert list(Draft202012Validator(_splice_node_id_schema()).iter_errors(node_id)) == []
