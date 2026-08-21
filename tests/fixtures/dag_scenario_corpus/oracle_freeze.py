"""Frozen-oracle registry for the unified-lineage campaign (spec §11).

Snapshots live at oracle_freeze/v1/<scenario_id>/<case_id>.json, written ONCE
at the recorded pre-flip HEAD — after WS1a Task 8a's nested fixtures land,
before the WS1b flip commit (WS1b Task 7) begins (ELSPETH_ORACLE_FREEZE=write).
The stored bytes — not the harness that produced them — are the WS1 delta
oracle: the harness itself reads the retired tri-columns and is rewritten at
the flip, and a rebuilt harness verifying its own rebuild is the self-oracle
shape §11 forbids.

The surface deliberately excludes audit_records and manifest blobs, so the new
audit tables (token_lineage_frames, group_records, group_losses) never churn
frozen bytes (fixture-oracle scout, Risk 4).
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.dag_scenario_corpus.schema import ScenarioRunEvidence

FREEZE_ROOT = Path(__file__).parent / "oracle_freeze" / "v1"
RETIRED_ROOT = Path(__file__).parent / "oracle_freeze" / "retired"


class OracleClass(enum.Enum):
    """Per-workstream oracle disposition (spec §11 vocabulary)."""

    FROZEN = "frozen"
    REGENERATED_WS1 = "regenerated-ws1"
    RULING_CASUALTY_WS2 = "ruling-casualty-ws2"
    CONTESTED = "contested"


SCENARIO_CLASSIFICATION: dict[str, OracleClass] = {
    "linear": OracleClass.FROZEN,
    "multiple-independent-sources": OracleClass.FROZEN,
    "multi-source-queue-fan-in": OracleClass.FROZEN,
    "conditional-routing": OracleClass.FROZEN,
    # Pure fan-out (both branches direct to sinks, no closer) — LEGAL per §7
    # rule 2. Spec rev 3.2 corrected the rev 3.1 "mixed-fork r23 casualty"
    # misnaming; FROZEN permanently, and the unbound-fork audit shape must
    # survive the WS1b flip byte-identical. (parallel-coalesces below is the
    # actual r23 casualty.)
    "fork-multiple-terminals-partial-failure": OracleClass.FROZEN,
    "fork-coalesce-policies": OracleClass.FROZEN,
    # Sequential, not nested: two depth-1 regions in series. Frozen as-is; the
    # genuinely nested differential fixtures (fork-in-fork depth-2,
    # expand-in-fork) are authored by WS1a Task 8a pre-flip and join this
    # table as FROZEN entries (protocols plan §S1 ordering).
    "sequential-nested-fork-coalesce": OracleClass.FROZEN,
    # ONE fork closing at TWO sibling coalesces — ruling 23 casualty, leaves the
    # frozen set at WS2 via §S3.
    "parallel-coalesces": OracleClass.RULING_CASUALTY_WS2,
    "aggregation-immutable-batch": OracleClass.FROZEN,
    "row-expansion-parent-child-recovery": OracleClass.FROZEN,
    # Ruling 27 pops branch frames on release: released tokens' branch_name goes
    # to None, and branch_name is a harness SORT key, so token-key ordinals can
    # reshuffle — a whole-projection WS1 delta adjudicated by hand (§S1), with
    # the invariant subset below enforced automatically forever.
    "row-union-interleave": OracleClass.REGENERATED_WS1,
    "retry-quarantine-discard-routed-errors": OracleClass.FROZEN,
    "sink-write-pending-redrive": OracleClass.FROZEN,
    "checkpoint-deterministic-resume": OracleClass.FROZEN,
    # pytest-evidence only: no v1 fixture directory, no harness cases to snapshot.
    "multi-worker-lease-reclaim-late-completion": OracleClass.FROZEN,
}


def frozen_surface(evidence: ScenarioRunEvidence) -> dict[str, object]:
    """The §11 frozen-invariant slice of one harness case's run evidence.

    Exactly the four stable projection classes plus the run summary — never
    audit_records, never manifest material, never raw group ids.
    """
    runtime = evidence.runtime
    surface: dict[str, object] = {
        "scenario_id": evidence.scenario_id,
        "case_id": evidence.case_id,
        "status": runtime.status,
        "rows": [runtime.rows_processed, runtime.rows_succeeded, runtime.rows_failed],
        "sink_outputs": [s.model_dump(mode="json") for s in runtime.sink_outputs],
    }
    projection = runtime.durable_projection
    if projection is not None:
        surface["tokens"] = [t.model_dump(mode="json") for t in projection.tokens]
        surface["terminal_dispositions"] = [d.model_dump(mode="json") for d in projection.terminal_dispositions]
        surface["expansions"] = [e.model_dump(mode="json") for e in projection.expansions]
    return surface


def canonical_bytes(surface: Mapping[str, object]) -> bytes:
    return json.dumps(surface, sort_keys=True, indent=1, ensure_ascii=True).encode("ascii") + b"\n"


def snapshot_path(scenario_id: str, case_id: str) -> Path:
    return FREEZE_ROOT / scenario_id / f"{case_id}.json"


def invariant_subset(surface: Mapping[str, object]) -> dict[str, object]:
    """The always-frozen slice for REGENERATED_WS1 fixtures.

    Ruling 27's pop may reshuffle token-key ordinals (branch_name is a harness
    sort key), so token keys and disposition keys are excluded; sink bytes,
    run summary, and the disposition MULTISET of (outcome, path, sink_name,
    error_hash) must never move.
    """
    dispositions = surface.get("terminal_dispositions") or []
    multiset = sorted(
        json.dumps(
            [
                entry.get("outcome"),
                entry.get("path"),
                entry.get("sink_name"),
                entry.get("error_hash"),
            ]
        )
        for entry in dispositions
    )
    return {
        "status": surface.get("status"),
        "rows": surface.get("rows"),
        "sink_outputs": surface.get("sink_outputs"),
        "disposition_multiset": multiset,
    }
