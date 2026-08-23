# Retirement: parallel-coalesces (pre-flip oracle snapshot)

**Ruling:** Ruling 23 (spec §7 rule 2, whole-roster fork closure). The pre-flip topology —
one fork gate (`parallel_fork`) closing at TWO sibling coalesces (`merge_left`, `merge_right`)
from a single flat `fork_to: [left_a, left_b, right_a, right_b]` — is a build-time
`GraphValidationError` ("closes at multiple barriers") under rule 2, landed with this commit's
DAG builder change.

**Date:** 2026-08-23.

**Adjudicator:** Panel synthesis (`.superpowers/sdd/2026-08-21-unified-lineage-ws2-config-validation/panel/synthesis.md`),
maintainer-ratified. RC-3 (`docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md:805-814`) names the
replacement topology; the panel resolved a transcription-drift ambiguity between two candidate readings
in favour of RC-3's own text.

**Replacement scenario:** Same scenario id (`parallel-coalesces`), same case ids
(`two-parallel-require-all`, `resume-after-left-finalize`) — only the fixture topology changed, in
`tests/fixtures/dag_scenario_corpus/v1/parallel-coalesces/two-parallel-require-all.yaml`. The new
topology (topology B): an outer pure-fan-out fork (`outer_fork`, `fork_to: [left_path, right_path]`)
opens two DISJOINT depth-1 regions — `left_path`/`right_path` are unbound (consumer-fed, spec §7 E2:
a fork branch may feed an ordinary downstream gate), each feeding its own inner whole-roster fork
(`left_fork`/`right_fork`) that closes at its own require-all nested coalesce (`merge_left`/`merge_right`)
with its own sink (`left`/`right`). By spec decision 6 (an unbound fork opens no region), this is two
disjoint depth-1 regions, NOT depth-2 nesting — RC-3's own "nested" phrasing in the protocols plan is
corrected in the rotation ledger note beside `EXPECTED_EVIDENCE_REGISTRY_SHA256`
(`tests/unit/architecture/test_dag_scenario_corpus_contract.py`). True depth-2 corpus coverage
(a fork nested INSIDE another fork's branch) remains WS1a Task 8a's job, blocked on the separate
E1a/E1b engine defects (branch-endpoint walker + outcome router), not this migration.

The recovery case's own fault-injection semantics (first sink finalizes, second sink held, resume
reuses the finalized effect/artifact idempotently) are UNCHANGED — the two-sink topology this case
depends on survives intact under topology B. Only its graph-shape assertions (node/edge/token/parent
counts) were updated to the new topology's real observed values, captured via the corpus harness
(never hand-computed).

**Manifest-rotation commit:** this commit (Task 6 + E2, branch `feature/unified-lineage`) —
`docs/architecture/dag/scenario-corpus/v1/manifest.yaml`'s `parallel-coalesces` scenario entry,
`tests/unit/architecture/test_dag_scenario_corpus_contract.py`'s `EXPECTED_EVIDENCE_REGISTRY_SHA256`/
`EXPECTED_CASE_REGISTRY_SHA256`/`EXPECTED_CASE_FIXTURE_SHA256`/`EXPECTED_PARALLEL_COALESCE_YAML` rotation,
and `tests/integration/core/dag/test_oracle_freeze.py`'s new `RULING_CASUALTY_WS2` retirement-exemption
arm (this scenario's pre-flip snapshot, retired here, is the first live use of that arm).

**Retired snapshots (this directory):** `two-parallel-require-all.json`, `resume-after-left-finalize.json` —
the pre-flip (WS1) frozen surfaces for the OLD topology. Never rewritten, never deleted; kept as the
permanent pre-migration record per protocols plan §S3.
