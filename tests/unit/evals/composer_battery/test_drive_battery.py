from __future__ import annotations

import copy
import json
from pathlib import Path

import drive_battery as db
import pytest
from evals.lib import battery_planner as db_planner
from evals.lib.battery_corpus import CorpusCase
from evals.lib.battery_scenario import load_scenario
from evals.lib.battery_score import path_from_disk

from tests.unit.evals.composer_battery import threadgen as tg
from tests.unit.evals.composer_battery.fake_http import FakeClient, happy_responders, ok

REPO = Path(__file__).resolve().parents[4]
ARGS = load_scenario(
    REPO / "evals/composer-battery/scenarios/fork_coalesce/scenario.json"
).canonical_arguments  # a real payload; the driver itself never loads scenarios
ENV = {"advisor_model": tg.ADVISOR, "composition_turns": 30, "discovery_turns": 10}


def _battery(tmp_path: Path, client: FakeClient, **kw) -> db.Battery:
    b = db.Battery(
        client,
        base="https://elspeth.foundryside.dev",
        round_name="r1",
        runs_dir=tmp_path / "runs",
        corpus_version=0,
        env_budgets=ENV,
        sleep=lambda s: None,
        **kw,
    )
    b.login("battery_local", "pw")
    return b


def test_login_hard_fails_without_access_token_and_caches_nothing(tmp_path: Path) -> None:
    client = FakeClient({"POST /api/auth/login": lambda c: ok({"detail": "bad credentials"}, 401)})
    b = db.Battery(client, base="x", round_name="r1", runs_dir=tmp_path, corpus_version=0, env_budgets=ENV, sleep=lambda s: None)
    with pytest.raises(db.BatteryAuthError):
        b.login("battery_local", "pw")
    assert client.token is None and not list(tmp_path.rglob("*.json"))


def test_patch_title_precedes_post_message_and_label_format(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client)
    verdict = b.run_prompt(
        label="battery/r1/fork_coalesce/1", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1
    )
    steps = client.steps()
    assert steps.index("PATCH /api/sessions/s1") < steps.index("POST /api/sessions/s1/messages")
    assert client.calls[steps.index("PATCH /api/sessions/s1")].json == {"title": "battery/r1/fork_coalesce/1"}
    assert client.calls[steps.index("POST /api/sessions/s1/messages")].timeout == 620.0
    assert verdict is None
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert [h["step"] for h in meta["http"]][:5] == [
        "create_session",
        "patch_title",
        "patch_preferences",
        "get_preferences",
        "post_message",
    ]
    assert meta["preferences"] == {"trust_mode": "auto_commit", "density_default": "high"}
    assert meta["identity"]["binding"]["tools_spec_hash"] == tg.TOOLS_HASH and meta["identity"]["binding"]["advisor_model"] == tg.ADVISOR
    assert (
        meta["identity"]["binding"]["composer_timeout_seconds"] == 600.0
        and meta["identity"]["recorded"]["frontend_build"] == "index-abc.js"
    )
    assert meta["identity"]["recorded"]["first_call_messages_hash"] == "mh2"
    assert meta["state_id"] == "state-2" and meta["server_terminal"]["source"] == "none"
    validate_call = client.calls[steps.index("POST /api/sessions/s1/validate")]
    assert validate_call.params == {"state_id": "state-2"}


def test_422_detail_is_captured_as_the_terminal_reason(tmp_path: Path) -> None:
    detail = {
        "error_type": "convergence",
        "detail": "x",
        "turns_used": 40,
        "budget_exhausted": "composition",
        "reason": "convergence_composition_budget",
        "recovery_text": "y",
    }
    r = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)
    r["POST /api/sessions/"] = lambda c: (
        ok({"detail": detail}, 422)
        if c.path.endswith("/messages")
        else ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})
    )
    client = FakeClient(r)
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1)
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    post = next(h for h in meta["http"] if h["step"] == "post_message")
    assert post["status"] == 422 and post["detail"]["turns_used"] == 40
    assert meta["server_terminal"] == {
        "budget_exhausted": "composition",
        "reason": "convergence_composition_budget",
        "source": "422_detail",
    }
    # non-200 ⇒ settle: at least two audit-count reads before the capture read
    assert client.steps().count("GET /api/sessions/s1/messages") >= 3


