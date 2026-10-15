# Composer 0.8.7 — paused status and resume point

Current work is **active** under the chronology correction below. Quota integration
was accepted and committed locally as `9d6533fe`; current progress and remaining
fixes are in [the work ledger](2026-10-15-composer-087-work-ledger.md) and the ignored
coordinator record. The pause/source/Git snapshots below remain historical.

Source1c has bounded acceptance from both independent and systems reviews:
104 affected behavior cases,436 gate cases and40 focused PostgreSQL cases pass;
78 witnessed deliberate failures cover21 regressions. Local Git integration is
next. Its original1191-pass/46-failure/2-teardown-error run remains historical
red evidence. The3277-finding keyless scan has no semantic drift and is reused
only for unchanged production inputs. Whole-plan and broader gates remain open.

**Historical pause report — resumed after chronology correction and fresh
readiness checks at 2026-10-15 20:17 UTC.** The 20:05 checkpoint reprocessed the
original pause; it was not a new direct user instruction. Preserve the report
below as history, but use this correction and coordinator state for execution.

The authorized Emmy bridge supplied the verified sequence:

1. Direct pause message `01a140f6-df63-7ac2-bf00-c51f6cb32f67` has UUIDv7-derived
   time **2026-10-15 19:08:03.299 UTC**. The API exposes no separate message
   timestamp; the original pause report recorded about 19:09 UTC.
2. John's later conditional continuation at **19:11:41 UTC**, message
   `Sentinel_45dc3ea305d081919a36fdb9f7503db3`, said to keep .7 working if healthy.
3. The existing owner acknowledged and resumed in turn
   `01a1410b-b114-78d0-8e9e-1d899f568edb` at **19:30:47 UTC**. A fresh supported
   thread read confirms its 171 items contain no new direct user messages.
4. Compaction `01a14124-ad80-7330-a33d-e2d0d9a46bf8` preceded this report's repeated
   pause. **20:05:42.083636 UTC is the status-write time**, not a new instruction
   timestamp. The bridge corrected its earlier unsupported characterization of
   that repeat as a newer stop. No new direct stop was found in the current read.

Fresh execution and ownership checks succeeded: HEAD and incoming cherry-pick
remain as recorded below; the original owner is `notLoaded/completed`; this
owner and the separate release owner are active. Host inspection found service
processes but no competing product writer/test or writable checkout descriptor.
The complete positive2 input comparison found exactly the five prepared Azure
docs/test changes, no missing inputs and no production/lint-source changes.
Capacity measured nine occupied slots at 20:17:02 UTC; remeasure before runtime.
The same existing writer/reviewers are resumed; no replacement owner is created.

The entire approved plan 0–15 and discovered findings remain in scope, with
16–18 conditional and all previous constraints retained. Raw readiness receipt:
`.claude/lanes/composer087-contracts-20261015/resume-chronology-readiness-20261015T2017Z.json`.

Recorded at **2026-10-15 20:02 UTC / 2026-10-16 07:02 Australia/Canberra**.

John's latest instruction is: **“please pause all work and write yourself a status report.”**
All work owned by this thread is paused. This instruction supersedes the earlier
19:11:41 UTC conditional continuation forwarded by Emmy. Do not resume agents,
implementation, checks, cleanup, signing preparation, PR work or merge work until
John explicitly resumes. This report records state; it does not authorize work.

## Stop and ownership

- `/root/implementation` and `/root/astra_review` were interrupted. The live
  agent list confirms both are `interrupted`.
- `/root/astra_obligations` had completed its systems review and remains idle.
  The separately requested worktree cleanup had already completed.
- Root's positive2 and keyless2 processes had already exited. A read-only host
  census at `2026-10-15T20:02:35Z` found no pytest master in this checkout and
  no JavaScript test runner. It observed four unrelated Keisei serial masters
  and one release-CI ELSPETH master with two workers; those belong to other
  owners and were left alone.
- No product edits, new checks, Git mutations, process kills, session-lock
  changes, deployments or external-provider calls were performed for this pause.
  Only stop actions, status reads and this checkpoint/coordination update occur.
