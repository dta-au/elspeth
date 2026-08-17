from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from evals.lib import battery_planner as pp

from elspeth.web.composer.protocol import PIPELINE_STAGED_AUTO_COMMIT_MESSAGE, PIPELINE_STAGED_REVIEW_MESSAGE
from tests.unit.evals.composer_battery import threadgen as tg

FC = pp.load_fixture("fork_coalesce")
ARGS = FC["canonical_arguments"]


def _write(
    run_dir: Path,
    messages: list[dict],
    *,
    state: dict | None,
    proposals: dict | None = None,
    is_valid: bool | None = True,
    meta: dict | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages.json").write_text(json.dumps(messages))
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
    if is_valid is not None:
        (run_dir / "validate.json").write_text(
            json.dumps({"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready"})
        )
    if proposals is not None:
        (run_dir / "proposals.json").write_text(json.dumps(proposals))
    (run_dir / "reviews.json").write_text("[]")
    (run_dir / "meta.json").write_text(json.dumps(meta or tg.meta(case="fork_coalesce")))


def _planner_thread(
    *,
    accepted: bool = True,
    info: tuple[str, ...] = ("catalog.selection", "plugin.schema"),
    codes: tuple[str | None, ...] = (),
    staged: str = PIPELINE_STAGED_AUTO_COMMIT_MESSAGE,
) -> list[dict]:
    rows = [tg.user_row(1)]
    seq = 2
    for i, code in enumerate((*codes, None), start=1):
        rows.append(tg.audit_row(seq, planner_ordinal=i))
        seq += 1
        outcome = (
            "accepted"
            if (code is None and accepted and i == len(codes) + 1)
            else ("discovery_executed" if code is None else "candidate_rejected")
        )
        rows.append(
            tg.planner_attempt_row(
                seq,
                ordinal=i,
                phase="discovery" if i == 1 else "candidate",
                outcome=outcome,
                planner_code=code,
                led_to="done" if outcome == "accepted" else "continue",
                new_information=info if i == 1 else (),
            )
        )
        seq += 1
    rows.append(tg.assistant_row(seq, content=staged if accepted else "I could not build this."))
    return rows


def test_pair_routing_precondition_and_fingerprint() -> None:
    for name in pp.PROBE_FIXTURES:
        pp.assert_pair_routes(pp.load_fixture(name)["intent"])  # both arms dry-run to their surface offline
    with pytest.raises(pp.ProbeUnpaired):
        pp.assert_pair_routes("I have some products and want to score them")  # never routes to the planner
    assert len(pp.classifier_fingerprint()) == 64
    assert set(pp.TRIPWIRE_FIXTURES) <= set(pp.PROBE_FIXTURES) and len(pp.PROBE_FIXTURES) == 10


def test_required_information_floor() -> None:
    assert pp.required_information(ARGS) == frozenset({"plugin.schema"})
    llm = pp.load_fixture("structured_llm")["canonical_arguments"]
    assert pp.required_information(llm) == frozenset({"plugin.schema", "model.catalog"})


def test_planner_arm_clean_and_triage(tmp_path: Path) -> None:
    _write(tmp_path / "P", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.surface == "planner" and r.surface_ok and r.floor_missing == [] and r.accepted_terminal and r.planner_calls == 1
    assert r.staged_variant == "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE" and r.staged_topology_ok is True and r.clean and r.reason is None
    _write(
        tmp_path / "P2",
        _planner_thread(codes=("DISCOVERY_NO_GAIN", "REPAIR_BLIND_REPEAT")),
        state={"id": "s", "version": 2, **copy.deepcopy(ARGS)},
    )
    r2 = pp.score_arm(tmp_path / "P2", "fork_coalesce", "P")
    assert r2.planner_calls == 3 and r2.planner_codes == {"DISCOVERY_NO_GAIN": 1, "REPAIR_BLIND_REPEAT": 1}
    assert r2.triage == {"kit_misled_discovery": 1, "error_message_not_actionable": 1} and not r2.clean and r2.accepted_terminal


def test_planner_arm_missing_floor_and_wrong_surface(tmp_path: Path) -> None:
    _write(tmp_path / "P", _planner_thread(info=("catalog.selection",)), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.floor_missing == ["plugin.schema"] and not r.clean
    _write(tmp_path / "L", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    r2 = pp.score_arm(tmp_path / "L", "fork_coalesce", "L")  # arm L was expected on the loop; the planner answered
    assert r2.surface == "planner" and not r2.surface_ok and not r2.clean and r2.reason == "surface planner != expected compose_loop"


def _truncated_planner_thread() -> list[dict]:
    """A planner run killed mid-flight: discovery ran, then the last provider call was cancelled with the
    request. No attempt carries ``led_to="terminal"``, so the outcome is unknown, not negative."""
    return [
        tg.user_row(1),
        tg.audit_row(2, planner_ordinal=1),
        tg.planner_attempt_row(3, ordinal=1, outcome="discovery_executed", led_to="continue", new_information=("catalog.selection",)),
        tg.audit_row(4, status="cancelled", tools=False, planner_ordinal=2),
    ]


def _terminated_planner_thread() -> list[dict]:
    """The fork_coalesce shape: the planner reached its OWN terminal (prose → MALFORMED_RESPONSE) and the
    5xx arrived after. The outcome is durably captured, so the run stays a product finding."""
    return [
        tg.user_row(1),
        tg.audit_row(2, planner_ordinal=1),
        tg.planner_attempt_row(3, ordinal=1, outcome="discovery_executed", led_to="continue", new_information=("catalog.selection",)),
        tg.audit_row(4, status="malformed_response", tools=False, planner_ordinal=2),
        tg.planner_attempt_row(
            4 + 1, ordinal=2, phase="prose", outcome="prose_reply", planner_code="MALFORMED_RESPONSE", led_to="terminal"
        ),
    ]


def test_planner_arm_excludes_an_unrecovered_http_run_rather_than_scoring_its_topology(tmp_path: Path) -> None:
    """A 5xx on post_message kills the run mid-flight, so "no committed state and no pending proposal" is a
    truncated read, not a planner verdict. The instrument half of the exclusion ladder is surface-agnostic;
    the planner branch declines ``score_path`` (loop-only by design) and must not lose it with the rest."""
    meta = tg.meta(case="fork_coalesce", post_status=524, instrument={**tg.Instrument().to_dict(), "http_unrecovered": "post_message 524"})
    _write(tmp_path / "P", _truncated_planner_thread(), state=None, is_valid=None, meta=meta)
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.excluded == "http" and not r.clean
    assert r.reason == "excluded: http (post_message 524)", r.reason


def test_a_captured_planner_terminal_outranks_an_http_exclusion(tmp_path: Path) -> None:
    """Mirrors the ladder's existing "a server terminal reason outranks transport" rule. The planner said why
    it stopped before delivery failed, so excluding here would bury a real defect to fix a false one."""
    meta = tg.meta(case="fork_coalesce", post_status=502, instrument={**tg.Instrument().to_dict(), "http_unrecovered": "post_message 502"})
    _write(tmp_path / "P", _terminated_planner_thread(), state=None, is_valid=None, meta=meta)
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.excluded is None and not r.clean
    assert r.planner_codes == {"MALFORMED_RESPONSE": 1} and r.triage == {"model": 1}


def test_tripwire_reports_an_instrument_exclusion_instead_of_a_topology_failure(tmp_path: Path) -> None:
    """elspeth-c18073bd8f: all three calib4 arms died at the edge (524/524/502), and the tripwire reported
    "topology: no committed state and no pending proposal" — a dead substrate read as a planner defect."""
    rd = tmp_path / "runs" / "r1"
    meta = tg.meta(case="fork_coalesce", post_status=524, instrument={**tg.Instrument().to_dict(), "http_unrecovered": "post_message 524"})
    for fixture in pp.TRIPWIRE_FIXTURES:
        _write(rd / "_tripwire" / fixture / "1", _truncated_planner_thread(), state=None, is_valid=None, meta=meta)
    table = {t["fixture"]: t for t in pp.score_tripwire_dir(rd)}
    for fixture in pp.TRIPWIRE_FIXTURES:
        assert table[fixture]["pass"] is False
        assert table[fixture]["excluded"] == "http"
        assert table[fixture]["reason"] == "excluded: http (post_message 524)", table[fixture]["reason"]


def test_loop_arm_uses_the_scenario_free_path_score(tmp_path: Path) -> None:
    _write(tmp_path / "L", tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r = pp.score_arm(tmp_path / "L", "fork_coalesce", "L")
    assert r.surface == "compose_loop" and r.surface_ok and r.information_seen == ["plugin.schema"] and r.floor_missing == []
    assert r.accepted_terminal and r.tool_bearing_calls == 2 and r.deviations == [] and r.clean and r.planner_calls == 0
    _write(tmp_path / "L2", tg.ideal_thread(ARGS, schema_calls=0), state=copy.deepcopy(ARGS))
    r2 = pp.score_arm(tmp_path / "L2", "fork_coalesce", "L")
    assert r2.floor_missing == ["plugin.schema"] and not r2.clean


def test_staged_topology_from_proposal_when_state_absent(tmp_path: Path) -> None:
    proposals = {
        "proposals": [{"id": "p1", "status": "pending", "tool_name": "set_pipeline", "arguments_redacted_json": copy.deepcopy(ARGS)}],
        "events": [],
    }
    _write(tmp_path / "P", _planner_thread(staged=PIPELINE_STAGED_REVIEW_MESSAGE), state=None, proposals=proposals, is_valid=None)
    r = pp.score_arm(tmp_path / "P", "fork_coalesce", "P")
    assert r.staged_variant == "PIPELINE_STAGED_REVIEW_MESSAGE" and r.staged_topology_ok is True
    wrong = copy.deepcopy(ARGS)
    wrong["outputs"][0]["plugin"] = "jsonl"
    _write(
        tmp_path / "P2",
        _planner_thread(staged=PIPELINE_STAGED_REVIEW_MESSAGE),
        state=None,
        proposals={
            "proposals": [{"id": "p1", "status": "pending", "tool_name": "set_pipeline", "arguments_redacted_json": wrong}],
            "events": [],
        },
        is_valid=None,
    )
    assert pp.score_arm(tmp_path / "P2", "fork_coalesce", "P").staged_topology_ok is False
    _write(tmp_path / "P3", _planner_thread(accepted=False), state=None, proposals={"proposals": [], "events": []}, is_valid=None)
    r3 = pp.score_arm(tmp_path / "P3", "fork_coalesce", "P")
    assert r3.staged_variant is None and r3.staged_topology_ok is None and not r3.accepted_terminal


def test_tripwire_table_pass_fail_and_undetermined(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "r1"
    _write(rd / "_tripwire" / "fork_coalesce" / "1", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    er = pp.load_fixture("error_routing")["canonical_arguments"]
    wrong = copy.deepcopy(er)
    wrong["outputs"][0]["plugin"] = "jsonl"
    _write(rd / "_tripwire" / "error_routing" / "1", _planner_thread(), state={"id": "s", "version": 2, **wrong})
    _write(rd / "_tripwire" / "linear_transform" / "1", [tg.user_row(1), tg.assistant_row(2, content="hello")], state=None, is_valid=None)
    table = pp.score_tripwire_dir(rd)
    by = {t["fixture"]: t for t in table}
    assert (
        by["fork_coalesce"]["pass"] is True
        and by["fork_coalesce"]["staged_variant"] == "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE"
        and by["fork_coalesce"]["surface"] == "planner"
    )
    assert by["error_routing"]["pass"] is False and "topology" in by["error_routing"]["reason"]
    assert (
        by["linear_transform"]["pass"] is False
        and by["linear_transform"]["surface"] == "undetermined"
        and by["linear_transform"]["reason"] == "surface undetermined"
    )
    assert json.loads((rd / "_tripwire" / "tripwire.json").read_text()) == table


def test_an_unreadable_capture_is_a_failed_tripwire_not_a_traceback(tmp_path: Path) -> None:
    """score_arm calls load_capture, which raises CaptureError. Uncontained, one malformed tripwire capture
    killed the whole round from inside fire()."""
    rd = tmp_path / "runs" / "r1"
    _write(rd / "_tripwire" / "fork_coalesce" / "1", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    broken = rd / "_tripwire" / "error_routing" / "1"
    broken.mkdir(parents=True)
    (broken / "messages.json").write_text("[1, 2, 3]")  # elements are not objects
    (broken / "meta.json").write_text(json.dumps(tg.meta()))
    table = {t["fixture"]: t for t in pp.score_tripwire_dir(rd)}
    assert table["error_routing"]["pass"] is False and table["error_routing"]["reason"].startswith("capture: ")
    assert table["fork_coalesce"]["pass"] is True  # the readable fixture is still scored


def test_an_unreadable_probe_arm_never_costs_the_arms_that_fired(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "r1"
    name = pp.PROBE_FIXTURES[0]
    args = pp.load_fixture(name)["canonical_arguments"]
    _write(rd / "_probe" / name / "P", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(args)})
    arm_l = rd / "_probe" / name / "L"
    arm_l.mkdir(parents=True)
    (arm_l / "messages.json").write_text("[1, 2, 3]")
    (arm_l / "meta.json").write_text(json.dumps(tg.meta()))
    arms = {(a["fixture"], a["arm"]): a for a in pp.score_probe_dir(rd)["arms"]}
    assert arms[(name, "P")]["surface_ok"] is True
    assert arms[(name, "L")]["clean"] is False and arms[(name, "L")]["reason"].startswith("capture: ")


def test_probe_dir_scores_ten_by_two_and_binds_fingerprint(tmp_path: Path) -> None:
    rd = tmp_path / "runs" / "r1"
    for name in pp.PROBE_FIXTURES:
        args = pp.load_fixture(name)["canonical_arguments"]
        _write(rd / "_probe" / name / "P", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(args)})
        _write(rd / "_probe" / name / "L", tg.ideal_thread(args), state=copy.deepcopy(args))
    doc = pp.score_probe_dir(rd)
    assert doc["classifier_fingerprint"] == pp.classifier_fingerprint() and len(doc["arms"]) == 20
    assert all(a["surface_ok"] for a in doc["arms"])
    assert (rd / "_probe" / "probe.md").exists() and "| fixture | arm |" in (rd / "_probe" / "probe.md").read_text()


def test_vocabularies_are_live_enum_members() -> None:
    from elspeth.contracts.composer_planner_audit import ComposerPlannerCode, ComposerPlannerInformationClass

    info_values = {m.value for m in ComposerPlannerInformationClass}
    assert set(pp.LOOP_TOOL_TO_INFO.values()) <= info_values and pp.required_information(ARGS) <= info_values
    codes = {m.value for m in ComposerPlannerCode}
    assert set(pp.TRIAGE_BY_CODE) <= codes, set(pp.TRIAGE_BY_CODE) - codes
    assert pp.triage_code("DISCOVERY_NO_GAIN") == "kit_misled_discovery" and pp.triage_code("REPAIR_EXHAUSTED") == "budget"
    assert pp.triage_code("repeated_fingerprint") == "error_message_not_actionable" and pp.triage_code("DECLINED") == "other"
