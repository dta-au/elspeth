"""TAUGHT: live argument schemas agree with exact, owned teaching.

This is a lexical ownership gate, not a semantic quality verdict. No argument
fences are authorized. Explicit argument markers are the stale-name authority;
arbitrary quoted plugin options, response keys and enum values are not arguments.
Response and argument gates share the same paragraph/table/fence reader.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.cicd.composer_teaching import (
    argument_teaching,
    is_quoted_leaf,
    owned_teaching_text,
    teaching_blocks,
    validate_teaching_scopes,
)
from scripts.cicd.composer_wire_census import census_taught_wire

from elspeth.web.composer.tools._dispatch import get_tool_definitions


def test_every_shipped_argument_is_taught_in_its_own_context() -> None:
    rows = census_taught_wire()
    definitions = get_tool_definitions()
    assert rows and any(row.shipped for row in rows.values())
    assert set(rows) == {definition["name"] for definition in definitions}
    assert {name: sorted(row.shipped - row.taught) for name, row in rows.items() if row.shipped - row.taught} == {}, (
        "Shipped arguments need a nonempty exact property description or a quoted key in their own tool context."
    )


def test_no_explicit_argument_declaration_is_stale() -> None:
    rows = census_taught_wire()
    assert {name: sorted(row.declared - row.shipped) for name, row in rows.items() if row.declared - row.shipped} == {}, (
        "An explicit teaching declaration names an argument absent from the live schema."
    )


def _definitions(*, description: str = "", property_description: str | None = None) -> list[dict[str, Any]]:
    schema: dict[str, Any] = {"type": "string"}
    if property_description is not None:
        schema["description"] = property_description
    return [
        {"name": "alpha", "description": description, "parameters": {"properties": {"knob": schema, "sibling": {"type": "string"}}}},
        {"name": "beta", "description": "Returns `knob`, `sibling` and `obsolete`.", "parameters": {"properties": {}}},
    ]


@pytest.mark.parametrize("description", ["Explains this value.", "Mentioning `sibling` still teaches only this property."])
def test_property_description_teaches_only_its_exact_argument(description: str) -> None:
    shipped, taught, declared = argument_teaching(_definitions(property_description=description), "")["alpha"]
    assert shipped == {"knob", "sibling"}
    assert taught == {"knob"}
    assert not declared


@pytest.mark.parametrize("description", [None, "", "  \n "])
def test_missing_or_blank_description_does_not_borrow_foreign_description(description: str | None) -> None:
    assert not argument_teaching(_definitions(property_description=description), "")["alpha"][1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("`alpha` accepts `knob`.", True),
        ("`alpha` accepts 'knob'.", True),
        ("`alpha` accepts knob.", False),
        ("`alpha` accepts `knob_extra`.", False),
        ("`beta` returns `knob`.", False),
        ("`alpha` and `beta` discuss `knob`.", False),
        ("`alpha` is useful.\n\nRead `knob`.", False),
        ("## `alpha`\n\nRead `knob`.", False),
        ("| `alpha` | accepts values |\n| `beta` | `knob` |", False),
        ("| `alpha` | accepts `knob` |\n| `beta` | unrelated |", True),
        ("```python\n`alpha` accepts `knob`\n```", False),
        ("~~~\n`alpha` accepts `knob`\n~~~", False),
        ("<!-- taught:begin argument alpha knob -->\n```\n`knob`\n```\n<!-- taught:end -->", True),
        ("<!-- taught:begin tool-data alpha data.knob -->\n`alpha` returns `knob`.\n<!-- taught:end -->", False),
    ],
)
def test_argument_context_ownership(text: str, expected: bool) -> None:
    assert ("knob" in argument_teaching(_definitions(), text)["alpha"][1]) is expected


def test_own_description_is_positive_but_explicit_response_scope_wins() -> None:
    assert argument_teaching(_definitions(description="Set `knob`."), "")["alpha"][1] == {"knob"}
    description = "<!-- taught:begin tool-data alpha data.knob -->\nReturns `knob`.\n<!-- taught:end -->"
    assert not argument_teaching(_definitions(description=description), "")["alpha"][1]


def test_stale_arguments_come_only_from_explicit_declarations() -> None:
    prompt = "`alpha` returns `obsolete` and uses plugin option `other`.\n\n<!-- taught:begin argument alpha removed -->\nSet `removed`.\n<!-- taught:end -->"
    shipped, taught, declared = argument_teaching(_definitions(description="Returns `legacy`."), prompt)["alpha"]
    assert declared - shipped == {"removed"}
    assert not taught


def test_shared_envelope_and_tool_specific_subsection_cannot_teach_homonyms() -> None:
    text = """<!-- taught:begin envelope * success -->
