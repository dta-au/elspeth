# Reality Check — Hallucination Hunt, T14–T18 (composer-async-operations plan)

Reviewer: reality-d. Scope: T14 (frontend cutover), T15 (deploy/budget decoupling), T16
(PostgreSQL crash windows), T17 (epoch bump + doc sweep), T18 (integration gates + local
acceptance). Repo: the main checkout, release/0.8.1, read-only.

Method: grepped/read the actual tree for every symbol, file:line citation, package.json
script, CI job, test id, deploy-file range, and epoch-mirror fragment sampled below, and
diffed the plan's claimed content against the live text. ~85 distinct references checked
(well above the 40 minimum). All but two orientation-only line numbers matched exactly,
including several multi-line ranges pinned to the exact opening/closing line of a function,
test, or config block.

## Symbols and file:line citations verified EXISTS (representative sample)

| Reference | Status | Evidence |
|---|---|---|
| `client.ts:48` `MessageWithStateResponse` import | EXISTS | `src/elspeth/web/frontend/src/api/client.ts:48` |
| `client.ts:59-66` guidedDecoder import block | EXISTS | ends exactly at `:66` (`} from "./guidedDecoder";`) |
| `client.ts:243-490` `parseResponse` | EXISTS | fn starts `:243`; `response.json()` return at `:490` |
| `client.ts:889-928` sendMessage/recompose block | EXISTS | doc-comment starts `:889`; blank line after `recompose`'s `}` at `:928` |
| `client.ts:607` `AbortSignal.timeout(5000)` | EXISTS | `fetchSystemStatus` |
| `sessionStore.ts` `isAmbiguousComposeNetworkFailure` `:409-430` | EXISTS | doc-comment `:409`, fn body ends `:429`/blank `:430` |
| `sessionStore.ts` `resyncAfterAmbiguousComposeFailure` `:927-1026` | EXISTS | doc-comment `:927`, fn closes `:1025`, blank `:1026` |
| `sessionStore.ts:1502` / `:1538` interface lines | EXISTS | `sendMessage(...)`/`retryMessage(...)` signatures, exact lines |
| `sessionStore.ts:4796` reset() anchor | EXISTS (approx, inside fn) | `reset()` at `:4793`, `:4796` is inside its body |
| `guidedDecoder.ts:109-131` `exactRecord` | EXISTS | exact match |
| `guidedDecoder.ts:2297-2340` `decodeGuidedStartOperationReconciliation` | EXISTS | exact match |
| jsdom `29.1.1`, `AbortSignal.any` in TS dom lib `:2549` and jsdom impl `:44` | EXISTS | installed jsdom is 29.1.1; both line numbers exact |
| package.json scripts: `typecheck`, `test`, `lint`, `build`, `lint:css`, `test:e2e` | EXIST | all present, matches T14 gate claims |
| `ci.yaml:1197` `e2e-frontend:`, `ci.yaml:1296` `frontend-unit:` | EXISTS | exact line numbers |
| 6 mock-factory `sendMessage:`/`recompose:` key sites (App.test.tsx:274-275, sessionStore.test.ts:32-33, sessionStore.guided.test.ts:42-43, CommandPalette.test.tsx:57-58, commandRegister.test.tsx:51-52, inlineSourceIntegration.test.tsx:175-176) | EXIST | all 6 exact |
| `client.recovery.test.ts:9` imports `sendMessage` | EXISTS | exact |
| `config.py:53-73` ceiling comment block, `:338-347` Field description, `:1186-1208` validator, `:1210-1233` warn fn | EXIST | ranges match function/comment boundaries exactly |
| `full-suite-gate.sh` stage names `ruff,mypy,contracts,lints,pytest,testcontainer` | VALID | script's `case` accepts exactly these 6 (`scripts/full-suite-gate.sh:117`); default `ruff,pytest` matches AGENTS.md |
| `full-suite-gate.sh` flags `--root`, `--log-dir`, `--stages`, `--execute`, `--detach` (T18) | VALID | all parsed by the script |
| Epoch symbols `SESSION_SCHEMA_EPOCH = 67` (`models.py:366`), `_COORDINATION_HARD_CUT_EPOCH = 67` (`schema.py:40`) | EXIST | exact |
| 6 epoch-pin test sites (test_schema9_epoch.py:62, test_web_blob_fencing.py:3204, test_blob_inline_resolutions_schema.py:70, test_interpretation_events_table.py:248, test_proposal_blob_effect_receipts_schema.py:26, test_schema.py:368) | EXIST | all `assert SESSION_SCHEMA_EPOCH == 67` at cited lines, matching T17's own Files-section citations |
| T17 `epoch_mirrors.py` — sampled ~19 of 31 doc/runbook text fragments (CHANGELOG.md, README.md, docs/runbooks/*, website/get-started.html) at e=67 | ALL PRESENT | grep count matched the script's own claimed "known-positive: mirrors=31 present=30 missing=1" |
| The one claimed-MISSING mirror: `docs/runbooks/aws-ecs-deployment.md` "understands session epoch 35, not epoch 67" | CONFIRMED STALE AT "not epoch 66" | `docs/runbooks/aws-ecs-deployment.md:1653` reads "...not epoch 66." exactly as T17 Step 9 predicts |
| `test_cross_process_composer_postgres.py` `_http_lifecycle_process`/`:43-121`, `_serve_lifecycle_commands:48`, `_receive`/`_command:200-207`, `test_killed_publisher_leaves_committed_snapshot_for_fresh_peer:507-537` (T16 model file) | EXIST | all exact |
| `tests/testcontainer/web/conftest.py:31-36` `_require_sequential_postgres_acceptance` rejects xdist | EXISTS | confirmed (function is `_require_sequential_postgres_acceptance`, checks `is_xdist_worker`) |
| `pyproject.toml` addopts deselects `testcontainer` marker by default | CONFIRMED | `pyproject.toml:455` |
| `deploy/compose/nginx.conf:33-34` `proxy_read_timeout/proxy_send_timeout 360s` (T15) | EXISTS | exact |
| `node_modules/playwright` resolvable via `require()` from the frontend worktree (T18 `accept.cjs`) | CONFIRMED | present as a transitive dep of `@playwright/test`; `require('./node_modules/playwright').chromium` resolves |
| `tests/unit/deployment/test_aws_ecs_terraform_package.py:3643` `test_documented_minimum_image_revision_is_the_true_settings_floor` (T15) | EXISTS | exact |
| `test_web_settings_exports_resolve.py:97` inside `test_every_exported_web_setting_resolves_to_a_live_field` (T15) | EXISTS, content matches | file lives at `tests/unit/deployment/test_web_settings_exports_resolve.py` (T15 cites it by bare filename only, no directory — not wrong, just under-specified relative to the plan's usual full-path convention) |
| `tsconfig.e2e.json` exists but no npm script typechecks it (T14 claim) | CONFIRMED | only `typecheck` (app+oidc) and `typecheck:workspace-e2e` (workspace-e2e) scripts exist; no script targets `tsconfig.e2e.json` |

## Findings

### Minor — T18 nginx.conf citation range is off by ~7 lines
- **File/loc:** `docs/plans/2026-09-20-composer-async-operations/T18.md:1067`
- **Claim:** "It keeps the `location /` shape of `deploy/compose/nginx.conf:18-37`"
- **Reality:** In `deploy/compose/nginx.conf` (37 lines total), the `server {` block starts at line 12 and
  `location / {` starts at line 25, closing at line 36 (server's own closing `}` is line 37). Line 18 falls
  inside the server preamble (`client_max_body_size`/`access_log`/`error_log` comments), not the `location /`
  block. The intended range is closer to `25-37`.
- **Impact:** Cosmetic only. The acceptance `nginx.conf` T18 actually writes (Step 12, lines 1071-1111) is a
  self-contained literal block, not derived by copying the cited range at execution time, and the plan text
  elsewhere is explicit that "every edit here is keyed on exact text, not on line numbers" (this convention is
  stated in T15 but not verbatim in T18 for this narrative sentence — it's descriptive prose, not an edit
  instruction). No executor action depends on this range being exact.
- **Fix:** Correct the citation to `deploy/compose/nginx.conf:25-37` (or `12-37` if the full `server{}` block is
  meant) in a later editing pass; not blocking.

### No other reality-check defects found
No hallucinated symbols, no wrong function signatures, no invented test files, no invalid CLI flags, no
mismatched epoch-mirror text, no non-existent deploy files, and no incorrect package.json / CI job references
were found across T14–T18. Every multi-line function/test/config-block range checked (30+ of them) began and
ended exactly where the plan says, including several that were only approximately describable from prose (e.g.
"from the line X through the closing Y") and turned out to match to the line. The T17 epoch-mirror sweep in
particular reproduces a genuine, currently-true stale-text defect in the live tree (`aws-ecs-deployment.md`
still says "not epoch 66") that the plan's own instrument would catch — this is strong evidence the mirror list
was generated by actually running the script against the tree, not hand-typed from memory.

## Confidence Assessment

**Overall Confidence:** High

| Finding | Confidence | Basis |
|---|---|---|
| nginx.conf:18-37 citation is imprecise | High | Directly counted lines in the live file; `location /` starts at 25 |
| No hallucinated symbols/paths in T14–T18 | High | ~85 distinct citations checked against the live tree with grep/sed/Read, near-100% exact match rate including precise multi-line ranges |
| full-suite-gate.sh stage names/flags are valid | High | Read the script's own `case` statement and flag parser |
| Epoch mirror list is accurate | High | Sampled 19/31 fragments directly; the one claimed-stale fragment was independently confirmed stale in the live file |

## Risk Assessment

**Implementation Risk:** Low (from a hallucination/reality standpoint specifically — this review does not assess
architecture, ordering, or test-coverage risk, which are other reviewers' lenses).
**Reversibility:** Easy (the one finding is a doc-citation typo fix).

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Executor follows the imprecise nginx.conf line range and edits/reads the wrong 7 lines while orienting | Low | Low (the plan's Step 12 supplies the literal replacement file, not an edit against that range) | Correct the citation to `25-37` |

## Information Gaps

1. [ ] **T14 Steps 5–20 and T16/T18 sections beyond what was sampled**: this review read T14 in full (both halves)
   and sampled T15/T16/T17/T18 heavily but not byte-for-byte end to end (each file runs 1100–4600 lines). Given
   the ~85/87 exact-match rate observed across every sampled section of every task file, further sampling is
   very unlikely to surface additional hallucinations, but a full line-by-line pass was out of budget for this
   lens pass alone.
2. [ ] **Bicep/ACA-specific line citations in T15 Step 11+ (not shown above)** were not individually re-verified
   against `workload.bicep` and `validate-workload-parameters.jq`; T15's own Step 1 positive-control grep sweep
   (lines 108-127) already re-verifies these against the live tree with recorded expected hit-counts, which is
   itself evidence of care, but I did not independently re-run that grep.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] This lens covers reality/grounding only — not architecture, ordering, or whether the tests as written
  actually assert the right things. See the other reviewers' output for those lenses.
- [ ] Re-run this check if the plan is edited further, especially around T18's nginx.conf citation.

### Assumptions Made
- The live tree at the time of this review (release/0.8.1, uncommitted `docs/plans/...` files per git status) is
  the correct baseline the plan claims to be measured against (`d479eb2b4` / `ea5fa50d5`); I did not check out
  those exact commits, only the current working tree, which the plan itself states is byte-identical for every
  path these tasks touch.

### Limitations
- Static existence/text-match only; does not execute any test, terraform plan, or bicep compile.
- Did not verify every one of the ~450+ individual `old_string`/`new_string` Edit-tool blocks across T14–T18
  byte-for-byte against the live files — sampled representative ones per task with a strong bias toward the
  ones named in the assigned lens (frontend files/functions/tests/mocks, package.json scripts, deploy
  files/tests, epoch mirror list, full-suite-gate.sh flags, nginx/compose acceptance setup).
