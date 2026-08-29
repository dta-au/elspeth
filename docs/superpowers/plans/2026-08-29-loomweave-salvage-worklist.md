# Loomweave salvage worklist — 2026-08-29

Condition of Loomweave's approval in [ADR-043](../../architecture/adr/043-project-tooling.md).
Every item cites the measurement that justifies it (three read-only lanes on
2026-08-29: transcript forensics over 180,517 Claude Code + 11,478 Codex tool
calls; a live capability probe against `ast`/`git` ground truth; a cost and
reliability profile of `.weft/loomweave/` and the run history). Product items
belong in the loomweave repository; project items are ELSPETH's.

**Provenance stamp.** Rows marked *pre-rebuild* were measured on the index as
the hook-launched runs left it (see A1); rows marked *post-rebuild* were
re-measured on 2026-08-29 after the venv-shell `loomweave analyze
--no-incremental` run `f085278d` (08:25–09:03Z). The distinction matters:
A3 and A4 shrink sharply once A1 is removed from the picture, and the salvage
decision in ADR-043 turns on exactly that.

## A. Product — reliability (what makes answers wrong or missing)

| # | Defect | Evidence | Fix shape |
|---|---|---|---|
| A1 | Hook-launched analyzes run without the project venv, so test files resolve zero calls into `elspeth.*` while marked `complete`; the incremental hash-skip then pins the empty rows | 400 of 1,834 function-bearing test files (1,946 indexed) under the **fixed** plugin, *pre-rebuild*. Bisected on one file with one plugin build: venv `python` on PATH → 13 cross-module edges; `VIRTUAL_ENV` alone or clean env → 0; `complete` either way. Filed as clarion-5cf9643de9. **Repaired on ELSPETH** by the venv-shell full run: tests→`elspeth.*` edges 14,708 → 47,579; test files with ≥1 resolved src call 364 → 1,426. Any hook-launched run re-pins whichever files it touches until the product fix lands | Resolve the interpreter from the project (`.venv`, `uv`) in `hook git-sync` and pass it as `python.pythonPath`; mark coverage `degraded(interpreter_mismatch, transient)` when first-party imports are unresolvable instead of `complete`, so the existing re-dispatch heals it |
| A2 | Class instantiation never becomes a `calls` edge — the graph holds **zero** `calls` edges targeting any `python:class:*` entity | `AuditIntegrityError`: 2,235 unresolved sites, 0 edges, `callers: []`; `PresentBase` 41/0. *Post-rebuild*: of 225 test files with `from elspeth` imports and no src call edge, 75 only instantiate classes (`test_schemas`, `test_models`, `test_config`…) | Treat `Name(...)` resolving to a class entity as a call to the class (or a `constructs` edge) and surface it in `entity_callers_list` |
| A3 | Per-file budget and reference cap still degrade a handful of very large files | *Post-rebuild*: 3 of 3,093 files degraded — `tool_batch.py` (2,354 lines) calls+refs `pyright_timeout` after 4 redispatches; `sessions/service.py` (13,645 lines) refs `reference_site_cap`; one further refs-cap file. The *pre-rebuild* probes showing direct calls in ≥1.5k-line **test** files as `unresolved why: dynamic` were confounded with A1: post-rebuild the 4,912-line `test_chat_solver.py` resolves 157 cross-module calls in 8.1 s. Re-probe before citing size as the cause for test files | Chunked reference resolution instead of a hard cap; label the file `degraded` in the answer, not just in a coverage table (the size-proportional budget has landed, #118) |
| A4 | Stale rows survive a full rebuild in the same file; tombstones and deleted-facts accumulate under incremental runs | **Genuine and post-rebuild**: 4 `create_app.<locals>.*` routes (`health`, `ready`, `system_status`, `_prometheus_metrics`) exist twice in `src/elspeth/web/app.py` — stale rows at lines 1358–1424 beside live rows at 1775–1852 — after `--no-incremental`. **Incremental-only**: the 937 src function tombstones and 1,016 `LMWV-FACT-ENTITY-DELETED` facts (185 for entities back on disk) measured *pre-rebuild* are gone post-rebuild (0 src files missing; stale-finding sweep retired 1,051). The "227 routes vs 86 real" figure is mostly test-fixture routes (95 src + 127 tests + 5 other; src 95 vs ~91 decorators) — not tombstones | Retire same-file duplicate rows on every run; under incremental runs, prune rows whose `last_seen_commit` is no longer reachable and retract deleted-facts when the entity reappears. Note `index_diff_get` records missing-file tombstones as retained **by design** (clarion-23a44085f9, REQ-ANALYZE-04): the prune proposal has to argue against that decision explicitly |
| A5 | `briefing_blocked: secret_present` hides docstrings for 10,708 entities (170 files, 31 % of files >3,000 lines) — the scanner flags sha256 fixture digests | 373 of 563 secret findings are `HighEntropyHex` 64-hex digests (*post-rebuild*; 342 *pre-*); the tree carries **168** `# secret-scan: allow-this-line` marker lines (`git grep -c`, summed) that Loomweave ignores; semantic search missed `redaction.py` for this reason | Honour the project's allow-marker; suppress high-entropy-hex on lines that are test fixtures/digest literals; block the *line*, not the file's every entity |
| A6 | `entity_unresolved_call_sites` is dominated by builtins and stdlib names | 279,030 rows *post-rebuild* (287,871 *pre-*: a full rebuild only removed 3 %, so the volume is the builtin flood, not accumulation): `len` 10,014, `pytest.raises` 9,663, `str` 8,888, `isinstance` 7,277, `type` 5,161 | Drop builtin/stdlib names at extraction; keep only sites that could resolve to a project entity |

## B. Product — operations (what makes it expensive or untrustworthy to run)

| # | Defect | Evidence | Fix shape |
|---|---|---|---|
| B1 | Re-analyze after every commit/merge/checkout on a shared checkout | 12–148 runs/day over the last 10 days (`runs` table); ~45 s floor for a 1-file change; 9 of 39 runs failed on 08-29 (5 to the host's 120 s per-file watchdog, which the plugin's 90 s cap plus the references pass can exceed) | Debounce (coalesce runs within N minutes) and/or trigger on `post-merge` only; skip when the diff touches no indexed roots; reconcile the host watchdog with the plugin budget |
| B2 | Process leaks: one `loomweave serve` per MCP client never reaped; orphaned pyright from the first post-fix run | 14 `serve` + 3 defunct at 19:xx AEST; pyright pid 3722401 at 103 % CPU / 2.5 GB for 1 h 40 min, ppid 1, with no analyze running (reaped by hand 2026-08-29). Two `runs` rows stuck `running` with dead owner pids | Serve idles out when its stdio closes; analyze owns pyright's lifecycle (kill on run end, adopt orphans at start); doctor reaps `running` rows whose owner pid is gone |
| B3 | Status oracles disagree and the hook over-reports | `analyze_status_get` said `failed` for run f085278d while the DB, heartbeat and `ps` showed it running, and `project_status_get.latest_run` vs `index_diff_get.latest_run` named different runs (observed once, on f085278d; they agree post-rebuild). The SessionStart hook prints "started a background analyze" when the lock prevented one (string confirmed in the binary; reproduced at session start 2026-08-29) | One source of truth for run state; the hook reports what it did |
| B4 | Any result over ~100 rows overflows the MCP result cap (routes 120 KB, relations 88 KB, resolve 90 KB) | 3 of 104 Claude calls persisted to disk (transcript forensics; not re-verified) | Default page sizes under the cap; cursors everywhere lists exist |
| B5 | Latency: median 3.1 s, p90 11 s per call vs Grep 0.01 s | transcript timing (inflated by batching, but the gap is two orders; not re-verified) | Keep the read path off the write lock; warm serve; avoid re-opening the 989 MB DB per call |

## C. Project — adoption (why agents don't reach for it even when it works)

| # | Problem | Evidence | Action |
|---|---|---|---|
| C1 | `entity_find` schema friction | 18 of 42 calls (43 %) failed with `unknown argument: name/query`; every "unrelated" follow-through sample was an argument-error retry | Product: accept `name`/`query` aliases or return the correct parameter in the error. Project: the `pattern` note in AGENTS.md (done 2026-08-29) |
| C2 | Search-specialist agents never use it | Explore agents: 4,659 calls, 0 Loomweave; teammate lanes (464 files): 0; only 28 of 3,233 sessions | Project: the scoped "reach for it for" list in AGENTS.md (done); brief Explore/Plan lanes explicitly on the three approved questions; consider a project skill that wraps the three calls |
| C3 | Agents re-grep after a zero-caller answer, and having learned to, skip Loomweave next time ("I end up grepping to confirm anyway — and once I'm going to grep, I skip the first step") | 12 grep-after-empty switches clustered in two sessions; 26 grep-after-non-empty are ordinary next steps; 6 of 12 `entity_callers_list` calls returned zero callers with `traversal_complete: true` on the pre-fix index | Product: A1–A3. Project: the "zero callers is not no callers" rule in AGENTS.md (done) |
| C4 | Nothing in `docs/agents/**` mentions Loomweave — no trap has ever been written down where agents read | cost profile §4 | Project: the AGENTS.md section (done); add traps to `recent-code-hints.md` as they are found |
| C5 | The stale-index warning is the single most-recorded reason agents hesitate (27 Codex + 11 Claude assistant statements; "the first Loomweave query in a session comes with a trust question"), and it is usually *true* on a shared checkout with worktree merges landing between sessions | phrase forensics; B1/B3 | Product: B1 (debounced, post-merge analyze) and B3 (a truthful hook) so a session starts fresh or says exactly what is stale; answer-level staleness per entity ("this entity's file changed since index") rather than a whole-index flag |

## D. Re-measure (the criterion in ADR-043)

Re-run the transcript forensics after the next delivery wave with the fixed
extractor in place and B1/B2 addressed:
`scripts/loomweave_usage_forensics.py` (copied from the lane's scratchpad on
2026-08-29; read-only over `~/.claude/projects/-home-john-elspeth`) reports
Loomweave calls per session class (main / lane / subagent by type),
empty-result and error rates per tool, and follow-through.
Success: lane and Explore agents make Loomweave calls for the approved
questions, `entity_find` error rate near zero, `entity_callers_list` empty
rate well under the pre-fix 50 %. Failure — zero lane/Explore adoption against
a working index — amends ADR-043 to a retirement.

## Post-rebuild measurement (replaces the ADR-043 "3 of 45 files degraded" figure)

Whole-tree `--no-incremental` run `f085278d`, launched from an activated-venv
shell, 2026-08-29 08:25–09:03Z (file phase) + post-passes to 09:26Z:

- 3,093 files analyzed; coverage 3,090 complete / 3 degraded (A3).
- 124,554 `calls` edges (58.7k before the extractor fixes; 90.5k after the
  hook-era heal); 290,976 edges total; 82,412 entities.
- 39 files with functions but no call extraction — all `complete` and all
  genuinely call-less (Protocol/ports/hookspec/lint-fixture files).
- Exemplar `chat_solver.build_step_chat_context_block`: 24 callers (23 from
  `test_chat_solver.py`), previously `[]`.
- Residual 225 test files with `from elspeth` imports and no src call edge:
  150 never call an imported name; 75 only instantiate classes (A2).

A3 persists post-fix at 3 files; A4's incremental tombstones do not, its
same-file duplicates do.
