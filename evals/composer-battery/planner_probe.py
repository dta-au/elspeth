"""§7 planner probe + tripwire — the LIVE wrapper. Fires each arm through the Battery (with proposal capture),
then scores offline via evals.lib.battery_planner. Keep this file thin; logic lives in the library."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.lib.battery_planner import (  # noqa: E402
    LOOP_PREFIX,
    PROBE_FIXTURES,
    TRIPWIRE_FIXTURES,
    assert_pair_routes,
    load_fixture,
    score_probe_dir,
    score_tripwire_dir,
)

if TYPE_CHECKING:  # pragma: no cover
    from drive_battery import Battery


def tripwire_preflight() -> None:
    """Classifier-drift check for every tripwire fixture. Raises ``ProbeUnpaired``; the driver runs it BEFORE
    the canary, so a grammar edit fails the round immediately instead of after ten canary runs are spent."""
    for fixture in TRIPWIRE_FIXTURES:
        assert_pair_routes(load_fixture(fixture)["intent"])


def run_tripwire(battery: Battery) -> Path:
    for fixture in TRIPWIRE_FIXTURES:
        run_dir = battery.round_dir / "_tripwire" / fixture / "1"
        if battery.resume_skip(run_dir):
            continue  # spec §4: a resume never re-fires or overwrites a captured page
        try:
            battery.run_prompt(
                label=f"battery/{battery.round}/_tripwire/{fixture}/1",
                prompt=load_fixture(fixture)["intent"],
                run_dir=run_dir,
                case="_tripwire",
                repeat=1,
                capture_proposals=True,
            )
        except Exception as exc:  # one fixture never costs the other two, nor the round
            print(f"tripwire {fixture}: {exc!r}", file=sys.stderr)
    score_tripwire_dir(battery.round_dir)
    return battery.round_dir / "_tripwire" / "tripwire.json"


def run_probe(battery: Battery) -> Path:
    for fixture in PROBE_FIXTURES:
        intent = load_fixture(fixture)["intent"]
        assert_pair_routes(intent)
        for arm, prompt in (("P", intent), ("L", LOOP_PREFIX + intent)):
            run_dir = battery.round_dir / "_probe" / fixture / arm
            if battery.resume_skip(run_dir):
                continue
            try:
                battery.run_prompt(
                    label=f"battery/{battery.round}/_probe/{fixture}/{arm}",
                    prompt=prompt,
                    run_dir=run_dir,
                    case="_probe",
                    repeat=1,
                    capture_proposals=True,
                )
            except Exception as exc:  # a crash on arm 14 of 20 must not lose the 13 already fired
                print(f"probe {fixture}/{arm}: {exc!r}", file=sys.stderr)
    score_probe_dir(battery.round_dir)
    return battery.round_dir / "_probe" / "probe.json"


__all__ = ["run_probe", "run_tripwire", "tripwire_preflight"]
