"""The thin live wrapper: fires each arm through the Battery with proposal capture, then scores offline via evals.lib.battery_planner."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import ClassVar

import planner_probe as pp  # evals/composer-battery/planner_probe.py via conftest sys.path
from evals.lib import battery_planner as bp

from elspeth.web.composer.protocol import PIPELINE_STAGED_AUTO_COMMIT_MESSAGE
from tests.unit.evals.composer_battery import threadgen as tg

FC = bp.load_fixture("fork_coalesce")
ARGS = FC["canonical_arguments"]


def _write(run_dir: Path, messages: list[dict], *, state: dict | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "messages.json").write_text(json.dumps(messages))
    if state is not None:
        (run_dir / "state.json").write_text(json.dumps(state))
        (run_dir / "validate.json").write_text(
            json.dumps({"is_valid": True, "checks": [], "errors": [], "warnings": [], "readiness": "ready"})
        )
    (run_dir / "reviews.json").write_text("[]")
    (run_dir / "meta.json").write_text(json.dumps(tg.meta(case="_probe")))


def _planner_thread() -> list[dict]:
    return [
        tg.user_row(1),
        tg.audit_row(2, planner_ordinal=1),
        tg.planner_attempt_row(3, ordinal=1, outcome="accepted", led_to="done", new_information=("plugin.schema",)),
        tg.assistant_row(4, content=PIPELINE_STAGED_AUTO_COMMIT_MESSAGE),
    ]


def test_run_tripwire_and_probe_use_the_battery_with_proposal_capture(tmp_path: Path) -> None:
    class StubBattery:
        round = "r1"
        round_dir = tmp_path / "runs" / "r1"
        runs_dir = tmp_path / "runs"
        resume = False
        calls: ClassVar[list[dict]] = []

        def resume_skip(self, run_dir: Path) -> bool:
            return self.resume and (run_dir / "messages.json").exists()

        def run_prompt(self, **kw):
            self.calls.append(kw)
            _write(
                kw["run_dir"],
                _planner_thread() if not kw["prompt"].startswith("Hi. ") else tg.ideal_thread(ARGS),
                state={"id": "s", "version": 2, **copy.deepcopy(ARGS)},
            )
            return None

    b = StubBattery()
    pp.run_tripwire(b)  # type: ignore[arg-type]
    assert [c["label"] for c in b.calls] == [f"battery/r1/_tripwire/{f}/1" for f in bp.TRIPWIRE_FIXTURES]
    assert all(c["capture_proposals"] for c in b.calls) and all(c["case"] == "_tripwire" for c in b.calls)
    assert (tmp_path / "runs/r1/_tripwire/tripwire.json").exists()
    b.calls.clear()
    pp.run_probe(b)  # type: ignore[arg-type]
    assert (
        len(b.calls) == 20
        and b.calls[0]["prompt"] == bp.load_fixture(bp.PROBE_FIXTURES[0])["intent"]
        and b.calls[1]["prompt"].startswith("Hi. ")
    )
    assert (tmp_path / "runs/r1/_probe/probe.json").exists()


def test_resume_never_refires_a_complete_tripwire_capture(tmp_path: Path) -> None:
    """spec §4: `--resume` never re-fetches or overwrites a captured page. The tripwire fired unconditionally,
    so resuming an interrupted round overwrote `_tripwire/<fixture>/1`."""

    class StubBattery:
        round = "r1"
        round_dir = tmp_path / "runs" / "r1"
        runs_dir = tmp_path / "runs"
        resume = True
        calls: ClassVar[list[dict]] = []

        def resume_skip(self, run_dir: Path) -> bool:
            return self.resume and (run_dir / "messages.json").exists()

        def run_prompt(self, **kw):
            self.calls.append(kw)
            _write(kw["run_dir"], _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
            return None

    done = bp.TRIPWIRE_FIXTURES[0]
    _write(tmp_path / "runs/r1/_tripwire" / done / "1", _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(ARGS)})
    stamp = (tmp_path / "runs/r1/_tripwire" / done / "1" / "messages.json").read_text()
    b = StubBattery()
    pp.run_tripwire(b)  # type: ignore[arg-type]
    assert [c["run_dir"].parent.name for c in b.calls] == list(bp.TRIPWIRE_FIXTURES[1:])
    assert (tmp_path / "runs/r1/_tripwire" / done / "1" / "messages.json").read_text() == stamp


def test_a_crashing_arm_never_stops_the_remaining_fixtures(tmp_path: Path) -> None:
    class StubBattery:
        round = "r1"
        round_dir = tmp_path / "runs" / "r1"
        runs_dir = tmp_path / "runs"
        resume = False
        calls: ClassVar[list[dict]] = []

        def resume_skip(self, run_dir: Path) -> bool:
            return False

        def run_prompt(self, **kw):
            self.calls.append(kw)
            fixture = kw["run_dir"].parent.name
            if fixture == bp.TRIPWIRE_FIXTURES[0]:
                raise RuntimeError("substrate refused the connection")
            args = bp.load_fixture(fixture)["canonical_arguments"]
            _write(kw["run_dir"], _planner_thread(), state={"id": "s", "version": 2, **copy.deepcopy(args)})
            return None

    b = StubBattery()
    pp.run_tripwire(b)  # type: ignore[arg-type]
    assert len(b.calls) == len(bp.TRIPWIRE_FIXTURES)  # every fixture was still attempted
    table = {t["fixture"]: t for t in json.loads((tmp_path / "runs/r1/_tripwire/tripwire.json").read_text())}
    assert table[bp.TRIPWIRE_FIXTURES[0]] == {
        "fixture": bp.TRIPWIRE_FIXTURES[0],
        "pass": False,
        "staged_variant": None,
        "planner_calls": 0,
        "planner_codes": {},
        "surface": "undetermined",
        "reason": "not fired",
    }
    assert table[bp.TRIPWIRE_FIXTURES[1]]["pass"] is True


def test_preflight_asserts_pair_routing_for_every_tripwire_fixture() -> None:
    """The routing assertion used to run inside the firing loop — i.e. after the canary was already spent."""
    pp.tripwire_preflight()  # the real fixtures route correctly today
    calls: list[str] = []
    original = pp.assert_pair_routes
    try:
        pp.assert_pair_routes = lambda intent: calls.append(intent)  # type: ignore[assignment]
        pp.tripwire_preflight()
    finally:
        pp.assert_pair_routes = original  # type: ignore[assignment]
    assert calls == [bp.load_fixture(f)["intent"] for f in bp.TRIPWIRE_FIXTURES]
