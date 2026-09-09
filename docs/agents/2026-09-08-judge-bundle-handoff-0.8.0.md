# 0.8.0 judge-bundle handoff (2026-09-08, second batch)

Continuation of [the initial judge-review analysis](2026-09-08-initial-judge-review-0.8.0-analysis.md).
This note records what landed on `release/0.8.0` after the first batch paused at
`3654ec604`, what evidence backs it, and what the operator has to fire so the
tier-model gate can go green and the branch can merge to `main`.

## What landed

| Commit | Change | Evidence |
| --- | --- | --- |
| `9be083850` | The three lifecycle defects deferred at the pause (`elspeth-ba3af151a7`, `elspeth-4844c270fa`, `elspeth-fa0e13545b`): `SessionOperationLease` now preserves every Tier-1 failure across child join, close, acquire/adopt cancellation and archive compensation. Tier-1 secondaries escape as instances grouped with the primary; ordinary secondaries keep the class-name-note redaction. | 13 regressions, each red on `3654ec604`; lifecycle module 65 passed; lease-adjacent selection 695 passed; full default suite 48,814 passed with 3 inherited reds. |
| `56e7ccaca` | Re-pins for the three inherited reds left by the docs sync `f24ec6869` (canonical hash corpus roster, deployment-platform docs wording, runtime-rejection parity header). | The three reproduced on a HEAD archive without any source change; 27 passed after re-pin. |
| `b1e8830b2` | `runtime_val_manifest._try_normalize_code_constant` let a `str`/`int`/`float` subclass constant hash identically to its base (the `primitive_subclass` arms were unreachable). `config_loading.load_settings` now declares the `@trust_boundary` its sibling already carried. | Regression fails 3/3 on the previous tree; contracts + config selections 426 passed; trust-boundary lints exit 0. |
| this commit | Corrects a source comment in `guided_operations.py` that claimed `type(x) is not C` gives no negative-branch narrowing (measured false under mypy by the review lane). | Comment only. |

PostgreSQL: `pytest tests/ -m testcontainer -n 0` on `56e7ccaca` gave 304 passed and one
teardown ERROR caused by a `stage_scan` writing under `.elspeth` during the run (the
`_refuse_in_repo_elspeth_writes` sweep, not the test); the single test reran green.
Never stage or annotate a bundle while a suite is running in the same checkout.

## The bundle, and why there are two rounds

The staged bundle is `.elspeth/staged-reviews/stage-scan-20260908-release-0.8.0-tail-r2.json`
(key-free, bound to the HEAD that carries this note). It holds 72 `stale_delete`,
94 `drift_repair` and 137 `justify` actions. Every `justify` action carries a
site-specific draft rationale; 23 `drift_repair` actions carry a fresh rationale
because the batch changed the code under them (the rest reuse their signed reason).

`tier_model_scan.routable_new_judgment_findings` deliberately defers an uncovered
finding whose identity prefix also has a `stale_delete` or `drift_repair` action in
the same bundle, so one bundle cannot both delete a stale entry and stage the
replacement judgment. 74 findings sit in that deferred set (15 in
`web/coordination/lifecycle.py`, 13 in `guided_operations.py`, 7 in
`web/execution/routes.py`, ...). Their rationales are already written and keyed
by the canonical finding key; after round one publishes, a fresh `stage_scan`
followed by `stage_annotate` with that map stages round two. The operator's own
first publication (424 accepted, 148 stale deletions) produced the same residue,
which is where most of this bundle's 137 `justify` actions come from.

## Where the rationales came from

| Source | Count | Notes |
| --- | --- | --- |
| Remediation-lane drafts recorded in `.claude/lanes/judge-blocks-20260908/` | 32 | The 88-block tail. |
| Historical ACCEPTED entries for the same function+rule, paired by quoted excerpt or AST path | 58 | Their fingerprints moved under the batch's edits; the judge re-rules against the live tree. |
| Fresh site reviews by eight read-only lanes (Opus) | 132 | 128 JUSTIFY, 4 DEFECT. All four defects are fixed in `b1e8830b2`; three of them share one root cause. Every cited test nodeid was collected. |
| Lifecycle rewrite | 17 | Written with the fix. |

Two review-lane observations worth keeping: a previously accepted rationale for
`reserve_or_replay_guided_operation` rested on a false typing claim and was not
reused; and the accepted history for `LandscapeDB._create_sqlcipher_engine`
defends a first-wins selection that HEAD now rejects outright, so the cohort
rationale was used instead.

## Operator steps

1. Verify the tree matches the bundle: `stage_status` on the bundle id (refuses a stale bundle).
2. Preview verdicts are non-authoritative and were recorded where `stage_preview` ran;
   BLOCKED previews list the sites to revisit before paying for the real call.
3. Fire round one with the key, dry-run first, then real:
   `elspeth-lints sign-bundle <bundle> --owner <id> --judge-transport codex-cli --judge-tools readonly --continue-on-block [--judge-concurrency N]`
   (paste the exact form from `stage_status`; exit 3 means published-with-blocks).
4. Re-stage round two from the published tree and annotate it with the prepared
   map (`round2-rationales-all.json` in the agent handoff); fire it the same way.
5. Rerun `elspeth-lints check --rules trust_tier.tier_model --root src/elspeth`;
   the remaining findings are BLOCKs to fix or re-justify, not drift.

Nothing has been pushed. No signature was minted or edited by an agent.
