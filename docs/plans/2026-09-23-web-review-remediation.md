# Web review remediation implementation plan

**Goal:** Resolve and independently verify every issue R01–R73 and G1–G6 from the [0.8.1 web review](../reviews/2026-09-23-release-0.8.1-web-review/README.md), then integrate the repairs into `release/0.8.1` with measured release gates.

**Architecture:** Give related defects one implementation owner, with exclusive ownership of shared files. Use isolated worktrees, independent reviewers, serial integration, and one coordinated full-suite checkpoint on the assembled production tree. Preserve the common Composer runtime, provider-authored graphs, strict persisted-data parsing, and shared tutorial path.

**Tech stack:** Python, FastAPI, SQLAlchemy, SQLite/PostgreSQL, React/TypeScript, Zustand, Vitest, Playwright, Docker/nginx.

**Status, 2026-09-30:** Recovered planning reference for local `release/0.8.1` integration. The original review and the diagnostic sheets preserve dated findings and acceptance requirements. They are not a current missing-work inventory. This document does not close findings or establish deployment, signing, or live-provider acceptance.

## Current execution context

Recovery began from `release/0.8.1` at `a2f0281ff`. At that source checkpoint,
Sessions and the coordination hard cut are epoch 71. Do not implement the
historical epoch-65-to-66 proposal below or reuse a deployed epoch. Any further
persisted-grammar change requires its own current contract analysis and the
next applicable epoch, with SQLite and PostgreSQL checks.

The later [complete Guided removal plan](2026-09-28-guided-mode-removal.md)
supersedes the earlier decision to retain Guided tutorial machinery. Commit
`7001600fe` removes Guided Composer and retains freeform onboarding. The
`composer/guided` and frontend chat Guided directories and
`sessions/_guided_step_chat.py` are absent at the recovery base; the tutorial
uses `TutorialFreeformShell.tsx`. G1–G6 therefore require removal/surviving-path
verification, not reconstruction of the retired mode. The
[guided disposition sheet](2026-09-23-web-review-remediation/guided.md) records
the remaining acceptance questions without claiming they have passed.

The advisor now admits structured responses through `advisor_output.py` and
`advisor_checkpoint.py`. R23's former markdown/regex repair is superseded;
test the current structured boundary rather than adding prose fallback.
Composer and session service extractions also moved ownership seams. Resolve
current owners and callers before using the old detail sheets' line references.

GitHub Issues is the shared tracker. Local legacy records and retired code-map
tools are historical evidence; do not reactivate them or import review IDs
automatically. The original implementation lanes below are scheduling
suggestions. The worktree recovery coordinator may combine overlapping
components after inspecting the current implementation and independent review.

Use the live recovery inventory to distinguish equivalent/cherry-picked work,
missing code, missing tests, and superseded designs. A finding is complete only
after current behavior and target-branch inclusion are verified. The sections
and four diagnostic appendices below retain the 2026-09-23 reasoning; this
current context takes precedence over their stale paths, epoch literals,
ownership assumptions, and provisional fixes.

## 1. Historical evidence and planning starting point

- Original review pin: `74c0ce0db`; use `git show 74c0ce0db:<path>` for its line references.
- Source inspected during planning: `1e9cafa9d47df5654926509ef13a2a3e9e4452c8`; review-only commit: `73fc6fc8121183ed628bfc94a2bbcba663ca97ec`. Refresh the integration base at dispatch; do not assume the shared release checkout stopped moving.
- The review commit contains 81 files: README, findings JSON, and 79 issue reports. Direct JSON/file/link validation returned `Issue files: 79 README issue links: 79 Source findings: 129`, with `confirmed: 109, refuted: 19, disputed: 1`. Every non-refuted finding maps to one listed issue; refuted findings map to none. The ten findings originally high each retain two verdicts.
- The cleanup script classified the pinned review worktree `REMOVABLE`, then removed it and `review/web-0.8.1-20260923`. Its commit remains an ancestor of release.
- R02 identified an epoch-65 local-store incompatibility at the planning base: that epoch admitted stores written before the mandatory v2 completion-gate grammar. Published-build reach and local-store reach were separate questions. The current admission question is whether old stores are rejected by the current epoch-71 startup contract.
- R16 is partially addressed by `1809379f6`; current durable retry advice still needs checking/fixing. Do not recreate the old withheld-summary fix or close the whole issue from that commit alone.
- Later polling changes (`7e52d8afc`, `5f7831412`) do not establish that R38/R48/R49 are fixed. Verify the actual surfaces identified in the issue reports.
- Existing credential-generation work at `601d6a9a9` is not an ancestor of the planning base. Reconcile its semantic diff and tests with the identity lane before editing credential deletion. An ancestry failure alone is not proof that no equivalent change landed.
- The original Guided retirement decision retained tutorial machinery and was mapped to GitHub #191. The later complete-removal decision supersedes that design. Neither a migration ticket nor source absence by itself proves the surviving freeform/tutorial acceptance requirements.

