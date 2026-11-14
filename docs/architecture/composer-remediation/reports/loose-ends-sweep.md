# Loose-ends sweep of integrate/composer-remediation-0.8.12

Read-only sweep. Scope: the 17 remediation commits (`5e192342c^..dd3b3524c`, 120 files with the first commit; `5e192342c..dd3b3524c` is 16 commits and 119 files), the handover commits `25301aa93` and `a1cc53b6d`, and the merge `4023a0658` of release/0.8.11-candidate. HEAD at the time of the sweep: `5bac5ba89`. No source, test or existing doc was edited. Raw command output for everything measured is under `.claude/lanes/composer-remediation-wave1/loose-ends/` (local; names given per finding).

Legend: VERIFIED = a command was run and its output is quoted or logged. INFERRED = read from code or docs, not executed.

## Summary

Blockers: (1) two stale whole-file hash pins plus a second recovery-registration call site break the reserve-admission proof and 21 fast tests; (2) the 0.8.11 candidate's own new test trips the static direct-writer gate. Should fix: lane commit `a1cc53b6d`, stale skill guidance for no-op/refused proposals, a dead boundary-annotated helper, checklist/RESUME drift, unverified integrated tree. Nothing half-landed was found in the rest of the range (no markers, no unreferenced new symbols, D2 root authority on all 11 path sites).

## Ranked findings

### 1. BLOCKS 0.8.12: two full-source guard pins are stale (VERIFIED)

`tests/helpers/required_work_reserve_recipes/reviewed-full-source-guards.json` pins a sha256 of the whole file for `src/elspeth/web/async_workers.py` and `src/elspeth/web/required_executor.py`. Commit `dc67bdd2f` changed both (+62 and +15 lines) but did not refresh the pins; `e192aa135` was the last time the JSON was refreshed.

Evidence (`full-source-guard.log`): the gate's own function `_reserve_full_source_failures` from `tests/unit/architecture/test_web_landscape_mutation_fencing.py`, called on the live tree, returns

    LIVE failures: ['full source changed src/elspeth/web/async_workers.py', 'full source changed src/elspeth/web/required_executor.py']

Control: the same function on the sources at `e192aa135` returns `[]`. File hashes: pinned `40b670cb74242c59…` / `4ce38dc66023afa5…`; live `3e97fa7f9a9106fa…` / `c2477c7d174b8cb3…` (identical at `dc67bdd2f`, `dd3b3524c` and HEAD).

Consequence (VERIFIED): 21 tests fail in `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` (`gates2.log`: 21 failed, 676 passed, 1 xfailed; every failure carries the same two `full source changed` strings), and the official proof `test_required_work_reserve_admission_on_actual_sources` reports the same global failures, see 1b.

Smallest action: review the 77 added lines in those two files as the pin demands (the guard exists to force that review), then re-pin the two hashes in the same commit. Do not blanket-repin.

### 1b. BLOCKS 0.8.12: the "required-work reserve admission" proof fails on the integrated tree, with a second cause (VERIFIED run, INFERRED attribution of the 32 rows)

Ran the body of `test_required_work_reserve_admission_on_actual_sources` directly (`_reserve_run(_production_units())`, script `run_reserve_proof.py`, 88 s on the merged tree), without pytest and without the heartbeat harness, against three trees. The two control trees were extracted with `git archive` into the loose-ends directory (since deleted) and run with `python -I` and an explicit `sys.path`:

| Tree | `global_failures` | `unproven_web_rows` |
| --- | --- | --- |
| merge-base + candidate tip `a67628c29` (control, no remediation) | `[]` | 0 (`reserve-proof-cand.log`) |
| remediation base `5e192342c^` (merge-base + `b8921de06` + `f598aa43d`) | `['writer target/field recipe changed src/elspeth/web/sessions/routes/composer/proposals.py:accept_composition_proposal']` | 32 (`reserve-proof-base.log`) |
| merged HEAD | `['full source changed src/elspeth/web/async_workers.py', 'full source changed src/elspeth/web/required_executor.py', 'recovery registration call/escape universe changed']` | 32 (`reserve-proof.log`) |