def test_client_timeout_reads_composer_progress_once(tmp_path: Path) -> None:
    r = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)

    def timeout_then(c):
        if c.path.endswith("/messages"):
            raise db.HttpTimeout("620s")
        return ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})

    r["POST /api/sessions/"] = timeout_then
    client = FakeClient(r)
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/fork_coalesce/1", case="fork_coalesce", repeat=1)
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert client.steps().count("GET /api/sessions/s1/composer-progress") == 1
    assert meta["server_terminal"] == {
        "budget_exhausted": "timeout",
        "reason": "convergence_wall_clock_timeout",
        "source": "composer_progress",
    }
    post = next(h for h in meta["http"] if h["step"] == "post_message")
    assert post["status"] is None


def test_pagination_full_page_triggers_next_fetch_and_truncation_is_flagged(tmp_path: Path) -> None:
    rows = [tg.user_row(i) for i in range(1, 1004)]  # 1003 rows → pages 500/500/3
    client = FakeClient(happy_responders(rows, state=None))
    b = _battery(tmp_path, client)
    b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    offsets = [
        c.params["offset"]
        for c in client.calls
        if c.path.endswith("/messages") and c.method == "GET" and c.params and c.params.get("include_tool_rows") == "true"
    ]
    assert offsets == [0, 500, 1000]
    assert len(json.loads((tmp_path / "runs/r1/x/1/messages.json").read_text())) == 1003
    # exactly 500 rows: a full page ALWAYS triggers a follow-up read (which returns 0 rows)
    client2 = FakeClient(happy_responders(rows[:500], state=None))
    _battery(tmp_path, client2).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/2", case="x", repeat=2)
    offsets2 = [
        c.params["offset"]
        for c in client2.calls
        if c.method == "GET" and c.path.endswith("/messages") and c.params and c.params.get("include_tool_rows") == "true"
    ]
    assert offsets2 == [0, 500]
    assert json.loads((tmp_path / "runs/r1/x/2/meta.json").read_text())["instrument"]["truncated"] is False
    # a page error after a full page ⇒ http_unrecovered AND truncated
    r3 = happy_responders(rows, state=None)
    calls_seen = {"n": 0}

    def flaky(c):
        if c.params and c.params.get("include_tool_rows") == "true":
            calls_seen["n"] += 1
            if calls_seen["n"] == 2:
                return ok({"detail": "boom"}, 502)
        off = int(c.params.get("offset", 0)) if c.params else 0
        return ok(rows[off : off + 500])

    r3["GET /api/sessions/s1/messages"] = flaky
    client3 = FakeClient(r3)
    _battery(tmp_path, client3).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/3", case="x", repeat=3)
    inst = json.loads((tmp_path / "runs/r1/x/3/meta.json").read_text())["instrument"]
    assert inst["truncated"] is True and inst["http_unrecovered"] == "GET /messages 502 at offset 500"


def test_capture_step_server_failures_are_instrument_exclusions_not_product_findings(tmp_path: Path) -> None:
    """C2: a 5xx on validate / state / messages is a SERVER fault. Without an instrument flag the missing
    artifact reads as `not is_valid` / an empty final state — a product finding for an outage — and never
    feeds the abort rule."""
    # (a) validate 5xx: state.json is written, validate.json is not
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["POST /api/sessions/"] = lambda c: (
        ok({"detail": "boom"}, 503) if c.path.endswith("/validate") else ok({"message": {}, "state": None, "proposals": []})
    )
    b = _battery(tmp_path, FakeClient(r), repeats=1)
    verdict = b.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    inst = json.loads((tmp_path / "runs/r1/x/1/meta.json").read_text())["instrument"]
    assert inst["http_unrecovered"] == "validate 503" and verdict in db.INSTRUMENT_KINDS
    assert not (tmp_path / "runs/r1/x/1/validate.json").exists()

    # (b) state 5xx: the legitimate "no state yet" answer is 200 + a null body, so only a non-200 is a fault
    r2 = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r2["GET /api/sessions/s1/state"] = lambda c: ok({"detail": "boom"}, 502)
    b2 = _battery(tmp_path, FakeClient(r2), repeats=1)
    v2 = b2.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/2", case="x", repeat=2)
    inst2 = json.loads((tmp_path / "runs/r1/x/2/meta.json").read_text())["instrument"]
    assert inst2["http_unrecovered"] == "get_state 502" and v2 in db.INSTRUMENT_KINDS
    r3 = happy_responders(tg.ideal_thread(ARGS), state=None)  # 200 + null body
    b3 = _battery(tmp_path, FakeClient(r3), repeats=1)
    b3.run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/3", case="x", repeat=3)
    assert json.loads((tmp_path / "runs/r1/x/3/meta.json").read_text())["instrument"]["http_unrecovered"] is None


