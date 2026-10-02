---
title: Collector vocabulary parity gaps outside the composer authoring path
labels: [area/composer, area/web, type/epic, needs-triage]
---

A pipeline node has a kind — `source`, `transform`, `aggregation`, `collector`, `gate`, `coalesce`, `row_union`, `queue`. `collector` is the most recent of them. Code that branches on node kind has to be updated for each new kind, and an inventory run on 2026-08-26 set out to find every place that had not been. It is not currently safe to work from: two of the three findings it ranked highest are not observable in the current tree.

## Background

A collector is a barrier node that buffers every member of one group and flushes when the group ends. It reuses the same plugin contract aggregations use, so anywhere the code asks "is this node a transform or an aggregation?" to decide whether a node carries a plugin, a collector should usually be in the answer too. It is defined in `src/elspeth/core/config.py` as `CollectorSettings`.

## What the inventory found

It opened 297 production sites that dispatch on node kind and classified 43 as parity gaps for `collector`, 113 as deliberate exclusions, 3 unclear and 138 not applicable. Earlier sweeps had all been scoped to the composer's authoring path; this one deliberately reached outside it, into `src/elspeth/core/` and the web surfaces. The detector aggregated by syntax-tree block over five literal shapes and was calibrated against four known sites, all four of which it caught.

## What no longer matches the tree

- The headline was that `src/elspeth/core/config.py` wrote collector plugin options into the audit snapshot without redacting secrets. The audit-redaction function in that file now has an explicit `collectors[*].options` arm, with a comment noting that collectors carry the same arbitrary secret-bearing option shape as a transform. The behaviour described is not present.
- The second was a disagreement between two hand-written sets of plugin-free node kinds — `_PLUGINLESS_STRUCTURAL_NODE_TYPES` in `src/elspeth/web/composer/state.py` and `_STRUCTURAL_NODE_TYPES` in the guided chat intent-management module. The comment block at `src/elspeth/web/audit_readiness/service.py:833-860` records this as a corrected reading: the first is a strict subset, not a conflict, and the second states the same rule reached from the other direction.

The third highest-ranked finding, a type annotation on the shareable-review surface, has not been re-checked here.

## Limitations, which still apply

1. **43 is a floor.** A lone `if node.node_type == "transform":` with no second kind token nearby is invisible to the detector. The inventory counted 198 such comparisons across 24 Python files. Five of the 43 gaps — including three security checks — were caught only because unrelated kind tokens happened to sit close by. Nothing systematically covers this shape.
2. **The guided-path result is partial.** That part of the sweep reported no gaps, but scoped its verdict to seven files and never opened `audit.py`, `_discovery.py`, `_display.py`, `errors.py`, `intent_management.py`, `profile.py`, `resolved.py` or `stage_transitions.py` under `src/elspeth/web/composer/guided/`. It is not clearance for that directory.
3. **Verification depth varies.** Twelve gaps were opened, read and traced across files. The remaining 31, and all 113 deliberate classifications, come from sub-reports that were not re-verified to the same standard. Nothing was inferred from naming alone.

## Sizing note

A related refactor — collapsing the duplicated kind vocabularies into shared constants — was measured at roughly 53 sites, well above an earlier estimate of 15 to 25. `PluginKind` is declared three times in Python (`web/plugin_policy/models.py`, `web/catalog/protocol.py`, `web/catalog/schemas.py`) and once more in the frontend, while `COMPOSER_NODE_TYPES` already exists in `web/composer/state.py` alongside independent re-declarations of the same set. The per-constant usage counts in the original measurement no longer match the tree.

## Scope

This is a large piece of work and the first step is not code.

1. Re-run the detector against the current tree and produce a fresh list. The original scan scripts were not kept in the repository, so this means writing the detector again — it is a syntax-tree walk for blocks comparing `node_type` against literal kind names, and it should be committed this time, under `tests/` or `scripts/`, so the next person does not repeat this.
2. Calibrate it before trusting it: plant a known gap and confirm it goes red, and confirm it stays green on a site that already handles collectors.
3. Split the surviving gaps into children by subsystem, worst-first. The inventory's own ranking was: silent wrong behaviour, then crash, then over-strict refusal, with confidentiality outranking all three.
4. Separately, decide whether the single-kind comparison shape in limitation 1 is worth a lint rule. That is a design decision nobody has taken, and 198 sites is too many to review by hand repeatedly.

Done looks like a committed, calibrated detector plus a child issue per surviving gap — not a single large change.

## Note

Because two of three headline findings do not reproduce, treat the counts above as a dated measurement rather than a current backlog.