Reading: the proof is clean on the candidate and was already red on the remediation base (one recipe pin, repaired later by `e192aa135`, which is why the base-era "02.03 verified" claim did not see it). On HEAD the three remaining global failures are all new since `e192aa135`:

- the two stale full-source hashes of finding 1;
- `recovery registration call/escape universe changed` from `tests/helpers/web_recovery_producer_contract.py:793`, which requires exactly one call to `configure_required_executor_recovery`. `dc67bdd2f` added `configure_standalone_executor_recovery` (`src/elspeth/web/async_workers.py:103-118`), a wrapper that calls it, so the call universe is now two. `grep -rn configure_required_executor_recovery src` shows the second call at `async_workers.py:113`.

The 32 "unproven web rows" (identical row set on base and HEAD) are all absent on the candidate. INFERRED, not shown: they are a consequence of the failed global checks rather than 32 independent defects, since the candidate with the same recipes has zero. Confirm by re-running after the three global failures are repaired.

Smallest action: (a) re-pin the two hashes after review; (b) decide whether the standalone wrapper is a legitimate second registration (then extend the reviewed producer contract and its negative controls, `tests/helpers/web_recovery_producer_contract.py`) or should be inlined so the web app stays the only caller; (c) re-run the three official proofs through `scripts/ci_proof_progress.py` as RESUME.md prescribes. RESUME.md states the proofs "have not run on dd3b3524c"; this is what they would have reported.


### 2. BLOCKS 0.8.12 (inherited from the candidate): `test_static_direct_writers_match_reviewed_allowlist` fails on the merged tree (VERIFIED, cause inherited from the 0.8.11 candidate)

Run (`gates1.log`, 1254 passed, 1 failed):

    FAILED tests/unit/web/sessions/test_static_direct_writers.py::test_static_direct_writers_match_reviewed_allowlist
    Unallowlisted direct writer sites:
      tests/integration/web/test_shareable_reviews_routes.py:606 [chat_messages/sqlalchemy_table_insert] in test_preview_reports_llm_authored_file_as_not_importable

The site was added by the candidate (shared graph import, `10d527e0f`/`280ff5440`). `git show origin/release/0.8.11-candidate:tests/unit/web/sessions/test_static_direct_writers.py | grep test_shareable_reviews_routes` prints nothing, so the candidate carries the same unallowlisted writer (INFERRED that its own tree fails the same way; the remediation commits only added three unrelated allowlist entries). The remediation added entries and the candidate added a writer; neither side textually conflicted.

Smallest action: add a justified `ReviewedWriter(path="tests/integration/web/test_shareable_reviews_routes.py", enclosing_symbol="test_preview_reports_llm_authored_file_as_not_importable", table="chat_messages", operation="sqlalchemy_table_insert", purpose=...)` to `_REVIEWED_ALLOWLIST`, or route the seed through an existing helper. Note the memory file lists a maintainer-approved xfail on the "sessions writer-review test"; this one is a plain failure, not an xfail, so confirm which test that note means before touching it.

### 3. SHOULD FIX before release: handover commit `a1cc53b6d` must not ship (VERIFIED counts)

See the section "Repo hygiene" below for the evidence and the removal recipe.

### 4. SHOULD FIX: skill text did not follow the new refusal/no-op behaviour (VERIFIED code, INFERRED model impact)

`git log 5e192342c^..dd3b3524c -- src/elspeth/web/composer/skills` is empty: neither `pipeline_composer.md` nor `pipeline_capabilities.md` was touched by the 17 commits, but behaviour the model sees changed.