The review is the historical source of truth. Record implementation progress in a separate issue ledger; do not rewrite its confirmed/refuted verdicts to represent repair status.

## 2. Subagent structure

The coordinator owns integration, dependencies, shared-file assignments, and acceptance. The default active set is up to six implementation agents, six independent review agents, and the coordinator: 13 of the available 17 slots. Use the remaining slots for targeted security/concurrency reviews or failed-regression diagnosis. This is a scheduling shape, not a cap on total agents; increase read-only review concurrency when useful.

Each lane below has one writer at a time. A fresh reviewer reads the complete changed files and tests, reproduces the relevant negative control, and reports against the exact candidate SHA. Writers never approve their own changes. R01, R02, R03/R14, R05, R07–R09, R25/R26, and R35/R36 additionally receive a specialist second review for contracts, persistence, security, or audit integrity.

Many findings are tiny or share a file. Use lane-manager's evidence and liveness discipline, but not its default one-ticket/one-worktree/full-suite-per-merge recipe. Its fit check explicitly calls for consolidation when files overlap. Its current Python-only verifier cannot certify Vitest or Playwright assertion failures. For these lanes, record native runner results and independent review directly; never mark a lane verified merely to satisfy the helper. Put working state under `.claude/lanes/web-review-remediation-20260923/`, not tracked plan sidecars.

Every lane report records: issue IDs; base/candidate SHA; changed paths; reproduction; regression command and exited result; negative control; relevant whole-tree gate results; review findings and resolution; remaining uncertainty. Keep ordinary `status.json` and `report.md` working files, not signed plans or approval receipts.

## 3. Ownership and issue assignment

Every issue has exactly one primary lane. Cross-lane review is encouraged; cross-lane writes require coordinator reassignment first. The linked detail sheets name source files, test files, scenarios, and commands.

