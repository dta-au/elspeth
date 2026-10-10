**Current status:** Actual Daybreak Blue final plan review is GO, CLI exit 0, with B1–B5 closed. See [current reconciliation](reconciliation-2026-10-06.md) and the [complete verdict](daybreak-plan-final-verdict-2026-10-06.md). The following is retained prior-baseline evidence; its old pending statements are historical. Application implementation and original-report recovery remain incomplete.

# Recovered retry candidate: custody and async-plan integration — 2026-10-06

**Dependency assessment complete; application remains on hold.** The recovered patch is readable, matches the supplied hashes, and passes `git apply --check` on the isolated streaming branch. It has not been applied, committed, tested locally, pushed or merged. This assessment does not satisfy genuine Daybreak Blue plan review or replace the complete source-artifact handoff.

## Exact artifact and verified custody

- Library identity: `libfile_ff9ba56b20b8819192b080411b2352cc`, file ID `file_00000000853481faa7594ced201c5247`, filename `elspeth-retry-cleanup-handoff.zip`, retained version `0`.
- Consumer-local bundle: `<consumer-workspace>/retry-custody/elspeth-retry-cleanup-handoff.zip`; extracted evidence and full candidate files under `retry-custody/extracted/`.
- Producer branch: `fix/composer-retry-recovery-20261005`, **uncommitted candidate**, base `edc844699a350a90a624089e12a5a1e75b7b2dde`. There is no candidate commit SHA to invent.
- Received using the current Library resolved-reference materialization helper. The initial sandboxed download failed; the same supported helper completed through the ordinary authorized network escalation. No guessed URL, alternate executor or lost Library metadata.

Measured local output:

```text
bundle_sha256=1f4075150ea5fc6b6da1b32c78384943ed34cac39ab87013c5009134548cedf4
library_identity=libfile_ff9ba56b20b8819192b080411b2352cc
library_version=0
zip_crc_check=None
patch_bytes=124398
patch_sha256=fc3490d436ad834a64c7a1193c920963b8599a8318524db60479cf483ce57bfd
changed-file manifest bytes/hashes verified=12
```

The supplied bundle/patch hashes match. `sha256sum --check SHA256SUMS` exited 0 with every package entry OK. Each changed-file snapshot's byte length and SHA-256 also matches the producer manifest. ZIP paths were validated against extraction-root escape and symlinks before extraction. The archive, exact patch, full changed files, reviews, successful logs and failed preliminary-run evidence are retained without modification. Preserve the Library item and original source artifacts; do not overwrite the streaming tree with base-relative full-file snapshots.

The producer states current patch exactly matches its final delivered patch, source/tests were unchanged after independent review, and only documentation validation was updated later. **No historical review-time tree hash was recorded.** Verified current package hashes cannot retroactively prove that review-time tree. Retain that limitation through any later integration/review.

## Current main and independent dependencies

A fresh fetch again returned `origin/main=edc844699a350a90a624089e12a5a1e75b7b2dde`; the isolated branch remains at that SHA. Read-only PR APIs returned:

```text
PR275 OPEN draft head=240f71d4956f6fa1953a9d35fe41072e11a9b2a8
PR276 OPEN draft head=bba870e35d90d301f1485c6130389cd30b40767f
retry_path_overlap(PR275)=[]
retry_path_overlap(PR276)=[]
```

Path intersections were calculated from the candidate manifest and current PR changed-file APIs, with known-positive and known-negative intersection controls. No direct path overlap does not establish semantic independence: retry eligibility relies on sessions/locking/leases that PR276 changes, while the future async work changes PR276's app/service/coordination seams. PR276 advanced from the earlier `d58482...` assessment and now includes session mutation fencing PostgreSQL and mutation-authority test changes. Leave both PR owners and main-bound merge authority intact, and consume their landed main after refresh. No dependency integration, PR mutation or merge occurred.

## Concrete overlap with the async plan

`git apply --numstat` provides the exact artifact inventory:

```text
24  6   src/elspeth/web/frontend/src/api/client.ts
18  0   src/elspeth/web/frontend/src/components/chat/MessageBubble.test.tsx
2   1   src/elspeth/web/frontend/src/components/chat/MessageBubble.tsx
9   0   src/elspeth/web/frontend/src/config/composer.ts
331 2   src/elspeth/web/frontend/src/stores/sessionStore.test.ts
225 163 src/elspeth/web/frontend/src/stores/sessionStore.ts
28  0   src/elspeth/web/frontend/src/test/composerOwnership.integration.test.tsx
41  19  src/elspeth/web/sessions/routes/composer/compose.py
146 0   docs/reviews/2026-10-05-composer-retry-recovery-cleanup.md
79  0   docs/reviews/2026-10-05-composer-timeout-investigation.md
138 0   src/elspeth/web/frontend/src/api/client.messages.test.ts
257 0   tests/unit/web/sessions/test_composer_retry_recovery.py
```

No package/lock path appears in this manifest or patch inventory. `git apply --check` exited 0 against the held branch. A clean application check is only textual feasibility at this base, not approval or combined-runtime proof.

| Candidate behavior / path | Integration disposition |
|---|---|
| Backend `compose.py` replaces last-row-is-user requirement with exact latest-user identity, no-tool assistant terminal guard, and exact same-user pending/committed pipeline-proposal guard | **Blocking plan reconciliation:** old N06 example at lines 2051–2059 rejects any non-user tail. It would reject the candidate's valid partial tool prefix and omit saved-outcome refusals. Contract E5/refusal table, N06, N10 handler parity, N11b turn/history and backend tests must all use the chosen candidate-aware semantics. Preserve serialization and refusal before provider work/side effects; rejected proposals do not block fresh explicit work. |
| Partial tool-call narration keeps original user before its prefix in ordered history; compose receives intent as fresh input against saved current state | Carry into N11b/history parity tests. Do not mechanically replay stored tools, duplicate user ingress, delete audit history or describe this as continuing an interrupted provider request. |
| `api/client.ts` retrieves sequential authenticated 500-row offset pages, preserves identity/order, rejects failed partial traversal and non-advancing full pages | Retain complete-history retrieval during N15 client refactor; retain new paging tests including >100/>1000, boundary, duplicates, authentication and abort behavior. It is separate from transport settlement. |
| `sessionStore.ts` treats transport/unstructured 504/524 as ambiguous, matches exact send/receipt identity, and automatically performs GET-only reconciliation | N14/N15 remove ingress 409 and `client_request_id` paths and replace identity with durable `operation_id`. Preserve invariants/tests, but replace obsolete implementation with operation GET/immutable submitted-body custody and separate observation-only attachment. Do not retain session progress/inflight as settlement authority. A later active-operation refusal does not resolve an earlier SQL admission that can still commit. |
| Unknown/missing progress or count and complete-without-final mixtures stay refresh-only; transient GET failures schedule another fenced read; older same-owner snapshots cannot overwrite newer state | Carry into the common stream/poll observer and reducers. Durable job result replaces advisory counts as authority; unknown stream state never starts compute or erases custody. Retain regression cases for the two fixed independent-review defects. |
| Explicit settled partial-turn Retry may invoke `/recompose`; saved replies/proposals cause GET refresh | For durable operations, an explicit **new** action gets a new operation ID/current bound head; recovery of ambiguous submission retains earlier exact ID/body. Keep this distinction in UI wording and tests. Automatic reconnect/refresh cannot dispatch providers. |
| `config/composer.ts` refresh-only codes and `MessageBubble.tsx` Refresh/Retry labels | Do not lose the read-only affordance when N15 deletes timeout helpers/config tests. Relocate/retain the semantic discriminator around operation outcome, saved-result guards and explicit new computation. |
| Navigation, generation, Stop-preflight and reset protections in store/ownership tests | Migrate to principal/provider-bound custody and one observer. Preserve auth/session ownership generations, logout cleanup, no POST after Stop during preflight, no stale terminal reducer application. |

