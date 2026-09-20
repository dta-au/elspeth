#!/usr/bin/env python3
"""Run mutation testing on ELSPETH core modules.

Mutation testing validates test effectiveness by introducing artificial bugs
(mutants) and checking if tests catch them. A high mutation score means tests
actually verify behavior, not just execute code.

Usage:
    # Run on canonical.py (default, critical module)
    python scripts/run_mutation_testing.py

    # Run on specific module
    python scripts/run_mutation_testing.py --module landscape/recorder.py

    # Run on all core modules (slow!)
    python scripts/run_mutation_testing.py --all

    # Show survived mutants from last run
    python scripts/run_mutation_testing.py --show-survivors

Target mutation scores:
    - canonical.py: 95%+ (hash integrity is foundational)
    - landscape/: 90%+ (audit trail is the legal record)
    - engine/: 85%+ (orchestration correctness)

Exit codes:
    0: Mutation testing completed (check output for score)
    1: Error during testing
    2: Mutation score below threshold (when --strict is used)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Module paths relative to src/elspeth/core/
MODULES = {
    "canonical": "canonical.py",
    "landscape/recorder": "landscape/recorder.py",
    "landscape/exporter": "landscape/exporter.py",
    "landscape/models": "landscape/models.py",
}

# Target mutation scores per module
THRESHOLDS = {
    "canonical.py": 95,
    "landscape/recorder.py": 90,
    "landscape/exporter.py": 90,
    "landscape/models.py": 90,
}

CORE_PATH = Path("src/elspeth/core")
CACHE_PATH = Path(".mutmut-cache")


def clean_cache() -> None:
    """Remove the mutmut 2.x SQLite cache file."""
    if CACHE_PATH.exists():
        print(f"🧹 Cleaning {CACHE_PATH}...")
        CACHE_PATH.unlink()


def run_mutmut(module_path: str, timeout_minutes: int = 120) -> int:
    """Run mutmut on a specific module.

    Args:
        module_path: Path relative to src/elspeth/core/
        timeout_minutes: Maximum time to wait

    Returns:
        Exit code from mutmut
    """
    full_path = CORE_PATH / module_path

    if not full_path.exists():
        print(f"❌ Module not found: {full_path}")
        return 1

    print(f"\n{'=' * 60}")
    print(f"🧬 Running mutation testing on: {module_path}")
    print(f"{'=' * 60}\n")

    cmd = [
        sys.executable,
        "-m",
        "mutmut",
        "run",
        "--paths-to-mutate",
        str(full_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            timeout=timeout_minutes * 60,
            check=False,
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"⏰ Timeout after {timeout_minutes} minutes")
        return 1
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
        return 1


def show_results() -> int:
    """Show mutation testing results."""
    print("\n📊 Mutation Testing Results:")
    print("-" * 40)

    cmd = [sys.executable, "-m", "mutmut", "results"]
    result = subprocess.run(cmd, check=False)
    return result.returncode


def show_survivors() -> int:
    """Show details of survived mutants."""
    print("\n🔍 Survived Mutants (tests didn't catch these bugs):")
    print("-" * 50)

    # Get list of survived mutant IDs
    cmd = [sys.executable, "-m", "mutmut", "result-ids", "survived"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print("❌ Could not read survived mutant IDs")
        return 1
    try:
        ids = [int(value) for value in result.stdout.split()]
    except ValueError:
        print("❌ Invalid survived mutant IDs")
        return 1
    if len(ids) != len(set(ids)) or any(mutant_id <= 0 for mutant_id in ids):
        print("❌ Invalid survived mutant IDs")
        return 1
    if not ids:
        print("No surviving mutants recorded; see results above for other mutant statuses.")
        return 0

    print("Surviving mutant IDs: " + " ".join(map(str, ids)))
    print("\nUse 'python -m mutmut show <id>' to inspect specific mutants")
    print("Use 'python -m mutmut html' to generate an HTML report\n")
    return 0


def calculate_score(module_path: str) -> tuple[int, int, float] | None:
    """Score one module using mutmut 2.x's machine-readable reports.

    JUnit inventories every mutant, including skipped ones, but does not mark
    skipped mutants as failures. Only ``result-ids killed`` proves a kill.
    Filtering by testcase file prevents cached results for other modules from
    inflating the score in --all and --no-clean runs. All non-killed mutants
    remain in the denominator. Missing or malformed reports are not a score.
    """
    result = subprocess.run([sys.executable, "-m", "mutmut", "junitxml"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    try:
        root = ET.fromstring(result.stdout)
        if root.tag != "testsuites":
            return None
        all_ids: set[int] = set()
        module_ids: set[int] = set()
        target = (CORE_PATH / module_path).resolve()
        for case in root.findall("./testsuite/testcase"):
            name = case.attrib["name"]
            if not name.startswith("Mutant #"):
                return None
            mutant_id = int(name.removeprefix("Mutant #"))
            if mutant_id <= 0 or mutant_id in all_ids:
                return None
            all_ids.add(mutant_id)
            if Path(case.attrib["file"]).resolve() == target:
                module_ids.add(mutant_id)
        if not module_ids:
            return None
        killed_result = subprocess.run(
            [sys.executable, "-m", "mutmut", "result-ids", "killed"], capture_output=True, text=True, check=False
        )
        if killed_result.returncode != 0:
            return None
        killed_list = [int(value) for value in killed_result.stdout.split()]
        killed_ids = set(killed_list)
        if len(killed_ids) != len(killed_list) or not killed_ids <= all_ids:
            return None
    except (ET.ParseError, KeyError, ValueError):
        return None
    killed = len(killed_ids & module_ids)
    total = len(module_ids)
    return killed, total, killed / total * 100


def main() -> int:
    """Run mutation testing."""
    parser = argparse.ArgumentParser(
        description="Run mutation testing on ELSPETH core modules",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--module",
        "-m",
        help="Module to test (e.g., canonical.py, landscape/recorder.py)",
        default="canonical.py",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run on all core modules (slow!)",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Reuse cached exploratory results (incompatible with --strict)",
    )
    parser.add_argument(
        "--show-survivors",
        action="store_true",
        help="Show survived mutants from last run",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error if score below threshold",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in minutes (default: 120)",
    )

    args = parser.parse_args()

    if args.strict and args.no_clean:
        print("❌ --strict requires fresh results; remove --no-clean (cached kills may predate changes to tests)")
        return 1

    # Show survivors and exit
    if args.show_survivors:
        if show_results() != 0:
            return 1
        return show_survivors()

    # Clean cache unless --no-clean
    if not args.no_clean:
        clean_cache()

    # Determine modules to test
    if args.all:
        modules = list(MODULES.values())
        print("🧬 Running mutation testing on ALL core modules")
        print("⚠️  This will take a long time (potentially hours)")
    else:
        modules = [args.module]

    # Run mutation testing
    exit_code = 0
    thresholds_by_path = {(CORE_PATH / path).resolve(): threshold for path, threshold in THRESHOLDS.items()}
    for module in modules:
        result = run_mutmut(module, timeout_minutes=args.timeout)
        # mutmut 2.x ORs survivor (2), timeout (4), and suspicious (8) bits.
        # Fatal errors set bit 1; signals and unknown statuses are errors too.
        if result not in (0, 2, 4, 6, 8, 10, 12, 14):
            print(f"❌ Mutation testing failed for {module} (exit {result})")
            return 1

        # Show results
        if show_results() != 0:
            print("❌ Could not read mutation results")
            return 1

        # Check threshold if strict mode
        if args.strict:
            score_data = calculate_score(module)
            if score_data is None:
                print(f"❌ Missing or invalid mutation score for {module}")
                return 1
            else:
                killed, total, score = score_data
                threshold = thresholds_by_path.get((CORE_PATH / module).resolve(), 80)
                print(f"\n📈 Score: {score:.1f}% ({killed}/{total} killed)")
                print(f"📊 Threshold: {threshold}%")
                if score < threshold:
                    print(f"❌ FAILED: Score {score:.1f}% below {threshold}% threshold")
                    exit_code = 2
                else:
                    print(f"✅ PASSED: Score {score:.1f}% meets {threshold}% threshold")

    print("\n" + "=" * 60)
    print("💡 Tips:")
    print("  - Use 'python -m mutmut show <id>' to inspect a mutant")
    print("  - Use 'python -m mutmut html' for an HTML report")
    print("  - Survived mutants reveal weak test assertions")
    print("=" * 60)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