def test_a_state_read_that_times_out_is_an_instrument_fault_too(tmp_path: Path) -> None:
    """`step` flags a transport error but NOT a client timeout, so `sr is None` bypassed a `sr is not None`
    guard entirely — and a timed-out state read also skips validate (state_id stays None), so the
    validate-side flag cannot rescue it. The run would score `state_empty`/`not is_valid`: a product finding
    for a server outage, which is exactly what C2 exists to prevent."""

    def times_out(c):
        raise db.HttpTimeout("30s")

    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions/s1/state"] = times_out
    client = FakeClient(r)
    run_dir = tmp_path / "runs/r1/x/1"
    verdict = _battery(tmp_path, client, repeats=1).run_prompt(label="l", prompt="p", run_dir=run_dir, case="x", repeat=1)
    meta = json.loads((run_dir / "meta.json").read_text())
    assert meta["instrument"]["http_unrecovered"].startswith("get_state timeout")
    assert not (run_dir / "state.json").exists() and not (run_dir / "validate.json").exists()
    assert "POST /api/sessions/s1/validate" not in client.steps()  # the validate seam never ran at all
    assert verdict in db.INSTRUMENT_KINDS and path_from_disk(run_dir).excluded_by_instrument


def test_a_5xx_compose_before_any_provider_call_aborts_the_round(tmp_path: Path) -> None:
    """C2 third shape: a crash-looping substrate answers POST /messages 5xx and writes zero audit rows. The
    composer's structured terminal is a 422, so a 5xx is a server fault — it must exclude as an instrument
    kind and trip the three-consecutive abort rather than burn the round as 95 product findings."""
    r = happy_responders([tg.user_row(1)], state=None)
    r["POST /api/sessions/"] = lambda c: (
        ok({"detail": "service_setup_failed"}, 500) if c.path.endswith("/messages") else ok({"is_valid": False, "errors": []})
    )
    b = _battery(tmp_path, FakeClient(r), repeats=5)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only=set(cases))
    assert all(c["excluded"] in db.INSTRUMENT_KINDS for c in doc["completed"])
    assert doc["aborted"] is True and doc["abort_reason"] == "3 consecutive instrument_error"
    inst = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())["instrument"]
    assert inst["http_unrecovered"] == "post_message 500"


def test_a_5xx_carrying_a_planner_terminal_is_a_product_outcome_not_an_instrument_fault(tmp_path: Path) -> None:
    """The blanket "any 5xx is a server fault" rule is false: ELSPETH answers a planner terminal with a 5xx.

    ``MALFORMED_RESPONSE`` — the planner reached its own verdict and said why — returns 502 carrying a
    structured ``composer_planner_failure`` envelope. Excluding that as an instrument fault discards a real
    product observation, which is how three edge-truncated runs and one COMPLETED planner terminal were filed
    together as a planner regression (elspeth-ad5628ecda). The discriminator is the envelope, not the status.
    """
    body = {"error_type": "composer_planner_failure", "failure_code": "invalid_provider_response", "planner_code": "MALFORMED_RESPONSE"}
    r = happy_responders([tg.user_row(1)], state=None)
    r["POST /api/sessions/"] = lambda c: (
        ok({"detail": body}, 502) if c.path.endswith("/messages") else ok({"is_valid": False, "errors": []})
    )
    b = _battery(tmp_path, FakeClient(r), repeats=1)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f")}
    b.fire(cases, tripwire=None, only=set(cases))
    meta = json.loads((tmp_path / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert meta["instrument"]["http_unrecovered"] is None, meta["instrument"]
    assert meta["server_terminal"] == {"budget_exhausted": None, "reason": "MALFORMED_RESPONSE", "source": "planner_5xx"}

    # A 422 carrying a planner envelope (`policy_blocked` does) keeps its MORE SPECIFIC 422_detail terminal:
    # the 5xx branch must not shadow it just because the body shape matches.
    policy = {
        "error_type": "composer_planner_failure",
        "failure_code": "policy_blocked",
        "planner_code": "VALIDATION_FAILED",
        "reason": "p",
    }
    r3 = happy_responders([tg.user_row(1)], state=None)
    r3["POST /api/sessions/"] = lambda c: (
        ok({"detail": policy}, 422) if c.path.endswith("/messages") else ok({"is_valid": False, "errors": []})
    )
    b3 = _battery(tmp_path / "b3", FakeClient(r3), repeats=1)
    b3.fire(cases, tripwire=None, only=set(cases))
    assert json.loads((tmp_path / "b3" / "runs/r1/fork_coalesce/1/meta.json").read_text())["server_terminal"]["source"] == "422_detail"

    # A 5xx WITHOUT a planner envelope is still a substrate fault — the rule narrows, it does not vanish.
    r2 = happy_responders([tg.user_row(1)], state=None)
    r2["POST /api/sessions/"] = lambda c: (
        ok({"detail": "service_setup_failed"}, 500) if c.path.endswith("/messages") else ok({"is_valid": False, "errors": []})
    )
    b2 = _battery(tmp_path / "b2", FakeClient(r2), repeats=1)
    b2.fire(cases, tripwire=None, only=set(cases))
    meta2 = json.loads((tmp_path / "b2" / "runs/r1/fork_coalesce/1/meta.json").read_text())
    assert meta2["instrument"]["http_unrecovered"] == "post_message 500"


def test_composer_progress_reasons_map_to_every_budget_and_a_missing_snapshot_is_source_none(tmp_path: Path) -> None:
    """M1: the scorer's terminal_missing keys on ``source``, so a progress read that produced no reason must
    not claim one; and the composition/discovery budgets must map as well as the wall clock."""
    for reason, budget in (("convergence_composition_budget", "composition"), ("convergence_discovery_budget", "discovery")):
        r = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)

        def timeout_then(c, _r=reason):
            if c.path.endswith("/messages"):
                raise db.HttpTimeout("620s")
            return ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})

        r["POST /api/sessions/"] = timeout_then
        r["GET /api/sessions/s1/composer-progress"] = lambda c, _r=reason: ok({"phase": "failed", "reason": _r})
        _battery(tmp_path, FakeClient(r), repeats=1).run_prompt(
            label="l", prompt="p", run_dir=tmp_path / "runs/r1/x" / budget, case="x", repeat=1
        )
        meta = json.loads((tmp_path / "runs/r1/x" / budget / "meta.json").read_text())
        assert meta["server_terminal"] == {"budget_exhausted": budget, "reason": reason, "source": "composer_progress"}
    r2 = happy_responders([tg.user_row(1), tg.audit_row(2)], state=None)

    def timeout_then_none(c):
        if c.path.endswith("/messages"):
            raise db.HttpTimeout("620s")
        return ok({"is_valid": False, "checks": [], "errors": [], "warnings": [], "readiness": "blocked"})

    r2["POST /api/sessions/"] = timeout_then_none
    r2["GET /api/sessions/s1/composer-progress"] = lambda c: ok({"detail": "gone"}, 404)
    _battery(tmp_path, FakeClient(r2), repeats=1).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/none", case="x", repeat=1)
    meta2 = json.loads((tmp_path / "runs/r1/x/none/meta.json").read_text())
    assert meta2["server_terminal"] == {"budget_exhausted": None, "reason": None, "source": "none"}