- This fork remains the designated sole Composer 0.8.7 integrator, thread
  `01a140e3-fc15-7900-b0c5-cc80ebc95fb2`. Root owns Git, ledger and test capacity;
  Sol is the sole product/test writer; independent Astra and systems reviews
  remain separate. Do not revive the superseded original .7 thread.
- Emmy and the relay have John-confirmed direction authority. Preserve their
  earlier approved decisions, but do not treat an older continuation as an
  override of this latest explicit pause.

## Exact checkout state

Worktree: Codex checkout `d66a/elspeth`; resolve its absolute path with
`git rev-parse --show-toplevel` when resuming.

Live Git output captured for this pause:

```text
branch: codex/composer087-contracts-20261015
HEAD: 32673a1b7f6572c7636bafbc57f653c8899ed99e
CHERRY_PICK_HEAD: acfcc58bd982e9849ca5fec57e6938a8baa98c57
UU src/elspeth/web/sessions/models.py
UU src/elspeth/web/sessions/schema.py
UU tests/unit/web/sessions/test_schema.py
?? tests/unit/web/sessions/test_quota_schema_epoch_contract.py
```

The three working-file conflict blocks were resolved during earlier work, but
their unmerged index entries and cherry-pick metadata remain. Root has not staged
the resolution or finished the incoming quota integration. Preserve the staged,
unstaged and untracked changes; do not reset, restore, stash, clean or repeat the
cherry-pick. There are additional release/docs/test edits in the current status.

The combined candidate uses Sessions epoch **77**, retaining the distinct .6
semantic-76 and standalone quota-only-76 histories as refused predecessors.
Landscape stays **49**. No user-store conversion, migration, reset or downgrade
has been performed or authorized.

## Completed local packets

These commits are on the current local ancestry, verified with `git log` at pause.
Their bounded acceptance does not establish merge, signing, release or deployment.

| Packet | Local commit and status |
|---|---|
| Candidate/full-replacement rejection identity, shared resolver and safe guidance | `620231a951b9bbc7fe28e3cc6b0e9ab38ebb898e`; independently accepted locally. |
| Shared/private link intent, explicit destination precedence and route-keyed snapshots | `9d8b43b962d733e9fe65ac1de1b088096a1cc731`; independently accepted locally; recorded 457 frontend and 7 backend checks plus TypeScript/ESLint. |
| Output mutation refusal identity, saved findings, privacy and schema tripwire | `208bf8d778344ff26583c3917b01a2d491f0b3b0`; independently accepted locally; recorded 479 positives and 25 valid deliberate failures across ten families. |
| Automatic-title SDK retry pins | `32673a1b7f6572c7636bafbc57f653c8899ed99e`; owner-accepted patch applied locally, with integrated title cases passing in positive1. Final combined quota acceptance remains pending. |

Container quota F1 (`acfcc58bd982e9849ca5fec57e6938a8baa98c57`) is still being
integrated. Its reviewed source provisions the configured token ceiling, allows
optional container storage while retaining mandatory identity storage, enforces a
cross-actor active singleton and joins startup work before database finalizers.
Retain the reviewed cancellation/exception ordering. Do not simplify it to make
the three deferred-propagation lint findings disappear.

## Latest measured evidence and remaining failure boundary

Raw artifacts are under `.claude/lanes/composer087-contracts-20261015/`.
Receipt JSON and complete JUnit were reread for this status report; no tests reran.

| Evidence | Recorded result |
|---|---|
| `quota-root-positive1-receipt.json` / `.xml` | Exit 1; 1316 cases: **1302 passed, 14 failed, 0 errors/skips**; frozen. Thirteen preservation assertions failed after correct refusal; one current compatibility-version mismatch. |
| Canonical static gate `quota-aggregate-static/20261015T185937Z-elspeth-721879/summary.txt` | Ruff, mypy and contracts each exit 0; driver 0; frozen=yes. It did not run default pytest or PostgreSQL. |
| Controlled preservation diagnostic | Main database, catalog, identity and policy rows unchanged; added an empty WAL and 32768-byte SHM. This established reader coordination effects; it did not justify bypassing live WAL. Original failure/diagnostic evidence is retained. |
| `quota-root-positive2-receipt.json` / `.xml` | Exit 1; 602 cases: **599 passed, 3 failed, 0 errors/skips**; frozen; 26.54 seconds. All 39 quota epoch/preservation cases passed, including live-WAL witnesses and actual-copy controls. |
| `quota-root-keyless2-receipt.json` and semantic delta | Exit 1; frozen; **3274 → 3277**, exactly one R6 and two R4 startup findings, zero semantic removals. This is drift evidence, not operator clearance. |

