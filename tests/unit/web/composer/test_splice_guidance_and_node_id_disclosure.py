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
    explanation, suggested_fix = explain_validation_code("splice_validation_failed")
    assert explanation == (
        "The inserted transform passed its own option checks, but the pipeline that results from inserting it fails "
        "context-aware validation, and splice_transform requires that whole result to validate. The rejected_mutation "
        "entries that follow this one name each error the insertion added, attributed to the component it concerns in "
        "rejected_component. An entry that shares its error_code with an error the pipeline already had may be that "
        "error, reworded. When no rejected_mutation entry follows, the insertion added no error: the errors were there "
        "before the splice. Nothing was mutated."
    )
    assert suggested_fix == (
        "Repair each error the insertion added: fix the inserted node's options (for a schema_contract_violation, "
        "remove or satisfy the fields it names) or splice at a position whose producer supplies those fields. Errors "
        "that were there before the splice, reworded or not, also refuse it, and the same splice is refused again until "
        "they are repaired: repair them first, or add the step with upsert_node and use upsert_node with "
        "routing_only=true to re-point the successor's input."
    )
    # The per-entry claim runs one way only: every added error is listed, not
    # every listed entry is added (a standing error the splice rewords can be
    # listed when it is not a field contract).
    assert "Each rejected_mutation entry that follows this one names an error the insertion added" not in explanation
    # A splice refused only for errors the pipeline already had forwards no
    # entry of its own, so an unconditional "retry splice_transform" would
    # send the planner back into the same refusal.
    assert "then retry splice_transform" not in suggested_fix


def test_splice_description_discloses_the_whole_pipeline_validity_requirement() -> None:
    # The handler validates the whole spliced pipeline and refuses on any
    # error, including errors standing before the splice; the description is
    # where the planner first learns the tool, so it states that precondition.
    definitions = {definition["name"]: definition for definition in get_tool_definitions()}
    description = " ".join(definitions["splice_transform"]["description"].split())
    assert (
        "The whole resulting pipeline must validate, or the splice is refused and nothing changes. "
        "Errors that were there before the splice also refuse it." in description
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