def test_read_integrity_error_is_recorded(tmp_path: Path) -> None:
    r = happy_responders([tg.user_row(1)], state=None)
    r["GET /api/sessions/s1/messages"] = lambda c: ok(
        {
            "error_type": "audit_integrity_error",
            "detail": "ELSPETH stopped before replying because it could not verify this session's audit trail.",
        },
        500,
    )
    client = FakeClient(r)
    _battery(tmp_path, client).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    inst = json.loads((tmp_path / "runs/r1/x/1/meta.json").read_text())["instrument"]
    assert inst["read_integrity"] and "audit trail" in inst["read_integrity"]


def test_review_loop_is_bounded_and_captured(tmp_path: Path) -> None:
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions/s1/interpretations"] = lambda c: ok({"events": [{"id": "e1", "status": "pending"}]})  # never drains
    client = FakeClient(r)
    _battery(tmp_path, client).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/1", case="x", repeat=1)
    assert client.steps().count("POST /api/sessions/s1/interpretations/e1/resolve") == 5
    reviews = json.loads((tmp_path / "runs/r1/x/1/reviews.json").read_text())
    assert [rv["round"] for rv in reviews] == [1, 2, 3, 4, 5]
    assert json.loads((tmp_path / "runs/r1/x/1/meta.json").read_text())["instrument"]["review_rounds_exhausted"] is True
    r2 = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    seen = {"n": 0}

    def drains(c):
        seen["n"] += 1
        return ok({"events": [{"id": "e1", "status": "pending"}]}) if seen["n"] == 1 else ok({"events": []})

    r2["GET /api/sessions/s1/interpretations"] = drains
    client2 = FakeClient(r2)
    _battery(tmp_path, client2).run_prompt(label="l", prompt="p", run_dir=tmp_path / "runs/r1/x/2", case="x", repeat=2)
    assert client2.steps().count("POST /api/sessions/s1/interpretations/e1/resolve") == 1
    assert json.loads((tmp_path / "runs/r1/x/2/meta.json").read_text())["instrument"]["review_rounds_exhausted"] is False


