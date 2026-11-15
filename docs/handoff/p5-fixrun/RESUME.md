# Run p5 / p5b — paused 2026-11-14 (maintainer instruction)

Main worktree: <repo>/.claude/worktrees/compassionate-brahmagupta
Branch: claude/compassionate-brahmagupta-o0nhlw, remote tip b02c94dc (nothing from p5 landed yet).
Integrator session session_01Szt2FzuKZ4Y8WpNigvatoY is paused until our HANDOVER; no PR from our branch;
no pushes to claude/zen-turing-pphown until 0.8.13 lands (integrator's constraint).

## Rulings in force (2026-11-14)
- Fix behaviour to be correct; never reword docs/tests to match a defect; pre-release breaking changes wanted.
- No plugin gets special rules (truncate declares string inputs like siblings; strict removed).
- Fix every sibling of a bug, not one site. Sweep items 1-6, 11 approved; HTTP deadline lane approved.
- Pending maintainer answers: #27 CSV formula neutralisation default (+ sweep item 10 cloud CSV sinks),
  #83 Dataverse row-fault error codes, sweep item 9 (LLM/HTTP call status by delivered content vs transport).
- Item D list (Textract policy, Chroma retry classification, tracing extras, #161 `code`, guardrail re-billing,
  masquerade +2, #131) to be re-presented one at a time with evidence.

## Lane state (branch = lane/<ticket>, worktree .claude/worktrees/lane-<ticket>, reports in lanes/<lane>/report.md)
| lane | state | notes |
|---|---|---|
| lane-02-p5-batch | DONE, report saved | verify was interrupted by the pause: re-verify |
| lane-03-p5-web | DONE, report saved | |
| lane-06-p5-templates | DONE, report saved | RED commit has 5 tests; 6th arrived with fix |
| lane-04-p5-db-chroma | DONE (+a7fb9dfc DuplicateDocumentError removal), report saved | census shared file |
| lane-01-p5-146-truncate | paused mid-work | see its report/hand-back |
| lane-01-p5-http-deadline (run p5b) | paused mid-work | see its report/hand-back |
| lane-05-p5-llm-audit | paused mid-work | scope includes sweep F2 credentials |
| lane-07-p5-io-validation | paused mid-work | scope includes sweep F1/F5/F6 |
| lane-08-p5-llm-pool-deadline | not dispatched | after llm-audit |

## Landing procedure on resume
1. Re-dispatch paused lanes with `lane_manager.py resume --lanes-dir .claude/lanes/fixrun/lanes --run-id p5 --redispatch`
   (and run-id p5b) plus the brief in p5/<lane>.brief.txt; the worktree and WIP commits are where they left off.
2. Landing: `setsid nohup bash .claude/lanes/fixrun/p2/drive.sh <lanes...> >> .claude/lanes/fixrun/p2/drive.out 2>&1 < /dev/null & disown`
   in order batch, web, templates, db-chroma, then the rest. Driver = verify (RED/GREEN/bounded suite.sh) -> merge -> branch-safety -> push.
3. After each LANDED: `git ls-remote origin claude/compassionate-brahmagupta-o0nhlw`, send integrator CHANGELOG/COMPAT from report.md.
4. After the last lane: scripts/full-suite-gate.sh --execute --detach --workers 4 --stages ruff,mypy,contracts,lints,pytest,timing;
   known env-only failures: caddy, frontend esbuild, sqlite3 CLI, py3.12/3.13 sharding, shallow-clone ADR commit, live-provider matrix (pre-existing on base).
5. Send integrator HANDOVER; tell maintainer the box is free.

## Host
4 CPU / 15 GB; 23+ kernel OOM kills during the run. suite.sh now -n 3 with a 5 GB memory wait; lanes have a 4 GB MEMORY GUARD.

## Operator re-sign additions from p5
- batch_drift_compare R8 _categorical_shifts fp=dc863616c567763c -> 2522e1065c2c62bc

## Updates during pause
- lane-07-p5-io-validation PAUSED at d2fc6dab, report saved; next step = finish final -n 2 regression (2 unidentified failures). Review its "S3 CSV/JSON-array strict by design" claim.
- Merge note: lane-06-p5-templates and lane-07-p5-io-validation both change azure_blob_sink.py -> source_file_hash must be recomputed on the merged tree.
- lane-01-p5-146-truncate PAUSED at 4dec8792, report saved (lanes/lane-01-p5-146-truncate/report.md); next = broad gate rerun. column_keyed_config_keys kept (structural, not a lookup carve-out). plugin_version bump left to maintainer.
- lane-01-p5-http-deadline PAUSED (WIP) at d40f8805, report saved; next = composer rerun (3 undiagnosed), mypy + tier scan rerun, non-WIP commit. Conflicts with lane-03-p5-web on web_scrape.py (land web first, then resolve + recompute hash). Add SDK LLM paths (AzureOpenAI http_client, litellm) to lane-08-p5-llm-pool-deadline's brief.
- lane-05-p5-llm-audit PAUSED at 49aa21cd, report saved; next = mypy, module tests, tier scan, troubleshooting doc.
- HOT FILES across lanes (expect conflicts; recompute source_file_hash after each merge): azure_blob_sink (templates, io-validation, llm-audit), azure_blob_source (io-validation, llm-audit), web_scrape.py (web, http-deadline, llm-audit), azure/ai_search (http-deadline, llm-audit). Land order suggestion: batch, web, templates, db-chroma, truncate, io-validation, llm-audit, http-deadline, then pool-deadline.
- All 8 dispatched lanes are stopped; no agents or drivers running.

## Paused 2026-11-15 for hardware move (maintainer instruction)
- All 8 unlanded lane branches pushed to origin (lane/p4-146-truncate, lane/p5-{batch,web,db-chroma,templates,http-deadline,llm-audit,io-validation}); SHAs match local.
- Lane state + scratchpad packed (tree copies excluded) onto branch lane/p5-handoff under docs/handoff/p5-fixrun/; restore per its README.
- On the new host: recreate lane worktrees from the pushed branches (`git worktree add .claude/worktrees/lane-<x> lane/<x>`), then follow "Landing procedure on resume". Re-verify lane-02-p5-batch first.
- 2026-11-15 integrator (session_01Szt2FzuKZ4Y8WpNigvatoY) is also moving hardware: send per-lane CHANGELOG/COMPAT and HANDOVER to the NEW integrator session the maintainer names (old one may not read). Integration tip still 01f39577; no pushes to #368 until 0.8.13 lands.
