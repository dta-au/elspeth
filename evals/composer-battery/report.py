"""CLI: score every run of a round offline and write report.json + report.md."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.lib.battery_corpus import SCENARIOS_DIR, load_corpus  # noqa: E402
from evals.lib.battery_report import CompareRefused, LateBinding, build_report, write_report  # noqa: E402
from evals.lib.battery_scenario import load_scenario  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--round", required=True, help="round name under evals/composer-battery/runs/")
    ap.add_argument("--compare", default=None, help="previous round name to diff against (refuses on binding mismatch)")
    ap.add_argument(
        "--force-compare",
        action="store_true",
        help="compare despite a binding mismatch; the report is stamped FORCED and its deltas are not attributable",
    )
    ap.add_argument("--runs-dir", default=str(REPO / "evals/composer-battery/runs"))
    ns = ap.parse_args(argv)
    version, cases = load_corpus()
    prompt_hashes = {name: hashlib.sha256(c.prompt.encode()).hexdigest() for name, c in cases.items()}
    scenarios = {p.name: load_scenario(p / "scenario.json") for p in SCENARIOS_DIR.iterdir() if (p / "scenario.json").exists()}
    runs = Path(ns.runs_dir)
    try:
        report = build_report(
            runs / ns.round,
            scenarios=scenarios,
            corpus_version=version,
            prompt_hashes=prompt_hashes,
            compare_to=(runs / ns.compare) if ns.compare else None,
            force_compare=ns.force_compare,
        )
    except (CompareRefused, LateBinding) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 65  # EX_DATAERR — sibling exit-code convention
    j, m = write_report(runs / ns.round, report)
    print(m.read_text())
    print(f"wrote {j} and {m}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