def test_fire_order_abort_and_case_flags(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=2)
    order: list[str] = []
    real = b.run_prompt

    def spy(**kw):
        order.append(kw["label"])
        return real(**kw)

    b.run_prompt = spy  # type: ignore[method-assign]
    tripped: list[str] = []
    cases = {
        "canary": CorpusCase("canary", "c"),
        "fork_coalesce": CorpusCase("fork_coalesce", "f"),
        "boolean_routing": CorpusCase("boolean_routing", "b"),
    }
    doc = b.fire(cases, tripwire=lambda bt: tripped.append("tw"))
    assert order[:10] == [f"battery/r1/canary/{i}" for i in range(1, 11)]
    assert tripped == ["tw"]
    assert order[10:] == [
        "battery/r1/boolean_routing/1",
        "battery/r1/fork_coalesce/1",
        "battery/r1/boolean_routing/2",
        "battery/r1/fork_coalesce/2",
    ]
    assert doc["aborted"] is False and len(doc["completed"]) == 14
    assert json.loads((tmp_path / "runs/r1/firing.json").read_text())["completed"][-1]["case"] == "fork_coalesce"


def test_should_abort_counts_instrument_kinds_only() -> None:
    assert db.should_abort(["http", "truncated", "capture"]) == "3 consecutive instrument_error"
    assert db.should_abort([None, "http", "http"]) is None
    assert db.should_abort(["surface", "surface", "surface"]) is None  # planner routing is a finding, never an abort
    assert db.should_abort(["http", "surface", "http"]) is None
    assert db.should_abort(["no_calls", "http", "http", "transport"]) == "3 consecutive instrument_error"


def test_fire_aborts_after_three_consecutive_instrument_errors(tmp_path: Path) -> None:
    dead = happy_responders([tg.user_row(1)], state=None)
    dead["GET /api/sessions/s1/messages"] = lambda c: ok({"detail": "bad gateway"}, 502)  # capture read fails ⇒ http (instrument)
    b = _battery(tmp_path, FakeClient(dead), repeats=5)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only={"fork_coalesce", "boolean_routing"})
    assert doc["aborted"] is True and doc["abort_reason"] == "3 consecutive instrument_error" and len(doc["completed"]) == 3
    assert all(c["excluded"] == "http" for c in doc["completed"])


def test_canary_instrument_failures_abort_before_tripwire_and_corpus(tmp_path: Path) -> None:
    dead = happy_responders([tg.user_row(1)], state=None)
    dead["GET /api/sessions/s1/messages"] = lambda c: ok({"detail": "bad gateway"}, 502)  # capture read fails ⇒ http (instrument)
    b = _battery(tmp_path, FakeClient(dead), repeats=5)
    tripped: list[str] = []
    cases = {"canary": CorpusCase("canary", "c"), "fork_coalesce": CorpusCase("fork_coalesce", "f")}
    doc = b.fire(cases, tripwire=lambda bt: tripped.append("tw"))
    assert doc["aborted"] is True and doc["abort_reason"] == "3 consecutive instrument_error"
    # 3 canary reps, not the full 10 — the streak aborts as soon as it hits 3 consecutive, before the tripwire fires
    assert len(doc["completed"]) == 3 and all(c["case"] == "canary" for c in doc["completed"])
    assert tripped == []  # aborted before the tripwire — and thus before any corpus case — could run


def test_canary_measurement_exclusions_never_abort_and_corpus_still_fires(tmp_path: Path) -> None:
    routed = happy_responders(
        [tg.user_row(1), tg.assistant_row(2, content="hi")], state=None
    )  # zero audit rows ⇒ surface undetermined (measurement kind)
    b = _battery(tmp_path, FakeClient(routed), repeats=1)
    tripped: list[str] = []
    cases = {"canary": CorpusCase("canary", "c"), "fork_coalesce": CorpusCase("fork_coalesce", "f")}
    doc = b.fire(cases, tripwire=lambda bt: tripped.append("tw"))
    assert doc["aborted"] is False
    canary_runs = [c for c in doc["completed"] if c["case"] == "canary"]
    assert len(canary_runs) == 10 and all(c["excluded"] == "surface" for c in canary_runs)
    assert tripped == ["tw"]  # measurement kinds never abort — the round proceeds normally
    assert any(c["case"] == "fork_coalesce" for c in doc["completed"])


def test_fire_never_aborts_on_measurement_exclusions(tmp_path: Path) -> None:
    routed = happy_responders(
        [tg.user_row(1), tg.assistant_row(2, content="hi")], state=None
    )  # zero audit rows ⇒ surface undetermined (measurement kind)
    b = _battery(tmp_path, FakeClient(routed), repeats=3)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only=set(cases))
    assert doc["aborted"] is False and len(doc["completed"]) == 6 and all(c["excluded"] == "surface" for c in doc["completed"])
    assert doc["case_flags"] == {}  # measurement kinds flag nothing either


