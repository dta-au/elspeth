from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from evals.lib.battery_capture import Instrument
from evals.lib.battery_report import (
    CompareRefused,
    LateBinding,
    build_report,
    ci_half_width_pp,
    collect_scores,
    render_markdown,
    write_report,
)
from evals.lib.battery_scenario import load_scenario

from tests.unit.evals.composer_battery import threadgen as tg

REPO = Path(__file__).resolve().parents[4]
SC = load_scenario(REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json")
CANARY = load_scenario(REPO / "evals/composer-battery/scenarios/canary/scenario.json")
SCENARIOS = {"fork_coalesce": SC, "canary": CANARY}


def _write_run(run_dir: Path, messages: list[dict], *, state: dict | None, meta: dict, is_valid: bool | None = True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages.json").write_text(json.dumps(messages))
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
    if is_valid is not None:
        (run_dir / "validate.json").write_text(
            json.dumps({"is_valid": is_valid, "checks": [], "errors": [], "warnings": [], "readiness": "ready"})
        )
    (run_dir / "reviews.json").write_text("[]")
    (run_dir / "meta.json").write_text(json.dumps(meta))


def _ideal(case: str, repeat: int, args: dict) -> tuple[list[dict], dict, dict]:
    return tg.ideal_thread(args), copy.deepcopy(args), tg.meta(case=case, repeat=repeat)


def _repair(args: dict) -> list[dict]:
    rows = tg.ideal_thread(args)
    rows[-1]["composition_state_id"] = None
    rows[-1]["content"] = json.dumps({"success": False, "errors": [{"code": "E1"}]})
    rows.append(tg.audit_row(20))
    rows.append(tg.assistant_row(21, [tg.call("sp2", "set_pipeline", args)]))
    rows.append(tg.tool_row(22, "sp2", "as21", state_id="state-3"))
    return rows


def _round(tmp_path: Path, name: str = "r1") -> Path:
    rd = tmp_path / "runs" / name
    fc = SC.canonical_arguments
    ca = CANARY.canonical_arguments
    for rep in (1, 2, 3):
        m, s, meta = _ideal("fork_coalesce", rep, fc)
        _write_run(rd / "fork_coalesce" / str(rep), m, state=s, meta=meta)
    # repeat 4: repair (hard); repeat 5: excluded (surface undetermined — no audit rows)
    _write_run(rd / "fork_coalesce" / "4", _repair(fc), state=copy.deepcopy(fc), meta=tg.meta(case="fork_coalesce", repeat=4))
    _write_run(
        rd / "fork_coalesce" / "5",
        [tg.user_row(1), tg.assistant_row(2, content="done")],
        state=copy.deepcopy(fc),
        meta=tg.meta(case="fork_coalesce", repeat=5),
    )
    for rep in range(1, 11):
        m, s, meta = _ideal("canary", rep, ca)
        _write_run(rd / "canary" / str(rep), m, state=s, meta=meta)
    (rd / "_tripwire").mkdir()
    (rd / "_tripwire" / "tripwire.json").write_text(
        json.dumps(
            [
                {
                    "fixture": "fork_coalesce",
                    "pass": True,
                    "staged_variant": "PIPELINE_STAGED_AUTO_COMMIT",
                    "planner_calls": 4,
                    "planner_codes": {},
                    "surface": "planner",
                    "reason": None,
                }
            ]
        )
    )
    (rd / "firing.json").write_text(
        json.dumps(
            {
                "round": name,
                "base": "https://elspeth.foundryside.dev",
                "started_at": "2026-08-17T00:00:00Z",
                "completed": [],
                "aborted": False,
                "abort_reason": None,
                "case_flags": {},
            }
        )
    )
    return rd


def test_collect_scores_writes_score_json_and_splits_canary(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    corpus, canary = collect_scores(rd, SCENARIOS)
    assert len(corpus) == 5 and len(canary) == 10
    assert (rd / "fork_coalesce/4/score.json").exists() and json.loads((rd / "fork_coalesce/4/score.json").read_text())["deviations"][0][
        "class"
    ] == "repair"


def test_pooled_uses_sum_over_sum_and_excludes_beside_n(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep["pooled"] == {
        "n": 4,
        "excluded": 1,
        "excluded_instrument": 0,
        "excluded_measurement": 1,
        "clean": 3,
        "optimal": 3,
        "hard": 1,
        "clean_ex_transport": 3,
        "unattributed_excess": 0,
        "below_floor": 0,
        "runs_with_retried_provider_error": 0,
        "clean_rate": 0.75,
        "optimal_rate": 0.75,
        "hard_rate": 0.25,
        "formula": "sum(successes)/sum(n)",
        "ci_half_width_pp": ci_half_width_pp(4),
    }
    assert rep["canary"] == {"n": 10, "non_optimal": 0, "flag": False}
    assert rep["exclusions"] == [] and rep["measurement_exclusions"] == [
        {"case": "fork_coalesce", "repeat": 5, "kind": "surface", "evidence": "surface_observed=undetermined"}
    ]
    assert len(rep["identity"]["binding"]["floors_sha256"]) == 64 and len(rep["identity"]["binding"]["taxonomy_sha256"]) == 64
    assert rep["tripwire"][0]["fixture"] == "fork_coalesce" and rep["degraded"] == {
        "flag": False,
        "reasons": [],
    }  # a measurement exclusion never degrades
    assert rep["findings"] and rep["findings"][0].startswith("measurement exclusions (surface/no_calls) in 20% of runs")


def test_by_case_by_repeat_and_ledger(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    case = next(c for c in rep["by_case"] if c["case"] == "fork_coalesce")
    assert (
        case["n"] == 4
        and case["excluded"] == 1
        and case["clean"] == 3
        and case["histogram"] == {"repair": 1}
        and case["per_case_ci_pp"] == ci_half_width_pp(4)
        and case["exclusion_streak"] is False
    )
    assert [r["repeat"] for r in rep["by_repeat"]] == [1, 2, 3, 4, 5]
    assert rep["by_repeat"][4] == {"repeat": 5, "n": 0, "excluded": 1, "clean": 0, "optimal": 0, "cached_prompt_tokens_median": None}
    assert rep["ledger"] == [
        {
            "case": "fork_coalesce",
            "class": "repair",
            "severity": "hard",
            "events": [
                {
                    "repeat": 4,
                    "sequence_no": [9, 22],
                    "tool": "set_pipeline",
                    "args_digest": rep["ledger"][0]["events"][0]["args_digest"],
                    "codes": ["E1"],
                    "audit_ordinal": 2,
                }
            ],
        }
    ]


def test_degraded_flags_canary_streak_and_exclusion_ratio(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    fc = SC.canonical_arguments
    # make canary 2/10 non-optimal (excess call) and fork_coalesce repeats 4 and 5 both INSTRUMENT-excluded → streak; instrument exclusions 2/5 = 40%
    for rep in (1, 2):
        rows = tg.ideal_thread(CANARY.canonical_arguments)
        rows.insert(2, tg.audit_row(20))
        _write_run(rd / "canary" / str(rep), rows, state=copy.deepcopy(CANARY.canonical_arguments), meta=tg.meta(case="canary", repeat=rep))
    broken = Instrument(http_unrecovered="GET /messages 502 at offset 0").to_dict()
    for rep in (4, 5):
        _write_run(
            rd / "fork_coalesce" / str(rep),
            tg.ideal_thread(fc),
            state=copy.deepcopy(fc),
            meta=tg.meta(case="fork_coalesce", repeat=rep, instrument=broken),
        )
    (rd / "firing.json").write_text(
        json.dumps(
            {
                "round": "r1",
                "base": "x",
                "started_at": "t",
                "completed": [],
                "aborted": True,
                "abort_reason": "3 consecutive instrument_error",
                "case_flags": {"fork_coalesce": ["instrument_error streak"]},
            }
        )
    )
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep["canary"] == {"n": 10, "non_optimal": 2, "flag": True}
    assert rep["degraded"]["flag"] is True
    assert set(rep["degraded"]["reasons"]) == {
        "canary: >1/10 non-optimal",
        "exclusions above 15%",
        "driver aborted: 3 consecutive instrument_error",
    }
    # canary skipped (--cases) ⇒ degraded, even though its flag is False
    for rep_dir in (rd / "canary").iterdir():
        for f in rep_dir.iterdir():
            f.unlink()
        rep_dir.rmdir()
    (rd / "canary").rmdir()
    rep2 = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep2["canary"] == {"n": 0, "non_optimal": 0, "flag": False} and "canary not fired at N=10" in rep2["degraded"]["reasons"]
    assert next(c for c in rep["by_case"] if c["case"] == "fork_coalesce")["exclusion_streak"] is True


def test_compare_refuses_on_binding_mismatch_and_prints_recorded_deltas(tmp_path: Path) -> None:
    prev = _round(tmp_path, "r0")
    write_report(prev, build_report(prev, scenarios=SCENARIOS, corpus_version=0))
    cur = _round(tmp_path, "r1")
    # recorded delta only (skill hash) → allowed and printed
    for meta_path in cur.rglob("meta.json"):
        doc = json.loads(meta_path.read_text())
        doc["identity"]["recorded"]["composer_skill_hash"] = "kit-v2"
        meta_path.write_text(json.dumps(doc))
    rep = build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)
    assert rep["compare"]["prev_round"] == "r0"
    assert rep["compare"]["recorded_deltas"]["composer_skill_hash"] == [None, "kit-v2"]
    assert rep["compare"]["pooled_delta"] == {"clean_pp": 0.0, "optimal_pp": 0.0, "hard_pp": 0.0}
    assert rep["compare"]["by_case_delta"][0]["indicative"] is True
    # a floor revision between rounds is a binding change: refused, not a kit delta
    moved = {**SCENARIOS, "fork_coalesce": copy.deepcopy(SC)}
    moved["fork_coalesce"].floor = copy.deepcopy(SC.floor)
    moved["fork_coalesce"].floor.tool_bearing_calls = 3
    with pytest.raises(CompareRefused, match="floors_sha256"):
        build_report(cur, scenarios=moved, corpus_version=0, compare_to=prev)
    md = render_markdown(rep)
    assert "composer_skill_hash" in md and "kit-v2" in md
    # binding delta → refused
    for meta_path in cur.rglob("meta.json"):
        doc = json.loads(meta_path.read_text())
        doc["identity"]["binding"]["composer_model"] = "openrouter/other/model"
        meta_path.write_text(json.dumps(doc))
    with pytest.raises(CompareRefused, match="composer_model"):
        build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)
    forced = build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev, force_compare=True)
    assert forced["compare"]["forced"] is True and forced["caveats"][0].startswith("FORCED COMPARE")
    # a null binding field on one side is NOT a match
    for meta_path in cur.rglob("meta.json"):
        doc = json.loads(meta_path.read_text())
        doc["identity"]["binding"]["composer_model"] = None
        meta_path.write_text(json.dumps(doc))
    with pytest.raises(CompareRefused, match="null"):
        build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)
    # corpus_version mismatch → refused (late-binding guard fires first when meta disagrees with the version being scored)
    with pytest.raises((CompareRefused, LateBinding), match="corpus_version"):
        build_report(_round(tmp_path, "r2"), scenarios=SCENARIOS, corpus_version=1, compare_to=prev)


def test_compare_delta_sign_is_current_minus_previous(tmp_path: Path) -> None:
    """pooled_delta/by_case_delta must be current minus previous. Two identical `_round` calls (as in
    test_compare_refuses_on_binding_mismatch_and_prints_recorded_deltas) tie at delta 0.0 either way a
    swapped ``pp(old, cur)`` would pass that test silently — so this pins a round pair with a real difference:
    prior round all-clean (5/5 ideal threads), current round with one hard deviation (the standard `_round`
    fixture, clean 3/4)."""
    prev = tmp_path / "runs" / "r0"
    fc = SC.canonical_arguments
    ca = CANARY.canonical_arguments
    for rep in range(1, 6):
        m, s, meta = _ideal("fork_coalesce", rep, fc)
        _write_run(prev / "fork_coalesce" / str(rep), m, state=s, meta=meta)
    for rep in range(1, 11):
        m, s, meta = _ideal("canary", rep, ca)
        _write_run(prev / "canary" / str(rep), m, state=s, meta=meta)
    (prev / "_tripwire").mkdir()
    (prev / "_tripwire" / "tripwire.json").write_text("[]")
    (prev / "firing.json").write_text(
        json.dumps(
            {"round": "r0", "base": "x", "started_at": "t", "completed": [], "aborted": False, "abort_reason": None, "case_flags": {}}
        )
    )
    write_report(prev, build_report(prev, scenarios=SCENARIOS, corpus_version=0))

    cur = _round(tmp_path, "r1")  # 3 ideal + 1 hard (repair) + 1 measurement-excluded ⇒ clean 3/4 = 0.75
    rep = build_report(cur, scenarios=SCENARIOS, corpus_version=0, compare_to=prev)

    # prev clean_rate = 5/5 = 1.0; cur clean_rate = 3/4 = 0.75 => delta = (0.75 - 1.0) * 100 = -25.0 -- NEGATIVE.
    # A swapped `pp(old, cur)` would report +25.0 here instead.
    assert rep["compare"]["pooled_delta"]["clean_pp"] == -25.0
    case_delta = next(d for d in rep["compare"]["by_case_delta"] if d["case"] == "fork_coalesce")
    assert case_delta["clean_pp"] == -25.0
    md = render_markdown(rep)
    assert "clean -25.0 pp" in md


def test_late_binding_guard_refuses_moved_corpus_or_prompt(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    with pytest.raises(LateBinding, match="corpus_version"):
        collect_scores(rd, SCENARIOS, corpus_version=1)
    with pytest.raises(LateBinding, match="prompt_sha256"):
        collect_scores(rd, SCENARIOS, corpus_version=0, prompt_hashes={"fork_coalesce": "not-the-hash"})
    corpus, _ = collect_scores(rd, SCENARIOS, corpus_version=0, prompt_hashes={"fork_coalesce": "x"})  # tg.meta stamps prompt_sha256 "x"
    assert len(corpus) == 5 and all(s.scenario_sha256 for s in corpus)


def test_markdown_carries_n_exclusions_and_formula_beside_every_rate(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    md = render_markdown(build_report(rd, scenarios=SCENARIOS, corpus_version=0))
    head = md.split("## Per-repeat")[0]
    assert "n=4" in head and "excluded=1" in head and "sum(successes)/sum(n)" in head
    assert "clean 75.0%" in head and "optimal 75.0%" in head and "hard 25.0%" in head
    assert "## Tripwire" in md and "PIPELINE_STAGED_AUTO_COMMIT" in md
    assert "## Deviation ledger" in md and "repair" in md and "E1" in md
    assert "compose-loop surface only" in md  # caveats header


def test_criteria_ledger_makes_a_non_clean_run_with_no_deviation_visible(tmp_path: Path) -> None:
    """A criterion failure (is_valid false, empty state, no schema before the first mutation, a build
    sentinel) fires no deviation event, so `clean` dropped with an empty histogram and nothing in the report
    a reader could act on without opening the raw transcript."""
    rd = _round(tmp_path)
    fc = SC.canonical_arguments
    _write_run(  # a completed thread the server declared invalid: red + green reasons, zero deviations
        rd / "fork_coalesce" / "3",
        tg.ideal_thread(fc),
        state=copy.deepcopy(fc),
        meta=tg.meta(case="fork_coalesce", repeat=3),
        is_valid=False,
    )
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    case = next(c for c in rep["by_case"] if c["case"] == "fork_coalesce")
    assert case["clean"] == 2 and case["histogram"] == {"repair": 1}  # the drop is invisible in the histogram
    row = next(c for c in rep["criteria"] if c["case"] == "fork_coalesce" and c["repeat"] == 3)
    assert row["red_reasons"] == ["final composition state has is_valid=false"]
    assert row["green_reasons"] == ["not is_valid", "topology: no valid state"]
    md = render_markdown(rep)
    assert "## Criteria ledger" in md and "fork_coalesce/3" in md and "is_valid=false" in md
    # and a round with no criterion failure says so rather than rendering an empty section
    assert "## Criteria ledger" in render_markdown(build_report(_round(tmp_path, "r9"), scenarios=SCENARIOS, corpus_version=0))


def test_a_tripwire_crash_recorded_by_the_driver_degrades_the_firing(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    doc = json.loads((rd / "firing.json").read_text())
    doc["tripwire_error"] = "RuntimeError('boom')"
    (rd / "firing.json").write_text(json.dumps(doc))
    rep = build_report(rd, scenarios=SCENARIOS, corpus_version=0)
    assert rep["degraded"]["flag"] is True and any("tripwire raised" in r for r in rep["degraded"]["reasons"])


def test_an_expected_topology_edit_trips_the_compare_refusal(tmp_path: Path) -> None:
    """M5: floors_sha256 bound only the floor and the option assertions, so an oracle edit moved `wrong_shape`
    silently between rounds — exactly what a floor edit is refused for."""
    prev = _round(tmp_path, "r0")
    write_report(prev, build_report(prev, scenarios=SCENARIOS, corpus_version=0))
    cur = _round(tmp_path, "r1")
    moved = {**SCENARIOS, "fork_coalesce": copy.deepcopy(SC)}
    moved["fork_coalesce"].expected_topology = copy.deepcopy(SC.expected_topology)
    moved["fork_coalesce"].expected_topology["nodes"][0]["plugin"] = "some_other_source"
    with pytest.raises(CompareRefused, match="floors_sha256"):
        build_report(cur, scenarios=moved, corpus_version=0, compare_to=prev)


def test_unknown_case_dir_is_loud(tmp_path: Path) -> None:
    rd = _round(tmp_path)
    (rd / "not_a_case" / "1").mkdir(parents=True)
    with pytest.raises(ValueError, match="not_a_case"):
        collect_scores(rd, SCENARIOS)


def test_ci_half_width() -> None:
    assert ci_half_width_pp(0) == 0 and ci_half_width_pp(5) == 44 and ci_half_width_pp(90) == 10