| Lane | Primary issues | Work and exclusive ownership | Dependencies |
|---|---|---|---|
| A — schema and compatibility | R02, R52 | Session and Landscape epoch sweeps; completion-gate format rejection; invalidated share snapshot behavior | Decide B/I durable shapes and D auth-event CHECK vocabulary before final sweeps |
| B — Composer state and audit | R03, R14, R15, R16, R21, R22, R23, R24, R25, R26, R53 | Review-only state/proposal version semantics; durable advisor recovery; truthful notices; atomic, attributable planner evidence | C before touching shared session repository; serialize with F/G/I in Composer files |
| C — lock order | R05 | One lock order across admission, ticket, heartbeat, progress, approval, identity invalidation | First persistence change; PostgreSQL test capacity reserved |
| D — identity and credential audit | R07, R08, R09, R59 | Live immutable identity authorization; create/reset/delete attribution and transactional audit | Reconcile credential fence branch first; contract handoff to L |
| E — Azure and deferred contracts | R01, R17, R18, R44, R45, R46, R47 | Safe profile-backed schema probes, multi-query marker traversal, search policy/assistance | Reconcile security writer ownership; release catalog/tools files before F |
| F — prompt edits and planner guidance | R27, R28, R29, R30, R31, R32, R33, R34 | Canonical prompt-part/digest mutation, usable repair tools, guidance registration, model catalog coverage | E then F for shared catalog/probe files; B then F for shared Composer files |
| G — pricing failures | R39, R40, R41, R42 | No futile pricing retries; accurate usage-versus-pricing classification; reject boolean token limits | B/F before shared compose/catalog files; coordinate K error UI |
| H — run configuration and recovery | R12, R13, R50, R51 | Audit effective rate settings; recover cancelled finalization; preserve original failure; validate persistence path at boot | C before shared persistence changes; hand runtime semantics to K/M |
| I — interpretation provenance | R35, R36 | Correct actor/provenance at server surfaces and state-revert repair | B/C before shared route/repository writes; durable schema decision to A |
| J — frontend state and refresh | R04, R37, R38, R65, R68, R69 | State refresh on decision-only writes; valid snapshot race handling; accept hydration recovery; conflict feedback | B version/event contract first; reserve session/interpretation store files |
| K — approval and run UI | R06, R20, R43, R48, R49, R54, R61, R62, R63, R64, R66, R67 | Multi-query review; all Run entry points; live-run attach; decoder/keyboard/layout correctness; actual E2E fixtures | J store/API handoff; E/F prompt contract handoff; H run behavior; G failure taxonomy |
| L — people/auth UI | R10, R19, R55, R56, R57, R58, R60, R70 | Cross-tab logout; reload-safe deletion recovery; account copy/layout/DTO cleanup; blob error fallback | D authorization/recovery contract; serialize shared api/client.ts edits with K |
| M — deployment and operator docs | R11, R71, R72, R73 | Trusted proxy/client IP; timeout/rate-limit docs; Docker ingress/firewall guidance | R11 can start early; finalize rate-limit docs after H |
| N — Guided removal and surviving tutorial paths | G1, G2, G3, G4, G5, G6 | Verify removal and surviving freeform behavior; proposal busy state, source admission, common tutorial path | Use the later complete-removal ruling; no shared-file collision with J/K/E/F |

Detail sheets:

- [Composer state and advisor/audit evidence](2026-09-23-web-review-remediation/composer.md).
- [Azure, prompt contracts, catalog and pricing](2026-09-23-web-review-remediation/contracts.md).
- [Persistence, identity, interpretation and deployment](2026-09-23-web-review-remediation/persistence.md).
- [Frontend stores, approvals, account flows and execution controls](2026-09-23-web-review-remediation/frontend.md).
- [Guided removal and surviving tutorial acceptance](2026-09-23-web-review-remediation/guided.md).

These sheets are planning inputs grouped by reviewer expertise; the table above is the authoritative issue assignment. File owners perform shared-file subpatches on behalf of another issue owner: D supplies R19's backend recovery capability; J supplies R39's session-store retry guard and G1's proposal cleanup; K supplies R39's message UI once G finalizes its failure taxonomy. I specifies R36's new interpretation origin; J/K implement its TypeScript type/decoder parity in their reserved files, with A owning enum/CHECK schema integration. Primary owners retain responsibility for complete acceptance. A owns all schema/epoch edits, including D's Landscape event vocabulary. The appendices' alternative batch suggestions do not authorize competing writers.

## 4. Shared-file scheduling and dispatch order

Before dispatch, expand each lane's exact changed-file set from the detail sheets and compare it with active worktree changes. Treat shared test files as shared ownership too. Reserve a whole file to one lane, even if proposed edits are far apart. Queue competing edits or consolidate them under the same writer. Never resolve a concurrency conflict by discarding a sibling's changes.

Particular collision points are `web/sessions/repository.py`, `web/sessions/routes/composer/compose.py`, `web/composer/service.py`, `web/composer/tools/sessions.py`, `web/composer/catalog.py`, `web/composer/validation.py`, `web/config.py`, frontend `api/client.ts`, `sessionStore.ts`, and workspace rendering files. Expand shorthand to actual paths before issuing a brief. Active advisor/security work can own some of these already; consult its owner before dispatch.

