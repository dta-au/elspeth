"""Tier-3 boundary coercion: LLM-stringified object arguments.

Root cause (proven from staging audit, sessions ``fd551d98`` / ``71d57b4f``,
model ``openrouter/openai/gpt-5.4-mini``): the deployed composer model
intermittently serialises a nested-object tool-call parameter as a JSON
*string* (``"options": "{}"``, ``"patch": "{\\"column\\":\\"url\\"}"``)
instead of emitting a JSON object. The redaction-bearing argument models
declare these fields as ``dict[str, Any]``, so a stringified value was
rejected with a ``ValidationError`` → ``ToolArgumentError`` and the whole
``set_pipeline`` / ``set_source_from_blob`` / ``patch_*`` call failed. Because
the model does this only some of the time, the composer build (and therefore
the demo + hello-world tutorial) was intermittently unbuildable.

The fix is a Tier-3 boundary ``BeforeValidator`` that parses a JSON-string
encoding an object back into the object it encodes. The string is an
equivalent wire encoding of the object — parsing it is meaning-preserving
coercion, not fabrication (CLAUDE.md "Data Manifesto", ``"42" -> 42`` class),
and is exempt from the defensive-programming ban as a documented trust-boundary
deserialisation.

Negative cases pin that the coercion is *narrow*: a non-string, a string that
is not valid JSON, and a string that decodes to a non-object (list/scalar) all
remain rejected, so genuinely malformed input still fails closed.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from elspeth.web.composer.redaction import (
    ApplyPipelineRecipeArgumentsModel,
    PatchNodeOptionsArgumentsModel,
    PatchOutputOptionsArgumentsModel,
    PatchSourceOptionsArgumentsModel,
    SetSourceArgumentsModel,
    SetSourceFromBlobArgumentsModel,
    _PipelineNodeModel,
    _PipelineOutputModel,
    _SetPipelineSourceModel,
)

_OBJECT_JSON = '{"column": "url", "schema": {"mode": "observed"}}'
_EXPECTED = {"column": "url", "schema": {"mode": "observed"}}


# ---------------------------------------------------------------------------
# GREEN behaviour: a JSON-string-encoded object coerces to the object.
# ---------------------------------------------------------------------------


def test_set_source_options_stringified_object_coerces() -> None:
    m = SetSourceArgumentsModel.model_validate(
        {"plugin": "text", "on_success": "out", "on_validation_failure": "discard", "options": _OBJECT_JSON}
    )
    assert m.options == _EXPECTED


def test_apply_recipe_slots_stringified_object_coerces() -> None:
    m = ApplyPipelineRecipeArgumentsModel.model_validate({"recipe_name": "r", "slots": '{"output_path": "out.jsonl"}'})
    assert m.slots == {"output_path": "out.jsonl"}


def test_set_source_from_blob_options_stringified_object_coerces() -> None:
    m = SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "out", "options": _OBJECT_JSON})
    assert m.options == _EXPECTED


def test_set_pipeline_source_options_stringified_object_coerces() -> None:
    m = _SetPipelineSourceModel.model_validate({"plugin": "text", "on_success": "rows", "blob_id": "b", "options": _OBJECT_JSON})
    assert m.options == _EXPECTED


def test_pipeline_node_options_stringified_object_coerces() -> None:
    node_options = '{"url_field": "url", "content_field": "content"}'
    m = _PipelineNodeModel.model_validate(
        {"id": "scrape", "node_type": "transform", "input": "rows", "plugin": "web_scrape", "options": node_options}
    )
    assert m.options == {"url_field": "url", "content_field": "content"}


def test_pipeline_output_options_stringified_object_coerces() -> None:
    m = _PipelineOutputModel.model_validate({"sink_name": "out", "plugin": "json", "options": '{"path": "results.json"}'})
    assert m.options == {"path": "results.json"}


def test_patch_source_options_stringified_object_coerces() -> None:
    m = PatchSourceOptionsArgumentsModel.model_validate({"patch": _OBJECT_JSON})
    assert m.patch == _EXPECTED


def test_patch_node_options_stringified_object_coerces() -> None:
    m = PatchNodeOptionsArgumentsModel.model_validate({"node_id": "n", "patch": _OBJECT_JSON})
    assert m.patch == _EXPECTED


def test_patch_output_options_stringified_object_coerces() -> None:
    m = PatchOutputOptionsArgumentsModel.model_validate({"sink_name": "s", "patch": _OBJECT_JSON})
    assert m.patch == _EXPECTED


# ---------------------------------------------------------------------------
# Pass-through: a genuine object is unchanged (identity of behaviour).
# ---------------------------------------------------------------------------


def test_genuine_object_options_pass_through_unchanged() -> None:
    m = SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "out", "options": dict(_EXPECTED)})
    assert m.options == _EXPECTED


def test_omitted_options_still_defaults_to_empty_dict() -> None:
    m = SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "out"})
    assert m.options == {}


# ---------------------------------------------------------------------------
# Negative cases: coercion is narrow — malformed input still fails closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "column=text",  # not JSON at all (pins existing test_promote_set_source_from_blob behaviour)
        "[1, 2, 3]",  # valid JSON, but a list — not an object
        '"just a string"',  # valid JSON, but a scalar
        "42",  # valid JSON, but a scalar
        "null",  # valid JSON null
    ],
)
def test_non_object_string_options_still_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "out", "options": bad})


@pytest.mark.parametrize(
    "bad",
    ["column=text", "[1, 2, 3]", '"scalar"', "42", "null"],
)
def test_non_object_string_patch_still_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        PatchSourceOptionsArgumentsModel.model_validate({"patch": bad})


# ---------------------------------------------------------------------------
# Resource bounds: a stringified object nested past the shared JSON depth
# budget is rejected as a typed argument error, never a raw RecursionError
# (elspeth-b944d2324a, forensic audit G13).
# ---------------------------------------------------------------------------

_DEEP_STRINGIFIED_OBJECT = '{"a":' * 100_000 + "1" + "}" * 100_000


def test_deeply_nested_stringified_object_is_rejected_without_recursion_error() -> None:
    """The bounded decoder rejects the string before the C decoder recurses."""
    with pytest.raises(ValidationError):
        PatchSourceOptionsArgumentsModel.model_validate({"patch": _DEEP_STRINGIFIED_OBJECT})


def test_deeply_nested_stringified_options_fail_closed_at_the_public_tool_seam() -> None:
    """Public dispatch witness: typed ToolArgumentError, raw string not echoed."""
    from elspeth.web.composer.protocol import ToolArgumentError
    from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool

    # set_source's argument model carries the ``_LlmJsonObject`` coercion on
    # ``options`` (upsert_node's does not, so it never reaches the decoder).
    with pytest.raises(ToolArgumentError) as exc_info:
        execute_tool(
            "set_source",
            {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard", "options": _DEEP_STRINGIFIED_OBJECT},
            _empty_state(),
            _mock_catalog(),
        )

    assert isinstance(exc_info.value.__cause__, ValidationError)
    assert '{"a":{"a":{"a":' not in str(exc_info.value)


def test_within_budget_stringified_object_still_coerces() -> None:
    """Control: the bound does not reject ordinary nesting."""
    nested = '{"a":' * 8 + '{"column": "url"}' + "}" * 8
    m = PatchSourceOptionsArgumentsModel.model_validate({"patch": nested})
    assert m.patch["a"]["a"]["a"]["a"]["a"]["a"]["a"]["a"] == {"column": "url"}