a. Explicit-approve sessions: `tool_batch.py` (`_unapplied_admission`, lines 218-250) now returns a refused or no-op mutation as direct tool feedback and publishes no proposal. For a no-op it returns a success result with `applied_component=None`, empty `affected_nodes`, no `validation_delta`, and the post-call hint "The pipeline is unchanged; no proposal was submitted for approval." The skill's failure-data block (`pipeline_composer.md:460-469`) still teaches only two outcomes, `APPROVAL_REQUIRED` and `PREVALIDATION_REJECTED`. Nothing tells the model that a successful result with no echo means "unchanged, nothing staged", while rule 5 (line 44-48) says to call `get_pipeline_state` only for "a successful mutation that omitted the echo". A model following the skill will re-read state after every no-op, and may tell the user a card is waiting.
b. `pipeline_composer.md:535-541` teaches `post_call_hints` as "plugin-authored next steps". The no-op hint above is server-authored.
c. `pipeline_composer.md:317-323, 640-650, 1441` repair a rejected source path by `create_blob` + `source.blob_id`. Over MCP (blob authoring is not exposed; `server.py:118` "excludes blob and secret tools") the new refusal text is "Local file paths require configured data root authority and must remain within the configured data root (--data-dir)." (`tools/_common.py:2336-2337`). If the MCP route serves this skill, its repair advice names tools the client does not have (INFERRED; not measured whether MCP serves the skill text).
d. Planned guidance-parity work (`reports/guidance-parity-design.md`) was not implemented, so none of this is covered by a pending commit.

Smallest action: add one taught-block sentence for the unchanged/no-proposal result and correct the `post_call_hints` wording, then re-pin the skill hash and the taught-block parity gate (the taught-block parity gate and the skill-hash pins; I did not identify the exact test files). Mind the standing rule that editing skills mid-suite causes hash-mismatch errors.

### 5. SHOULD FIX: dead function with a live trust-boundary annotation (VERIFIED)

`src/elspeth/web/composer/tools/blobs.py:520-533` `_blob_id_uuid_validation_error` (decorated `@observation_boundary(... suppresses=("R5",))`) has no caller left in `src/` or `tests/` after `05bcab939`; every site now uses `canonical_resource_uuid` plus `_BLOB_ID_UUID_ERROR`. Command: `grep -rn _blob_id_uuid_validation_error src tests` returns only the definition. The decorator carries an R5 suppression for code that no longer runs, so the tier-model gate (INFERRED: its allowlist or baseline may still carry an entry for it) is paying for dead code. `blob-identity-review.md:31` says the helper "retains its explicit non-string refusal", which is now unreachable; non-string ids rely on pydantic models upstream (all callers checked: `canonical_resource_uuid(123)` raises `AttributeError`, so a future caller that skips the model will crash instead of refusing).

Smallest action: delete the function and its boundary annotation (and its allowlist/baseline entry), or have the nine remaining sites use it. Keep `_BLOB_ID_UUID_ERROR`.

### 6. SHOULD FIX: CHECKLIST.md and RESUME.md drift (VERIFIED by reading and grep)