Positive2 and keyless2 share recorded input digest
`92d7ff16140f141b75f14c717c5155e469a6771ab2725f60c6896e29907512f1`.
The last completed quota handoff, freeze2, has digest
`56db040c7ce6b2c2a530bb8410e9a4f33c2e51eaa20ccc1868474a92a767abf0`.
Current docs/test edits are later than that handoff; do not describe the current
whole checkout as identical to freeze2 or claim a new green 1316/602-case run.

The three positive2 failures are all in
`tests/unit/web/test_azure_container_apps_runbook_contract.py`:

```text
test_every_epoch_literal_matches_the_live_constants
test_runbooks_cite_the_measured_facts_and_the_bundle_and_declare_their_status
test_prospective_receipt_paths_match_the_current_release
```

Sol prepared a narrow repair before this pause:

- Keep the truthful historical 0.8.6 / Sessions 76 / Landscape 49 sentence,
  label it explicitly historical and require exactly one exact occurrence.
  Continue enforcing current epoch literals and JSON facts elsewhere.
- Update prospective evidence paths to 0.8.7 in the cold-install runbook,
  existing-service redeploy runbook, deployment README and operating-Azure skill.
  No real evidence receipt or deployment was created.
- Add actual-text controls for current prose/JSON drift and historical sentence
  corruption, removal and duplication, with known-positive controls.

The actual test file SHA at pause is
`dcbc1f846b03c1d20b5dcced2a97222daa22342cb83c7539d54f285da4d1fb5a`.
Independent Astra reported source acceptance of that exact file and the five
docs/test changes. Sol reported scoped Ruff exit 0 and no production-Python
changes from positive2; Astra independently matched unchanged production/lint
inputs. These are source/static findings. The repaired runtime checks have not
run, and **`quota-integration-freeze3.json` does not yet exist**.

The preservation correction keeps strict byte/presence equality for static
DELETE-journal refusal fixtures, and separate WAL-aware operational cases for
durable data, logical state, committed-WAL visibility and owner continuity.
Production read-only admission is unchanged. No immutable-reader shortcut,
sidecar deletion or cleanup is allowed as a substitute for those obligations.

## Full approved worklist

The detailed defect inventory and earlier packet evidence remain in
[the work ledger](2026-10-15-composer-087-work-ledger.md). Its quota continuation
paragraph predates positive2 and the Azure repair; the current boundary above
takes precedence. All remaining work below is paused.