**Wave 0 — reconcile and reproduce.** Freeze a named integration base under `.claude/worktrees/`. Inventory active branches and tracker ownership. Locate any existing matching work; do not create 79 duplicate tracker issues. Revalidate all findings against the base, preserving the 19 refutations. Add a regression first for each behavioral defect. Record any already-fixed claim with the exact reachable commit plus a passing regression. Record whether each issue is `open`, `partial`, `fixed-and-verified`, or `removed-and-verified`.

**Wave 1 — release blockers.** Start C, D, E, and M's proxy fix if their file reservations are disjoint and current reproductions show missing repairs. Start A's old-store rejection reproduction and B's contract design read-only. Land/review C before B writes shared persistence code. Prioritize R01, R02, R03/R04, R05, R06, and R07–R09 over cosmetic work. The original hold applied to epoch-65 builds; current acceptance must validate the current startup contract rather than ship an old schema.

**Wave 2 — state, prompt and runtime correctness.** Run B after C; run F after E; run H when its config/repository reservations are free. Start D-dependent L and I/J after their contracts are settled. Shared Composer files can force B → F → G → I even where the business behaviors are independent. Use free slots for reviewers and disjoint frontend fixes, not competing writers.

**Wave 3 — UI integration and compatibility sweep.** Complete J/K/L, surviving tutorial/removal verification N, and remaining G/M work. A owns any consolidated session-epoch update after B/I settle new durable shapes. Read the current live epoch and use the next applicable epoch for later incompatible grammar: never reuse a deployed epoch or treat the historical epoch-66 proposal as current. Update current docs/examples/tests, preserving correctly pinned historical incident records.

**Wave 4 — assemble and challenge.** Merge reviewed lanes serially into the isolated integration branch. Re-run affected checks on every merge/conflict resolution. Test one combined end-to-end route early (profile-only Azure → multi-query LLM → approval → execution) so integration feedback arrives before the tail of low findings. At the final frozen candidate, run the broad gates and the cross-lane acceptance matrix below. Return findings to the owning lane and repeat only affected checks unless the production tree change requires another broad run.

Do not wait for all 14 lanes before reviewing the first patch. Do not start a fresh lane on an apparently abandoned branch until agent status, heartbeat, and worktree activity agree that its owner is gone.

## 5. Design decisions the first patches must implement

**R01/R17/R18: validate the real contract.** A probe may supply construction-only credentials/prompt material, never synthetic pipeline structure or invented field guarantees. Derive Azure input/output requirements from the selected profile and actual plugin contract, including custom field names and closed index pins. Check multi-query deferred markers at every actual prompt site. A genuine missing downstream field must still fail Stage 1. Include no-network validation and real provider-boundary execution tests separately; a stubbed contract test is not live Azure acceptance.

**R02: fail at startup.** Verify old stores are rejected before serving requests and fresh stores at the current live epoch initialize successfully on SQLite and PostgreSQL. Keep strict completion-gate parsing and verify all sentinel/fingerprint/coordination constants. Do not repair persisted Tier-1 rows opportunistically, add a legacy fallback parser, or reinstate the obsolete epoch-66 target. Any operator store reset remains a separate destructive action with an exact store list; preserve auth credentials unless explicitly included.

**R03/R04/R14: one coherent decision contract.** Preserve immutable composition history and durable advisor decisions. The first B checkpoint maps every proposal/approval binding, including blob custody/effect receipts, and selects atomic rebinding of eligible same-turn proposals to an explicitly review-only successor, or a separate fenced gate-fact persistence mechanism if it is demonstrably smaller and preserves history. Do not mutate an existing composition history row in place. Equality of graph content alone is insufficient authority to rebind a proposal. Give J an explicit refresh signal for changed review facts, preserve facts through every mid-turn head and abort, and retain genuine stale-proposal conflicts. Settle this choice before either writer implements; prove successful acceptance, rejection, recovery, and reload together.

**R05: remove the cycle.** Choose and document the lock order across every transaction participating in session/identity/provider admission; the current session-first writers must agree with provider-attempt insertion and its foreign-key lock. Tests must force both sides of the former interleaving using barriers and real PostgreSQL. A retry loop around `40P01`, SQLite serialization, or a sleep that happens to pass is not a fix.