def test_fire_flags_a_case_on_two_consecutive_repeats_but_continues(tmp_path: Path) -> None:
    good = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    client = FakeClient(good)

    def paged(c):
        # the fake keys on the title of the most recent PATCH: boolean_routing captures fail (instrument), others are ideal
        last_title = next(
            (k.json["title"] for k in reversed(client.calls) if k.method == "PATCH" and isinstance(k.json, dict) and "title" in k.json), ""
        )
        return ok({"detail": "bad gateway"}, 502) if "boolean_routing" in last_title else ok(tg.ideal_thread(ARGS))

    good["GET /api/sessions/s1/messages"] = paged
    b = _battery(tmp_path, client, repeats=3)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only=set(cases))
    assert doc["aborted"] is False  # round-robin: never 3 consecutive
    assert doc["case_flags"]["boolean_routing"] == ["instrument_error on two consecutive repeats"]


def test_fire_contains_an_unexpected_exception_and_continues(tmp_path: Path) -> None:
    good = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    b = _battery(tmp_path, FakeClient(good), repeats=1)
    real = b.run_prompt

    def boom(**kw):
        if kw["case"] == "boolean_routing":
            raise RuntimeError("unexpected")
        return real(**kw)

    b.run_prompt = boom  # type: ignore[method-assign]
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only=set(cases))
    assert [c["excluded"] for c in doc["completed"]] == ["http", None]
    meta = json.loads((tmp_path / "runs/r1/boolean_routing/1/meta.json").read_text())
    assert meta["instrument"]["http_unrecovered"].startswith("driver exception: RuntimeError")


def test_a_crashing_tripwire_is_contained_and_recorded_not_fatal(tmp_path: Path) -> None:
    """spec §4: a multi-hour round never dies on one traceback. The tripwire ran outside containment, so a
    malformed tripwire capture killed fire() AFTER the canary's ten runs were already spent."""
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)

    def boom(_battery_arg):
        raise RuntimeError("tripwire capture unreadable")

    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f")}
    doc = b.fire(cases, tripwire=boom, only={"fork_coalesce"})
    assert doc["aborted"] is False and len(doc["completed"]) == 1  # the corpus still fired
    assert doc["tripwire_error"] == "RuntimeError('tripwire capture unreadable')"
    assert json.loads((tmp_path / "runs/r1/firing.json").read_text())["tripwire_error"] == doc["tripwire_error"]


def test_preflight_runs_before_the_canary_is_spent(tmp_path: Path) -> None:
    """Classifier drift used to raise at tripwire time — i.e. after ten canary runs. It is a config failure
    and must fire before anything is fired at the substrate."""
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)
    fired: list[str] = []
    real = b.run_prompt

    def spy(**kw):
        fired.append(kw["label"])
        return real(**kw)

    b.run_prompt = spy  # type: ignore[method-assign]

    def unpaired():
        raise db_planner.ProbeUnpaired("pair does not route")

    with pytest.raises(db_planner.ProbeUnpaired):
        b.fire({"canary": CorpusCase("canary", "c")}, tripwire=None, preflight=unpaired)
    assert fired == []  # nothing fired at the substrate at all


def test_contained_run_survives_a_second_failure_inside_the_handler(tmp_path: Path) -> None:
    """I4: the containment handler itself re-scores the run. If that second read raises, the exception escapes
    containment and kills the round — so the handler falls back to the `capture` verdict without re-scoring."""
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)

    def boom(**kw):
        raise RuntimeError("first failure")

    def boom_again(*a, **kw):
        raise OSError("read-only filesystem")

    b.run_prompt = boom  # type: ignore[method-assign]
    b._write_meta = boom_again  # type: ignore[method-assign]
    doc = b.fire({"fork_coalesce": CorpusCase("fork_coalesce", "f")}, tripwire=None, only={"fork_coalesce"})
    assert doc["aborted"] is False and [c["excluded"] for c in doc["completed"]] == ["capture"]


