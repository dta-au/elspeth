# Collector node-kind vocabulary sweep — 2026-08-26

Raw data behind epic `elspeth-04566b913d` (43 GAPs over 297 production sites)
and the refactor sizing in `elspeth-b3117ec3ac`. Preserved here because the
session scratchpad it was produced in is not durable.

| file | what it is |
|---|---|
| `scan2.py.txt` | the detector: AST-block aggregation over five literal shapes, calibrated against four known sites |
| `blocks.txt` | all 381 blocks containing >=2 distinct kind tokens |
| `vocab_full.txt` | full-vocabulary enumerations (1 found) |
| `vocab_subsets.txt` | the 149 proper-subset collections, in 28 shapes — the sizing measurement |
| `L1_composer.txt`, `L2_guided.txt`, `L4_engine.txt`, `L5_frontend.txt` | per-lane candidate lists |

`L3_web.txt` was not recovered from the scratchpad; the web-lane findings survive
in the tickets themselves (notably the audit secret leak and the shareable-review
500, both independently re-verified).

## Two limitations to carry forward

1. **The detector cannot see a bare single-kind check.** A lone
   `if node.node_type == "transform":` with no second kind token in the same AST
   block is invisible to it. There are 198 such comparisons across 24 Python
   files, and five confirmed GAPs were caught only by coincidence. **43 is a
   floor, not a total.**
2. **The guided-lane zero is partial** — scoped to 7 files; `guided/audit.py`,
   `_discovery.py`, `_display.py`, `errors.py`, `intent_management.py`,
   `profile.py`, `resolved.py` and `stage_transitions.py` were never opened.

Re-running `scan2.py.txt` (rename to .py first) after the canonical-constants refactor is the cheapest way
to confirm the vocabulary sites actually collapsed.
