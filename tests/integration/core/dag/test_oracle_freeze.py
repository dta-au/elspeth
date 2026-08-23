"""WS1 frozen-oracle gate (unified-lineage protocols plan §S1/§S2; spec §11).

Modes:
  default                       — recompute each case's frozen surface through the
                                  live harness and compare against the STORED
                                  snapshot bytes (the pre-flip record).
  ELSPETH_ORACLE_FREEZE=write   — (re)write snapshots. Legitimate ONLY at the
                                  recorded pre-flip freeze commit (protocols plan
                                  Task 3) or an §S3-adjudicated retirement; any
                                  other rewrite is oracle tampering.

CAMPAIGN INSTRUMENT: this suite re-runs every corpus harness case (~minutes).
It exists for the duration of the unified-lineage campaign and IS DELETED with
the campaign's closing slice (prerelease no-tech-debt posture; ratified
2026-08-22 — do not promote it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.fixtures.dag_scenario_corpus import oracle_freeze
from tests.fixtures.dag_scenario_corpus.harness import run_scenario_case
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.oracle_freeze import RETIRED_ROOT
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.dag_scenario_corpus.schema import BuildExpectation, HarnessCaseSpec, ScenarioSpec

_MANIFEST = load_manifest()
_CASES = [(scenario, case) for scenario, case in iter_harness_cases(_MANIFEST) if not isinstance(case.expected, BuildExpectation)]
_WRITE = os.environ.get("ELSPETH_ORACLE_FREEZE") == "write"


@pytest.mark.parametrize(
    ("scenario", "case"),
    _CASES,
    ids=[f"{scenario.id}--{case.id}" for scenario, case in _CASES],
)
def test_frozen_oracle_surface(
    scenario: ScenarioSpec,
    case: HarnessCaseSpec,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    classification = oracle_freeze.SCENARIO_CLASSIFICATION[scenario.id]
    if classification is oracle_freeze.OracleClass.CONTESTED:
        pytest.fail(
            f"Scenario {scenario.id!r} is CONTESTED in the oracle classification: "
            "maintainer adjudication is required before the freeze is "
            "authoritative (protocols plan §S1). No scenario is contested today; "
            "a new CONTESTED entry fails closed here by design."
        )

    if classification is oracle_freeze.OracleClass.RULING_CASUALTY_WS2:
        retired_migration = RETIRED_ROOT / scenario.id / "MIGRATION.md"
        if retired_migration.exists():
            # The class's own definition: RULING_CASUALTY_WS2 "leaves the
            # frozen set at WS2 only with an adjudicated MIGRATION.md" —
            # once retired, the live surface is deliberately incomparable to
            # the pre-migration snapshot (that is the whole point of the
            # migration), so the byte-identical/invariant-subset checks below
            # do not apply to this case any more.
            pytest.skip(
                f"{scenario.id!r} is a RULING_CASUALTY_WS2 scenario retired via §S3 "
                f"({retired_migration}); its migrated surface is no longer compared "
                "against the pre-migration frozen snapshot."
            )
        # Not yet retired: still frozen, compared exactly like FROZEN below —
        # a RULING_CASUALTY_WS2 classification alone grants no exemption.

    install_corpus_plugin_manager(monkeypatch)
    evidence = run_scenario_case(scenario, case, tmp_path)
    surface = oracle_freeze.frozen_surface(evidence)
    path = oracle_freeze.snapshot_path(scenario.id, case.id)

    if _WRITE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(oracle_freeze.canonical_bytes(surface))
        return

    if not path.exists():
        pytest.fail(
            f"No frozen snapshot at {path} — run the freeze writer at the recorded "
            "pre-flip commit (protocols plan Task 3) before the WS1b flip "
            "(WS1b Task 7) starts."
        )

    stored = path.read_bytes()
    if classification is oracle_freeze.OracleClass.REGENERATED_WS1:
        stored_surface = json.loads(stored.decode("ascii"))
        assert oracle_freeze.invariant_subset(surface) == oracle_freeze.invariant_subset(stored_surface), (
            f"REGENERATED_WS1 fixture {scenario.id}/{case.id} moved OUTSIDE the "
            "invariant subset (sink bytes / disposition multiset / run summary) — "
            "that is a behaviour delta, not the adjudicated ruling-27 representation delta."
        )
    else:
        assert oracle_freeze.canonical_bytes(surface) == stored, (
            f"FROZEN fixture {scenario.id}/{case.id} diverged from the pre-flip "
            "snapshot. Per spec §11 this fails the WS1 checkpoint: STOP, do not "
            "regenerate the snapshot, and surface the delta to the maintainer."
        )


def test_no_snapshot_exists_without_a_live_manifest_case() -> None:
    """A snapshot whose case left the manifest must go through §S3 retirement."""
    if not oracle_freeze.FREEZE_ROOT.exists():
        pytest.skip("freeze not yet executed (protocols plan Task 3)")
    live = {(scenario.id, case.id) for scenario, case in _CASES}
    stored = {(p.parent.name, p.stem) for p in oracle_freeze.FREEZE_ROOT.glob("*/*.json")}
    orphans = sorted(stored - live)
    assert orphans == [], (
        f"Snapshots with no live manifest case: {orphans} — retire them via "
        "protocols plan §S3 (git mv to oracle_freeze/retired/ + MIGRATION.md), "
        "never delete in place."
    )
