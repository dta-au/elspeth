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
    }
    plan.update(overrides)
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return path


# ── the shipped plans ──────────────────────────────────────────────────────


def test_shipped_plans_have_fifty_required_edits_and_optional_extras() -> None:
    for path in (edit_plan.DEFAULT_PLAN, edit_plan.MIXED_PLAN):
        plan = edit_plan.load_plan(path)
        optional = {name for name, spec in plan.targets.items() if "optional" in spec and spec["optional"]}
        required = [edit for edit in plan.edits if not plan.roles_of(edit) & optional]
        assert len(required) == 50
        assert len(plan.edits) == 54
        assert plan.forbidden_tools == ("set_pipeline",)


@pytest.mark.parametrize("path", [edit_plan.DEFAULT_PLAN, edit_plan.MIXED_PLAN])
def test_shipped_plans_carry_no_deployment_or_personal_details(path: Path) -> None:
    assert not re.search(r"https?://|@|/home/|/Users/|[0-9a-f]{8}-[0-9a-f]{4}-", path.read_text())


def test_shipped_plan_resolves_on_a_graph_with_its_roles_and_leaves_no_placeholder() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    state = graph()
    targets = edit_plan.resolve_targets(plan, state, {})
    assert targets == {"source": "rows", "head": "classify", "llm_file": "summarise", "output": "out", "csv_source": "rows"}
    edits = edit_plan.resolve_edits(plan, targets, state)
    assert [edit["number"] for edit in edits] == list(range(1, 55))
    assert [edit["id"] for edit in edits if not edit["applicable"]] == ["secret-switch", "secret-restore"]
    assert not re.search(r"\{[a-z_]+(:[^}]*)?\}", json.dumps([edit for edit in edits if edit["applicable"]]))
    restore = next(edit for edit in edits if edit["id"] == "restore-pipeline-name")
    assert restore["message"] == 'Put the pipeline name back to "Pipeline".'
    unset = next(edit for edit in edits if edit["id"] == "restore-max-tokens")
    assert unset["message"].endswith("back to no value (remove the setting).")
    assert "null" not in unset["message"]


def test_a_blank_was_value_never_renders_as_the_word_null() -> None:
    view = edit_plan.graph_view(graph())
    assert edit_plan.render_was(view, "/nodes/classify/description") == "no value (remove the setting)"
    assert edit_plan.render_was(view, "/nodes/classify/options/max_tokens") == "no value (remove the setting)"
    assert edit_plan.render_was(view, "/metadata/name") == '"Pipeline"'


def test_missing_required_target_is_a_usage_error_naming_the_override_flag() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    state = graph()
    state["nodes"] = [node for node in state["nodes"] if node["id"] != "summarise"]
    with pytest.raises(UsageError, match=r"llm_file .*--target NAME=ID"):
        edit_plan.resolve_targets(plan, state, {})


def test_target_override_must_fit_its_role() -> None:
    plan = edit_plan.load_plan(edit_plan.MIXED_PLAN)
    assert edit_plan.resolve_targets(plan, graph(), {"llm_inline": "classify"})["llm_inline"] == "classify"
    with pytest.raises(UsageError, match="llm_inline=summarise"):
        edit_plan.resolve_targets(plan, graph(), {"llm_inline": "summarise"})
    with pytest.raises(UsageError, match="not targets"):
        edit_plan.resolve_targets(plan, graph(), {"nonsense": "classify"})


def test_secret_roles_resolve_only_on_a_node_holding_a_secret_reference() -> None:
    plan = edit_plan.load_plan(edit_plan.DEFAULT_PLAN)
    state = graph()
    state["nodes"][1]["options"]["api_key"] = {"secret_ref": "MODEL_KEY"}
    targets = edit_plan.resolve_targets(plan, state, {"spare_secret": "SPARE"})
    assert {name: targets[name] for name in ("secret_node", "secret_option", "secret_name", "spare_secret")} == {
        "secret_node": "summarise",
        "secret_option": "api_key",
        "secret_name": "MODEL_KEY",
        "spare_secret": "SPARE",
    }


