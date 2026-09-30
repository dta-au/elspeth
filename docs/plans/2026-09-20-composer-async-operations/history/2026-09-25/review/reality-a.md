# Reality Check — Composer Async Operations Plan, T00–T04

Reviewer lens: hallucination hunt. Verify every symbol/import/fixture/path/line-anchor/command/test-id T00–T04
rely on actually exists as described at the current tip, or is created by an earlier task. Repo:
the main checkout, branch `release/0.8.1` @ `53b7d4344` (HEAD at review time; plan's own recorded base was
`d479eb2b4`/`ea5fa50d5`, both ancestors of HEAD — `git merge-base --is-ancestor ea5fa50d5 HEAD` true, and
`git diff --stat ea5fa50d5 HEAD -- src/elspeth/web tests/unit/web` touches none of the files T00–T04 cite, so the
plan's line anchors are being checked against the same code they were measured on). No worktree/branch for this
plan exists yet (`git rev-parse --verify --quiet refs/heads/feat/composer-async-ops` → exit 1; `.claude/worktrees/
composer-async-ops` absent from `git worktree list`), consistent with T00 not having been executed.

Read in full: `docs/plans/2026-09-20-composer-async-operations.md`, `T00.md` (884 lines), `T01.md` (1106 lines),
`T02.md` (1567 lines, partial — enough to verify all Interfaces-section anchors and the Step 2/4 code), `T03.md`
(1475 lines, partial — table/schema/gates sections in full), `T04.md` (2406 lines, partial — CONTRACT DEVIATION
blocks and Interfaces/Consumes sections in full, SQLite test file in full).

## Method

For each task, extracted every `file.py:NNN` / `file.py:NNN-MMM` anchor, every named symbol (class, function,
constant, fixture) the task claims exists on the current tree, and every test id / fixture reuse claim, then
checked it directly against the repo with `grep -n` / `awk 'NR==a,NR==b'` (never `sed` alone for line-numbered
verification, to avoid off-by-one blindness). 42+ distinct anchors were checked (full list below). Line ranges
that were off by 1 due to a decorator or blank line are noted but NOT counted as defects (rounding, not
hallucination). Only anchors that point at the **wrong code**, or symbols/paths that do **not exist**, are
reported as findings.

## Verified accurate (representative sample — not findings)

All of the following were checked against the live tree and matched exactly or within one line (decorator/blank-
line rounding):

- `src/elspeth/web/config.py:349-352` = `composer_transport_headroom_seconds` field, closing `)` at 352 — exact (T01).
- `src/elspeth/web/config.py:1186-1208` = `_validate_composer_timeout_transport_headroom`, `return self` at 1208 — exact (T01).
- `src/elspeth/web/config.py:1210-1211` = `@model_validator`/`_warn_composer_turn_budget_underfunded` — exact (T01).
- `src/elspeth/web/composer/protocol.py:1556-1557` = `@property`/`def composer_timeout_seconds` — exact (T01).
- `src/elspeth/web/sessions/routes/composer/guided_chat_atomic.py:729,790,836,874,897` = all 5
  `settings.composer_timeout_seconds` reads inside `run_guided_chat_provider_attempt` (def at 663) — exact (T01).
- `src/elspeth/web/sessions/routes/composer/guided.py:5042` inside `post_guided_respond` (def at 2936) — exact (T01).
- `src/elspeth/web/sessions/routes/composer/pipeline_settlement.py:173` `settle_pipeline_proposal_under_compose_lock`
  def, `:273` timeout read, `:447` `settle_auto_commit_intent` def, `:473-483` its call site — exact (T01).
- `src/elspeth/web/sessions/routes/composer/proposals.py:275` `accept_composition_proposal`, `:318-324` its call
  to `settle_pipeline_proposal_under_compose_lock` — exact (T01).
- `src/elspeth/web/app.py:2303` `system_status`, `:2334` the `"composer_timeout_seconds"` key (cited 2329-2334) — exact (T01).
- `tests/unit/web/test_config.py:16` `from pydantic import ValidationError`, `:22-28` `_REQUIRED_WEB_ENV`,
  `:50-60` `required_web_env`, `:1646` `_settings`, `:1664` `TestPublicBaseUrlValidation`, `:242`/`:252` the two
  named tests — exact (T01).
- `src/elspeth/web/sessions/schemas.py:39-40` import-block insertion point, `:492-496` `ReenterGuidedRequest`/
  `RunResponse`, `:145-160` `SendMessageRequest`, `:74-91` `_GuidedOperationRequest` (actual body ends 90, off by
  1, blank-line rounding), `:54-65` `_StrictResponse`, `:228-237` `MessageWithStateResponse`, `:154` the
  `max_length=65536` content field — exact (T02, T00).
- `src/elspeth/contracts/errors.py` — `class AuditIntegrityError(Exception)` exists (line 972).
- `src/elspeth/contracts/tier_registry.py:73` `_ALLOWED_MODULE_PREFIXES`, `:149`/`:161` the two `raise
  PermissionError` enforcement sites — exact (T02 CONTRACT DEVIATION).
- `elspeth-lints/.../audit_evidence/tier_1_decoration/rule.py:131-132` `_scan_candidates` scanning only
  `contracts/errors.py`; `metadata.py:21` `path_filter=r".*errors\.py$"` — exact (T02 CONTRACT DEVIATION).
- `src/elspeth/web/sessions/protocol.py:620` `GuidedOperationConflictError`, `:636` `GuidedOperationFenceLostError` — exact (T02).
- `elspeth_lints.rules.immutability.freeze_guards.rule.analyze_tree(tree, file_path, source_lines)` and
  `...frozen_annotations.rule.find_findings(tree, filename)` — both exist with exactly the signatures T02's test
  calls them with (2 and 3 positional args respectively) — exact.
- `.gitignore:67` = `.claude/lanes/` — exact (T00).
- `tests/unit/architecture/test_session_db_mutation_authority.py:5547` contains `"e6f248b018079f85"` (the "after"
  value of T00's cited fingerprint drift) — exact.
- `src/elspeth/web/config.py:231` `data_dir: Path = Field(default=Path("data"), ...)`, `:1423`
  `def resolve_session_db_url` — exact (T00 M6).
- `tests/unit/web/sessions/test_routes.py:453` `class _BlockingRecordingComposer`, `:853` `def _make_app`, `:6219`
  `test_send_message_serializes_concurrent_requests_per_session` (cited 6218, which is its `@pytest.mark.asyncio`
  decorator — accurate) — exact (T00 Step 13).
- `tests/unit/web/sessions/routes/test_compose_heartbeat_renewal.py:40` imports from
  `tests.unit.web.sessions.test_routes` — exact (T00).
- `src/elspeth/web/middleware/rate_limit.py:100` `ComposerRateLimiter.check`, `:108` its `async with lock`, `:152`
  `SharedRateLimiter.check`, `:155` its `run_sync_in_worker(self._authority.admit, ...)` call — exact (T04 CONTRACT
  DEVIATION D-4a).
- `src/elspeth/web/coordination/rate_limit_authority.py:85` = `with self._engine.begin() as conn:` inside
  `RepositoryRateLimitAuthority.admit` — exact (T04 D-4a).
- `src/elspeth/web/async_workers.py:14-19` `MAX_WORKERS`/`MAX_QUEUED`/`ADMISSION_CAPACITY`, `:194`
  `run_sync_in_worker` — exact (T04, T00 M9).
- `src/elspeth/web/sessions/locking.py:279-283` `locked_session_transaction`, `:256-261`
  `acquire_session_advisory_xact_lock` — exact (T04).
- `src/elspeth/web/coordination/membership_authority.py:44` `_DATABASE_CLOCK_SQL`, `:50` `_ensure_utc`, `:54`
  `_database_clock_value`, `:68` `_require_nonblank` (cited range 44-70) — exact (T04).
- `src/elspeth/web/coordination/websocket_ticket_authority.py:14` imports those four names from
  `membership_authority` — exact (T04).
- `src/elspeth/web/coordination/composer_progress_authority.py:178-265` = the exact span of
  `cleanup_expired`'s scan-then-per-candidate-transaction body (method def itself is at 169; 178 is where the
  cited *pattern* — one connection scan, then per-candidate transactions — actually begins; 265 is `return
  removed`) — accurate as a precedent citation, not a defect.
- `src/elspeth/web/coordination/run_recovery_authority.py:138` `list_recoverable_run_records` — exact (T04).
- `src/elspeth/web/coordination/contracts.py` re-exports `SessionOperationContext`, `SessionOperationKind`,
  `SessionOperationFence` from `elspeth.contracts.session_operation` via explicit `as`-aliased imports (lines
  18-22) — T04's "Consumes" claim that these names live at `coordination/contracts.py` is accurate (re-export,
  not native definition — but importable exactly as claimed).