def test_identity_never_performs_io_when_status_is_unprimed_and_unreachable(tmp_path: Path) -> None:
    """The re-entrancy hole this guards: _write_meta (called from _contained's exception handler) builds
    identity via _identity — if _identity did I/O and that I/O failed too, the second failure would escape
    containment and kill fire() outright. _identity must read only the (possibly still-unprimed) cache."""
    good = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))

    def status_unreachable(c):
        raise db.HttpTimeout("status unreachable")

    good["GET /api/system/status"] = status_unreachable
    b = _battery(tmp_path, FakeClient(good), repeats=1)
    real = b.run_prompt

    def boom(**kw):
        if kw["case"] == "boolean_routing":
            raise RuntimeError("unexpected")
        return real(**kw)

    b.run_prompt = boom  # type: ignore[method-assign]
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f"), "boolean_routing": CorpusCase("boolean_routing", "b")}
    doc = b.fire(cases, tripwire=None, only=set(cases))
    assert doc["aborted"] is False  # fire() survives the unreachable status endpoint entirely
    assert [c["excluded"] for c in doc["completed"]] == ["http", None]
    meta = json.loads((tmp_path / "runs/r1/boolean_routing/1/meta.json").read_text())
    assert meta["instrument"]["http_unrecovered"].startswith("driver exception: RuntimeError")
    assert meta["identity"]["binding"]["composer_model"] is None  # status never reachable; no I/O escaped containment


def test_system_status_non_200_raises_battery_identity_error(tmp_path: Path) -> None:
    client = FakeClient(
        {
            "POST /api/auth/login": lambda c: ok({"access_token": "tok"}),
            "GET /api/system/status": lambda c: ok({"detail": "unavailable"}, 503),
        }
    )
    b = db.Battery(client, base="x", round_name="r1", runs_dir=tmp_path, corpus_version=0, env_budgets=ENV, sleep=lambda s: None)
    b.login("u", "p")
    with pytest.raises(db.BatteryIdentityError):
        b.system_status()


def _write_valid_env_file(env_file: Path) -> None:
    env_file.write_text(
        "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=30\n"
        "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10\n"
        "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL=m\n"
    )


