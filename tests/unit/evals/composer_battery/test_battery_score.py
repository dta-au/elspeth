"""Scorer both-ways, near-miss, cross-class negatives, terminal boundaries, instrument honesty (spec §6)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from evals.lib.battery_capture import Capture, Instrument
from evals.lib.battery_scenario import Floor, load_scenario
from evals.lib.battery_score import (
    EXCLUSION_KINDS,
    INSTRUMENT_KINDS,
    MEASUREMENT_KINDS,
    PATH_CLASSES,
    SEVERITY,
    PathScore,
    Score,
    judge,
    path_from_disk,
    score_from_disk,
    score_path,
    score_run,
    write_score,
)

from elspeth.web.composer.tools.discovery import is_mutation_tool
from tests.unit.evals.composer_battery import threadgen as tg

REPO = Path(__file__).resolve().parents[4]
SC = load_scenario(REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json")
ARGS = SC.canonical_arguments

SPEC_MUTATION_VOCAB = {
    "set_pipeline",
    "set_source",
    "set_source_from_blob",
    "set_source_from_blobs",
    "set_output",
    "upsert_node",
    "upsert_edge",
    "patch_source_options",
    "patch_node_options",
    "patch_output_options",
    "remove_node",
    "remove_edge",
    "remove_output",
    "clear_source",
    "splice_transform",
    "apply_pipeline_recipe",
    "set_metadata",
}


def _classes(score: Score) -> list[str]:
    return [d.cls for d in score.deviations]


def _ideal() -> Capture:
    return tg.capture(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))


def test_spec_mutation_vocabulary_is_the_registry_mutation_set() -> None:
    assert all(is_mutation_tool(n) for n in SPEC_MUTATION_VOCAB), [n for n in SPEC_MUTATION_VOCAB if not is_mutation_tool(n)]


def test_ideal_thread_at_floor_is_clean_and_optimal() -> None:
    s = score_run(_ideal(), SC)
    assert s.surface_observed == "compose_loop"
    assert (s.tool_bearing_calls, s.advisor_calls, s.other_text_calls, s.audited_provider_calls) == (2, 0, 0, 2)
    assert s.floor == 2 and s.excess == 0
    assert s.deviations == [] and s.green and not s.red and s.is_valid and not s.wrong_shape
    assert s.clean and s.optimal and s.excluded is None
    assert s.tokens == {"prompt": 200, "completion": 20, "cached_prompt": 0, "unknown_calls": 0}
    assert s.cost == pytest.approx(0.02) and s.wall_ms == 6000  # audit rows :02→:03 and :07→:08
    assert s.review_rounds == 0 and s.recipe_used is False


def test_advisor_bucket_by_model_not_by_null_tools_hash() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(30, tools=False, model=tg.ADVISOR))
    rows.append(tg.audit_row(31, tools=False, model=tg.COMPOSER))  # a text-only composer call is NOT advisor
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert (s.tool_bearing_calls, s.advisor_calls, s.other_text_calls, s.audited_provider_calls) == (2, 1, 1, 4)
    assert s.optimal  # text-only calls do not count against the floor


def test_repair_fires_on_durable_not_applied_then_retry_and_not_backtrack() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["outcome"] = "applied"  # lying stamp on the first attempt
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": False, "error": "validation failed", "errors": [{"code": "E_MISSING_SINK"}]})
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS, stamp="applied")]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["repair"]
    d = s.deviations[0]
    assert d.tool == "set_pipeline" and d.sequence_no == (9, 22) and "E_MISSING_SINK" in d.codes and d.audit_ordinal == 2
    assert s.tool_bearing_calls == 3 and s.excess == 1 and not s.clean and s.green


def test_near_miss_durable_applied_with_lying_rejected_stamp_does_not_repair() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["outcome"] = "rejected"  # stamp lies; tool row has composition_state_id
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.deviations == [] and s.clean


def test_cancelled_outcome_is_not_applied_so_a_retry_is_a_repair() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-1]["composition_state_id"] = None
    rows[-1]["tool_calls"] = [{"_kind": "audit", "status": "cancelled", "version_before": 1, "version_after": 1}]
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    assert _classes(score_run(tg.capture(rows, state=ARGS), SC)) == ["repair"]


def test_backtrack_fires_on_wholesale_reset_and_not_repair() -> None:
    rows = tg.ideal_thread(ARGS)  # first set_pipeline applied
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", ARGS)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["backtrack"] and s.deviations[0].sequence_no == (9, 22)


def test_backtrack_fires_on_remove_after_apply() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("rm", "remove_node", {"node_id": "fork"})]))
    rows.append(tg.tool_row(22, "rm", "as21", state_id="state-3"))
    assert _classes(score_run(tg.capture(rows, state=ARGS), SC)) == ["backtrack"]


def test_excess_discovery_both_ways() -> None:
    p0 = {"plugin_type": "transform", "plugin_name": "p0"}
    rows = [
        tg.user_row(1),
        tg.audit_row(2),
        tg.assistant_row(3, [tg.call("d0", "get_plugin_schema", p0)]),
        tg.tool_row(4, "d0", "as3"),
        tg.audit_row(5),
        tg.assistant_row(6, [tg.call("d0b", "get_plugin_schema", p0)]),
        tg.tool_row(7, "d0b", "as6"),  # second read, no mutation between
        tg.audit_row(8),
        tg.assistant_row(9, [tg.call("sp", "set_pipeline", ARGS)]),
        tg.tool_row(10, "sp", "as9", state_id="state-2"),
    ]
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["excess_discovery"] and s.deviations[0].tool == "get_plugin_schema" and s.deviations[0].sequence_no == (6, 7)
    # a re-read AFTER an applied mutation is not excess_discovery (state may have changed) — but the extra
    # tool-bearing call is still above floor, so it surfaces honestly as unattributed_excess
    rows2 = tg.ideal_thread(ARGS)
    rows2.append(tg.audit_row(30))
    rows2.append(tg.assistant_row(31, [tg.call("st", "get_pipeline_state", {})]))
    rows2.append(tg.tool_row(32, "st", "as31"))
    assert _classes(score_run(tg.capture(rows2, state=ARGS), SC)) == ["unattributed_excess"]


def test_schema_fumble_on_repeated_patch_of_same_target() -> None:
    rows = tg.ideal_thread(ARGS)
    for i, seq in enumerate((20, 23)):
        rows.append(tg.audit_row(seq))
        rows.append(tg.assistant_row(seq + 1, [tg.call(f"pn{i}", "patch_node_options", {"node_id": "fork", "options": {"x": i}})]))
        rows.append(tg.tool_row(seq + 2, f"pn{i}", f"as{seq + 1}", state_id=f"state-{3 + i}"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["schema_fumble"] and s.deviations[0].sequence_no == (24, 25)


def test_data_setup_detour_and_rework() -> None:
    rows = [
        tg.user_row(1),
        tg.audit_row(2),
        tg.assistant_row(3, [tg.call("cb", "create_blob", {"filename": "rows.csv", "content": "a,b\n1,2"})]),
        tg.tool_row(4, "cb", "as3", content={"success": True, "blob_id": "b1"}),
    ]
    rows += [
        tg.audit_row(5),
        tg.assistant_row(6, [tg.call("ub", "update_blob", {"blob_id": "b1", "content": "a,b\n1,3"})]),
        tg.tool_row(7, "ub", "as6", content={"success": True}),
    ]
    rows += [tg.audit_row(8), tg.assistant_row(9, [tg.call("sp", "set_pipeline", ARGS)]), tg.tool_row(10, "sp", "as9", state_id="state-2")]
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["data_setup_detour", "data_rework"]
    assert [SEVERITY[c] for c in _classes(s)] == ["soft", "soft"]


def test_retried_provider_error_counts_but_unrecovered_transport_excludes() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(
        1, tg.audit_row(0, status="api_error", prompt_tokens=None, completion_tokens=None, cost=None, error_class="APIConnectionError")
    )
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["retried_provider_error"] and s.retried_calls == 1 and s.excluded is None
    assert s.tokens["unknown_calls"] == 1 and s.audited_provider_calls == 3
    dead = [tg.user_row(1), tg.audit_row(2, status="timeout", prompt_tokens=None, completion_tokens=None, cost=None)]
    s2 = score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=tg.meta(post_status=500)), SC)
    assert s2.excluded == "transport" and not s2.clean


def test_malformed_output_is_a_hard_deviation_not_an_exclusion() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(2, tg.audit_row(20, status="malformed_response"))
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert _classes(s) == ["malformed_output"] and SEVERITY["malformed_output"] == "hard" and s.excluded is None


def test_surface_honesty_zero_audit_rows_and_planner_rows() -> None:
    empty = tg.capture([tg.user_row(1), tg.assistant_row(2, content="I built it.")], state=ARGS)
    s = score_run(empty, SC)
    assert s.surface_observed == "undetermined" and s.excluded == "surface"
    rows = tg.ideal_thread(ARGS)
    rows.append(tg.audit_row(30, planner_ordinal=1))
    assert score_run(tg.capture(rows, state=ARGS), SC).excluded == "surface"
    rows2 = tg.ideal_thread(ARGS)
    rows2.append(tg.planner_attempt_row(30))
    s3 = score_run(tg.capture(rows2, state=ARGS), SC)
    assert s3.surface_observed == "planner" and s3.excluded == "surface"


def test_truncated_and_read_integrity_and_auth_and_http_come_from_meta() -> None:
    base = tg.ideal_thread(ARGS)

    def excl(**kw):
        return score_run(tg.capture(base, state=ARGS, meta_doc=tg.meta(instrument=Instrument(**kw).to_dict())), SC)

    assert excl(truncated=True).excluded == "truncated"
    assert excl(read_integrity="AuditIntegrityError: seq gap").excluded == "read_integrity"
    assert excl(auth_failed=True).excluded == "auth"
    assert excl(http_unrecovered="GET /messages 502").excluded == "http"
    s = excl(review_rounds_exhausted=True)
    assert s.excluded == "http" and "review rounds" in (s.exclusion_evidence or "")
    # a drifted meta contract (renamed key) is a CAPTURE exclusion, never a clean run
    bad = tg.meta()
    bad["instrument"] = {**Instrument().to_dict(), "http_error": None}
    del bad["instrument"]["http_unrecovered"]
    with pytest.raises(Exception, match="instrument"):
        score_path(tg.capture(base, state=ARGS, meta_doc=bad))


def test_terminal_boundaries_read_the_captured_body_not_turn_counts() -> None:
    dead = [
        tg.user_row(1),
        tg.audit_row(2),
        tg.assistant_row(3, [tg.call("d0", "get_plugin_schema", {"plugin_type": "source", "plugin_name": "csv"})]),
        tg.tool_row(4, "d0", "as3"),
    ]
    comp = tg.meta(
        post_status=422,
        detail={"turns_used": 40, "budget_exhausted": "composition", "reason": "convergence_composition_budget"},
        terminal={"budget_exhausted": "composition", "reason": "convergence_composition_budget", "source": "422_detail"},
    )
    s = score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=comp), SC)
    assert _classes(s) == ["turn_exhaustion"] and s.excluded is None and not s.green
    disc = tg.meta(
        post_status=422,
        detail={"turns_used": 39, "budget_exhausted": "discovery", "reason": "convergence_discovery_budget"},
        terminal={"budget_exhausted": "discovery", "reason": "convergence_discovery_budget", "source": "422_detail"},
    )
    assert _classes(score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=disc), SC)) == ["turn_exhaustion"]
    wall = tg.meta(
        post_status=None,
        terminal={"budget_exhausted": "timeout", "reason": "convergence_wall_clock_timeout", "source": "composer_progress"},
    )
    assert _classes(score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=wall), SC)) == ["wall_timeout"]
    # a provider-call TIMEOUT status is NOT wall_timeout; with a recovered retry it is retried_provider_error
    prov = list(dead)
    prov.insert(1, tg.audit_row(0, status="timeout", prompt_tokens=None, completion_tokens=None, cost=None))
    s4 = score_run(tg.capture(prov, state=None, is_valid=None, meta_doc=comp), SC)
    assert "wall_timeout" not in _classes(s4) and "retried_provider_error" in _classes(s4)
    # non-200 with no terminal body ⇒ terminal_missing (excluded)
    missing = tg.meta(post_status=500)
    assert score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=missing), SC).excluded == "terminal_missing"
    # a 200 with a valid state is never terminal_missing even though the terminal source is none
    assert score_run(_ideal(), SC).excluded is None


def test_decline_and_passivity_are_hard_and_not_terminal_missing() -> None:
    passive = [tg.user_row(1), tg.audit_row(2, tools=True), tg.assistant_row(3, content="I can build that. Would you like me to proceed?")]
    s = score_run(tg.capture(passive, state=None, is_valid=None), SC)
    assert _classes(s) == ["passivity"] and s.red and s.excluded is None
    prose = [tg.user_row(1), tg.audit_row(2, tools=True), tg.assistant_row(3, content="Here is how you would do it: use csv then json.")]
    s2 = score_run(tg.capture(prose, state=None, is_valid=None), SC)
    assert _classes(s2) == ["decline"] and s2.red


def test_wrong_shape_and_option_assertions() -> None:
    wrong = copy.deepcopy(ARGS)
    wrong["outputs"][0]["plugin"] = "jsonl"
    s = score_run(tg.capture(tg.ideal_thread(wrong), state=wrong), SC)
    assert s.wrong_shape and _classes(s) == ["wrong_shape"] and not s.green and not s.clean and s.is_valid
    # invalid final state: not wrong_shape (shape is only judged on is_valid), red
    s2 = score_run(tg.capture(tg.ideal_thread(ARGS), state=ARGS, is_valid=False), SC)
    assert not s2.wrong_shape and s2.red and not s2.green


def test_unattributed_excess_is_visible() -> None:
    rows = tg.ideal_thread(ARGS)
    rows.insert(2, tg.audit_row(20))  # an extra tool-bearing call that produced no tool call at all
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.excess == 1 and _classes(s) == ["unattributed_excess"] and not s.clean


def test_approval_pending_and_recipe_flag() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-2]["tool_calls"][0]["function"]["name"] = "apply_pipeline_recipe"
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": True, "status": "APPROVAL_REQUIRED", "proposal_id": "p1"})
    s = score_run(tg.capture(rows, state=ARGS), SC)
    assert s.recipe_used and "approval_pending" in _classes(s)


def test_cancelled_last_row_on_a_wall_timeout_is_hard_not_excluded() -> None:
    dead = [
        tg.user_row(1),
        tg.audit_row(2),
        tg.assistant_row(3, [tg.call("d0", "get_plugin_schema", {"plugin_type": "source", "plugin_name": "csv"})]),
        tg.tool_row(4, "d0", "as3"),
    ]
    dead.append(
        tg.audit_row(5, status="cancelled", prompt_tokens=None, completion_tokens=None, cost=None)
    )  # coordinator cancelled the in-flight call at shutdown
    wall = tg.meta(
        post_status=None,
        terminal={"budget_exhausted": "timeout", "reason": "convergence_wall_clock_timeout", "source": "composer_progress"},
    )
    s = score_run(tg.capture(dead, state=None, is_valid=None, meta_doc=wall), SC)
    assert s.excluded is None and _classes(s) == ["wall_timeout"]
    # an api_error as the last row with a captured turn budget: still hard turn_exhaustion, not transport
    dead2 = [*dead[:-1], tg.audit_row(5, status="api_error", prompt_tokens=None, completion_tokens=None, cost=None)]
    comp = tg.meta(
        post_status=422,
        detail={"turns_used": 40, "budget_exhausted": "composition", "reason": "convergence_composition_budget"},
        terminal={"budget_exhausted": "composition", "reason": "convergence_composition_budget", "source": "422_detail"},
    )
    s2 = score_run(tg.capture(dead2, state=None, is_valid=None, meta_doc=comp), SC)
    assert s2.excluded is None and _classes(s2) == ["turn_exhaustion"]


def test_abandoned_mutation_keeps_its_codes_and_is_not_a_decline() -> None:
    rows = tg.ideal_thread(ARGS)
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": False, "errors": [{"code": "E_BAD_SINK"}]})
    rows.append(tg.assistant_row(30, content="I could not complete the pipeline."))
    s = score_run(tg.capture(rows, state=None, is_valid=None), SC)
    assert _classes(s) == ["abandoned_mutation"] and s.deviations[0].codes == ("E_BAD_SINK",) and SEVERITY["abandoned_mutation"] == "hard"
    assert "decline" not in _classes(s) and s.red


def test_below_floor_is_flagged_not_hidden() -> None:
    rows = [
        tg.user_row(1),
        tg.audit_row(2),
        tg.assistant_row(
            3, [tg.call("d0", "get_plugin_schema", {"plugin_type": "source", "plugin_name": "csv"}), tg.call("sp", "set_pipeline", ARGS)]
        ),
        tg.tool_row(4, "d0", "as3"),
        tg.tool_row(5, "sp", "as3", state_id="state-2"),
    ]
    s = score_run(tg.capture(rows, state=ARGS), SC)  # discovery and mutation in ONE tool-bearing call: 1 < floor 2
    assert s.tool_bearing_calls == 1 and s.below_floor and s.clean and not s.optimal and s.excess == 0
    assert SEVERITY["unattributed_excess"] == "unattributed" and s.scenario_sha256 and len(s.scenario_sha256) == 64


def test_no_calls_when_audit_rows_exist_but_none_are_tool_bearing() -> None:
    rows = [tg.user_row(1), tg.audit_row(2, tools=False, model=tg.ADVISOR), tg.assistant_row(3, content="ok")]
    assert score_run(tg.capture(rows, state=None, is_valid=None), SC).excluded == "no_calls"


def test_score_path_is_scenario_free_and_judge_binds_the_floor() -> None:
    path = score_path(_ideal())
    assert isinstance(path, PathScore) and path.excluded is None and path.deviations == [] and path.tool_bearing_calls == 2
    assert path.schema_read_before_first_mutation is True and path.applied_any and not path.state_empty
    assert {d.cls for d in path.deviations} <= PATH_CLASSES
    s = judge(SC, path)
    assert s.optimal
    stricter = copy.deepcopy(SC)
    stricter.floor = Floor(
        tool_bearing_calls=1,
        components={"discovery": 0, "dependent_listing": 0, "mutation": 1},
        repairs=0,
        backtracks=0,
        derivation=["test"],
        pre_calibration=1,
        post_calibration=None,
    )
    s2 = judge(stricter, path)  # same path, tighter floor: excess appears, and only in judge
    assert s2.excess == 1 and [d.cls for d in s2.deviations] == ["unattributed_excess"] and not s2.clean
    assert (
        set(EXCLUSION_KINDS) == set(INSTRUMENT_KINDS) | set(MEASUREMENT_KINDS)
        and "surface" in MEASUREMENT_KINDS
        and "no_calls" in MEASUREMENT_KINDS
    )
    assert not score_path(
        tg.capture([tg.user_row(1), tg.assistant_row(2, content="hi")], state=None, is_valid=None)
    ).excluded_by_instrument  # surface: a finding, not a fault


def test_score_json_round_trip_and_capture_error(tmp_path: Path) -> None:
    s = score_run(_ideal(), SC)
    p = write_score(tmp_path, s)
    doc = json.loads(p.read_text())
    assert (
        doc["clean"] is True
        and doc["deviations"] == []
        and doc["excluded"] is None
        and set(doc["tokens"]) == {"prompt", "completion", "cached_prompt", "unknown_calls"}
    )
    assert set(EXCLUSION_KINDS) == {
        "no_calls",
        "auth",
        "http",
        "read_integrity",
        "truncated",
        "surface",
        "terminal_missing",
        "transport",
        "capture",
    }
    (tmp_path / "meta.json").write_text("{}")  # no messages.json
    s2 = score_from_disk(tmp_path, SC)
    assert s2.excluded == "capture" and s2.exclusion_evidence
    assert path_from_disk(tmp_path).excluded == "capture" and path_from_disk(tmp_path).excluded_by_instrument
