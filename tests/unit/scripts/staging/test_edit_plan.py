"""Edit-plan loading, target resolution and the per-edit verdict checks of the Composer edit battery."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import edit_plan
from scripts.staging.opslib import UsageError

FILE_TEXT = {"blob-1": "Summarise the row."}


def graph() -> dict[str, Any]:
    """A small graph with the roles the shipped plan needs: a source, its head, an inline and a file prompt."""

    def llm(node_id: str, input_: str, on_success: str, template: object) -> dict[str, Any]:
        return {
            "id": node_id,
            "node_type": "transform",
            "plugin": "llm",
            "input": input_,
            "on_success": on_success,
            "on_error": "discard",
            "options": {"prompt_template": template, "model": "m"},
            "description": None,
        }

    return {
        "id": "state-0",
        "version": 1,
        "sources": {"rows": {"plugin": "csv", "on_success": "c0", "options": {}, "on_validation_failure": "discard"}},
        "nodes": [
            llm("classify", "c0", "c1", "Classify the row."),
            llm("summarise", "c1", "out", {"blob_ref": "blob-1", "mode": "inline_content", "sha256": "x"}),
        ],
        "edges": [
            {"id": "e1", "from_node": "rows", "to_node": "classify", "edge_type": "on_success", "label": None},
            {"id": "e2", "from_node": "classify", "to_node": "summarise", "edge_type": "on_success", "label": None},
        ],
        "outputs": [{"name": "out", "plugin": "json", "options": {"path": "out.jsonl"}, "on_write_failure": "discard"}],
        "metadata": {"name": "Pipeline", "description": "Original description."},
        "is_valid": True,
        "validation_errors": None,
    }


def read_text(marker: dict[str, Any]) -> str:
    return FILE_TEXT[marker["blob_ref"]]


def write_plan(tmp_path: Path, edits: list[dict[str, Any]], **overrides: Any) -> Path:
    plan = {
        "format": edit_plan.PLAN_FORMAT,
        "name": "test-plan",
        "description": "",
        "forbidden_tools": ["set_pipeline"],
        "targets": {"source": {"kind": "first_source"}, "head": {"kind": "consumer_of", "target": "source"}},
        "edits": edits,
        **overrides,
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return path


# ── the shipped plan ───────────────────────────────────────────────────────


def test_shipped_plan_has_fifty_edits_over_the_composer_editing_tools() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    assert len(plan.edits) == 50
    tools = {tool for edit in plan.edits if "tools_any" in edit for tool in edit["tools_any"]}
    assert {
        "set_metadata",
        "splice_transform",
        "upsert_node",
        "patch_node_options",
        "patch_output_options",
        "set_output",
        "remove_output",
        "remove_node",
        "change_substring",
        "wire_blob_inline_ref",
    } <= tools
    assert plan.forbidden_tools == ("set_pipeline",)


def test_shipped_plan_carries_no_deployment_or_personal_details() -> None:
    text = edit_plan.DEFAULT_PLAN.read_text()
    assert not re.search(r"https?://|@|/home/|/Users/|[0-9a-f]{8}-[0-9a-f]{4}-", text)


def test_shipped_plan_resolves_on_a_graph_with_its_roles_and_leaves_no_placeholder() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    state = graph()
    targets = edit_plan.resolve_targets(plan, state, {})
    assert targets == {"source": "rows", "head": "classify", "llm_inline": "classify", "llm_file": "summarise"}
    edits = edit_plan.resolve_edits(plan, targets, state)
    assert [edit["number"] for edit in edits] == list(range(1, 51))
    assert not re.search(r"\{[a-z_]+(:[^}]*)?\}", json.dumps(edits))
    restore = next(edit for edit in edits if edit["id"] == "restore-pipeline-name")
    assert restore["message"] == 'Set the pipeline name back to "Pipeline".'
    unset = next(edit for edit in edits if edit["id"] == "restore-max-tokens")
    assert "nothing (remove the setting" in unset["message"]


def test_missing_target_is_a_usage_error_naming_the_override_flag() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    state = graph()
    state["nodes"] = [node for node in state["nodes"] if node["id"] != "summarise"]
    with pytest.raises(UsageError, match=r"llm_file .*--target NAME=ID"):
        edit_plan.resolve_targets(plan, state, {})


def test_target_override_must_fit_its_role() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    assert edit_plan.resolve_targets(plan, graph(), {"llm_inline": "classify"})["llm_inline"] == "classify"
    with pytest.raises(UsageError, match="llm_inline=summarise"):
        edit_plan.resolve_targets(plan, graph(), {"llm_inline": "summarise"})
    with pytest.raises(UsageError, match="not targets"):
        edit_plan.resolve_targets(plan, graph(), {"nonsense": "classify"})


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        ({"id": "x", "message": "m", "expect": []}, "at least one expectation"),
        ({"id": "x", "message": "m", "expect": [{"equals": "/a"}]}, "malformed equals"),
        ({"id": "x", "message": "m", "expect": [{"equals": "/a", "value": 1, "absent": "/b"}]}, "exactly one"),
        ({"id": "x", "message": "{nowhere}", "expect": [{"present": "/a"}]}, "not a plan target"),
        ({"id": "x", "message": "m", "expect": [{"present": "/a"}], "approve": {"kind": "vague_term", "node": "n"}}, "approve"),
        ({"id": "x", "message": "m", "expect": [{"text": "/a", "append": "s", "remove": "t"}]}, "malformed text"),
    ],
)
def test_malformed_plans_are_refused(tmp_path: Path, edit: dict[str, Any], match: str) -> None:
    with pytest.raises(UsageError, match=match):
        edit_plan.load_plan(write_plan(tmp_path, [edit]))


def test_duplicate_edit_ids_are_refused(tmp_path: Path) -> None:
    edit = {"id": "same", "message": "m", "expect": [{"present": "/metadata"}]}
    with pytest.raises(UsageError, match="unique"):
        edit_plan.load_plan(write_plan(tmp_path, [edit, edit]))


# ── verdict checks: each with a positive and a negative control ───────────


def resolved(expect: list[dict[str, Any]], may_change: list[str] | None = None) -> dict[str, Any]:
    return {"expect": expect, "may_change": may_change or [], "tools_any": []}


def check(expect: list[dict[str, Any]], after: dict[str, Any], before: dict[str, Any] | None = None) -> list[str]:
    before_view = edit_plan.graph_view(before or graph())
    failures, _ = edit_plan.check_expectations(
        resolved(expect), before_view, edit_plan.graph_view(after), edit_plan.graph_view(graph()), read_text
    )
    return failures


def with_insert(state: dict[str, Any], node_id: str = "battery_a") -> dict[str, Any]:
    changed = copy.deepcopy(state)
    changed["sources"]["rows"]["on_success"] = "ca"
    changed["nodes"].append(
        {
            "id": node_id,
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "ca",
            "on_success": "c0",
            "on_error": None,
            "options": {},
        }
    )
    return changed


def test_chain_accepts_the_inserted_step_and_rejects_a_bypass_or_a_second_consumer() -> None:
    expectation = [{"chain": {"from": "rows", "to": "classify", "via": ["battery_a"]}}]
    assert check(expectation, with_insert(graph())) == []
    bypass = with_insert(graph())
    bypass["sources"]["rows"]["on_success"] = "c0"
    assert check(expectation, bypass) == ["expect[0]:chain"]
    forked = with_insert(graph())
    forked["nodes"].append({**forked["nodes"][0], "id": "stray", "input": "c0"})
    assert check(expectation, forked) == ["expect[0]:chain"]
    forked_midway = with_insert(graph())
    forked_midway["nodes"].append({**forked_midway["nodes"][0], "id": "stray", "input": "ca"})
    assert check(expectation, forked_midway) == ["expect[0]:chain"]


def test_text_append_replace_and_remove_are_exact() -> None:
    pointer = "/nodes/classify/options/prompt_template"
    after = graph()
    after["nodes"][0]["options"]["prompt_template"] = "Classify the row.\nBattery marker one."
    assert check([{"text": pointer, "append": "Battery marker one.", "form": "inline"}], after) == []
    assert check([{"text": pointer, "append": "Battery marker two."}], after) == [f"expect[0]:text:{pointer}"]
    replaced = copy.deepcopy(after)
    replaced["nodes"][0]["options"]["prompt_template"] = "Classify the row.\nBattery marker two."
    assert check([{"text": pointer, "replace": ["marker one", "marker two"]}], replaced, before=after) == []
    reworded = copy.deepcopy(replaced)
    reworded["nodes"][0]["options"]["prompt_template"] = "Classify each row.\nBattery marker two."
    assert check([{"text": pointer, "replace": ["marker one", "marker two"]}], reworded, before=after) != []
    assert check([{"text": pointer, "remove": "Battery marker two."}], graph(), before=replaced) == []
    assert check([{"text": pointer, "remove": "Battery marker two."}], replaced, before=replaced) != []


def test_text_reads_a_bound_file_and_pins_its_form() -> None:
    pointer = "/nodes/summarise/options/prompt_template"
    FILE_TEXT["blob-2"] = "Summarise the row.\nBattery file marker one."
    after = graph()
    after["nodes"][1]["options"]["prompt_template"] = {"blob_ref": "blob-2", "mode": "inline_content", "sha256": "y"}
    assert check([{"text": pointer, "append": "Battery file marker one.", "form": "file"}], after) == []
    inline = graph()
    inline["nodes"][1]["options"]["prompt_template"] = "Summarise the row.\nBattery file marker one."
    assert check([{"text": pointer, "append": "Battery file marker one.", "form": "file"}], inline) != []


def test_restored_compares_with_the_attempt_baseline_and_accepts_null_for_an_absent_setting() -> None:
    changed = graph()
    changed["nodes"][0]["options"]["max_tokens"] = 777
    assert check([{"restored": "/nodes/classify/options/max_tokens"}], changed) != []
    cleared = graph()
    cleared["nodes"][0]["options"]["max_tokens"] = None
    assert check([{"restored": "/nodes/classify/options/max_tokens"}], cleared) == []
    renamed = graph()
    renamed["metadata"]["name"] = "Other"
    assert check([{"restored": "/metadata/name"}], renamed) != []
    assert check([{"restored": "/metadata/name"}], graph()) == []


def test_unrelated_changes_are_every_difference_no_expectation_covers() -> None:
    before = edit_plan.graph_view(graph())
    after_state = with_insert(graph())
    after_state["nodes"][1]["description"] = "drifted"
    after = edit_plan.graph_view(after_state)
    _, covered = edit_plan.check_expectations(
        resolved([{"present": "/nodes/battery_a"}, {"chain": {"from": "rows", "to": "classify", "via": ["battery_a"]}}]),
        before,
        after,
        before,
        read_text,
    )
    assert edit_plan.unrelated_changes(before, after, covered) == ["/nodes/summarise/description"]


def test_a_pointer_covers_its_own_subtree_only() -> None:
    assert edit_plan.covers("/nodes/battery_a", "/nodes/battery_a/options/schema")
    assert not edit_plan.covers("/nodes/battery_a", "/nodes/battery_ab")


def test_new_validation_errors_are_counted_by_occurrence() -> None:
    before = graph()
    before["validation_errors"] = [{"error_code": "x", "component": "a", "message": "m"}]
    after = copy.deepcopy(before)
    assert edit_plan.new_validation_errors(before, after) == []
    after["validation_errors"].append({"error_code": "x", "component": "a", "message": "m"})
    assert len(edit_plan.new_validation_errors(before, after)) == 1


def test_tool_findings_need_an_expected_tool_and_refuse_an_applied_forbidden_one() -> None:
    edit = {"tools_any": ["change_substring"]}
    used = {"tool_outcomes": {"change_substring": {"applied": 1}}}
    assert edit_plan.tool_findings(edit, used, ["set_pipeline"]) == []
    rejected = {"tool_outcomes": {"change_substring": {"rejected": 2}}}
    assert edit_plan.tool_findings(edit, rejected, ["set_pipeline"]) == ["expected_tool_not_used:change_substring"]
    rebuilt = {"tool_outcomes": {"change_substring": {"applied": 1}, "set_pipeline": {"applied": 1}}}
    assert edit_plan.tool_findings(edit, rebuilt, ["set_pipeline"]) == ["forbidden_tool_applied:set_pipeline"]