def test_load_credentials_bad_mode_exits_64_via_main(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    creds = state_dir / "credentials.json"
    creds.write_text(json.dumps({"username": "u", "password": "p"}))
    creds.chmod(0o644)  # group/other-readable: must be 600
    env_file = tmp_path / "web.env"
    _write_valid_env_file(env_file)
    rc = db.main(["--round", "r1", "--state-dir", str(state_dir), "--env-file", str(env_file), "--runs-dir", str(tmp_path / "runs")])
    assert rc == 64  # config/usage — never the exit-1 "aborted by the instrument rules" code


def test_load_credentials_missing_key_exits_64_via_main(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    creds = state_dir / "credentials.json"
    creds.write_text(json.dumps({"username": "u"}))  # no "password"
    creds.chmod(0o600)
    env_file = tmp_path / "web.env"
    _write_valid_env_file(env_file)
    rc = db.main(["--round", "r1", "--state-dir", str(state_dir), "--env-file", str(env_file), "--runs-dir", str(tmp_path / "runs")])
    assert rc == 64


def test_resume_skips_complete_runs_and_never_refetches(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f")}
    b.fire(cases, tripwire=None, only={"fork_coalesce"})
    n_calls = len(client.calls)
    stamp = (tmp_path / "runs/r1/fork_coalesce/1/messages.json").read_text()
    b2 = _battery(tmp_path, client, repeats=1, resume=True)
    b2.fire(cases, tripwire=None, only={"fork_coalesce"})
    # login of the second battery, plus fire()'s once-up-front identity-cache priming — never a re-fetch of the
    # resumed run's capture artifacts (no create_session/messages/state/etc. calls for the completed case)
    assert len(client.calls) == n_calls + 2
    assert (tmp_path / "runs/r1/fork_coalesce/1/messages.json").read_text() == stamp


def test_cleanup_deletes_only_this_rounds_complete_sessions(tmp_path: Path) -> None:
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions"] = lambda c: ok(
        [
            {"id": "s1", "user_id": "u", "title": "battery/r1/fork_coalesce/1", "created_at": "t", "updated_at": "t", "archived": False},
            {
                "id": "s9",
                "user_id": "u",
                "title": "battery/r1/fork_coalesce/2",
                "created_at": "t",
                "updated_at": "t",
                "archived": False,
            },  # no capture on disk
            {
                "id": "s8",
                "user_id": "u",
                "title": "battery/r0/fork_coalesce/1",
                "created_at": "t",
                "updated_at": "t",
                "archived": False,
            },  # other round
            {"id": "s7", "user_id": "u", "title": "My real session", "created_at": "t", "updated_at": "t", "archived": False},
        ]
    )
    client = FakeClient(r)
    b = _battery(tmp_path, client, repeats=1)
    b.fire({"fork_coalesce": CorpusCase("fork_coalesce", "f")}, tripwire=None, only={"fork_coalesce"})
    assert b.cleanup() == ["s1"]
    assert [c.path for c in client.calls if c.method == "DELETE"] == ["/api/sessions/s1"]


def test_cleanup_paginates_and_reaches_tripwire_sessions(tmp_path: Path) -> None:
    """I5: GET /api/sessions defaults to limit=50 and caps at 200, so an unpaginated read left most of a
    95-run round undeleted, silently. Tripwire/probe titles carry three segments after the prefix and were
    skipped forever."""
    sessions = [
        {"id": f"s{i}", "user_id": "u", "title": f"battery/r1/fork_coalesce/{i}", "created_at": "t", "updated_at": "t", "archived": False}
        for i in range(1, 251)
    ]
    sessions.append(
        {
            "id": "tw",
            "user_id": "u",
            "title": "battery/r1/_tripwire/fork_coalesce/1",
            "created_at": "t",
            "updated_at": "t",
            "archived": False,
        }
    )
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    pages: list[dict] = []

    def listed(c):
        params = c.params or {}
        pages.append(params)
        off, lim = int(params.get("offset", 0)), int(params.get("limit", 50))
        return ok(sessions[off : off + min(lim, 200)])  # OFFSET over the LIVE table, as the server does

    def delete(c):
        sid = c.path.rsplit("/", 1)[-1]
        # the fake models the real effect of a delete: the row leaves the table every later OFFSET applies to.
        # Without this, deleting mid-pagination silently skips as many sessions as it deleted and no test sees it.
        sessions[:] = [s for s in sessions if s["id"] != sid]
        return ok(None, 204)

    r["GET /api/sessions"] = listed
    r["DELETE /api/sessions/"] = delete
    client = FakeClient(r)
    b = _battery(tmp_path, client, repeats=1)
    # Complete captures on disk for runs 1-3 (page 1), runs 201-203 (the FIRST three rows of page 2) and the
    # tripwire fixture. The 201-203 placement is the guard: delete-while-paginating removes three rows from the
    # live table before asking for offset=200, so those three slide into the page already consumed and are
    # never seen — the same silent under-deletion this finding was filed for, one page further in.
    targets = ["fork_coalesce/1", "fork_coalesce/2", "fork_coalesce/3", "fork_coalesce/201", "fork_coalesce/202", "fork_coalesce/203"]
    for rel in [*targets, "_tripwire/fork_coalesce/1"]:
        run_dir = tmp_path / "runs/r1" / rel
        run_dir.mkdir(parents=True, exist_ok=True)
        for name in ("messages.json", "reviews.json"):
            (run_dir / name).write_text("[]")
        (run_dir / "meta.json").write_text("{}")
    assert b.cleanup() == ["s1", "s2", "s3", "s201", "s202", "s203", "tw"]
    assert [p["offset"] for p in pages] == [0, 200]  # a full page always fetches again; the short page stops


def test_cleanup_survives_a_non_200_listing_body(tmp_path: Path) -> None:
    """A non-200 body is a dict: iterating it yielded strings, not sessions."""
    r = happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS))
    r["GET /api/sessions"] = lambda c: ok({"detail": "unavailable"}, 503)
    b = _battery(tmp_path, FakeClient(r), repeats=1)
    assert b.cleanup() == []


def test_unknown_case_name_exits_64_before_any_network(tmp_path: Path) -> None:
    """I6: `--cases fork_coalese` used to fire nothing and exit 0 — a silently empty round."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    creds = state_dir / "credentials.json"
    creds.write_text(json.dumps({"username": "u", "password": "p"}))
    creds.chmod(0o600)
    env_file = tmp_path / "web.env"
    _write_valid_env_file(env_file)
    rc = db.main(
        [
            "--round",
            "r1",
            "--cases",
            "fork_coalese,canary",
            "--state-dir",
            str(state_dir),
            "--env-file",
            str(env_file),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert rc == 64  # returned before login: no RequestsClient call was ever made


def test_read_env_budgets(tmp_path: Path) -> None:
    env = tmp_path / "web.env"
    env.write_text(
        "# c\nELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=30\nELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10\nELSPETH_WEB__COMPOSER_ADVISOR_MODEL=openrouter/anthropic/claude-opus-4-8\n"
    )
    budgets = db.read_env_budgets(env)
    assert {k: v for k, v in budgets.items() if not k.startswith("_")} == {
        "advisor_model": "openrouter/anthropic/claude-opus-4-8",
        "composition_turns": 30,
        "discovery_turns": 10,
    }
    assert len(budgets["_env_file_sha256"]) == 64  # recorded identity: an operator-asserted budget change is visible as a delta
    env.write_text("ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10\n")
    with pytest.raises(ValueError, match="COMPOSER_ADVISOR_MODEL"):
        db.read_env_budgets(env)