- 09.01 and 09.02 (non-string / NUL path crash) are open boxes (`CHECKLIST.md:331,333`) but fixed in code by `1bc91710f`: `is_local_path_value` at `tools/_common.py:2357, 2402, 2416, 2463` with "...without NUL" refusals, defined in `web/paths.py:41`. Already identified by `reports/quick-wins-triage.md` (untracked in the main checkout, not in this branch).
- 02.07 to 02.12 are labelled "In progress" although each cites a landed, reviewed commit (`ef14c2fc1`, `cf23f38d8`, `e3d07d59d`, `14fef5e53`, `66739807c`). What remains is only "combined gates and branch integration", not code. Nothing in code is missing for them that the text names; 02.07 covers update/delete blob and state-only secret/blob markers (`tool_batch.py:1742-1750`, `admit_blob_mutation` at `tools/blobs.py:1757`) and `create_blob` is deliberately excluded (comment at `tool_batch.py:1745-1747`).
- Counting: 5 boxes checked, 315 open (`grep -c`). Items the text itself calls "Reviewed and integrated" (03.01-03.05, 02.17, 02.18) stay unchecked by the tracker's own rule; the headline is accurate but unhelpful for a release decision.
- RESUME.md contradicts `a1cc53b6d`: "Raw logs, temporary mutants ... remain local ... intentionally not committed" and "Do not commit the ignored lane directory" (Durable material section) versus the commit that published exactly that directory. Only the opening paragraph carries a "superseded" note. RESUME.md, CHECKLIST.md and 15 reports cite `.claude/lanes/composer-remediation-wave1/...` paths (42 occurrences total, `grep -rc`); they dangle once the lane is removed (finding 3).
- RESUME.md also names `/workspace/elspeth` and an external chatgpt.com "live checklist Page" as the authority ("later Page edits take precedence"); the repository checklist is therefore not self-contained. Same link in `reports/next-remediation-candidates.md`.
- RESUME.md says the branch "is a work-in-progress backup, not a declaration ... ready to merge" and "No merge to main, PR or release is part of the pause operation". Both will be false text on a release branch.

Smallest action: tick 09.01/09.02 with the evidence above; relabel 02.07-02.12 "landed, awaiting combined gates"; replace the RESUME opening with a 0.8.12 status paragraph or move RESUME.md and the lane-citing reports to a clearly historical location.

### 7. SHOULD FIX: nothing has been verified on the integrated tree (VERIFIED from RESUME.md and the merge log, INFERRED consequences)

- RESUME.md: ordinary tests, the three proofs and the PostgreSQL selection "have not run on dd3b3524c". The lint corpus is 5,454 findings against a 5,433 baseline and is still unadjudicated (RESUME "Pending combined work" item 1). The merge then added 33 more commits from the candidate (82 files), so nothing measured at `dd3b3524c` carries over without checking.
- The only post-merge run recorded is `.claude/lanes/composer-remediation-wave1/merge-0811-conflict-tests.log` (25 passed, two conflicted files).
- This sweep ran a few cheap gates; see "Gate results".

### 8. COSMETIC / watch list

- CHANGELOG.md has no 0.8.11 entry and `pyproject.toml` still says `0.8.10` on this branch and on the candidate. The new `## 0.8.12 - Unreleased` entry (commit `5bac5ba89`) says the package version has not been advanced. The entry omits the new error code `proposal_resource_unavailable` (`error_codes.py:202`, raised at `tool_batch.py:2319`) and the new rejection reason `no_change` (`proposal_authority.py:102`, `protocol.py:129`, `pipeline_settlement.py:496`, frontend `proposal_no_change` at `sessionStore.ts:1425`). Both are emitted and consistent across their two registries; neither is documented in prose.
- Commit-count wording: the "17 commits" include `5e192342c` itself; `5e192342c..dd3b3524c` is 16 commits.
- `tool_batch.py:2085-2093` comment "This is now the APPROVAL_REQUIRED envelope's shape on the same path" is ambiguous about which path.

## What was checked and found consistent (VERIFIED unless marked)

- No TODO/FIXME/xfail/`noqa`/`type: ignore` markers were added in `src`, `tests` or `config` by the range (`git diff -U0 | grep` over added lines; only three `skipif(sys.platform != "linux")` on Linux-only process controls and the word "temporary" in path-variable names).
- No new function or class in `src` is unreferenced: all 106 names added by `def`/`class` in the range have at least one use besides the definition (loop over `git diff` names with `grep -w`).
- All 11 path-validation call sites in `composer/tools/{sources,sessions,outputs,transforms}.py` pass `mcp_root_authority=context.mcp_root_authority` (grep count 11 of 11), so D2 admission is on every tool route that takes a local path.
- Soft-mapping census on the merged tree: `python -m scripts.check_contracts` exit 0, "2617 soft occurrences across 418 files" (`check_contracts.log`). The two sides' census edits (remediation moved 2 entries, candidate added 2) combine correctly.
- `canonical_resource_uuid` receives only pydantic-validated strings on all callers (checked `_resolve_source_blob`, `_resolve_source_blobs`, `set_pipeline` source).
- Overlap of the two diffs is only four files: `config/cicd/soft-mapping-census.yaml`, `tools/blobs.py`, `test_composer_against_gateway.py`, `test_endpoint_affordance.py`. The `blobs.py` hunks are disjoint (candidate: `_set_nested_option`/`_apply_inline_blob_marker` replace_existing; remediation: canonical ids, admission).