| Item | Current disposition |
|---|---|
| 0. Inventories and ownership | Completed baseline inventories. |
| 1. Rejection/operation/feedback migration | Candidate and output packets accepted locally. Source packet 1c design reviewed; source, transform/splice, blob, secret, discovery and remaining dispatch work pending. |
| 2. Saved-but-blocked persistence/reload/UI | Joined behavioral evidence pending. |
| 3. Model-catalog provenance | Fixed in inherited .6; .7 regression closure pending. |
| 4. CSV failure choice and assistance provenance | Runtime refusal exists; false omission-defaults-to-discard guidance and associated provenance checks pending. |
| 5. Scoring rubric | Complete scale, anchors and meaning approved in the existing criterion reviewed value. Systems design complete; both resolution orders/final rendering/literal preservation implementation pending. |
| 6. Public argument admission | Characterization pending; no actual public bypass established. |
| 7. Typed services and faithful doubles | Production owner exists; real-signature/context-forwarding evidence pending. |
| 8. Source proof context/unavailability | Terminal readiness-blocked direction and concrete systems design approved; implementation across preview/completion/review pending. |
| 9. Settlement versus historical review-debt repair | Ownership reviewed; explicit debt/repair operations, placement, idempotence and lease correction pending. |
| 10. Persisted record admission | Narrow singular-plus-plural refusal, including empty plural maps, approved; preserve supported unambiguous forms. Implementation/compatibility characterization pending. |
| 11. Validation phase truthfulness | Checked/failed/skipped and next-action behavior pending; no new phase schema or repair credit approved. |
| 12. Source-demand capacity refusal | Stable fail-closed normalization across producers/consumers pending; preserve completed work. |
| 13. Tool lifecycle coverage | Tool map recorded; broader success/refusal/retry/cancellation/privacy/reload evidence pending. |
| 14. Plugin/profile qualification | Inventory complete; case-specific construction/parity/repair evidence pending. Inventory does not qualify live providers or close #195. |
| 15. Lifecycle/API/UI distinctions | Rejected, saved-blocked, pending, validated, executable and executed witnesses pending. |
| 16. Repair credit | Conditional; not started. |
| 17. Request-local cache (#246) | Conditional; not started. |
| 18. Validation fairness/admission (#249) | Conditional; not started. |

Additional open findings include shared-view global keyboard actions; ready plural
source blobs with NULL hashes; redundant invalid-MIME recovery and the inaccurate
MIME CHECK comment; policy refusal after schema access; malformed/null source
claims; missing proof context; unlocked content fallback; late source unavailability
or false exhaustion; and per-query scoring-override teaching. These remain
explicitly tracked in the ledger, not silently dropped after quota integration.
LiteLLM conditional parameter repair/fallback and the inherited global writer
inventory expected failure remain separate unqualified boundaries. Deployed
shared-link incident and streaming causation have not been established.

The separately authorized cleanup completed earlier: its saved report records
**21 merged clean worktrees removed, registrations 71 → 50**, unchanged branch
refs/active owners and four merged trees retained for substantive local state.
No new cleanup or live recount was performed for this pause. Report:
`.claude/lanes/composer087-contracts-20261015/worktree-cleanup/report.md`.

## Resume point, only after explicit authorization

1. Read this report and coordination state. Confirm ownership, latest user/relay
   directions, Git status and preserved bytes before restarting the same agents.
2. Finish the narrow Azure handoff and review. Capture the actual current inputs;
   verify the unchanged-production claim before reusing positive2/keyless evidence.
3. Allocate one serial runtime only after fresh host capacity measurement. Shared
   ceiling remains 12, with release/.6 priority. Use the existing main venv and
   explicit worktree `src` plus `elspeth-lints/src` import roots; do not rebuild it.
4. Run affected Azure docs/gate cases and their current/historical controls.
   Then run the pending quota deliberate families: epoch76 admitted (one node),
   missing identity CHECK admitted (one node), committed WAL ignored (two nodes).
   Inspect activation witnesses, actual assertion failures and complete exit/JUnit.
5. Run the focused three PostgreSQL files serially with `-m testcontainer -n 0`:
   quota policy authority, graph review facts and schema probe. This does not
   replace the eventual full PostgreSQL gate.
6. Obtain final exact-candidate independent Astra and systems acceptance; root
   then completes the quota Git integration with canonical safety checks and
   normal hooks, preserving the index/worktree and all unrelated state.
7. Continue the remaining approved items 0–15 and discovered fixes. Complete
   appropriate aggregate gates, protected CI, operator signature clearance and
   exact-head review before the authorized draft PR/protected merge boundary.

No integrated quota deliberate negatives or PostgreSQL run has completed yet.
No full .7 default/full PostgreSQL qualification, final combined quota verdict,
operator signing clearance, PR, merge, release or deployment is established.
The three startup lint findings require the complete key-free census/worklist
and operator process; agents never hold the HMAC key. Paid adjudication/provider
calls, new spending, user-store migration/reset, protection bypass and deployment
remain outside authorization. Do not launch work merely to close those gaps while
this pause stands.
