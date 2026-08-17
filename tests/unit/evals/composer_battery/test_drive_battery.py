from __future__ import annotations

import copy
import json
from pathlib import Path

import drive_battery as db
import pytest
from evals.lib.battery_corpus import CorpusCase
from evals.lib.battery_scenario import load_scenario

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


def test_resume_skips_complete_runs_and_never_refetches(tmp_path: Path) -> None:
    client = FakeClient(happy_responders(tg.ideal_thread(ARGS), state=copy.deepcopy(ARGS)))
    b = _battery(tmp_path, client, repeats=1)
    cases = {"fork_coalesce": CorpusCase("fork_coalesce", "f")}
    b.fire(cases, tripwire=None, only={"fork_coalesce"})
    n_calls = len(client.calls)
    stamp = (tmp_path / "runs/r1/fork_coalesce/1/messages.json").read_text()
    b2 = _battery(tmp_path, client, repeats=1, resume=True)
    b2.fire(cases, tripwire=None, only={"fork_coalesce"})
    assert len(client.calls) == n_calls + 1  # only the login of the second battery
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