The original source finding 9 describes protections already on main; the candidate intentionally strengthens/replaces some of them to support partial tool prefixes. Retaining the old **exact** non-user-tail predicate would be a regression. Preserve its safety purpose with the candidate's latest-user/saved-outcome semantics and review the combined contract explicitly.

## Evidence inherited from producer, not Nyx test results

Read the packaged independent acceptance/final reviews and final runner logs. Producer reports exit 0 for the final frontend run, frozen focused backend run and PostgreSQL lifecycle run. Stored raw runner summaries are:

```text
Test Files 248 passed (248)
Tests 4058 passed (4058)
337 passed in 102.89s
10 passed in 78.22s
```

These are historical producer evidence; no suites ran here. Producer also reports static/baseline controls and no added/removed keyless lint diagnostics/warnings, with scanner exit 1 on both trees. Shape-only comparison is not operator signature clearance. Broad backend selection was **not** frozen/green: producer records 3873 passed with 3 fixture failures, 3 skips and 1 expected failure, followed by fixture correction and the focused frozen rerun. Full Python and full PostgreSQL selections were not run. The ordinary frontend dependency precheck also failed on untouched main; do not hide that baseline limitation or duplicate PR275 dependency repair.

The final independent review found no remaining must-fix source defect in that candidate, after correcting (1) GET failure stopping one-shot recovery and (2) stale recovery overwriting newer messages. This is not a Daybreak Blue review of the final streaming plan or combined code.

Retain its actual boundaries:

1. Explicit retry is fresh computation and may repeat non-idempotent effects (including blob creation); no exactly-once effect guarantee or durable attempt receipt exists in this synchronous candidate.
2. Proposal browser wire lacks originating user identity; backend exact association remains authoritative, and reload may initially offer Retry before saved-proposal refusal leads to retrieval.
3. Reload cannot reconstruct every permanent failure code (for example planner policy versus provider fault) from old progress vocabulary; backend policy remains authority.
4. Message/state/proposal/progress reads are non-atomic; mixed snapshots are treated conservatively. The future job/result authority must close the planned terminal-publication seam.
5. Cancellation/disconnect still control baseline work. The candidate supplies no server-owned durable 202 operation or guarantee of a final reply after disconnect.
6. Deployed build/middlebox/timeouts remain unknown; no production/provider/infrastructure work is evidence here.

## Next disposition and hold

The exact retry candidate gap is closed for consumer custody and dependency assessment. This closes neither original-report recovery nor expanded-plan export custody. All 19 numbered expanded-export parts are now [recorded separately](pending-task-export-2026-10-06.md), and the [23 proposed documents](draft-export-2026-10-06/README.md) are reconstructed and custody-verified. Reconciliation remains pending.

After complete handoff and readiness approval, integrate the **exact patch** on the held review branch in a controlled step, then rebuild async changes around these invariants; do not duplicate or silently drop the retry fix. Revalidate affected candidate tests against current main/dependencies, add combined operation/stream cases, and run required whole-tree/PostgreSQL gates with coordinated capacity. Obtain real Daybreak Blue (`gpt-daybreak-blue-latest`) plan review after payload approval and before implementation. John retains the local-testing/no-merge hold.

## Current reconciliation update

The [current-source reconciliation](reconciliation-2026-10-06.md) now records resolved draft errata, restored active tasks, exact candidate-aware recompose/custody semantics, local dependency preparation and completed bounded baseline results. Earlier pending statements above are historical observations. Original two-review coverage, product scope, remaining runtime evidence, review-payload approval and actual Daybreak Blue review remain open; candidate/application code remains unapplied and the no-merge/local-testing hold remains.

## Independent N00 measurement update

See [current measurement evidence](n00-measurements-2026-10-06.md) and [registered handlers/direct exits](appendix-a-2026-10-06.md). These supersede earlier pending-baseline statuses only within their measured scope. Browser body/aggregate proposals now use 512 KiB / 1 MiB after current accepted fixtures exceeded 256 KiB. Product choice, original two-report custody, review-payload authorization and actual Daybreak Blue plan review remain open; implementation is NO-GO. Original raw findings, complete historical task analyses and exact unapplied retry evidence remain retained.
