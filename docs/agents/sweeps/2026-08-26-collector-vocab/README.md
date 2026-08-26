# Collector node-kind vocabulary sweep — 2026-08-26

Raw data behind epic `elspeth-3da1474353` (43 GAPs; see the count corrections
below) and the refactor sizing in `elspeth-b3117ec3ac`. Preserved here because
the session scratchpad it was produced in is not durable.

Originally filed under `elspeth-04566b913d`, which was consolidated into
`elspeth-3da1474353` on 2026-08-26.

| file | what it is |
|---|---|
| `scan2.py.txt` | the detector: AST-block aggregation over five literal shapes, calibrated against four known sites |
| `blocks.txt` | all 381 blocks containing >=2 distinct kind tokens |
| `vocab_full.txt` | full-vocabulary enumerations (1 found) |
| `vocab_subsets.txt` | the 149 proper-subset collections, in 28 shapes — the sizing measurement |
| `L1_composer.txt`, `L2_guided.txt`, `L3_webrest.txt`, `L4_engine.txt`, `L5_frontend.txt` | per-lane candidate lists |

`L3_webrest.txt` was missing from the first rescue and was recovered separately
(commit `fd496cef9`). It is the web/REST lane — the one carrying the
shareable-review 500 — so any statement that the L3 findings survive only in the
tickets predates that commit. Its provenance was independently confirmed: the
falsification pass re-derived the L3 row set mechanically from `blocks.txt` and
obtained 73 rows **set-identical** to the rescued file.

## Corrections from the 2026-08-26 falsification pass

The detector itself is SOUND: re-running `scan2.py.txt` unmodified reproduces
`TOTAL BLOCKS: 381` byte-identically, zero drift. The 381 -> 297 reduction is a
principled rule (the 297 are exactly the blocks whose `HAS` lacks `collector`;
of the 84 excluded, 76 already mention collector and the 8 residuals are test
fixtures). No production block was silently dropped. Two DESCRIPTIONS of what
was counted are wrong:

1. **"297 production sites" is wrong — it is 257.** Forty of the 297 are
   frontend test/spec files, all in L5. Gap rate is therefore 43/257 = 16.7%,
   not 14.5%. Test handling was inconsistent: 8 test blocks dropped, 40 kept.
2. **"198 bare comparisons across 24 files" mis-sizes the residual.** The 198
   reproduces exactly under `node_type ==/!= "<kind>"`, so it is not invented —
   but 164 of those sit inside >=2-kind blocks the detector already emitted and
   the lanes already triaged. Only **33 across 14 files** are genuinely
   invisible. On a broader, defensible left-hand side (`component_kind`,
   `target.kind`, `plugin_type`) the invisible set is **120 across 39 files**.
   Quote whichever you mean, with its denominator.

## Two limitations to carry forward

1. **The detector cannot see a bare single-kind check.** A lone
   `if node.node_type == "transform":` with no second kind token in the same AST
   block is invisible to it, and five confirmed GAPs were caught only by
   coincidence. **43 is a floor, not a total.** See correction 2 above for the
   size of the invisible set.
2. **The DELIBERATE bucket is not auditable.** The 113 DELIBERATE
   classifications are enumerated in no surviving artifact — the rescued lane
   files carry only `HAS=`/`MISS=` columns, with no GAP/DELIBERATE/NA label, and
   the scratchpad is gone. Individual rulings survive in prose on some children,
   but the bucket as a whole cannot be sampled. A sample of six worst-looking
   rows from the 254-row non-GAP residue found all six correctly classified.
   **If this sweep is re-run, have the detector emit the classification
   alongside each row.**

## Resolved: the guided-lane zero is now EARNED, not partial

The original caveat said the guided-lane zero was scoped to 7 files and that
`guided/audit.py`, `_discovery.py`, `_display.py`, `errors.py`,
`intent_management.py`, `profile.py`, `resolved.py` and `stage_transitions.py`
were never opened. All eight have since been opened: **zero gaps**. None appears
in `blocks.txt` at all, and every one has zero `collector`, `node_type`,
`NodeType`, `PluginKind` and `node_kind` references. `stage_transitions.py` was
the strongest candidate — its bare `kind == "source"` sites are the
source/output component-review stage machine and it contains no node handling
whatsoever. This SHRINKS the epic's residual risk rather than widening it.

Re-running `scan2.py.txt` (rename to .py first) after the canonical-constants refactor is the cheapest way
to confirm the vocabulary sites actually collapsed.