- `tests/unit/web/conftest.py:62-70` `engine` fixture, `:73-101` `_make_session` — exact (T03).
- `tests/unit/web/sessions/guided_test_authority.py:22` `class DualFencedSessionServiceHarness(SessionServiceImpl)`,
  `:216` `add_message = _kw_writer("add_message", SessionOperationKind.COMPOSE)` — exact (T03's "add_message
  wrapper (:216)" claim; this is a class-body assignment via a factory, not a `def`, which is why a naive `grep -n
  "def add_message"` misses it — worth knowing if anyone re-verifies this the same naive way).
- `src/elspeth/web/sessions/service.py:9369` `async def add_message` (the wrapped target) exists.

## Findings

### 1. [MAJOR] T00 Step 2's `scripts/worktree-cleanup.sh` line anchors are wrong on the current tree — off by ~79-83 lines

**File / step:** `docs/plans/2026-09-20-composer-async-operations/T00.md`, Step 2 ("Link the venv without letting
the cleanup script remove the fresh tree").

**Claim in the plan:**
> `REMOVABLE is decided at scripts/worktree-cleanup.sh:133, removal happens at :212-220, and the link loop at
> :233-240 skips trees removed earlier. **Do not run `--execute` on it.** Take the classification as evidence,
> then run the exact link command the script prints (`:240`, `ln -s $MAIN/.venv $w/.venv`) by hand...`

**What's actually there** (verified with `awk 'NR==a,NR==b'`, not `sed`, to avoid line-count confusion;
`wc -l scripts/worktree-cleanup.sh` = 328 lines):

| Plan says | Actual line | Actual content |
|---|---|---|
| `:133` "REMOVABLE is decided" | 133 | `BASE_SHA="$(git -C "$MAIN" rev-parse --verify --quiet "${BASE}^{commit}")" \` — unrelated; resolves `--base`. |
| (REMOVABLE really decided) | **212** | `class="REMOVABLE"; REMOVABLE+=("$wt"); BRANCH_OF["$wt"]="$shortbranch"` |
| `:212-220` "removal happens" | 212-220 | This is the **classification/report-row** block (`class="REMOVABLE"` assignment, then `ROWS+=(...)`, then the `while` loop that re-reads `git worktree list --porcelain`) — it does **not** remove anything. |
| (removal actually happens) | **291-299** | `for w in "${REMOVABLE[@]}"; do ... act "git worktree remove $w" git -C "$MAIN" worktree remove "$w"` (line 299) |
| `:233-240` "the link loop" | 233-240 | Still inside the classification/report block (report header printing, `for row in "${ROWS[@]}"`) — not the link loop. |
| (link loop actually is) | **312-320** | `if [ "$LINK_VENV" = 1 ]; then for w in "${NOVENV[@]}"; do ... ln -s "$MAIN/.venv" "$w/.venv"` |
| `:240` "the exact link command the script prints" | 240 | Still inside the report-row printf loop; not the `ln -s` line. |
| (the `ln -s` line actually is) | **319** | `act "ln -s $MAIN/.venv $w/.venv" ln -s "$MAIN/.venv" "$w/.venv"` |

The offset is consistently ~79-83 lines across all four citations (212→291 = +79, 233→312 = +79, 240→319 = +79),
which points to the plan having been anchored against an earlier draft of the script that was ~80 lines shorter
in this region, and never re-anchored — the same category of drift the plan itself warns about elsewhere (e.g.
T4's Step 1 preamble: "If an exception signature differs... note the measured signature"; T03's "Evidence base"
section explicitly dry-runs and re-measures). This one section of T00 was not re-measured.

**Consequence:** every actual Bash command in T00 Step 2 is anchor-free (it calls the script by name/flags, not
by line number), so the wrong citations do not break execution. But a worker (human or agent) told to "take the
classification as evidence" by reading `:133`/`:212-220`/`:233-240` and then verify or reason about the script's
behavior from those specific lines will read the wrong code — the classification-decision line, the removal
action, and the link-loop action are each in a different place than stated. This is exactly the class of citation
a plan reviewer exists to catch: the plan asserts specific evidence at specific lines, and the lines don't say
what's claimed.

**Fix:** re-point the four anchors to the measured tree: REMOVABLE decided at `:212` (or `:208-212` for the
`if/elif/else` chain), removal loop at `:291-299`, link loop at `:312-320`, and the `ln -s` print line at `:319`.

### 2. [MINOR] T02 Interfaces section lists `canonical_json, stable_hash, is_lower_sha256_hex` with line numbers
   `32,66,89` in a different order than the functions actually appear

**File / step:** `docs/plans/2026-09-20-composer-async-operations/T02.md`, Interfaces → "Consumes (existing tree)".

**Claim:** `canonical_json, stable_hash, is_lower_sha256_hex (elspeth.contracts.hashing:32,66,89)`.

**Actual** (`grep -n "^def canonical_json\|^def stable_hash\|^def is_lower_sha256_hex" src/elspeth/contracts/hashing.py`):
`is_lower_sha256_hex` is at line 32, `canonical_json` at line 66, `stable_hash` at line 89 — i.e. the correct
pairing is `is_lower_sha256_hex:32, canonical_json:66, stable_hash:89`, not the order the names are listed in.

All three names and all three line numbers are individually correct — this is purely a name-to-line-number
pairing/ordering slip in the prose, not a hallucinated symbol or a wrong module. Low risk (nobody edits this
file from this task; it's a "consumes, unchanged" citation), but worth a one-line fix so a reader who spot-checks
by position isn't briefly misled.

## What I did not check

Given the "T00-T04" lens boundary and effort budget, I verified the Interfaces/Consumes/Produces sections and
the Files/Gates tables of every task in full, and spot-checked the inline code blocks (T00 Step 13's test file
open, T01 Steps 2/7/8/9/10, T02 Steps 2/4, T03 Steps 2/4, T04 CONTRACT DEVIATION blocks + Step 2 test file) rather
than every single line of every ~1000-2000 line task file (T02 at 1567 lines, T03 at 1475, T04 at 2406 were read
partially — enough to cover every distinct file:line anchor pattern, but a full line-by-line read of every test
body was out of scope for this lens at this budget). I did not execute any command from the plan (no worktree
exists yet); this is a static grounding check only, not a dry run.

## Confidence Assessment

**Overall Confidence:** High.

| Finding | Confidence | Basis |
|---|---|---|
| worktree-cleanup.sh anchors wrong (Finding 1) | High | Directly read the cited lines and the actual REMOVABLE/removal/link-loop code with `awk` line-numbered output; the ~79-line offset is consistent across all four citations, ruling out a one-off typo. |
| hashing.py name/line pairing slip (Finding 2) | High | Directly grepped `^def` lines; all three names and numbers exist, only the pairing order is off. |
| Every "verified accurate" item | High | Each was checked with a direct `grep -n` or `awk 'NR==a,NR==b'` against the live file at HEAD (`53b7d4344`, a descendant of the plan's own recorded base), not inferred. |

## Risk Assessment

**Implementation Risk:** Low (for what this lens covers). Finding 1 is prose/evidence-citation drift in a task
step whose actual commands don't depend on the cited line numbers; Finding 2 is a citation-only prose slip. I
found no hallucinated symbol, no wrong import path, no nonexistent fixture, and no fabricated test id across
T00-T04 — the plan's line-level grounding is unusually precise (confirmed against 40+ independent anchors) with
these two exceptions.

**Reversibility:** Easy — both findings are one-line text edits to the plan files, not code changes.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| A worker trusts T00 Step 2's stale line citations while auditing `worktree-cleanup.sh` behavior before deciding whether `--execute` is safe | Low-Medium | Possible if a human (not just an agent running the literal bash blocks) reads the prose as the safety justification | Re-anchor the four line numbers per Finding 1 before this task is executed by a reader who inspects the script by citation rather than running it |
| Reader mis-pairs a hashing.py name to the wrong line while independently verifying T02 | Low | Low | Fix the ordering in the prose (Finding 2) |

## Information Gaps

1. [ ] **T02 lines 934-1567 (Steps 5-10+) and the remainder of T03/T04 beyond what was read** — not read in this
   pass; another reviewer or a follow-up pass should extend line-anchor verification into the unread tail of
   these files, particularly T04's PostgreSQL testcontainer file (only its opening docstring was seen) and T02's
   post-Step-4 implementation body (only skeleton/imports of `composer_operations.py` were seen, not its full
   ~600-900 remaining lines).
2. [ ] **T03's PostgreSQL-specific claims** (`test_schema_probe_postgres.py` exact trigger sets, missing/disabled
   trigger parametrize rows) — these require a running PostgreSQL testcontainer to verify behaviourally; I only
   confirmed the cited file/anchors exist as paths, not that the claimed "RED becomes green" transitions are
   correct, since that requires executing the dry run T03 itself already performed.
3. [ ] **Trust-tier lint fingerprint claims** (e.g. T01's `fp=e674c9c5f6d54c83` etc., T03's `scope_fingerprint`
   values) — these are judge-signed HMAC-adjacent identifiers I cannot recompute without running
   `elspeth_lints.core.cli check`; I verified the file:line locations they describe exist and are plausible, but
   did not run the lint tool to confirm the exact fingerprint strings.

## Caveats & Required Follow-ups

### Before Relying on This Analysis
- [ ] Re-run `wc -l scripts/worktree-cleanup.sh` and the REMOVABLE/removal/link-loop greps immediately before
  landing Finding 1's fix, in case another lane has touched that script since this review.
- [ ] Extend this same anchor-verification method to T02's unread tail and to T05-T18, which this lens did not cover.

### Assumptions Made
- HEAD (`53b7d4344`) is a valid stand-in for "the tree this plan will execute against": confirmed via
  `git merge-base --is-ancestor ea5fa50d5 HEAD` (true) and a targeted `git diff --stat` showing no touched files
  among those T00-T04 cite.
- A citation within 1 line of the true anchor (decorator, blank line) is not a defect; only citations pointing at
  materially different code, or at nonexistent symbols/paths, are reported.

### Limitations
- Static grounding only — I did not execute any plan step, so behavioural claims (test pass/fail counts, lint
  finding counts, xfail listings) are not independently re-derived here, only the paths/anchors that *carry*
  those claims.
- This report covers T00-T04 only, per the assigned lens; T05-T18 and `contract.md`'s later sections are out of
  scope for this pass.