TOOLS = {"tools_any": ["set_metadata"]}


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        ({"id": "x", "message": "m", "expect": [], **TOOLS}, "at least one expectation"),
        ({"id": "x", "message": "m", "expect": [{"equals": "/a"}], **TOOLS}, "malformed equals"),
        ({"id": "x", "message": "m", "expect": [{"equals": "/a", "value": 1, "absent": "/b"}], **TOOLS}, "exactly one"),
        ({"id": "x", "message": "{nowhere}", "expect": [{"present": "/a"}], **TOOLS}, "not a plan target"),
        ({"id": "x", "message": "m", "expect": [{"present": "/a"}], "approve": {"kind": "vague_term", "node": "n"}, **TOOLS}, "approve"),
        ({"id": "x", "message": "m", "expect": [{"text": "/a", "append": "s", "remove": "t"}], **TOOLS}, "malformed text"),
        ({"id": "x", "message": "m", "expect": [{"present": "/a"}]}, "names its acceptable tools"),
        ({"id": "x", "message": "use set_metadata", "expect": [{"present": "/a"}], **TOOLS}, "names a tool"),
        ({"id": "x", "message": "m", "expect": [{"present": "/a"}], "tools_any": ["set_pipeline"]}, "forbidden tool"),
        ({"id": "x", "message": "m", "expect": [{"present": "/a"}], "may_change": ["/edges"], **TOOLS}, "whole collection"),
        ({"id": "x", "message": "m", "expect": [{"new_node": "n", "fields": {}}], **TOOLS}, "malformed new_node"),
    ],
)
def test_malformed_plans_are_refused(tmp_path: Path, edit: dict[str, Any], match: str) -> None:
    with pytest.raises(UsageError, match=match):
        edit_plan.load_plan(write_plan(tmp_path, [edit]))


def test_an_operator_role_must_be_optional(tmp_path: Path) -> None:
    edit = {"id": "x", "message": "m", "expect": [{"present": "/a"}], **TOOLS}
    with pytest.raises(UsageError, match="must be optional"):
        edit_plan.load_plan(write_plan(tmp_path, [edit], targets={"spare": {"kind": "operator"}}))


def test_duplicate_edit_ids_are_refused(tmp_path: Path) -> None:
    edit = {"id": "same", "message": "m", "expect": [{"present": "/metadata"}], **TOOLS}
    with pytest.raises(UsageError, match="unique"):
        edit_plan.load_plan(write_plan(tmp_path, [edit, edit]))


def test_an_edit_needing_an_absent_optional_role_resolves_as_not_applicable(tmp_path: Path) -> None:
    edit = {"id": "x", "message": "Use {spare}.", "expect": [{"present": "/a"}], **TOOLS}
    plan = edit_plan.load_plan(write_plan(tmp_path, [edit], targets={"spare": {"kind": "operator", "optional": True}}))
    targets = edit_plan.resolve_targets(plan, graph(), {})
    assert targets == {}
    assert edit_plan.resolve_edits(plan, targets, graph()) == [{"number": 1, "id": "x", "applicable": False, "missing_roles": ["spare"]}]


# ── verdict checks: each with a positive and a negative control ───────────


def resolved(expect: list[dict[str, Any]], may_change: list[str] | None = None) -> dict[str, Any]:
    return {"expect": expect, "may_change": may_change or [], "edges_touching": [], "tools_any": [], "tools_all": []}


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
    assert check(expectation, bypass) == ["expect[0]:chain:rows>classify"]
    forked = with_insert(graph())
    forked["nodes"].append({**forked["nodes"][0], "id": "stray", "input": "c0"})
    assert check(expectation, forked) == ["expect[0]:chain:rows>classify"]
    forked_midway = with_insert(graph())
    forked_midway["nodes"].append({**forked_midway["nodes"][0], "id": "stray", "input": "ca"})
    assert check(expectation, forked_midway) == ["expect[0]:chain:rows>classify"]


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


def test_tool_findings_need_an_acceptable_tool_and_flag_a_forbidden_one_called_and_not_rejected() -> None:
    edit = {"tools_any": ["change_substring"], "tools_all": []}
    used = {"tool_outcomes": {"change_substring": {"applied": 1}}}
    assert edit_plan.tool_findings(edit, used, ["set_pipeline"]) == []
    rejected = {"tool_outcomes": {"change_substring": {"rejected": 2}}}
    assert edit_plan.tool_findings(edit, rejected, ["set_pipeline"]) == ["expected_tool_not_used:change_substring"]
    for stamp in ("applied", "failed", "cancelled", "completed", "unstamped"):
        rebuilt = {"tool_outcomes": {"change_substring": {"applied": 1}, "set_pipeline": {stamp: 1}}}
        assert edit_plan.tool_findings(edit, rebuilt, ["set_pipeline"]) == ["forbidden_tool_called:set_pipeline"]
    refused = {"tool_outcomes": {"change_substring": {"applied": 1}, "set_pipeline": {"rejected": 1}}}
    assert edit_plan.tool_findings(edit, refused, ["set_pipeline"]) == []
    both = {"tools_any": [], "tools_all": ["create_blob", "wire_blob_inline_ref"]}
    assert edit_plan.tool_findings(both, {"tool_outcomes": {"create_blob": {"completed": 1}}}, []) == [
        "required_tool_not_used:wire_blob_inline_ref"
    ]


