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
        calls: ClassVar[list[dict]] = []

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
