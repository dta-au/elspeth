# Collector node-kind vocabulary sweep — 2026-08-26

Raw data behind epic `elspeth-3da1474353` (43 GAPs — a lane-reported count with
no surviving enumeration; see the corrections below) and the refactor sizing in `elspeth-b3117ec3ac`. Preserved here because
the session scratchpad it was produced in is not durable.

Originally filed under `elspeth-04566b913d`, which was consolidated into
`elspeth-3da1474353` on 2026-08-26.

| file | what it is |
|---|---|
| `scan2.py.txt` | the detector: AST-block aggregation over five literal shapes; calibration is thin — see below |
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
`TOTAL BLOCKS: 381`, and the output matches `blocks.txt` exactly once trailing
whitespace is normalised (82 lines carry trailing spaces that were stripped when
the file was committed). Zero semantic drift. The 381 -> 297 reduction is a
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
   the lanes already triaged, and one is a COMMENT
   (`web/interpretation_state.py:76`) rather than a live comparison, which is
   why 164 + 33 = 197 and not 198. Only **33 across 14 files** are genuinely
   invisible. Widening the left-hand side to ANY operand — not just `node_type`
   (33 sites) but `kind` (26), `component_type` (12), `component_kind` (5),
   `owner_kind` (4), `plugin_type` (2) and others — the invisible set is
   **120 across 39 files**. Quote whichever you mean, with its denominator AND
   its LHS rule; naming only the rare operands yields roughly 40, not 120.

3. **The four-site calibration is thin — do not read confidence from it.** The
   epic records no calibration set and no artifact preserves one. Three sites
   were recoverable from the fix-class commits, and the detector handles all
   three correctly: `web/sessions/routes/_helpers.py:737`,
   `web/execution/preflight.py:208` and
   `web/composer/tools/transforms.py:577`/`:1418` all carry `collector` in `HAS`
   and were correctly excluded from the 297 as complete, while the known-open
   sibling `preflight.py:83` is correctly present as a candidate. But all three
   land in the same fix class, by the same author, on the same day
   (`8ee2a6be1`, `a7e7fb995`) — one calibration point wearing three hats. It
   shows the detector does not FALSE-POSITIVE on an already-complete block. It
   says nothing about false negatives, which is exactly where correction 2
   locates the real weakness. The sweep's credibility rests on the twelve
   personally-traced GAPs, not on this.

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

## Resolved: the guided-lane zero is EARNED for collector vocabulary

The original caveat said the guided-lane zero was scoped to 7 files and that
`guided/audit.py`, `_discovery.py`, `_display.py`, `errors.py`,
`intent_management.py`, `profile.py`, `resolved.py` and `stage_transitions.py`
were never opened. All eight have since been opened: **zero gaps**. None appears
in `blocks.txt` at all, and every one has zero `collector`, `node_type`,
`NodeType`, `PluginKind` and `node_kind` references. `stage_transitions.py` was
the strongest candidate — its bare `kind == "source"` sites are the
source/output component-review stage machine and it contains no node handling
whatsoever. This SHRINKS the epic's residual risk rather than widening it.
Scope: those eight files are cleared for collector node-kind vocabulary drift
only — they were not reviewed for anything else.

Re-running `scan2.py.txt` (rename to .py first) after the canonical-constants refactor is the cheapest way
to confirm the vocabulary sites actually collapsed.
