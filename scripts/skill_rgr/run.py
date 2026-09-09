"""Run a scenario through the RGR harness.

Usage:
    python -m scripts.skill_rgr.run calibration --model gpt-5.5 --label red
    python -m scripts.skill_rgr.run batch1 --model gpt-5.5 --label red
    python -m scripts.skill_rgr.run batch1 --skill /tmp/edited_skill.md --label green
    python -m scripts.skill_rgr.run batch1 --model claude-opus-4-7 --label green-claude

Environment:
    OPENAI_API_KEY     — required for gpt-* models
    ANTHROPIC_API_KEY  — required for claude-* models
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HARNESS_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_HARNESS_ROOT))
# Also make sibling scenario modules importable from each other
# (e.g. batch1_refactor_insist imports batch1_pressured).
sys.path.insert(0, str(_HARNESS_ROOT / "scenarios"))


def _load_batch1() -> Any:
    from scenarios.batch1 import BATCH1  # type: ignore[import-not-found]

    return BATCH1


def _load_batch1_pressured() -> Any:
    from scenarios.batch1_pressured import BATCH1_PRESSURED  # type: ignore[import-not-found]

    return BATCH1_PRESSURED


def _load_batch1_refactor_incomplete() -> Any:
    from scenarios.batch1_refactor_incomplete import BATCH1_REFACTOR_INCOMPLETE  # type: ignore[import-not-found]

    return BATCH1_REFACTOR_INCOMPLETE


def _load_batch1_refactor_insist() -> Any:
    from scenarios.batch1_refactor_insist import BATCH1_REFACTOR_INSIST  # type: ignore[import-not-found]

    return BATCH1_REFACTOR_INSIST


def _load_batch1_refactor_override() -> Any:
    from scenarios.batch1_refactor_override import BATCH1_REFACTOR_OVERRIDE  # type: ignore[import-not-found]

    return BATCH1_REFACTOR_OVERRIDE


def _load_batch3_bootstrap() -> Any:
    from scenarios.batch3_bootstrap import BATCH3_BOOTSTRAP  # type: ignore[import-not-found]

    return BATCH3_BOOTSTRAP


def _load_batch3_contract_loop() -> Any:
    from scenarios.batch3_contract_loop import BATCH3_CONTRACT_LOOP  # type: ignore[import-not-found]

    return BATCH3_CONTRACT_LOOP


def _load_batch3_contract_loop_insist() -> Any:
    from scenarios.batch3_contract_loop_insist import BATCH3_CONTRACT_LOOP_INSIST  # type: ignore[import-not-found]

    return BATCH3_CONTRACT_LOOP_INSIST


def _load_calibration() -> Any:
    from scenarios.calibration import CALIBRATION  # type: ignore[import-not-found]

    return CALIBRATION


_SCENARIO_LOADERS: dict[str, Callable[[], Any]] = {
    "batch1": _load_batch1,
    "batch1_pressured": _load_batch1_pressured,
    "batch1_refactor_incomplete": _load_batch1_refactor_incomplete,
    "batch1_refactor_insist": _load_batch1_refactor_insist,
    "batch1_refactor_override": _load_batch1_refactor_override,
    "batch3_bootstrap": _load_batch3_bootstrap,
    "batch3_contract_loop": _load_batch3_contract_loop,
    "batch3_contract_loop_insist": _load_batch3_contract_loop_insist,
    "calibration": _load_calibration,
}


def _load_scenario(name: str) -> Any:
    """Load one scenario from the closed registry without eager imports."""
    try:
        loader = _SCENARIO_LOADERS[name]
    except KeyError as exc:
        supported = ", ".join(sorted(_SCENARIO_LOADERS))
        raise ValueError(f"Unknown scenario {name!r}; choose one of: {supported}") from exc
    return loader()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", help="Scenario module name under scenarios/")
    parser.add_argument(
        "--model",
        default="openrouter/openai/gpt-5",
        help=(
            "litellm model identifier. OpenRouter routes use the "
            "openrouter/<vendor>/<model> form (e.g. "
            "openrouter/openai/gpt-5, openrouter/anthropic/claude-opus-4)."
        ),
    )
    parser.add_argument(
        "--skill",
        default=None,
        help="Path to an alternate skill markdown file (for GREEN runs)",
    )
    parser.add_argument(
        "--label",
        default="run",
        help="Filename label for the transcript",
    )
    parser.add_argument(
        "--phase",
        choices=["red", "green", "none"],
        default="none",
        help="Predicate phase to evaluate after the run",
    )
    args = parser.parse_args()

    from harness import evaluate, load_skill, run_scenario  # type: ignore[import-not-found]

    scenario = _load_scenario(args.scenario)
    skill_text = load_skill(override_path=Path(args.skill)) if args.skill else load_skill()

    transcript = run_scenario(scenario, skill_text=skill_text, model=args.model, label=args.label)

    print(f"Scenario: {scenario.name}")
    print(f"Model:    {args.model}")
    print(f"Skill:    {args.skill or '<production>'}")
    print(f"Turns:    {sum(1 for e in transcript if e.get('role') == 'assistant')}")
    print(f"Tools:    {sum(1 for e in transcript if e.get('role') == 'tool')}")

    if args.phase != "none":
        results = evaluate(transcript, scenario, phase=args.phase)
        all_pass = all(results.values())
        print(f"\n{args.phase.upper()} predicate results:")
        print(json.dumps(results, indent=2))
        print(f"\n{args.phase.upper()} {'CONFIRMED' if all_pass else 'NOT confirmed'}")
        return 0 if all_pass else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