def test_refused_tool_calls_are_warnings() -> None:
    evidence = {"tool_outcomes": {"patch_node_options": {"rejected": 2, "applied": 1}, "set_metadata": {"applied": 1}}}
    assert edit_plan.tool_warnings(evidence) == ["tool_call_rejected:patch_node_options:2"]


# ── preconditions, new components, renames ────────────────────────────────


def test_an_expectation_that_already_holds_is_a_precondition_failure() -> None:
    view = edit_plan.graph_view(graph())
    holds = resolved([{"equals": "/metadata/name", "value": "Pipeline"}, {"equals": "/metadata/description", "value": "New"}])
    assert edit_plan.preconditions(holds, view, view, read_text) == ["precondition:expect[0]_already_holds"]
    pending = resolved([{"equals": "/metadata/name", "value": "Battery check"}])
    assert edit_plan.preconditions(pending, view, view, read_text) == []


def new_step(**fields: Any) -> dict[str, Any]:
    return {
        "new_node": "battery_a",
        "fields": {"plugin": "passthrough", "node_type": "transform", **fields},
        "option_keys": ["schema"],
    }


def test_a_new_node_is_checked_field_by_field() -> None:
    assert check([new_step()], with_insert(graph())) == []
    wrong_plugin = with_insert(graph())
    wrong_plugin["nodes"][-1]["plugin"] = "field_mapper"
    assert check([new_step()], wrong_plugin) == ["expect[0]:new_node:/nodes/battery_a"]
    extra_option = with_insert(graph())
    extra_option["nodes"][-1]["options"] = {"schema": {"mode": "observed"}, "note": "unasked"}
    assert check([new_step()], extra_option) == ["expect[0]:new_node:/nodes/battery_a"]
    described = with_insert(graph())
    described["nodes"][-1]["description"] = "unasked"
    assert check([new_step()], described) == ["expect[0]:new_node:/nodes/battery_a"]
    discard = with_insert(graph())
    discard["nodes"][-1]["on_error"] = "discard"
    assert check([new_step()], discard) == ["expect[0]:new_node:/nodes/battery_a"]
    assert check([new_step(on_error="discard")], discard) == []
    assert check([new_step()], with_insert(graph()), before=with_insert(graph())) == ["expect[0]:new_node:/nodes/battery_a"]


def test_a_new_output_checks_its_plugin_path_and_option_keys() -> None:
    expectation = {
        "new_output": "battery_out",
        "fields": {"plugin": "json", "on_write_failure": "discard"},
        "options": {"path": {"ends_with": "battery_out.jsonl"}},
        "option_keys": ["path", "format"],
    }
    added = graph()
    added["outputs"].append(
        {"name": "battery_out", "plugin": "json", "options": {"path": "x/battery_out.jsonl"}, "on_write_failure": "discard"}
    )
    assert check([expectation], added) == []
    other_path = copy.deepcopy(added)
    other_path["outputs"][-1]["options"]["path"] = "x/elsewhere.jsonl"
    assert check([expectation], other_path) == ["expect[0]:new_output:/outputs/battery_out"]
    csv = copy.deepcopy(added)
    csv["outputs"][-1]["plugin"] = "csv"
    assert check([expectation], csv) == ["expect[0]:new_output:/outputs/battery_out"]
    extra = copy.deepcopy(added)
    extra["outputs"][-1]["options"]["mode"] = "append"
    assert check([expectation], extra) == ["expect[0]:new_output:/outputs/battery_out"]