**R07–R09: identify the human administrator.** Authorization must follow live authority and immutable identity, not a reusable username. Preserve the distinction between the credential-only development grant and a full local Administrator role; do not silently broaden either. Bind any retained development grant durably to the consumed bootstrap identity. Prove ordinary local admins retain intended access, demoted/deleted identities lose their applicable grants, and username reuse never inherits bootstrap privilege. Include actor attribution for create/reset/delete without passwords, hashes, or credential material in audit payloads. Auth and Landscape are separate stores: use durable mutation intent/outcome and generation-fenced recovery rather than assuming a cross-store transaction. Add fault-injection proof that audit failure cannot leave an unaudited usable replacement credential. New auth-event CHECK vocabulary requires a separate Landscape epoch sweep (43 at the planning base), coordinated by A, as well as any auth-store schema change. Preserve the existing credential-generation fence.

**R28: measure before choosing catalog budget.** Measure actual serialized catalog/scaffold bytes and omitted providers using the installed registry. The standing ruling in `tests/unit/web/composer/test_pipeline_planner.py:160–168` already permits a 10% band above the baseline: use that authority for a measured repair within the band without asking again. Bring concrete coverage/cost options to the operator only if the necessary change leaves the authorized band or resets the baseline. A shrink beyond the documented lower boundary likewise needs judgment. Correct discoverability and truthful docs, never trim load-bearing teaching to satisfy the ratchet. Keep any genuinely outstanding decision visible; it does not hold unrelated lanes.

**R22/R25/R26/R35/R36: preserve evidence semantics.** The mandatory turn-settlement cohort, including withheld prose and its bindings, must settle atomically or roll back. Preserve already-durable provider-attempt/accounting and failure records of real incurred activity. Bind prose to the actual provider attempt/call. Parse provider text at the untrusted boundary and either refuse optional raw capture while retaining mandatory evidence, or use an explicitly encoded representation safe for PostgreSQL text. Never silently rewrite the captured prose. Audit actor labels must identify the actual server/user/LLM origin in the hashed canonical payload. If this changes persisted grammar, A owns the epoch impact.

**R11: trust only the intended proxy.** Configure nginx forwarding and uvicorn trust together. Demonstrate distinct clients remain distinct and direct/untrusted forwarded headers cannot spoof identity or rate-limit attribution. Do not solve this by accepting arbitrary forwarded headers on a publicly reachable app port. Validate Compose bindings and update operational firewall guidance against the actual network path.

**G1–G6: verify removal and surviving requirements.** Complete Guided removal is already the chosen architecture. J/N verify the current proposal-action ownership and stale-session races, source header admission, and ordinary provider-backed tutorial path described in the disposition sheet. Do not rebuild retired Guided turns or schema forms. Preserve user-visible requirements that still apply and record any surviving defect with its current production seam.

## 6. Worker and reviewer brief

Send a bounded brief containing the following, with every placeholder resolved:

> Work in the named isolated worktree at the recorded base SHA. Own only the listed issue IDs, source files, and test files. Read the complete original issue files, relevant AGENTS.md, CONTRIBUTING whole-tree gates, and current code. Confirm no active lane owns these files. Reproduce the behavioral failure through the production seam; write the regression; run it and record the expected failing assertion. Implement the smallest complete fix, preserving Composer provider/tutorial invariants and trust-domain rules. Run the regression, affected tests, and relevant whole-tree gates. Python tests use `-n 0`; frontend workers and Playwright use one worker unless the coordinator allocates more. No broad suites, live-provider spend, deployments, shared-store resets, pushes, signatures, or other branches' cleanup. Record exact commands/exits and uncertainties in the lane report. Commit only your paths after branch-safety-check. Return only the report path and short summary.

For documentation-only/dead-comment changes, use direct content/source review rather than manufacturing unit tests. For removed branches, include caller/production-flow evidence and an appropriate surviving-path regression. A collection error or fixture crash is not a successful negative control.

The independent review brief adds:

> Review the exact candidate SHA against its base and the issue's original verdicts. Read full touched files, check the fix's actual failure path and affected callers, and independently run or inspect reproducible regression evidence. Attempt to falsify the fix with stale/reordered input, failure midway, duplicate/retried operation, invalid types, and the named issue-specific negative cases. Look for strengthened tests that still pass on the unfixed tree, weakened assertions, validation bypasses, raw secrets in audit, provider bypasses, tutorial special paths, and unauthorized suppressions. Return a file containing concrete blockers or approval and its limits. Do not write production code on the author's branch.

## 7. Validation and test capacity

A designated coordinator owns broad-test capacity. Inspect running pytest/testcontainer processes and host load before allocating it. Normal implementation lanes run serial focused tests; reserve a single serial PostgreSQL container job. Do not multiply default `-n 12` across agents.

Use the existing main environment explicitly or create a real worktree-local environment. Do not create `.venv` symlinks or install into a shared environment through one. Export **both** source roots, then verify both imports point into the worktree. The lane-manager helper currently creates a symlink, so do not use its dispatch/verification environment setup blindly.

Example Python regression command; replace the test path with the exact selection from the detail sheet:

```bash
cd "$WORKTREE" && export PYTHONPATH="$WORKTREE/src:$WORKTREE/elspeth-lints/src"
"$PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
"$PYTHON" -m pytest "$TEST_PATH" -n 0 > "$LANE_LOG" 2>&1
result=$?
printf 'exit=%s\n' "$result"
exit "$result"
```

Run the same regression against the unfixed base with the necessary test-only patch, then the candidate. Capture actual runner assertions, not text matching on `FAILED`. New test dependencies must not turn the base run into an import failure. No need to run a full suite for every small documentation/UI fix.

Frontend validation, from the candidate's frontend directory, using its lockfile and the repo's Node/npm versions:

```bash
cd "$WORKTREE/src/elspeth/web/frontend" && npm run test -- --maxWorkers=1 <test-files>
cd "$WORKTREE/src/elspeth/web/frontend" && npm run typecheck
cd "$WORKTREE/src/elspeth/web/frontend" && npm run typecheck:workspace-e2e
cd "$WORKTREE/src/elspeth/web/frontend" && npm run lint
cd "$WORKTREE/src/elspeth/web/frontend" && npm run lint:css
cd "$WORKTREE/src/elspeth/web/frontend" && npm run build
cd "$WORKTREE/src/elspeth/web/frontend" && npm run test:e2e -- --workers=1 <spec-files>
```

Resolve the angle-bracket selections before execution; run each command with its own lane-private log and exit capture. Use the existing Composer harness and Playwright config; do not add a second E2E runner. Confirm any database/service used by the harness is lane-private before running it.

The default Playwright config intentionally blanks provider credentials; it proves seeded UI flows, not provider-backed Composer acceptance. For the combined authoring/run flow, use a lane-private, already-running service with controlled provider adapters and the existing `playwright.staging.config.ts` via `STAGING_BASE_URL`. Configure its test identity through the existing staging harness, verify the target is the candidate, and add the bounded remediation scenarios there. For separately authorized live-provider checks, use `evals/composer-harness/hardmode/harness.sh --doctor` and the existing scenario/harness setup, then the staging config. Never point a mutation test at a shared deployment by default. R62 also needs WebKit/Safari keyboard verification; the default Chromium-only project does not prove that behavior.

At integration, contracts, graph/schema behavior, persistence and concurrency make the broad Python and PostgreSQL gates necessary. The script hardcodes `$WORKTREE/.venv/bin/python`; unlike focused commands, an explicit main interpreter is insufficient. Provision a real worktree-local `.venv` with the project's locked dependencies and required development/extras groups first, verify it is not a symlink, then run the script's dry run and provenance checks before execution:

```bash
cd "$WORKTREE" && scripts/full-suite-gate.sh --execute --detach \
  --root "$WORKTREE" --log-dir "$GATE_LOG_DIR" \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Read the completed `summary.txt` and `.done` evidence, including the script's literal `frozen=yes`; a moving tree invalidates the measurement. Run CI-only whole-tree checks whose inputs changed as described in CONTRIBUTING. The existing trust-tier signature corpus is a known separate gate: measure candidate versus base, repair new defects, and report remaining operator signing work. Never globally clear/re-sign suppressions or claim a shape-only run is authoritative signing. Other failed gates must be resolved or explicitly attributed with candidate/base evidence.

## 8. Combined acceptance matrix

| Flow | Required evidence |
|---|---|
| Upgrade | Old epoch-65/v1 store refused at startup; fresh current stores accepted on SQLite and PostgreSQL; malformed current Tier-1 evidence remains rejected |
| RAG | Profile-only Azure Search with default/custom fields and closed index pin reaches a required-fields consumer; truly absent fields rejected; configured execution uses the real provider boundary |
| Review → accept | Same-turn proposal remains acceptable after decision-only review; genuine stale proposal still 409; latest advisor verdict appears without reload |
| Review recovery | Mid-turn write then timeout/cancellation/recovery preserves durable blocking fact; graph rejection and transient outage have truthful, distinct retry behavior |
| Prompt review | Multi-query-only templates/system prompt review renders the actual reviewed material; approval works and stale material cannot be approved |
| Concurrency | Forced admission versus heartbeat/progress/ticket/approval/invalidation interleavings complete without a lock cycle and without accepting revoked authority |
| Accounts | Every live local admin retains intended rights; deleted/demoted identity and reused username do not; create/reset/delete include actor; partial deletion can be resumed after reload |
| Frontend races | Failed newer refresh does not lose a valid older snapshot; accept hydration can recover; stale-session responses cannot overwrite current state; 409 explanation remains visible |
| Run controls | Every button/palette/shortcut respects composer busy and attached pending/running runs; cancel-finalization recovery completes and original failures remain diagnosable |
| Audit | Effective rate settings match approval/config hash/Landscape; planner evidence is atomic and bound to its call; invalid provider prose has defined handling; server interpretation origin is truthful |
| Network | Distinct external clients produce distinct trusted IPs; untrusted forwarded headers cannot spoof them; ingress and firewall docs match Compose bindings |
| Tutorial/accessibility | Same provider-backed freeform backend; no retired Guided route/component required; actual empty/nonempty workspace E2E; keyboard scrolling in approvals; narrow-screen controls remain usable |

Use controlled provider doubles for deterministic error cases, with exact limits stated. Real Azure/live deployment acceptance is a separate result, not implied by the local matrix. Arrange any needed endpoint/credentials/operator authorization before live acceptance rather than silently skipping it.

## 9. Integration and completion rules

1. The coordinator verifies lane evidence from disk and Git, independently reads reviewer reports, and resolves every blocker before serial integration.
2. Rebase/merge candidates only against a named frozen base; run branch-safety-check before every commit/merge/rebase. Merge conflicts require renewed review of changed semantics and affected tests.
3. Keep an ordinary working ledger with all 79 IDs, owner, status, exact commit, regression, reviewer, and target integration SHA. Closure requires the fix/removal reachable from the target branch plus verification on its current behavior. A prepared branch, migration ticket closure, or lane's message is insufficient.
4. The final accounting must have 79 terminal, evidenced dispositions and zero omitted IDs. `Partial`, `blocked`, `needs live acceptance`, and `superseded but unverified` remain visible unfinished work. Do not close the whole task while any required repair is unresolved.
5. Finish broad validation on the assembled candidate, then reconcile it to the requested local release branch when implementation/integration is authorized. Re-verify ancestry and current HEAD afterward; preserve unrelated dirty/untracked work.
6. Retire only this run's merged worktrees with the canonical dry-run-first cleanup script. Archive unique ignored material before any authorized discard. Never clean sibling worktrees merely because they look old.
7. Report separately: locally implemented, independently reviewed, locally integrated SHA, scoped/broad test results, PostgreSQL proof, remote pushed SHA, signature state, and deployed/live-accepted SHA. Push, operator signing, destructive store reset, and deployment remain separate actions.

The first execution checkpoint should deliver the reproduced release blockers, reconciled ownership, and the first independently reviewed R01/R05/R07–R09 patches. The final deliverable is the repaired product and its measured acceptance, not a collection of agent completion messages.
