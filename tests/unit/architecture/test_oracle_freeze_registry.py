"""Registry pins for the unified-lineage frozen-oracle protocol (spec §11).

The classification table is the per-workstream oracle versioning authority:
FROZEN fixtures must diff byte-identical across WS1; REGENERATED_WS1 keeps an
always-frozen invariant subset; RULING_CASUALTY_WS2 leaves the frozen set at
WS2 only with an adjudicated MIGRATION.md; CONTESTED blocks the freeze until
the maintainer rules.
"""

import json

from tests.fixtures.dag_scenario_corpus.oracle_freeze import (
    RETIRED_ROOT,
    SCENARIO_CLASSIFICATION,
    OracleClass,
    canonical_bytes,
    invariant_subset,
)
from tests.fixtures.dag_scenario_corpus.schema import EXPECTED_SCENARIOS


def test_every_expected_scenario_is_classified_and_orphans_carry_migration_records() -> None:
    expected = {name for name, _title in EXPECTED_SCENARIOS}
    classified = set(SCENARIO_CLASSIFICATION)
    assert expected <= classified, f"unclassified scenarios: {sorted(expected - classified)}"
    for orphan in sorted(classified - expected):
        record = RETIRED_ROOT / orphan / "MIGRATION.md"
        assert record.exists(), (
            f"Scenario {orphan!r} left EXPECTED_SCENARIOS without an adjudicated "
            f"migration record at {record} — retirement must be distinguishable "
            f"from tampering (spec §11, protocols plan §S3)."
        )


def test_no_scenario_is_contested() -> None:
    # fork-multiple-terminals-partial-failure was adjudicated FROZEN (pure
    # fan-out, LEGAL per §7 rule 2; spec rev 3.2 corrected the rev 3.1
    # misnaming). CONTESTED remains as the fail-closed class for any FUTURE
    # unadjudicated entry — the set must stay empty.
    contested = sorted(n for n, c in SCENARIO_CLASSIFICATION.items() if c is OracleClass.CONTESTED)
    assert contested == []


def test_ws1_and_ws2_delta_classes_match_the_ratified_adjudication() -> None:
    assert SCENARIO_CLASSIFICATION["row-union-interleave"] is OracleClass.REGENERATED_WS1
    assert SCENARIO_CLASSIFICATION["parallel-coalesces"] is OracleClass.RULING_CASUALTY_WS2
    # Spec rev 3.2: pure fan-out, fully unbound, stays legal/frozen — the
    # unbound-fork audit shape must survive the WS1b flip byte-identical.
    assert SCENARIO_CLASSIFICATION["fork-multiple-terminals-partial-failure"] is OracleClass.FROZEN


def test_invariant_subset_excludes_token_keys_but_pins_sink_bytes_and_dispositions() -> None:
    surface = {
        "scenario_id": "s",
        "case_id": "c",
        "status": "completed",
        "rows": [2, 1, 1],
        "sink_outputs": [{"sink_name": "out", "rows": ['{"a":1}']}],
        "tokens": [{"key": "src:0#0"}],
        "terminal_dispositions": [
            {
                "key": "k1",
                "token_key": "src:0#0",
                "outcome": "success",
                "path": "coalesced",
                "sink_name": "out",
            }
        ],
        "expansions": [],
    }
    reshuffled = dict(
        surface,
        tokens=[{"key": "src:0#1"}],
        terminal_dispositions=[dict(surface["terminal_dispositions"][0], key="k9", token_key="src:0#1")],
    )
    assert invariant_subset(surface) == invariant_subset(reshuffled)
    changed_sink = dict(surface, sink_outputs=[{"sink_name": "out", "rows": ['{"a":2}']}])
    assert invariant_subset(surface) != invariant_subset(changed_sink)
    changed_path = dict(
        surface,
        terminal_dispositions=[dict(surface["terminal_dispositions"][0], path="scope_group_failed")],
    )
    assert invariant_subset(surface) != invariant_subset(changed_path)


def test_canonical_bytes_are_deterministic_and_newline_terminated() -> None:
    surface = {"b": 1, "a": [None, "x"]}
    first = canonical_bytes(surface)
    assert first == canonical_bytes(json.loads(first.decode("ascii")))
    assert first.endswith(b"\n")