def test_a_rename_keeps_every_setting_and_plugin() -> None:
    before = with_insert(graph(), "battery_b")
    before["nodes"][-1]["options"] = {"schema": {"mode": "observed"}}
    before["nodes"][-1]["description"] = "Battery step B."
    renamed = copy.deepcopy(before)
    renamed["nodes"][-1]["id"] = "battery_d"
    expectation = [{"renamed_node": {"from": "battery_b", "to": "battery_d"}}]
    assert check(expectation, renamed, before=before) == []
    dropped = copy.deepcopy(renamed)
    dropped["nodes"][-1]["options"] = {}
    assert check(expectation, dropped, before=before) == ["expect[0]:renamed_node:battery_b>battery_d"]
    replugged = copy.deepcopy(renamed)
    replugged["nodes"][-1]["plugin"] = "field_mapper"
    assert check(expectation, replugged, before=before) == ["expect[0]:renamed_node:battery_b>battery_d"]
    undescribed = copy.deepcopy(renamed)
    undescribed["nodes"][-1]["description"] = None
    assert check(expectation, undescribed, before=before) == ["expect[0]:renamed_node:battery_b>battery_d"]
    kept_old = copy.deepcopy(renamed)
    kept_old["nodes"].append(before["nodes"][-1])
    assert check(expectation, kept_old, before=before) == ["expect[0]:renamed_node:battery_b>battery_d"]


# ── the graph view, server-written fields, leftovers ───────────────────────


def test_rows_with_distinct_ids_compare_by_id_not_position() -> None:
    before = graph()
    before["nodes"][0]["options"]["checks"] = [{"id": "r1", "rule": "a"}, {"id": "r2", "rule": "b"}]
    reordered = copy.deepcopy(before)
    reordered["nodes"][0]["options"]["checks"].reverse()
    assert edit_plan.unrelated_changes(edit_plan.graph_view(before), edit_plan.graph_view(reordered), []) == []
    edited = copy.deepcopy(reordered)
    edited["nodes"][0]["options"]["checks"][1]["rule"] = "c"
    assert edit_plan.unrelated_changes(edit_plan.graph_view(before), edit_plan.graph_view(edited), []) == [
        "/nodes/classify/options/checks/id=r1/rule"
    ]
    positional = copy.deepcopy(before)
    positional["nodes"][0]["options"]["order"] = ["a", "b"]
    swapped = copy.deepcopy(positional)
    swapped["nodes"][0]["options"]["order"] = ["b", "a"]
    assert edit_plan.unrelated_changes(edit_plan.graph_view(positional), edit_plan.graph_view(swapped), []) != []


def test_an_absent_null_or_empty_description_is_the_same_but_the_word_null_is_not() -> None:
    blank = graph()
    blank["nodes"][0]["description"] = ""
    absent = graph()
    del absent["nodes"][0]["description"]
    for other in (blank, absent):
        assert edit_plan.unrelated_changes(edit_plan.graph_view(graph()), edit_plan.graph_view(other), []) == []
    word = graph()
    word["nodes"][0]["description"] = "null"
    assert edit_plan.unrelated_changes(edit_plan.graph_view(graph()), edit_plan.graph_view(word), []) == ["/nodes/classify/description"]


def test_server_written_review_fields_are_allowed_by_name_only() -> None:
    before = graph()
    after = copy.deepcopy(before)
    options = after["nodes"][0]["options"]
    options["interpretation_requirements"] = [{"id": "q1", "kind": "llm_prompt_template", "status": "pending"}]
    options["approved_prompt_artifact_hash"] = "abc"
    after["sources"]["rows"]["options"]["source_authoring"] = {"hash": "h"}
    options["temperature"] = 0.9
    changes = edit_plan.classify_changes(edit_plan.graph_view(before), edit_plan.graph_view(after), [])
    assert changes.unrelated == ["/nodes/classify/options/temperature"]
    assert changes.server_written == [
        "/nodes/classify/options/approved_prompt_artifact_hash",
        "/nodes/classify/options/interpretation_requirements",
        "/sources/rows/options/source_authoring",
    ]


def test_server_written_review_fields_count_only_on_the_components_named() -> None:
    before = graph()
    after = copy.deepcopy(before)
    for node in after["nodes"]:
        node["options"]["interpretation_requirements"] = [{"id": "q1", "kind": "llm_prompt_template", "status": "pending"}]
        node["options"]["approved_prompt_artifact_hash"] = "abc"
    after["sources"]["rows"]["options"]["source_authoring"] = {"hash": "h"}
    changes = edit_plan.classify_changes(edit_plan.graph_view(before), edit_plan.graph_view(after), [], named={"summarise"})
    assert changes.server_written == [
        "/nodes/summarise/options/approved_prompt_artifact_hash",
        "/nodes/summarise/options/interpretation_requirements",
    ]
    assert changes.unrelated == [
        "/nodes/classify/options/approved_prompt_artifact_hash",
        "/nodes/classify/options/interpretation_requirements",
        "/sources/rows/options/source_authoring",
    ]
    nobody = edit_plan.classify_changes(edit_plan.graph_view(before), edit_plan.graph_view(after), [], named=set())
    assert nobody.server_written == []


