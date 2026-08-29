# Loomweave salvage worklist — 2026-08-29

Condition of Loomweave's approval in [ADR-043](../../architecture/adr/043-project-tooling.md).
Every item cites the measurement that justifies it (three read-only lanes on
2026-08-29: transcript forensics over 180,517 Claude Code + 11,478 Codex tool
calls; a live capability probe against `ast`/`git` ground truth; a cost and
reliability profile of `.weft/loomweave/` and the run history). Product items
belong in the loomweave repository; project items are ELSPETH's.

## A. Product — reliability (what makes answers wrong or missing)

| # | Defect | Evidence | Fix shape |
|---|---|---|---|
| A1 | Hook-launched analyzes run without the project venv, so test files resolve zero calls into `elspeth.*` while marked `complete` | 400 of 1,346 test files under the **fixed** plugin; clarion-5cf9643de9 | Resolve the interpreter from the project (`.venv`, `uv`) in `hook git-sync`, and mark coverage `degraded` when third-party/first-party imports are unresolvable instead of `complete` |
| A2 | Class instantiation never becomes a `calls` edge | `AuditIntegrityError`: 2,235 sites, 0 edges, `callers: []`; `PresentBase` 41/0 | Treat `Name(...)` resolving to a class entity as a call to the class (or a `constructs` edge) and surface it in `entity_callers_list` |
| A3 | Direct calls in large test files (≥1.5k lines) returned as `unresolved why: dynamic` | b1/b3 probes: 9 and 18 plain direct calls unresolved; `pyright_timeout` on `tool_batch.py` (2,354 lines, 4 redispatches), `reference_site_cap` on `sessions/service.py` (13,645 lines) | Per-file pyright budget scaled by size; chunked reference resolution instead of a hard cap; label the file `degraded` in the answer, not just in a coverage table |
| A4 | Tombstones never pruned: 937 src function rows and 1,016 `LMWV-FACT-ENTITY-DELETED` facts (185 for entities that exist on disk again) inflate every list answer; 4 `create_app.<locals>.*` routes survive a rename | route probe 227 vs 86 real; size sweep "indexed > ast" in every bucket | Prune rows whose `last_seen_commit` is no longer reachable after a full run; retract deleted-facts when the entity reappears |
| A5 | `briefing_blocked: secret_present` hides docstrings for 10,708 entities (170 files, 31 % of files >3,000 lines) — the scanner flags sha256 fixture digests | 342 of 563 secret findings are 64-hex digests; only 32 lines carry the project's `# secret-scan: allow-this-line` marker; semantic search missed `redaction.py` for this reason | Honour the project's allow-marker; suppress high-entropy-hex on lines that are test fixtures/digest literals; block the *line*, not the file's every entity |
| A6 | Cumulative `entity_unresolved_call_sites` (287,871 rows) is never compacted; builtins dominate (`len` 10k, `str` 9k, `pytest.raises` 10k) | cost profile §3c | Drop builtin/stdlib names at extraction; keep only sites that could resolve to a project entity |

## B. Product — operations (what makes it expensive or untrustworthy to run)

| # | Defect | Evidence | Fix shape |
|---|---|---|---|
| B1 | Re-analyze after every commit/merge/checkout on a shared checkout | 39–148 runs/day over 10 days; ~45 s floor for a 1-file change; 12 of 39 failed on 08-29 | Debounce (coalesce runs within N minutes) and/or trigger on `post-merge` only; skip when the diff touches no indexed roots |
| B2 | Process leaks: one `loomweave serve` per MCP client never reaped; orphaned pyright from the first post-fix run | 13 `serve` + 3 defunct; pyright pid 3722401 at 102 % CPU / 2.57 GB for >60 min, ppid 1 | Serve idles out when its stdio closes; analyze owns pyright's lifecycle (kill on run end, adopt orphans at start) |
| B3 | Status oracles disagree: `analyze_status_get` says `failed` for a run the DB, heartbeat and `ps` show running; `project_status_get.latest_run` vs `index_diff_get.latest_run` name different runs; hook prints "started a background analyze" when the lock prevented one | measured on run f085278d | One source of truth for run state; the hook reports what it did |
| B4 | Any result over ~100 rows overflows the MCP result cap (routes 120 KB, relations 88 KB, resolve 90 KB) | 3 of 104 Claude calls persisted to disk | Default page sizes under the cap; cursors everywhere lists exist |
| B5 | Latency: median 3.1 s, p90 11 s per call vs Grep 0.01 s | transcript timing (inflated by batching, but the gap is two orders) | Keep the read path off the write lock; warm serve; avoid re-opening the 889 MB DB per call |

## C. Project — adoption (why agents don't reach for it even when it works)

| # | Problem | Evidence | Action |
|---|---|---|---|
| C1 | `entity_find` schema friction | 18 of 42 calls (43 %) failed with `unknown argument: name/query`; every "unrelated" follow-through sample was an argument-error retry | Product: accept `name`/`query` aliases or return the correct parameter in the error. Project: the `pattern` note in AGENTS.md (done 2026-08-29) |
| C2 | Search-specialist agents never use it | Explore agents: 4,659 calls, 0 Loomweave; teammate lanes (464 files): 0; only 28 of 3,233 sessions | Project: the scoped "reach for it for" list in AGENTS.md (done); brief Explore/Plan lanes explicitly on the three approved questions; consider a project skill that wraps the three calls |
| C3 | Agents re-grep after a Loomweave answer | 13 of 40 sampled calls; 6 of 12 `entity_callers_list` calls returned zero callers with `traversal_complete: true` on the pre-fix index | Product: A1–A3. Project: the "zero callers is not no callers" rule in AGENTS.md (done) |
| C4 | Nothing in `docs/agents/**` mentions Loomweave — no trap has ever been written down where agents read | cost profile §4 | Project: the AGENTS.md section (done); add traps to `recent-code-hints.md` as they are found |

## D. Re-measure (the criterion in ADR-043)

Re-run the transcript forensics after the next delivery wave with the fixed
extractor in place and B1/B2 addressed:
`/tmp/…/scratchpad/loom-usage/analyze.py` (keep a copy under `scripts/` if it
is to be reused) reports Loomweave calls per session class (main / lane /
subagent by type), empty-result and error rates per tool, and follow-through.
Success: lane and Explore agents make Loomweave calls for the approved
questions, `entity_find` error rate near zero, `entity_callers_list` empty
rate well under the pre-fix 50 %. Failure — zero lane/Explore adoption against
a working index — amends ADR-043 to a retirement.

## Pending measurement

The whole-tree post-fix size sweep and re-probe (against the `--no-incremental`
run started 2026-08-29 08:25Z) had not completed when this was written; its
result replaces the "3 of 45 files degraded" incremental figure in ADR-043 and
decides whether A3 persists post-fix.
