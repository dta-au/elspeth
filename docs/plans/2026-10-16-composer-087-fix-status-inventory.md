# Composer 0.8.7 fixes and changes — status inventory

This is a historical inventory. The full scope, including all new smoke-audit
findings, and current four-state delivery tracking are now maintained in
[master issue #315](https://github.com/dta-au/elspeth/issues/315), with a
[repository snapshot](2026-10-16-composer-087-master-defect-list.md).
Discovery Group A is now committed as `f0c8a597e`; source investigation of
SMK-06 advisor input budgeting is the sole next development work. The dated
measurements below retain their original boundaries.

**Current authority, 16 October:** `codex/composer087-contracts-20261015` is the
designated canonical target for the new 0.8.7 release. Exactly two efforts are
active: qualification of its nine accepted commits at `696db482`, and finishing
the existing Discovery Group A draft as the sole development effort. Required
reviews/checks still govern delivery; PRs and merges when green are authorized.
Up to 12 test workers and sole device/checkout custody are assigned to root.
No other fix is currently active. Earlier pause/continuation wording is superseded
for these two efforts; the dated inventory and evidence below remain historical.

As of **2026-10-16 13:02:41 AEDT (02:02:41 UTC)**, the local 0.8.7 integration branch
`codex/composer087-contracts-20261015` is at
`696db482b3057b9a5d600b14ee448f12215ad6ff`. Candidate, output, source, transform,
graph, quota and shared-link packets are committed locally. The full approved
Composer plan remains in progress.

**The accepted code below is committed locally on the 0.8.7 integration branch.**
Every listed accepted commit is an ancestor of the current 0.8.7 HEAD; none is an
ancestor of the measured local main or cached origin/main. These ancestry checks
do not establish squash/cherry-pick equivalence, and live remote merge remains
unverified. No release or deployment is qualified by this inventory. No remote
fetch, network lookup, project import, test, database or provider call was made
for this inventory.

Status meanings:

- **Committed locally on the 0.8.7 integration branch**: accepted bounded packet is on the current 0.8.7 branch;
  broader integration, protected CI and release gates remain separate.
- **In development**: implementation exists and is undergoing qualification.
- **Started — analysis/design**: work beyond the baseline inventory exists;
  implementation or the required behavioral expansion has not started.
- **Not started**: the stated 0.8.7 implementation/qualification remains pending;
  inherited behavior and baseline source mapping are not new delivery.
- **Deferred/conditional**: explicitly outside current automatic implementation.
- **Complete**: completed non-code task or ignored local harness repair.

The numbered plan, packet breakdown and discovery tables are different views
of the same work. In particular, packets 1a–1f and their individual discoveries
belong to item 1; they must not be added together as independent plan progress.
Items 2 and 15 share the joined persistence/API/UI qualification boundary.

## Numbered 0.8.7 plan

| ID | Fix or change | Status | What remains / boundary |
|---|---|---|---|
| 0 | Establish baseline, registries, profile inventory and ownership | Complete | Actual registry/direct-call measurements recorded; this does not establish universal behavioral coverage. |
| 1 | Precise rejection identity, operation meaning and closed feedback | In development | Candidate, output, source and transform packets committed. Graph is also committed; discovery, blob, secret and remaining dispatch families still due. See packet breakdown below. |
| 2 | Saved-but-blocked state through persistence, reload and UI | Not started — joined 0.8.7 qualification | Preserve inherited 0.8.6 teaching; prove mutation/refusal/crash → exact retained revision → authenticated history/reload/UI without claiming readiness. Shares evidence with 15. |
| 3 | Model-catalog provenance | Not started — 0.8.7 regression closure | Production guidance was fixed in 0.8.6 and is inherited. Prove current supplied catalogue/permitted discovery, stale names and refusal behavior; no repeated repair required. |
| 4 | Explicit CSV failure choice and truthful assistance | Started — source plan | Runtime explicit-choice guard already exists. Correct false “omission defaults to discard” assistance and measure stamp, case-manifest, restart-history and registry-digest consequences. |
| 5 | Scoring scale, anchors and meaning under one reviewed criterion | Started — approved, systems-reviewed design | Implement complete rubric in existing criterion string, preserve literal content and independent prompt review, prove both resolution orders. No new override mechanism. |
| 6 | Public argument admission versus owned internal dispatch | Started — source characterization | Inspected public MCP/web arms enable admission. Behavioral guard/context/forwarding controls remain; no public bypass has been established. |
| 7 | Typed services and faithful test doubles | Not started — full item qualification | Existing typed `ComposerAppServices` retained. Narrow source/output autospec repair is committed, but broad real-signature/context/owner forwarding checks remain. |
| 8 | Explicit source-proof context and unavailable content | Started — approved, systems-reviewed design | Implement terminal readiness-blocked outcome and private proof contexts; distinguish no claim, malformed claim and unavailable proof, preserve custody/completed work. No automatic model wait/recovery loop. |
| 9 | Fresh settlement versus historical review-debt repair | Started — placement reviewed | Implement owning debt query/repair placement and unnecessary exclusive-lease correction; preserve receipt ordering, advisory precheck and under-writer rechecks. |
| 10 | Persisted record admission / mixed source forms | Started — approved design | Characterize compatibility, narrowly refuse non-null singular source plus empty or populated plural sources; preserve supported unambiguous/incomplete forms. No migration/reset. |
| 11 | Honest checked, failed and skipped validation phases | Not started — behavioral expansion | Prove truthful next actions and prerequisite stops; no new phase schema or repair-credit change approved. |
| 12 | Stable source-demand capacity refusal | Started — source-traced design | Normalize preflight/completion/surfacing/persistence to the existing bounded refusal, preserving completed work and fail-closed behavior. #247 was open at the baseline read; not refreshed remotely. |
| 13 | Tool-family lifecycle coverage | Started — acceptance map | Actual tool map complete; broader success/refusal/retry/cancellation/context/privacy/reload checks still due. Existing packet checks do not close all tool families. |
| 14 | Plugin/profile qualification and guided repair | Started — inventory/acceptance map | Construction, contract parity, misuse and repair checks remain. Installed/synthetic-profile inventory is not live-provider qualification or closure of #195. |
| 15 | Rejected, saved-blocked, pending, validated, executable and executed outcomes | Not started — joined 0.8.7 qualification | Trace exact distinctions through lifecycle/API/reconnect/reload/UI and privacy. Existing outcome carriers retained; shares work with 2. |
| 16 | Repair-credit policy | Deferred/conditional — not started | Requires item-11 evidence and a separate policy decision. |
| 17 | Request-local source-demand cache (#246) | Deferred/conditional — not started | Requires measured cost/lifetime and a decision; authoritative writer rechecks remain. |
| 18 | Validation fairness/admission (#249) | Deferred/conditional — not started | Requires contention/fairness evidence and policy decision; no authorization defect established. |

Evidence: [working ledger](2026-10-15-composer-087-work-ledger.md) (current worklist and approved-plan reconciliation);
the item-4 and item-6 root briefs, systems design reports and coordinator decisions
in the lane. “Started” here refers to substantive analysis/design beyond packet-0
mapping, not started implementation.

## Item 1 packet breakdown

| Packet / family | Status | Delivered scope / next boundary | Evidence |
|---|---|---|---|
| 1a: candidate/full replacement and shared resolver | Committed locally on the 0.8.7 integration branch — `620231a` | Explicit candidate versus saved-state operations, closed internal feedback, actual policy reasons, schema faults, privacy and precise CSV refusals. Remaining caller families separate. | [reviews/astra-packet1a-postimplementation.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-packet1a-postimplementation.md); ledger:321–599 |
| 1b: output set/patch/remove | Committed locally on the 0.8.7 integration branch — `208bf8d` | Saved findings, precise codes/subjects, credential payload and safe path projection. Later schema-fault gap closed in 1d. | [reviews/astra-packet1b-postimplementation.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-packet1b-postimplementation.md); ledger:660–698 |
| 1c: source mutations and plural blob resolver | Committed locally on the 0.8.7 integration branch — `41348ede` | Saved refusal/integrity ownership, pure policy admission, exact tuple and credential/census repairs. | [reviews/astra-packet1c-source-integration-fork.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-packet1c-source-integration-fork.md); [reviews/astra-packet1c-freeze2-systems-review.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-packet1c-freeze2-systems-review.md) |
| 1d: output admitted-schema follow-up | Committed locally on the 0.8.7 integration branch — `3f2db4f0` | Two output callers propagate admitted schema faults; faithful source/output provider doubles repaired. | [reviews/astra-output-schema-followup-fork.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-output-schema-followup-fork.md); [reviews/astra-output-schema-systems-review.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-output-schema-systems-review.md) |
| 1e: transform/node/splice | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Saved findings, codes/subjects, policy/schema/privacy/credential ownership, attributed splice counterfactual findings; preserves topology, atomicity and replay. | [reviews/astra-transform-splice-integration-review.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-transform-splice-integration-review.md); [reviews/astra-transform-splice-systems-review.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-transform-splice-systems-review.md) |
| 1f: edge wiring and removal | Committed locally on the 0.8.7 integration branch — `696db482` | Saved findings/code/subject for edge/missing-node/missing-edge/late refusals, three direct guidance entries, unused import cleanup and graph fixture repair. All focused checks, both final reviews and normal local integration complete. | `graph-wiring-root-positive2-*`, `graph-wiring-root-gates1-*`, [graph-wiring-root-all-negatives-summary.json](../../.claude/lanes/composer087-contracts-20261015/graph-wiring-root-all-negatives-summary.json), [graph-wiring-root-keyless-semantic-delta.json](../../.claude/lanes/composer087-contracts-20261015/graph-wiring-root-keyless-semantic-delta.json), [graph-wiring-root-final-runtime-bindings.json](../../.claude/lanes/composer087-contracts-20261015/graph-wiring-root-final-runtime-bindings.json), both final graph reviews |
| Discovery Group A: schema, assistance, baseline diff | Nearly code-complete — sole active development effort | Production draft and scoped Ruff/format, mypy and contracts checks complete. Independent placement reviews, regression tests/controls and focused qualification pending. Draft remains uncommitted and separate from the nine accepted commits. | [discovery-rejection-result.md](../../.claude/lanes/composer087-contracts-20261015/discovery-rejection-result.md); [discovery-rejection-implementation-brief.md](../../.claude/lanes/composer087-contracts-20261015/discovery-rejection-implementation-brief.md); [reviews/astra-discovery-dispatch-rejection-design.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-discovery-dispatch-rejection-design.md) |
| Other discovery / remaining dispatch | Started — source design, no implementation | Group B and remaining dispatch migration require separate bounded work; do not infer a public admission defect. | Same discovery/dispatch design; ledger item 1 |
| Blob caller family | Not started — implementation | Legacy producers remain to be migrated/qualified; earlier shared blob/source fixes do not close all blob tools. | Ledger item 1 and tool-family map:165–174 |
| Secret caller/context family | Started — reviewed source design; no implementation | Coherent secret refusals, precise codes/subjects and static guidance designed, including shared missing-context dispatch exits. Retain required service/user authority, exact authorization and privacy. | [reviews/astra-secret-rejection-design.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-secret-rejection-design.md); discovery brief exclusions |

Graph qualification is bounded: 98 affected positives, 568 owning-gate cases,
18 focused PostgreSQL cases, 36 intended witnessed failures across nine negative
families, and keyless 3277 → 3277 with zero semantic additions/removals. All 13
fresh runs share identical before/after/current maps in the qualification capture;
subsequent status-document updates are outside that capture. Original 96-pass/2-failure
fixture run remains red. These overlapping selections are not summed or called
a whole-branch green. The PostgreSQL URL-absence evidence is the root launch
transcript recorded in the final proof, not runner/capacity enforcement.
Normal preflight and commit/hooks exited 0; all six committed blobs match reviewed
staging and working bytes, with no hook changes and a clean post-commit tree.
See [graph-wiring-root-final-runtime-bindings.json](../../.claude/lanes/composer087-contracts-20261015/graph-wiring-root-final-runtime-bindings.json) and
[graph-wiring-postcommit-verification.json](../../.claude/lanes/composer087-contracts-20261015/graph-wiring-postcommit-verification.json).

## Additional fixes and changes found during delivery

Each row below is an explicit discovery/subchange; rows naming an item or
packet above are consolidated with that owner, not extra independent plan items.

| Fix / change | Status | Scope / remaining boundary | Evidence |
|---|---|---|---|
| Policy reasons flattened and admitted schema faults mislabeled as unavailable | Committed locally on the 0.8.7 integration branch — 1a/1c/1d/1e; other families pending | Exact reasons and direct admitted-schema failures fixed in selected owners. No all-caller closure. | Corresponding packet reviews above |
| Candidate path refusals disclose private server-root details | Committed locally on the 0.8.7 integration branch — `620231a` | Static safe guidance and actual public/planner privacy controls. | 1a review |
| Repeated masked classifiers falsely imply an impossible request | Committed locally on the 0.8.7 integration branch — `620231a` | Truthful withheld guidance retains planner-owned repeat identity. | 1a review |
| Inline CSV parse failure, scan cap and proven header conflict conflated | Committed locally on the 0.8.7 integration branch — `620231a` | Typed inspection-owned reasons; matching headers do not claim verified data rows. | 1a review:5–20; ledger:456–478 |
| Uppercase canonical quota codes miss noisy-text guidance | Committed locally on the 0.8.7 integration branch — `620231a` | Case-insensitive lookup comparison, canonical emitted codes retained. | Ledger:566–595 |
| CSV read-authority fingerprint moved | Committed locally on the 0.8.7 integration branch — `620231a` | Bounded owner rebind, actual copied DML/escape controls; global writer inventory remains separate. | Ledger:549–564 |
| Output missing-target attribution and standing findings lost | Committed locally on the 0.8.7 integration branch — `208bf8d` | Precise output subject/code, original graph/version and credential/schema identity retained. | 1b review |
| Schema-augmentation AST gate misses migrated saved helper | Committed locally on the 0.8.7 integration branch — `208bf8d` | Owning gate sees actual missing-identity mutation. | Ledger:670–673 |
| Q1: configured container token ceiling not provisioned | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Serialized create-or-verify startup; broader quota re-audit/whole-branch gates pending. | Quota reviews; ledger:93, final quota section |
| Q2: automatic title makes hidden SDK retries | Committed locally on the 0.8.7 integration branch — `32673a1` | Retry-disable pins with physical transport/accounting evidence; no universal provider fallback qualification. | Quota reviews; ledger:94,611 |
| Q3: token-only container policy requires a storage cap | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Container storage optional; identity storage remains mandatory, explicit values retained. | Quota reviews; ledger:95 |
| Q4: active container singleton is keyed by actor | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Checked constant slot and focused PostgreSQL authority proof. Full PostgreSQL gate separate. | Quota reviews; ledger:96 |
| Q5: provisioning can outlive database owners on cancellation | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Exact retained worker joined through cancellation before owner shutdown; deferred propagation retained. | Quota reviews; ledger:97 |
| Quota epoch 76 collides with 0.8.6 semantic epoch 76 | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Integrated Sessions epoch 77 preserves both contracts and refuses both incompatible predecessors. No user-store conversion. | Quota reviews; ledger:98 |
| Predecessor preservation tests treat new WAL/SHM as destructive mutation | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Strict DELETE-journal equality retained; WAL-aware logical preservation and committed-WAL visibility proved separately. | Quota reviews; ledger final quota acceptance |
| Shared current candidate compatibility version remains 0.8.6 | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Current version/documentation aligned to 0.8.7; historical version facts retained. | Ledger:735–741, final quota acceptance |
| Models documentation falsely describes stale-store deletion | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Documentation now reflects preserve/refuse. No conversion/reset behavior added. | Ledger final quota acceptance |
| Azure current/history contract fixtures and future evidence paths stale | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Preserve historical 0.8.6/session76/Landscape49 facts, pin current prose/JSON and prospective paths. No live receipt fabricated. | Quota review; ledger final quota acceptance |
| New predecessor test bypasses required session-engine factory | Committed locally on the 0.8.7 integration branch — `9d6533fe` | Existing read-only factory used; affected epoch/gate checks and initializer negative pass their intended contracts. | [reviews/astra-quota-engine-hook-followup.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-quota-engine-hook-followup.md); ledger:118 |
| SL1: malformed shared fragment opens recipient workspace | Committed locally on the 0.8.7 integration branch — `9d8b43b` | Authenticated shared-error surface, no fallback into recipient workspace. | Shared-link integration review |
| SL2: unavailable private link loses intent and resumes recipient workspace | Committed locally on the 0.8.7 integration branch — `9d8b43b` | Intent preserved through bootstrap, navigation and authentication remount. | Shared-link integration review |
| SL3: stale authentication destination A overrides explicit B | Committed locally on the 0.8.7 integration branch — `9d8b43b` | Current explicit destination takes precedence. | Shared-link integration review |
| SL4: old shared snapshot remains under a new token | Committed locally on the 0.8.7 integration branch — `9d8b43b` | Route-identity remount and existing fetch cancellation. | Shared-link integration review |
| SL5: shared-view shortcuts act on recipient workspace | Deferred — no implementation | New-session shortcut reproduced; validation shortcut source-supported only. No sender mutation/cross-user access established. Historical relay scope expansion was retracted. | Shared-link review; `shared_link_relay_correction` coordinator entry |
| Ready plural source blob with NULL hash offered as model repair | Committed locally on the 0.8.7 integration branch — `41348ede` | Owned integrity failure retained, corruption/laundering controls qualify it. | Source1c reviews |
| Invalid stored MIME has redundant recoverable branch / false DB-CHECK comment | Committed locally on the 0.8.7 integration branch — `41348ede` | Row reader retains integrity ownership; actual write/read guards documented. No constraint/epoch change. | Source1c reviews |
| Source policy refusal occurs after schema access | Committed locally on the 0.8.7 integration branch — `41348ede` | Pure admission before direct schema owner; actual policy reasons and admitted faults preserved. | Source1c reviews |
| Null/malformed source claims disappear or are string-coerced | Started — design only; implementation not started | Included in item 8's explicit present-claim refusal. | Ledger:108,715–722; item-8 systems design |
| Missing proof context conflated with no authored claim | Started — design only; implementation not started | Included in item 8; absent and unavailable must remain distinct. | Ledger:109,715–722 |
| Metadata-only proof permits later unlocked content fallback | Started — design only; implementation not started | Included in item 8; preserve content/custody owner and no-read rules. | Ledger:110,715–722 |
| Source unavailability reaches completion too late / becomes false exhaustion | Started — design only; implementation not started | Included in item 8 across preview/completion/review entrances; terminal readiness-blocked. | Ledger:111,715–722 |
| Per-query scoring override does not inherit node criterion reference | Started — boundary identified, characterization/teaching pending | Consolidated with item 5; no new override mechanism approved. | Ledger:112,416 |
| Output admitted ValueError/KeyError converted to availability repair | Committed locally on the 0.8.7 integration branch — `3f2db4f0` | Separate two-caller follow-up closes the gap deliberately left by 1b. | Output-schema reviews |
| Aggregation count refusal hides standing saved findings | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Actual saved findings, precise guidance and witnessed regression control. | Transform/splice reviews |
| Transform provider-path refusal exposes private path/root text | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Static safe projection on upsert, patch and splice preparation. | Transform/splice reviews |
| Node refusals omit codes/subjects or mask admitted schema faults | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Precise producer/consumer facts, admitted identity and fault propagation. Missing-node removal is covered by the separate committed graph packet. | Transform/splice reviews |
| Splice counterfactual findings lose attribution / standing saved findings | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Attributed proposed finding plus saved findings, no private proposal publication; atomic/replay behavior preserved. | Transform/splice reviews |
| Edge/missing-node/missing-edge/late graph refusals lose precise saved context | Committed locally on the 0.8.7 integration branch — `696db482` | Seven calls in four handlers migrated; code/guidance and unused import cleanup have complete focused receipts, both final ACCEPT reviews and normal integration. | Graph reviews and completed receipts above |
| Secret refusal codes and subjects are incomplete, including shared missing-context exits | Started — reviewed source design; no implementation | Three proposed new closed codes: `secret_context_unavailable`, `secret_target_required`, `secret_target_unsupported`. Preserve existing distinct resolvability/authorization/placement codes and exact destination or honest whole-graph `None`. No security admission failure established. | [reviews/astra-secret-rejection-design.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-secret-rejection-design.md), proposed code/subject map |
| Existing secret policy codes lack direct static guidance | Started — reviewed source design; no implementation | Actual accessor measurement finds `missing_secret_ref`, `unauthorized_secret_ref`, `disallowed_secret_ref` registered but without exact guidance. Six static guidance records proposed: these three plus the three new codes above. Preserve failed discovery shape and mutation-only guidance. | [secret-refusal-live-guidance-baseline.json](../../.claude/lanes/composer087-contracts-20261015/secret-refusal-live-guidance-baseline.json); [reviews/astra-secret-rejection-design.md](../../.claude/lanes/composer087-contracts-20261015/reviews/astra-secret-rejection-design.md) |
| Credential-envelope census names old adapter, not actual payload builder | Committed locally on the 0.8.7 integration branch — `41348ede` | Actual builder and per-row constructor ownership controlled; no wire expansion. | Source1c reviews; ledger:119 |
| Source test fixtures/outer-fault/profile/endpoint oracles invalid | Committed locally on the 0.8.7 integration branch — `41348ede` | Real owner premises and actual causal exceptions/invocations; original red receipt preserved. No new production defect established by those failures. | Source1c reviews; ledger:120 |
| Source/output turn provider mocks lack real interface specs | Committed locally on the 0.8.7 integration branch — `3f2db4f0` | Autospec actual bound provider method; affected cases and owning mock gate checked. Does not close all item 7. | Output-schema reviews; ledger:121 |
| Replay fixtures use invalid empty fixed schema before replay comparison | Committed locally on the 0.8.7 integration branch — `44d03a2c` | New and inherited fixtures now use admitted divergent options and exact replay conflict; three fresh cases. Original red retained. | Transform/splice reviews; ledger:122 |
| Count-guidance fixture assumes only one raw saved-graph error | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Exact standing findings plus focused planner guidance asserted. | Transform/splice reviews; ledger:123 |
| Full-replacement fixture expects invented code from codeless saved helper | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Uses actual candidate-only owner, precise sink-policy diagnostic/code/subject. | Transform/splice reviews; ledger:124 |
| Graph slot fixture clears its own required warning during setup | Committed locally on the 0.8.7 integration branch — `696db482` | Real independent warning retained; fresh focused 98 cases pass. Original 96-pass/2-failure run remains red. | Graph positive2, both graph reviews and working ledger |
| Negative harness requests fixtures before pytest lifecycle | Complete — ignored harness repaired | Normal autouse fixture; valid paired failures subsequently qualified. Discarded initial errors not credited. | Ledger:480–489,537–547 |
| Output mutation harness fails frozen/mutable comparison or wrong privacy oracle | Complete — ignored harness repaired | Invalid negatives excluded; actual identity/publication and canary assertions subsequently reached. | Ledger:682–690; 1b review |
| Negative selector reads only last digit of multi-digit control | Complete — ignored root runner repaired | Controlled full-suffix selection; actual transform families ran in intended modes. Prior single-digit receipts unaffected. | [root-negative-selector-repair-proof.json](../../.claude/lanes/composer087-contracts-20261015/root-negative-selector-repair-proof.json); ledger:125 |
| Secret scanner flags synthetic credential canary | Committed locally on the 0.8.7 integration branch — `44d03a2c` | Documented per-line marker/formatting only; complete-module AST identical, normal hooks pass. Original failed preflight retained. | Transform reviews; [transform-splice-root-comment-only-proof.json](../../.claude/lanes/composer087-contracts-20261015/transform-splice-root-comment-only-proof.json); ledger:126 |
| Initial host capacity scanner drops unreadable container cwd | Complete — local instrument corrected | Unreadable cwd retained; canonical evidence exposed undercount. Every new runtime allocation still requires a fresh host census. | Coordinator `fork_handover.capacity_correction` |

## Open qualification, observations and future work

These are not all confirmed defects or authorized implementation tasks.

| Item | Status | Meaning / boundary | Evidence |
|---|---|---|---|
| Three new startup R4/R6 deferred-propagation lint findings | Started — source reviewed; adjudication/signing open | Correct cancellation lifecycle retained. Keyless zero subsequent drift does not clear these or operator signatures. | Quota reviews; ledger:99 |
| Global Sessions writer-authority inventory expected failure | Not started — separate unqualified inventory | Inherited XFAIL remains; bounded CSV read rebind is not global writer clearance. | Ledger:518–523,549–564; 1a review |
| Broader profile `ValueError` catch boundary | Started — observation only | Needs characterization; no new product root cause or repair established. | Status report:271–275 |
| Service advisor-context elision after first successful mutation | Started — source observation only | Existing comment/control flow recorded; needs lifecycle characterization before defect or repair claim. | Coordinator `source_observations_pending_characterization`; status:271–275 |
| LiteLLM conditional parameter repair/fallback | Started — limitation documented | Title retry pins do not establish all endpoint/SDK behavior; no blanket fix delivered. | Ledger:113; quota reviews |
| Quota preserving-conversion proposal | Started — isolated experiment only | Standalone predecessor/target proposal, not integrated epoch-77 conversion support or authorized user-store migration. | Ledger:636–655 |
| End-to-end quota re-audit | Deferred until 0.8.7 integration settles | Focused accepted quota packet is not a complete fresh audit of final 0.8.7. | Coordinator priority remediation boundary |
| Deployed shared-link incident / streaming causation | Not established | Offline link bugs confirmed and fixed; live causal claim remains unproven. | Shared-link integration review:97–105 |
| Future backend Composer provider streaming | Started — initial exploration complete; implementation not started | Browser already observes durable jobs via SSE/status. Provider streaming is new and does not itself remove independent deadlines; 0.8.7 retains priority. | `streaming-evaluation/report.md:1–49` |
| Future advisor force-return to user on bug/loop/design failure | Started — initial exploration complete; implementation not started | Requires enforceable typed orchestration decision, preserve completed work and terminal persistence. Proposed copy: “The composer is unable to compose this graph due to a bug, loop or other design issue. Please submit a bug report.” No automatic bug filing. | [streaming-evaluation/report.md](../../.claude/lanes/composer087-contracts-20261015/streaming-evaluation/report.md); direct user copy and coordinator .8 boundary |
| Future deployment-example timeout drift | Started — source observation only; correction not started | Linux example describes nginx 240s; inspected Compose nginx is 360s. Not a live configuration claim or identified cause of observed timeout. | `streaming-evaluation/report.md:47` |
| Merged clean worktree cleanup | Complete | Saved execution verifies 21 removed, registry 71 → 50, branches/protected owners retained. These are historical cleanup measurements, not fresh current registry counts. | [worktree-cleanup/report.md](../../.claude/lanes/composer087-contracts-20261015/worktree-cleanup/report.md) |
| Four merged worktrees with substantive ignored state | Deferred — retained | Authentication/local stores/signing worklists/scratch source preserved. No further deletion implied. | [worktree-cleanup/report.md](../../.claude/lanes/composer087-contracts-20261015/worktree-cleanup/report.md) |
| Whole 0.8.7 integration / release gates | Not started or incomplete at this snapshot | Full default/full serial PostgreSQL, final applicable native/UI/provider gates, protected exact-head CI, PR/merge and operator adjudication/signing remain. No 0.8.7 release/deployment claim. | Ledger:135–137; status:309–314 |

## Live Git evidence

Measured refs (local or cached; no fetch):

```text
refs/heads/codex/composer087-contracts-20261015 696db482b3057b9a5d600b14ee448f12215ad6ff
refs/heads/main 9d7735c094eab212549868afeaf6019272cf5c57
refs/remotes/origin/main ac7e5aac43b4f20bd10dd420b43dd439f5371f55
```

`git merge-base --is-ancestor <commit> <target>` raw exit results:

| Commit | Current 0.8.7 HEAD | Local main | Cached origin/main |
|---|---:|---:|---:|
| `620231a951b9bbc7fe28e3cc6b0e9ab38ebb898e` | 0 | 1 | 1 |
| `9d8b43b962d733e9fe65ac1de1b088096a1cc731` | 0 | 1 | 1 |
| `208bf8d778344ff26583c3917b01a2d491f0b3b0` | 0 | 1 | 1 |
| `32673a1b7f6572c7636bafbc57f653c8899ed99e` | 0 | 1 | 1 |
| `9d6533fe07ef0cdb5270611cc4f1b343cc39de5f` | 0 | 1 | 1 |
| `41348ede3c667bf337ab6597a59df0f9e02dc5a5` | 0 | 1 | 1 |
| `3f2db4f0e92958ecf40afd58fcc6383f2c90521f` | 0 | 1 | 1 |
| `44d03a2c45a4446b04a2a433c7611434f0ba4a6d` | 0 | 1 | 1 |
| `696db482b3057b9a5d600b14ee448f12215ad6ff` | 0 | 1 | 1 |

Controls: HEAD → HEAD returned 0; new graph commit → original baseline
`02e6b893f35230db544c1f277988c61b89203a54` returned 1. Git ref selection included
the known current branch and excluded a planted nonexistent ref. Root confirmed
there is no separately identified 0.8.7 release target; “Committed locally on 0.8.7
integration branch” is the intended practical status wording.

The graph post-commit receipt records an empty status at 01:58:38 UTC. The live
status read immediately before writing this inventory was:

```text
 M docs/plans/2026-10-15-composer-087-work-ledger.md
 M docs/plans/2026-10-16-composer-087-paused-status.md
```

This new inventory becomes an expected untracked document after the read above.
Root owns any later updates to the two existing ledger/status documents; those
progress edits must not be confused with the earlier clean graph commit receipt.

## Evidence and limitations

The [working ledger](2026-10-15-composer-087-work-ledger.md) and
[current status](2026-10-16-composer-087-paused-status.md) preserve full scope and
history. Lane evidence links below are relative to this checkout; ignored receipts
are local coordination artifacts and are not published by this document.
The [original inventory draft](../../.claude/lanes/composer087-contracts-20261015/fix-status-inventory/report.md)
retains its earlier snapshot and detailed review qualifications.

Live remote PR/CI/merge and deployment were not queried. Local ancestry does not
assess squash/cherry-pick equivalence. Issue openness and cleanup registry totals
are explicitly historical where stated. Local acceptance does not establish
protected CI, operator signing, release, deployment or completion of all planned
items. Future streaming/advisor work remains exploration only; SL5 and conditional
items 16–18 remain deferred. This status list starts no additional work.