def test_an_edit_names_the_components_in_its_pointers_chains_new_and_renamed_steps_and_edge_list() -> None:
    edit = {
        "expect": [
            {"equals": "/nodes/classify/options/max_tokens", "value": 1237},
            {"chain": {"from": "rows", "to": "summarise", "via": ["battery_a"]}},
            {"renamed_node": {"from": "battery_b", "to": "battery_d"}},
            {"new_output": "battery_out", "fields": {}, "option_keys": []},
            {"equals": "/metadata/name", "value": "x"},
        ],
        "may_change": ["/outputs/out/options"],
        "edges_touching": ["battery_e"],
    }
    assert edit_plan.named_components(edit) == {
        "classify",
        "rows",
        "summarise",
        "battery_a",
        "battery_b",
        "battery_d",
        "battery_out",
        "out",
        "battery_e",
    }


def control(node_id: str, input_: str, on_success: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "node_type": "transform",
        "plugin": "shield",
        "input": input_,
        "on_success": on_success,
        "on_error": "discard",
        "options": {"interpretation_requirements": [{"id": "d1", "user_term": edit_plan.AUTO_WIRED_CONTROL_TERM, "status": "pending"}]},
    }


def test_an_auto_wired_required_control_is_server_written_and_the_chain_steps_through_it() -> None:
    after = with_insert(graph())
    after["sources"]["rows"]["on_success"] = "cx"
    after["nodes"].append(control("prompt_shield_auto_1", "cx", "ca"))
    expectation = [new_step(), {"chain": {"from": "rows", "to": "classify", "via": ["battery_a"]}}]
    assert check(expectation, after) == []
    before_view, after_view = edit_plan.graph_view(graph()), edit_plan.graph_view(after)
    _, covered = edit_plan.check_expectations(resolved(expectation), before_view, after_view, before_view, read_text)
    changes = edit_plan.classify_changes(before_view, after_view, covered)
    assert changes.unrelated == []
    assert "/nodes/prompt_shield_auto_1" in changes.server_written
    impostor = copy.deepcopy(after)
    impostor["nodes"][-1]["options"]["interpretation_requirements"][0]["user_term"] = "something_else"
    assert check(expectation, impostor) == ["expect[1]:chain:rows>classify"]


def test_leftovers_of_an_earlier_attempt_are_found() -> None:
    assert edit_plan.leftover_findings(graph(), read_text) == []
    left = with_insert(graph())
    left["outputs"].append({"name": "battery_out", "plugin": "json", "options": {}, "on_write_failure": "discard"})
    left["metadata"]["name"] = "Battery check, second round"
    FILE_TEXT["blob-3"] = "Summarise the row.\nBattery file marker two."
    left["nodes"][1]["options"]["prompt_template"] = {"blob_ref": "blob-3", "mode": "inline_content", "sha256": "z"}
    assert edit_plan.leftover_findings(left, read_text) == [
        "/nodes/battery_a",
        "/outputs/battery_out",
        "/metadata/name",
        "/nodes/summarise/options/prompt_template",
    ]


def test_as_found_compares_prompts_by_text_and_ignores_server_written_fields() -> None:
    FILE_TEXT["blob-4"] = "Summarise the row.\n"
    rebound = graph()
    rebound["nodes"][1]["options"]["prompt_template"] = {"blob_ref": "blob-4", "mode": "inline_content", "sha256": "w"}
    rebound["nodes"][1]["options"]["approved_prompt_artifact_hash"] = "new"
    assert edit_plan.as_found_differences(graph(), rebound, read_text) == []
    FILE_TEXT["blob-5"] = "Summarise the row differently."
    changed = graph()
    changed["nodes"][1]["options"]["prompt_template"] = {"blob_ref": "blob-5", "mode": "inline_content", "sha256": "v"}
    assert edit_plan.as_found_differences(graph(), changed, read_text) == ["/nodes/summarise/options/prompt_template/text"]