## Repo hygiene: what `a1cc53b6d` must not ship

### Measured (VERIFIED)

- 11,685 tracked files under `.claude/lanes/composer-remediation-wave1/` (`git ls-files | wc -l`); 11,688 files changed in the commit in total, +2,796,856 lines. Working-tree size of the directory is 1.2 GB (`du -sh`); the tracked plain-git content is about 248 MB (`git ls-tree -r -l`), the pack is 212 MiB, and 599 files are Git LFS objects (`git lfs ls-files`): Playwright Chromium, caddy and its tarball, two sqlite `.deb` files, an ONNX model and its tarball.
- Content that is not documentation: 3,298 `.py` files, of which 3,227 sit under `*/src/` (full copies of `src/elspeth` and `elspeth_lints` used as mutant and baseline trees: `d1-stage-race-baseline`, `park-identity-evidence/{negative-control,fix1-refusal-mutant}`), 597 `.log` files, `.exit`/`.frozen-exit` receipts, multi-MB JSON lint corpora (`combined-wave-before.json` 5.9 MB, `d1-backend-issues.json` 2.2 MB), 17 ELF binaries (sqlite3 binaries are plain git, not LFS), and 1,662 tracked symlinks whose targets are absolute `/workspace/elspeth/...` paths (first 400 sampled: all 400 start with `/workspace/elspeth`).
- `/workspace/elspeth` appears in 550 lane files; none of the lane files contain `/home/john` (`git grep -l`), so the user-home-path privacy test is not tripped. 32 files under `docs/architecture/composer-remediation` and `composer-tool-assessment` also embed `/workspace/elspeth` (63 occurrences); these would stay with the docs.
- Credential-shaped strings: a scan of the lane for `AKIA…`, PEM private-key headers, `ghp_…` and `sk-ant-…` found nothing (control: the AKIA pattern matches a synthetic key). The lane's own `.gitignore` already admits that some credential-shaped files were left out, so the commit is a filtered copy, not a clean one; the filter is a filename list, not a content check.
- `.gitattributes`: before `a1cc53b6d` the file existed and was empty (0 bytes at `6825994b4`); the commit added five LFS rules, all scoped to the lane directory. `.gitignore`: `.claude/lanes/` became `.claude/lanes/*` plus `!.claude/lanes/composer-remediation-wave1/` and `!.../.gitignore`. Effect: every new file anyone writes into this lane directory is now untracked-visible (`git status` here already shows `merge-0811-conflict-tests.log` and this sweep's `loose-ends/`).
- LFS: `.github/workflows` has no `lfs` setting (grep empty), so CI checks out pointer text for those 599 files. `filter.lfs.required=true` is set in this checkout's git config, so a clone without git-lfs fails on checkout of this branch.
- `fix/composer-tool-remediation` (with `a1cc53b6d`) is already on `origin` (`git branch -r --contains a1cc53b6d`), so the blobs and LFS objects exist remotely. Dropping the commit from this integration branch does not remove them from that remote branch.

### What the gates and tests do with the directory (VERIFIED where run)

- The lint walker excludes `.claude/lanes` (`tests/unit/elspeth_lints/test_ast_walker.py:109`, `test_python_file_walker_authority.py:100`), and the whole-tree helper `tests/helpers/tree_gate.py` drops gitignored files via `git ls-files --others --ignored`, which no longer covers a tracked, un-ignored lane directory. It does still go through `iter_python_files`, which carries the exclusion. Ran `test_agent_docs_privacy`, `test_deploy_ignore_policy`, `test_python_file_walker_authority`, `test_pre_commit_triggers`, `test_adr_public_integrity`: 80 passed (`hygiene-tests.log`). `test_static_direct_writers` scans `src/` and `tests/` only. `scripts/check_contracts.py` scans `src/`. `pytest` `testpaths = ["tests"]`; ruff has no lane exclusion (`pyproject.toml:320` excludes only lint fixtures) so a bare `ruff check .` or `mypy .` from the repo root would walk 3,227 mutant `.py` files, though pre-commit passes explicit paths.
- `test_agent_docs_privacy` only sees `*.md`; 139 tracked markdown files live under `.claude/lanes`; it passed.
- INFERRED, not run: whole-repo tools run from the root (`ruff check .`, secret scanner over a touched file's old lines, `git grep` based sweeps) will see this tree; the pre-commit secret scanner rescans every line of a touched file, and a future edit to any lane file re-triggers it.

### Minimal clean removal (not performed)

1. `git rm -r --cached -q .claude/lanes/composer-remediation-wave1` (keeps local files for whoever still needs them; use `git rm -r` to delete).
2. Restore `.gitattributes` to the empty file it was: `git checkout 6825994b4 -- .gitattributes`.
3. Restore the `.gitignore` hunk: replace the three lines `.claude/lanes/*`, `!.claude/lanes/composer-remediation-wave1/`, `!.claude/lanes/composer-remediation-wave1/.gitignore` with the single original line `.claude/lanes/`.
4. Keep `docs/architecture/composer-remediation/**` and `docs/architecture/composer-tool-assessment/**`; they are outside the lane. Revert the lane paragraph that `a1cc53b6d` added to the first paragraph of `RESUME.md` (it links to `REMOTE-RECOVERY.md`, which disappears with the lane), and decide separately what to do with the 42 doc references to `.claude/lanes/composer-remediation-wave1/...` (finding 6): they are evidence citations; a one-line "local only, not shipped" note at the top of `reports/` is enough.
5. History: a plain `git rm` commit still leaves the 212 MiB pack and the LFS objects in the branch history. If the release must not carry them, rebuild the integration branch without `a1cc53b6d` (cherry-pick the 17 commits, `25301aa93` and the 0.8.11 merge onto a fresh branch) or land with a squash merge. This is a decision for the maintainer; the sweep did not touch history.

## Gate results (run in this worktree, provenance verified)

All runs used `PYTHONPATH=$W/src:$W/elspeth-lints/src` and `-o pythonpath=...`; `elspeth.__file__` resolved inside the worktree (printed in `full-source-guard.log`). Logs are in `.claude/lanes/composer-remediation-wave1/loose-ends/`.

| Check | Result |
| --- | --- |
| `python -m scripts.check_contracts` (includes soft-mapping census) | exit 0, census 2617 soft / 418 files |
| attribute-contracts, masquerade, dag-scenario corpus, credential boundary inventory, static direct writers, shareable_reviews unit dir, tool-result envelope gate, tools discovery, blob inline tools (1255 tests, `-n 4`, `gates1.log`) | 1254 passed, 1 failed: finding 2 |
| reviewed-contracts, session-DB mutation authority, operation fence wiring, routes_state, shareable-reviews routes integration, async worker settlement (698 tests, `-n 4`, `gates2.log`) | 676 passed, 1 xfailed, 21 failed: all 21 in `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`, all with `assert ['full source changed src/elspeth/web/async_workers.py', 'full source changed src/elspeth/web/required_executor.py'] == []`: finding 1 |
| hygiene tests listed above | 80 passed |

Also run: the proof body for three trees (finding 1b). Not run (outside the brief): full suite, the three official proof tests as pytest, PostgreSQL testcontainers, frontend Vitest, the lint corpus.