`success` is the envelope outcome.
<!-- taught:end -->

### A tool-specific subsection
`alpha` returns `knob`.
"""
    blocks = teaching_blocks(text, frozenset({"alpha", "beta"}), site="probe")
    validate_teaching_scopes(blocks, frozenset({("envelope", "*", "success")}))
    assert is_quoted_leaf("success", owned_teaching_text(blocks, "envelope", "*", "success"))
    assert not is_quoted_leaf("data.success", owned_teaching_text(blocks, "tool-data", "alpha", "data.success"))
    assert not is_quoted_leaf("knob", owned_teaching_text(blocks, "envelope", "*", "knob"))
    assert not is_quoted_leaf("data.knob", owned_teaching_text(blocks, "tool-data", "beta", "data.knob"))


def test_canonical_row_selectors_do_not_credit_siblings_or_arguments() -> None:
    text = """<!-- taught:begin tool-data alpha data.node.* row=node; tool-data alpha data.output.* row=output -->
| Family | Fields |
| node | `id`, `plugin` |
| output | `sink_name`, `plugin` |
<!-- taught:end -->
"""
    blocks = teaching_blocks(text, frozenset({"alpha"}), site="probe")
    validate_teaching_scopes(blocks, frozenset({("tool-data", "alpha", "data.node.id"), ("tool-data", "alpha", "data.output.sink_name")}))
    assert is_quoted_leaf("id", owned_teaching_text(blocks, "tool-data", "alpha", "data.node.id"))
    assert not is_quoted_leaf("sink_name", owned_teaching_text(blocks, "tool-data", "alpha", "data.node.sink_name"))
    assert not is_quoted_leaf("id", owned_teaching_text(blocks, "argument", "alpha", "id"))


@pytest.mark.parametrize(
    "text",
    [
        "<!-- taught:end -->",
        "<!-- taught:begin argument alpha knob -->\n`knob`",
        "<!-- taught:begin argument alpha knob -->\n<!-- taught:begin argument alpha sibling -->",
        "<!-- taught:begin argument retired knob -->\n<!-- taught:end -->",
        "<!-- taught:begin tool-data * data.knob -->\n<!-- taught:end -->",
        "<!-- taught:begin tool-data alpha data.* -->\n<!-- taught:end -->",
        "<!-- taught:begin argument alpha knob.* -->\n<!-- taught:end -->",
        "<!-- taught:begin argument alpha -->\n<!-- taught:end -->",
        "<!-- taught:begin argument alpha knob; argument alpha knob -->\n<!-- taught:end -->",
        "<!-- taught:begin tool-data alpha data.node.* row=missing -->\n| node | `id` |\n<!-- taught:end -->",
        "<!-- taught:begin tool-data alpha data.node.* row=* -->\n<!-- taught:end -->",
        "<!-- taught:unknown alpha -->",
        "```\nunclosed",
    ],
)
def test_malformed_or_stale_ownership_refused(text: str) -> None:
    with pytest.raises(ValueError):
        teaching_blocks(text, frozenset({"alpha"}), site="probe")


@pytest.mark.parametrize("scope", ["obsolete alpha data.knob", "tool-data alpha data.removed", "tool-data alpha data.removed.*"])
def test_response_scope_must_match_live_surface_tool_and_path(scope: str) -> None:
    blocks = teaching_blocks(f"<!-- taught:begin {scope} -->\n`knob`\n<!-- taught:end -->", frozenset({"alpha"}), site="probe")
    with pytest.raises(ValueError, match="stale teaching scope"):
        validate_teaching_scopes(blocks, frozenset({("tool-data", "alpha", "data.knob")}))


def test_explicit_scope_does_not_extend_to_following_paragraph_or_fence() -> None:
    text = "<!-- taught:begin argument alpha knob -->\nNo quoted key here.\n<!-- taught:end -->\n\nRead `knob`.\n\n```\n`knob`\n```"
    assert not argument_teaching(_definitions(), text)["alpha"][1]


def test_nested_property_description_cannot_teach_its_parent() -> None:
    definitions = _definitions()
    definitions[0]["parameters"]["properties"]["knob"] = {
        "type": "object",
        "properties": {"child": {"type": "string", "description": "Explains the child."}},
    }
    assert not argument_teaching(definitions, "")["alpha"][1]


@pytest.mark.parametrize(
    ("description", "prompt", "expected"),
    [
        ("", "", False),
        ("", "`beta` returns `knob`.", False),
        ("", "`alpha` returns `knob`.", True),
        ("Returns `knob`.", "", True),
        ("", "<!-- taught:begin envelope * knob -->\n`knob`\n<!-- taught:end -->", False),
        ("", "<!-- taught:begin argument alpha knob -->\n`knob`\n<!-- taught:end -->", False),
        ("<!-- taught:begin argument alpha knob -->\n`knob`\n<!-- taught:end -->", "", False),
    ],
)
def test_envelope_gate_uses_owned_reader(monkeypatch: pytest.MonkeyPatch, description: str, prompt: str, expected: bool) -> None:
    from tests.unit.web.composer import test_tool_result_envelope_gate as gate

    shipped = gate.ShippedKey("tool-data", "alpha", "data.knob", "probe")
    monkeypatch.setattr(gate, "_all_descriptions", lambda: {"alpha": description, "beta": "Returns `knob`."})
    monkeypatch.setattr(gate, "build_system_prompt", lambda _: prompt)
    monkeypatch.setattr(gate, "shipped_keys", lambda: [shipped, gate.ShippedKey("envelope", "*", "knob", "probe")])
    assert gate.is_taught(shipped) is expected


def test_singleton_state_records_require_teaching_separate_from_existing_list_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.web.composer import test_tool_result_envelope_gate as gate

    monkeypatch.setattr(gate, "_all_descriptions", lambda: {"get_pipeline_state": ""})
    monkeypatch.setattr(gate, "build_system_prompt", lambda _: "")
    assert gate.is_taught(gate.ShippedKey("tool-data", "get_pipeline_state", "data.nodes[].id", "probe"))
    assert not gate.is_taught(gate.ShippedKey("tool-data", "get_pipeline_state", "data.node.id", "probe"))
    assert not gate.is_taught(gate.ShippedKey("tool-data", "get_pipeline_state", "data.output.sink_name", "probe"))


def test_scope_comments_inside_fenced_example_are_not_declarations() -> None:
    prompt = "```\n<!-- taught:begin argument alpha removed -->\n`removed`\n<!-- taught:end -->\n```"
    assert argument_teaching(_definitions(), prompt)["alpha"][2] == frozenset()


def test_repeated_scope_selector_must_match_within_each_region() -> None:
    good = "<!-- taught:begin tool-data alpha data.node.* row=node -->\n| node | `id` |\n<!-- taught:end -->\n"
    stale = "<!-- taught:begin tool-data alpha data.node.* row=node -->\n| output | `sink_name` |\n<!-- taught:end -->\n"
    with pytest.raises(ValueError, match="stale teaching table-row selector"):
        teaching_blocks(good + stale, frozenset({"alpha"}), site="probe")


def test_repeated_complete_scope_regions_are_each_valid() -> None:
    first = "<!-- taught:begin tool-data alpha data.node.* row=node -->\n| node | `id` |\n<!-- taught:end -->\n"
    second = "<!-- taught:begin tool-data alpha data.node.* row=node -->\n| node | `plugin` |\n<!-- taught:end -->\n"
    blocks = teaching_blocks(first + second, frozenset({"alpha"}), site="probe")
    validate_teaching_scopes(blocks, frozenset({("tool-data", "alpha", "data.node.id"), ("tool-data", "alpha", "data.node.plugin")}))
    assert is_quoted_leaf("id", owned_teaching_text(blocks, "tool-data", "alpha", "data.node.id"))
    assert is_quoted_leaf("plugin", owned_teaching_text(blocks, "tool-data", "alpha", "data.node.plugin"))
