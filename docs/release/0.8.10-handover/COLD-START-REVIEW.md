# Cold-start review of the 0.8.10 handover (ledger + every lane's cold-start notes)

Reviewer: independent Fable session, read-only. No edits to any reviewed file, no git writes, no tests, no messages to lane owners (see § Method for why none was needed).

## Scope and method

- `date` at first read: **Sun Oct 25 09:39:21 AM AEDT 2026**. `date` at the last measurement: **09:54:29** (plus the three post-draft controls at about 10:03: worktree counts, the config.py positive control, the landcheck verdicts). `date` at finish: see the last line of this file.
- Primary: `HANDOVER-LEDGER.md` (mtime 09:34:24, "measured at 09:33" at `d7ad18596`). Supporting: `RESUME-FIRST.md`, `AGENT-BRIEF.md`, `PAUSE-PROTOCOL.md`, `STATUS.md` (339 lines), `HANDOVER-LEDGER-review.md`, the repo's `AGENTS.md`/`CLAUDE.md`, `docs/release/0.8.10-master-plan.md`, `docs/release/0.8.10-custody-handover-prompt.md`, and the `## COLD START (paused 2026-10-25)` section of each lane report: `protocol-report.md:157`, `u45-fix-report.md:39`, `u45-officer-gate.md:1046`, `u46fix/EVIDENCE.md:70` (+ `u46-report.md:29`), `overlay-report.md:410`, `runtime-report.md:295`, `T5e-metadata-writer.md:8`, `issue327-rest-report.md:91`, `u02-engine-report.md:200`, `u30-report.md:6`, `u22-report.md:151`, and `exceptions-status.md` (no section).
- Sources: `git` read-only from `<repo>` (`git --no-optional-locks status` per worktree), `ps`/`/proc/<pid>/cwd`, the lane directories under `.claude/lanes/release-0.8.10/` and under each worktree's `.claude/lanes/<unit>/`.
- Instrument controls, run before any negative was trusted:
  - `git rev-parse --verify --quiet codex/0.8.10-nonexistent-control` → rc=1; `origin/release/does-not-exist` → rc=1 (a missing ref fails, does not echo).
  - `git merge-base --is-ancestor deadbeef0 release/0.8.10-assembly` → `fatal: Not a valid object name`, rc=128.
  - Process grep control: `ps -eo args | grep -c bash` → 18 (the grep can hit); the test/codex grep over the same table matched only IDE/daemon rows.
  - Known-positive for `git cherry`: `codex/0.8.10-runtime-u38-final` (landed as `d7ad18596`) → 0 `+` of 1 ahead; known-negative: every unlanded branch returns `+` for every commit.
  - Known-positive for the lane-note search instrument (`grep -n "COLD START"`): 12 of 13 reports hit; `exceptions-status.md` returns nothing, as the ledger says.
- Lane owners were not messaged. Every question the documents could not answer was answerable from git or the worktree lane directories (notably the exceptions lane, below). The one residual question (whether the exceptions lane triaged the two open Astra findings) is answered by git: no code changed for them after the review, so the state is "open" whatever the lane would say. Resuming that lane during the pause, given its record of ignoring four pause notices, was judged a worse risk than leaving the question to the coordinator.

## Verified correct (no finding)

| Claim | Command | Result |
|---|---|---|
| Target tip `d7ad18596` (U-38), 140 ahead of `origin/main`, `origin/main`=`main`=`3b28d537b` | `git rev-parse --short=9 release/0.8.10-assembly`; `git rev-list --count origin/main..release/0.8.10-assembly` | `d7ad18596` (05:23:14); 140 |
| `origin/release/0.8.10-assembly` = `5297041d6`, 10 behind | `git rev-list --left-right --count origin/release/0.8.10-assembly...release/0.8.10-assembly` | `0 10` |
| Root checkout is on the target | `git symbolic-ref --short HEAD` | `release/0.8.10-assembly` |
| §3 every branch tip and ahead/behind: reserve-repins `e187089ed` 7/0; overlay-fixes `51f8b7872` 4/0; physical-audit-fakes `99f94d421` 1/0; protocol `0cfa7d7a3` 17/0; u45-fix `c26167c4d` 9/0; t5e `6c22638f3` 1/7; u02-engine `430b3bd46` 5/0; runtime-u35b-land `a8aeab905` 1/0; runtime-u35b `4c5e08d2d` 22/0; issue327-rest `20408a1ec` 23/0; codec-residue `82dd2ff49` 3/0; exceptions `ddc1ec9e8` 9/0; u30 `f8fc444c9` 2/0; u22 `d0dcfc321` 8/7; tier-analysis `ee99a90c0` 1/0; u46-diagnosis `6af0328e3` 13/80; residue-u45-integration `1fb71a3bf` 10/0; u46-fix `aa044516b` 4/19 | `git rev-list --left-right --count release/0.8.10-assembly...<b>` for each | all match; `git cherry` `+` count equals the ahead count for every one (no hidden patch-equivalent landings) |
| Row 1: `506561cd4` is the first commit on reserve-repins; order `506561cd4 afaedadc0 1dd80bebd f17dd9d71 435f872ef 15986e352 e187089ed` | `git log --reverse --format='%h %s' release/0.8.10-assembly..codex/0.8.10-reserve-repins` | identical order and subjects; `435f872ef` touches the fencing file |
| Row 3: protocol `src/elspeth/web/config.py` byte-identical to the target (the duplicate `composer_continuation_enabled` is gone: 1 definition on each side); preserve ref `247b7f5af` | `git diff release/0.8.10-assembly codex/0.8.10-protocol -- src/elspeth/web/config.py \| wc -l` → 0; **positive control** `-- src/elspeth/web/composer_stream.py` → 204 lines, `-- src/elspeth/web/app.py` → 15 (the instrument fires on a changed file; my first control used a non-existent path and returned 0 — the exact failure AGENTS.md warns about, caught before use) | 0; `preserve/0.8.10-protocol-u41-pre-rebase 247b7f5af` |
| Row 3: "0 failing at base and at every commit" | `grep -o 'tests=\|failures=\|errors='` over `.claude/worktrees/protocol-20261025/.claude/lanes/protocol/landcheck/*.xml` | 13 backend xml (base + 12 commits) and 6 frontend xml (base + 5 commits), every one `failures="0" errors="0"`; backend tests 691 → 796, frontend 4456 → 4504 |
| Row 4: u45-fix = c1..c9 on `d7ad18596`; `303286be6` is c2 | `git log --reverse … codex/0.8.10-u45-fix`; `git merge-base --is-ancestor 303286be6 codex/0.8.10-u45-fix` | 9 subjects match the lane report; yes |
| Row 5: T5e service.py pin is the hash of the file at `6c22638f3` | `git show 6c22638f3:src/elspeth/web/sessions/service.py \| sha256sum` vs the json | both `57be012e…` |
| Row 7: `a8aeab905` tree == `4c5e08d2d` tree; `tests/helpers/continuation_budgets.py` byte-identical on `a8aeab905`, `b296259db`, codec-residue tip | `git rev-parse <sha>^{tree}`; `git show <ref>:tests/helpers/continuation_budgets.py \| sha256sum` | `8be0b8da5` both; `1cf04ef7` ×3 |
| Row 7 / protocol: BOTH lease patches are already in `a8aeab905` | distinctive `+` lines of `attachment-application-budgets-full.patch` grepped in `git show a8aeab905:tests/unit/web/sessions/test_continuation_attachment_application.py` | `OWNER_LEASE_SECONDS` 16 hits (target 0); each `-full`-only line present ([1]/[5]) |
| Row 8: `04641f5ee`, `073fd6f3f`, `06ef4563b`, `91064f603`, `51d66f699`, `ce904993a` on issue327-rest | `git merge-base --is-ancestor` ×6 | all yes |
| Row 9: `3d91561d3` on codec-residue; `c338a79cd` NOT on u22 (so "never `--onto c338a79cd`" is right); u22 forks at `dcb742ecc` | `git merge-base --is-ancestor`; `git merge-base release/0.8.10-assembly codex/0.8.10-u22` | yes; no (rc=1); `dcb742ecc` |
| Row 10: the 9 exceptions commits and their timestamps (06:08 → 07:40) | `git log --reverse --format='%h %ci %s' release/0.8.10-assembly..codex/0.8.10-exceptions` | 602830a86, d411959d0, 748d548db, 3035bfeeb, fe9278445, ef4096ebb, c6a4e3f9f, 9055b82a8, ddc1ec9e8 |
| Row 14: tier doc path | `git diff --name-only release/0.8.10-assembly codex/0.8.10-tier-analysis` | `docs/architecture/design-notes/0.8.10-continuation-trust-tier-analysis.md` (91 lines) |
| U-46: 4 commits T1/T2/P1/(b); `preserve/0.8.10-u46-b-wip`=`36dca5e94`; its diff to `aa044516b` is the comment only | `git log …u46-fix`; `git diff --stat 36dca5e94 aa044516b` | 4 subjects match; `1 file, 3 insertions(+), 1 deletion(-)` in the fencing file |
| U-46 diagnosis doc + instruments are tracked on the branch | `git ls-tree -r codex/0.8.10-u46-diagnosis docs/release/` | `0.8.10-fencing-gate-diagnosis.md` + `u46-instruments/*.py` (10) |
| overlay-report: `carve-partition` `e34b1d875` 4/138; b3-base holds 1 modified recipe | `git rev-list --left-right --count`; `git --no-optional-locks status --short` | `138 4`; ` M tests/helpers/required_work_reserve_recipes/reviewed-writer-recipes.json` |
| §1 `a3b0e2f86` on target, not on main; `582cc93c2` on target | `git merge-base --is-ancestor` | yes / no / yes |
| §0 pytest-timeout installed, not configured | `grep -n "pytest-timeout\|^timeout\|addopts" pyproject.toml`; `sed -n 470,520p` | lines 103, 212; `[tool.pytest.ini_options]` has no `timeout`, `addopts` = `-ra --strict-markers --strict-config -m … -n 12` |
| §9 preserve refs = 30 | `git for-each-ref refs/heads/preserve/ \| wc -l` | 30; every ref named in a lane note resolves |
| §1 scripts and flags exist | `ls scripts/{full-suite-gate,branch-safety-check,work-at-risk,worktree-cleanup}.sh`; `grep -c -- --stages`; `grep -c allow-protected` | all present; 5; 5 |
| §1 both `gh` accounts are logged in | `gh auth status` | tachyon-beep (active), johnm-dta |
| Hang files and the tool exist on the target | `git ls-tree release/0.8.10-assembly <3 paths> \| wc -l` | 3 |
| `0.8.10-work-packages.md` is untracked; `.claude/lanes/` is ignored; `docs/release/0.8.10-handover/` would NOT be ignored | `git ls-files --error-unmatch`; `git check-ignore -v` | "did not match"; `.gitignore:69:.claude/lanes/`; rc=1 |
| `85f709968` (exceptions-status.md's tip) is tree-identical to `748d548db` | `git diff --stat 85f709968 748d548db` | empty |
| Process quiescence | `ps -eo … \| grep -E 'pytest\|codex exec\|vitest\|full-suite-gate\|fencing_inventory\|claude -p'`; `/proc/<pid>/cwd` for every python/node/codex pid under `<repo>` | no pytest, vitest, codex exec, gate or `claude -p`; load 0.21, 55 GB available. Only the codex app-server daemons and MCP servers (not reviews), and **pid 2879262 — a VS Code Playwright-extension `test-server` (cwd `<repo>`, 14 h old)**: not a lane run, but "no test process is running" is inexact |
| Tree cleanliness | `git --no-optional-locks status --short` in all 160 registered worktrees | 158 clean; dirty = root checkout (11 paths, the maintainer's) and `b3-base-20261025` (1) — exactly as the ledger says. `runtime-20261025`, dirty at STATUS 09:19, is clean now |

## Findings

Classes: WRONG (the statement is false) · STALE (true when written, false now, or superseded in the same document) · OVERSTATED (true but stronger than its source) · UNSOURCED (no artefact backs it) · OMITTED (a cold reader needs it and it is absent) · NOT-DURABLE (held only in a gitignored path, a scratchpad, /tmp or a transcript) · INCONSISTENT (two documents, or two parts of one, disagree).

### WRONG

**W1 — Ledger §3 row 10 and §4: "U-20 F4–F7 … are UNREVIEWED; the lane wrote no COLD START".** The second half is true; the first is false in the dangerous direction.
```
$ ls -la --time-style=+%H:%M .claude/worktrees/exceptions-20261025/.claude/lanes/exceptions/astra-branch.md
07:37 … astra-branch.md 4977
$ head -3 …/astra-branch.md
Reviewed `d7ad18596..c6a4e3f9f`. Four findings; no blocker established. Static review only …
1. **Major — P4 failure loses the unpersisted tool-evidence suffix.** service.py:3186 … 2985 …
$ git log -1 --format=%B 9055b82a8 | grep Astra
Found by the Astra branch review.
$ git diff --stat c6a4e3f9f ddc1ec9e8
 …composerOperationObserver.faults.test.ts | 3 +++
 …reviewed-full-source-guards.json          | 4 ++--
$ git diff c6a4e3f9f ddc1ec9e8 | grep -c diagnostic_reference
0
```
An Astra (static) review of the whole exceptions series exists. Finding 4 (restored-Stop test) was fixed by `9055b82a8`; finding 2 (F1 guard digests) by `ddc1ec9e8` — the guards half only; the `tool_batch.py:1019` carrier-model half is untouched. **Finding 1 (Major: after a successful tool then a service refusal, failed P4 publication drops the unpersisted tool invocations; cleanup drains only LLM calls, `composer_turn.py:1920`) and finding 3 (Minor: `diagnostic_reference=None` on both primary rejection routes, `provider_gateway.py:755/814`) have no code change after the review and no disposition anywhere** (`grep -n -i "suffix\|astra-branch\|unpersisted" HANDOVER-LEDGER.md STATUS.md exceptions-status.md` → nothing). Each Sol part also has a hand-off note with red/green and mutation evidence (`u20-f5-a/report.md`: "62 focused tests passed … Every new test received its named mutation"; `part-b-note.md`, `part-c-note.md`, `f6-note.md`, `u20-f7-part-e/note.md`).
Correction: "Reviewed once by Astra (static, `astra-branch.md`, 07:37) over `d7ad18596..c6a4e3f9f`: 4 findings, #2 and #4 fixed (`ddc1ec9e8`, `9055b82a8`), **#1 Major and #3 Minor OPEN and unowned**. Sol's red/green + mutation notes exist per part under `.claude/worktrees/exceptions-20261025/.claude/lanes/{u20-f5-a,u20-part-b-20261025,u20-f4-part-c,f6-primary-rejection,u20-f7-part-e}/`. No Claude review of parts B–E, no keyless diff, no landing-rule chain, no PG."

**W2 — Ledger §3 row 10: "U-20 F4–F7 (`748d548db`…`ddc1ec9e8`) were written by Sol".** The range is 7 commits; 5 are U-20 (F5A, F5B, F4, F6, F7). `9055b82a8` is a U-19 test fix and `ddc1ec9e8` a fencing re-pin, both Claude's responses to the Astra review (their bodies say so). Correction: "U-20 F4–F7 = `748d548db 3035bfeeb fe9278445 ef4096ebb c6a4e3f9f`; then `9055b82a8` (U-19 test, Astra #4) and `ddc1ec9e8` (F1 guard digests, Astra #2)." Also "written by Sol during the pause" is true only from `3035bfeeb` (06:37) on: `748d548db` is timestamped 06:22:18, before the 06:30 pause order, and STATUS 06:24 already recorded it (as `85f709968`).

**W3 — `exceptions-status.md`: "tip 85f709968 … 85f709968 WIP: Sol U-20 part A, UNREVIEWED, no red/green verification yet".**
```
$ git branch -a --contains 85f709968        → (nothing)
$ git reflog show codex/0.8.10-exceptions | sed -n 8,9p
d411959d0 reset: moving to HEAD~1
85f709968 rebase (finish): …
```
The branch is at `ddc1ec9e8` (9 commits); `85f709968` was reset away and re-committed as `748d548db` (tree-identical) with a real message, then 6 more commits followed. The "no red/green" line is contradicted by `u20-f5-a/report.md` (06:08). Correction: rewrite the file as a COLD START section (see § exceptions).

**W4 — Ledger header "Written: 2026-10-25 09:35 AEDT (`date`)".** `stat -c %y HANDOVER-LEDGER.md` → `09:34:24`. The last write precedes the stamp; the previous review's W3 recurred, by under a minute. Correction: stamp from `date` after the last edit, or say "09:34".

**W5 — `protocol-report.md:202`: "the pre-existing reds in `tests/unit/web/composer/test_continuation_prompt_producers.py` (U-44b queue)".**
```
$ git ls-tree -r --name-only release/0.8.10-assembly | grep prompt_producers
src/elspeth/web/composer/continuation_prompt_producers.py
tests/testcontainer/web/test_continuation_prompt_producers_postgres.py
tests/unit/web/sessions/test_continuation_prompt_producers.py
```
The same non-existent path the report itself diagnoses at :191 as the cause of `exit 5`. Correction: `tests/unit/web/sessions/test_continuation_prompt_producers.py`.

**W6 — `u45-officer-gate.md:1057-1058`: "Lane branch `codex/0.8.10-u45-fix` @ `1b59c10a1`, plus an uncommitted c9 that becomes a `wip(...)` commit at the pause"; :1123 "c9, uncommitted".** `git rev-parse codex/0.8.10-u45-fix` → `c26167c4d`; the same section says at :1220 "c9 is COMPLETE and committed as `c26167c4d` — not WIP". The lead paragraph a cold reader meets first is stale and contradicts the body. Correction: tip `c26167c4d`, nine commits, nothing uncommitted.

**W7 — `u30-report.md:235`: "The brief's `-n 2` already complies."** `grep -n -- '-n 2' AGENT-BRIEF.md` → no match; the brief says `-n 3` (hard) and `-n 4` (template). Correction: delete the sentence; the lane's own step-1 command uses `-n 4` (see I3).

### STALE

**S1 — Ledger §2 "3 U-45 §10 defects in composition_init".** STATUS 05:47: "7F/684P/5S … all 7 in test_continuation_composition_initialization.py"; STATUS 05:58: "parent 20F, tip 12F, all 12 tip ids also fail on the parent; … 3 timeouts → U-45 §10 defects; 3 bootstrap ExceptionGroup → TimeoutError, both the load-sensitive 1.0 s retention timeout". The file's red count on the target was 7 and 12 in two runs; 3 is the §10 subset. Correction: "7–12 reds in `test_continuation_composition_initialization.py` (load-sensitive 1.0 s retention timeout), of which 3 are the U-45 §10 defects fixed by part 1."

**S2 — `protocol-report.md:192/206/211`: the attachment-application lease patch "sent to design-runtime … `-full.patch` … its verification run was stopped"; open question "design-runtime: fold the attachment-application budgets into U-35b".** Measured above (verified table): every `-full`-only line is present in `a8aeab905`; `runtime-report.md:302` says "RESOLVED … live_handover-after passes on SQLite + PG". Correction: close the open question; the patches are durable via `a8aeab905`/`4c5e08d2d`; the only thing left is line coordination at landing.

**S3 — `u46fix/EVIDENCE.md:94` cites `corpus_compare.py` (and the oracle) from the lane dir as the P1 evidence instruments, while the branch carries different copies.**
```
$ diff <(git show codex/0.8.10-u46-diagnosis:docs/release/u46-instruments/corpus_compare.py) .claude/lanes/release-0.8.10/u46fix/corpus_compare.py | grep -c '^[<>]'
64
$ … u46oracle.py … → 7      (u46count.py: identical)
lane mtimes 03:28 / 02:39; corpus_old.json / corpus_p1.json written 04:46–04:49; branch copies committed 06:33 (6af0328e3)
```
The versions that produced the P1 equivalence evidence are the gitignored lane copies; the tracked copies were revised afterwards. Correction: state in EVIDENCE.md which version produced `corpus_old.json`/`corpus_p1.json`, and either re-commit those versions or re-run the comparison with the tracked ones at (a) time.

**S4 — `exceptions-status.md`: "Fencing file: … not run."** `ls …/lanes/exceptions/ | grep fencing` → `f1-fencing.log f1-fencing2.log f1-fencing3.log f1-fencing-base-failed.log f1-fencing-failed-ids.txt`, and `done.txt` lists fencing ids that completed. Partial runs exist (the ones the coordinator killed at 09:25 were later ones). Correction: "whole-file runs never finished (killed 09:25); partial id lists in `done.txt`/`f1-fencing-failed-ids.txt`."

**S5 — `u45-fix-report.md:115-117`: `chain3.sh` "kept at `<session-tmp>` — copy it into the lane directory before relying on it".** `ls .claude/worktrees/u45-fix-20261025/.claude/lanes/u45-fix/chain3.sh` → present. The copy was made; the pointer was not updated. Correction: point at the lane copy (and see N5: `u45pairs.py` lives in the *bisect* tree's lane dir, not the fix tree's).

### OVERSTATED

**O1 — Ledger §3 row 2 / `overlay-report.md:421`: overlay-fixes' first 3 commits "are superseded copies" / "OLDER copies".**
```
$ git patch-id --stable:  f3d0ed123 7c7f538b…  vs afaedadc0 109545f0…   (differ)
                          c473866ac 3af2ecd4…  vs 1dd80bebd f6ee2c9e…   (differ)
                          c332e4fd1 a82fc564…  == f17dd9d71 a82fc564…   (identical)
$ git range-diff afaedadc0^..c332e4fd1 506561cd4..f17dd9d71 → the only hunks are pin line numbers (+3) and the service.py sha256
```
Substantively superseded (the differences are the re-derived pins after `506561cd4`), so "do NOT land them" stands; "copies" is not literally true and `git cherry` will not drop two of them. Correction: "re-derived on `506561cd4`: 1 patch-identical, 2 differ only in pin values (range-diff); drop all three."

**O2 — `issue327-rest-report.md:97`: f3 `c153f5bb0` and u17 `98b090715` are "fully contained in the rest branch".** `git cherry codex/0.8.10-issue327-rest <branch> | grep '^+'` → 2 on each: `79efe4e98 wip(test): TC-006 failing-first …`, `b4ac65611 fix(composer): inspect sources through the shared fenced blob reader`. Their 4 `src/` files are byte-identical to the target (TC-006 landed folded as `9aff4e441`); the test file differs by 168 lines from the target. Correction: "contained via the target's TC-006 fold, not via the rest branch; the two pre-fold TC-006 commits are the only `git cherry` residue."

### UNSOURCED

**U1 — `RESUME-FIRST.md` Task 0.1 default owner "u45-firstcompose, because it already holds the duration data"; ledger §0.1 "Derive the value from `--durations`".** `grep -rl -- '--durations' .claude/worktrees/u46-fix/.claude/lanes .claude/worktrees/u46-doc/.claude/lanes .claude/lanes/release-0.8.10/u46fix` → nothing; the only mention anywhere is RESUME-FIRST itself. The lane holds fencing-tool wall/CPU timings, not suite `--durations`. Deriving `--durations` needs a suite run, which hangs without the `--ignore`s. Correction: "no duration data exists; the owner's first step is a `--durations=0` run of the default selection with both hang files `--ignore`d, under the capacity rules (slot required), then the derivation."

**U2 — Ledger §3 row 6 / §6 item 3 / `u02-engine-report.md:238`: "d937ac4ca".** `git merge-base --is-ancestor d937ac4ca codex/0.8.10-u02-engine` → no; `git branch --contains d937ac4ca` → `codex/engine010-carry-20261021`; `git log --grep=d937ac4ca release/0.8.10-assembly` → `7f058738c fix(engine): preserve unstarted work across cooperative shutdown`. The SHA is the carry source; a cold reader's ancestry check fails. Correction: "the d937ac4ca question (landed on the target as `7f058738c`)".

**U3 — Ledger §2 "6 in `test_composer_lease_adapter` (`TypeError: capture() got an unexpected keyword`): unowned."** Source is carve's message (STATUS 07:55) only; no log path, no ids. `u02-engine-report.md` lists ids and signatures for the other 8 target reds; none for these 6. Correction: name the log (or mark "ids not recorded; re-measure").

**U4 — `RESUME-FIRST.md` table: reserve contracts "runs stalled at 27% and 41%".** `grep -n "27%\|41%" STATUS.md issue327-rest-report.md u02-engine-report.md` → nothing; EVIDENCE.md:102-103 says 29% (contracts) and 43% (fencing); STATUS 06:20 says logs static 57/94 min. Correction: cite EVIDENCE's 29%/43% or the STATUS minutes.

### OMITTED

**M1 — The backup push was not done.** `origin/release/0.8.10-assembly` = `5297041d6`, 10 behind (verified at 09:40). PAUSE-PROTOCOL: "The coordinator does the only push, a backup of the target." Ledger §2: "a backup push is pending at closeout." `grep -n -i push STATUS.md | tail` → last push entry 03:32. Ten landed commits (U-38 `d7ad18596`, the AGENTS.md landing rule `a2cbca5b0`, TC-006, T5b/T5d, the F-1 follow-up…) exist only on this host, through a multi-day pause. Correction: push with `scripts/branch-safety-check.sh --intent push --allow-protected`, then cancel the triggered CI runs as johnm-dta (§1 rule), and log it in STATUS.

**M2 — Ledger §7 (release defect list) lacks the two open Astra findings from the exceptions review** (W1): F5 P4-failure evidence loss (Major) and F6 `diagnostic_reference` omission (Minor). Both are release-defect candidates in Continuation code with no owner.

**M3 — `0.8.10-work-packages.md`** (untracked, mtime 04:20). STATUS 06:36: "the work-packages refresh runs via a subagent"; STATUS 20:05 (10-24): "refresh at every landing/milestone". Not refreshed after 7 landings and the pause; the ledger only says move-or-delete before the merge. Correction: either refresh it from the 09:35 ledger or mark it superseded by the ledger in its header.

**M4 — T5e's owed ruling is missing from the ledger.** `T5e-metadata-writer.md:110` and STATUS 06:50: "OWED RULING (coordinator): which reviewed full-source guard ids count as landing evidence while the fencing file (the only pin verifier) is a KNOWN HANG". Ledger §3 row 5 lists only "rebase … re-derive pins; web selection"; §6/§7 do not carry it. It resolves with Task 0.2, so row 5 should say "lands after 0.2 or with a coordinator ruling on interim pin evidence".

**M5 — No worktree→branch→notes map.** `git worktree list --porcelain` has 160 entries; `grep -c '.claude/worktrees/'` → 144 under `.claude/worktrees/`, `grep -c DETACHED` → 58 detached scratch trees (all 58 under `.claude/worktrees/`). The ledger names branches; the worktree paths are scattered across 13 reports (e.g. reserve-repins lives in `overlay-fixes-20261025`, tier-analysis in `physical-audit-20261025`, residue-u45-integration in `protocol-basescan`). A cold coordinator needs one table: lane · branch · worktree · notes dir (release lane dir AND `<worktree>/.claude/lanes/<unit>/`).

**M6 — `…/lanes/exceptions/sol-f1.md` cites `docs/release/0.8.10-f1-executor-unavailable-note.md`.** `git log --all -- docs/release/0.8.10-f1-executor-unavailable-note.md` → nothing; not in the worktree. F1's red/green pointer is dangling (the `f1-u18/` logs remain). Correction: record that the note never existed in git and point at `f1-u18/`.

**M7 — `codex/0.8.10-runtime-u37` exists at `a8aeab905`** (the U-35b squash tip), with worktree `runtime-u37` on it (created 09:31). Ledger row 13 says only "`runtime-u37` worktree is empty". After U-35b is cherry-picked, `git cherry` on that branch shows `-` only if the pick is byte-clean; otherwise it looks like unlanded work. Correction: name the branch and its intent (U-37 starts here, on top of the U-35b squash).

**M8 — Ledger §8 (Hazards) omits three hazards the lane notes recorded, each of which bit this release:** (i) the PreToolUse hook blocks `git checkout -- <path>`; restore with `git restore --source=HEAD` / `git show HEAD:<path> > <path>` (`u46fix/EVIDENCE.md:135`, `u02-engine-report.md:264`); (ii) `pgrep -f` matches the shell running it, so "N processes" is wrong — count PIDs via `/proc/<pid>/cwd` (`u45-fix-report.md:395-401`, and the AGENTS.md `until ! pgrep -f` wait-loop trap); (iii) a pytest selection with a missing path collects nothing and exits 5 while looking like a run (`protocol-report.md:215`; it cost the residue landing check its evidence). Correction: add all three to §8.

### NOT-DURABLE

**N1 — The whole handover is gitignored.** `git check-ignore -v .claude/lanes/release-0.8.10/HANDOVER-LEDGER.md` → `.gitignore:69:.claude/lanes/`. That covers the ledger, RESUME-FIRST, AGENT-BRIEF, PAUSE-PROTOCOL, STATUS, both reviews and all 13 lane reports. The tracked documents do not carry Task 0: `grep -n -i "U-46\|pytest-timeout\|hang" docs/release/0.8.10-master-plan.md` → only the `CM … 600 s timeout | U-46` row; `docs/release/0.8.10-custody-handover-prompt.md:175` points the reader at `.claude/lanes/release-0.8.10/STATUS.md`. `u45-fix-report.md:421-431` names the same hazard about itself (14-day lane retirement). `git check-ignore -v docs/release/0.8.10-handover/HANDOVER-LEDGER.md` → rc=1 (not ignored). Recommendation in § Mutations #1.

**N2 — u30's next step depends on gitignored reports and instruments.** `u30-report.md:85` "Read `L/planner-tests-report.md`. Apply its plan"; `L` = `.claude/worktrees/u30-20261025/.claude/lanes/u30/`: `planner-tests-report.md`, `guard-tests-report.md`, `validation-tests-report.md`, `reachability.py`, `a9.sh`, `deleted-names.json`, `a9-base/` (all present, all ignored).

**N3 — u22's seam-4 open items** live in `.claude/worktrees/u22-20261025/.claude/lanes/u22/PARKED-section7.md` (present, ignored); `u22-report.md:175/192` depends on it.

**N4 — issue327's U-17 rebind instruments are in a session scratchpad.** `find <session-tmp> -name 'rebind*' -o -name 'status*'` → `…/0ecf926f-…/scratchpad/rebind.py`, `status_pass.py`. `issue327-rest-report.md:116` says the "rebind/status scripts [are] described in 20408a1ec's message"; `git log -1 --format=%B 20408a1ec` describes the primitives (`tool_contracts.py`, sha `f1ccc5322e0f808d` — recoverable: `git show 1530e7610:scripts/tool_contracts.py | sha256sum` → `f1ccc5322e0f808d`) but not the two scripts. The step "a second pass must report `CHANGED 0`" cannot be run cold without them.

**N5 — U-45 landing instruments and captures are gitignored:** `chain3.sh`, `dictscan.py`, `f4control.py`, `affected-files.txt` in `u45-fix-20261025/.claude/lanes/u45-fix/`; `u45pairs.py` and the 11 `p2-*.txt` pair captures (the baseline the c8/c9/c10 chain compares against) in `u45-bisect-20261025/.claude/lanes/u45-fix/`. The officer's own harnesses were already lost with its scratchpad (`u45-officer-gate.md:1068`) and are reproduced inline — the right shape.

**N6 — U-46 evidence** (`u46fix/out/tool_b.json`, `plantcheck.json`, `corpus_*.json` 2×54 MB, contracts logs) and the instrument versions that produced it (S3) are under `.claude/lanes/release-0.8.10/u46fix/` (ignored). The Mythos rulings (`mythos-p0-ruling.md`, `mythos-a-design-review.md`, `mythos-a2-confirm.md`) — rulings the (a) design must satisfy — are ignored too.

**N7 — The exceptions lane's entire evidence base** (Astra review, 7 Sol hand-off notes, 8 evidence dirs, ~420 files) is only under `.claude/worktrees/exceptions-20261025/.claude/lanes/`; nothing in the release lane dir except the 10-line status file.

**N8 — `u02-engine-report.md:235`: "The parser is in this session's history"** — the (id, first `E` line) comparator exists only in a transcript; `arch-idsig-diff.txt` shows its output shape only.

**N9 — Lane harnesses under worktree lane dirs:** T5e `ttdiff.py` (the keyless multiset diff with the planted-finding control); protocol `bin/uv` shim (the only safe way to run Playwright W-1/W-2 in a linked-venv worktree) and `landcheck/run.sh`; residue `landcheck/run.sh`. All ignored, all named as next-step tools.

**N10 — Source-shaped files under gitignored paths in the exceptions tree:** `…/lanes/exceptions/advisor_checkpoint.py.fixed` (identical to the branch file: `diff … | wc -l` → 0, so a copy) and `…/lanes/exceptions/d-fixed/src/`. AGENTS.md § Subagent Reporting: "Source under a gitignored path is a defect". Low: copies, not unlanded work.

### INCONSISTENT

**I1 — AGENT-BRIEF worker counts.** `:60` template `… -m pytest <paths> -n 4`; `:77` "Default per lane: at most `-n 4`"; `:83` "focused files at up to `-n 4`"; versus `:68` HARD RULE "At most `-n 3`" and RESUME-FIRST "`-n 3` max". The brief says the hard rules override, but a cold reader copies the template. Correction: edit `:60/:77/:83` to `-n 3`.

**I2 — AGENT-BRIEF § KNOWN HANG** says `--ignore` the fencing file "to directory-wide runs" and only "until the coordinator says U-46 (b) has landed"; the HARD RULES and RESUME-FIRST say both files, every run, and RESUME-FIRST 0.5 keeps the `--ignore`s until the no-ignore gate completes. The (b) trigger is a trap: per `u46fix/EVIDENCE.md:95` and STATUS 06:20, (b) alone turns the fencing file into a **34.5-minute RED** run (961 rows), not a green one; a lane that drops the `--ignore` at "(b) landed" puts a half-hour red walk into every directory run until (a) lands. Correction: both files, every run, until Task 0.5 is declared done; then delete the section as planned.

**I3 — Lane commands that break the hard rules as written:** `u30-report.md:86` `-n 4`; residue `landcheck/run.sh:8` `-n 4` with no `timeout` (the report says to edit it first, :200); `u46fix/EVIDENCE.md:111` `timeout 7200` (cap 2400 "unless the coordinator grants more" — no grant recorded; a 34.5-min tool run needs one). Correction: fix the commands, and record the U-46 grant in the ledger.

**I4 — STATUS ordering vs labels.** Lines 292–293: an entry labelled 06:24 precedes one labelled 06:20 — physical evidence for the 09:19 correction ("labels after 06:20 were estimated"). The ledger cites "the 09:18 correction"; the entry is labelled 09:19. Correction: "09:19".

**I5 — `u45-fix-report.md:429-431`: "The durable copies of its transferable content are the memory notes".** AGENTS.md § Subagent Reporting: "A durable deliverable … goes to a tracked path under `docs/`". A session's memory is neither tracked nor shared with the coordinator. Correction: see § Mutations #1 (snapshot the lane reports under `docs/release/0.8.10-handover/`).

## Per-lane review

Each lane: branch/tip/evidence claims vs git · cold-executability of the next steps · durability · consistency with the ledger and RESUME-FIRST · verdict · mutations.

### design-protocol — `protocol-report.md` § COLD START

- **Git:** `codex/0.8.10-protocol` @ `0cfa7d7a3`, 17/0 ✓; 17 subjects match the fold/rebase story (`db005a63d` carries the folded "means 1" fix) ✓; `config.py` identical to target ✓; `preserve/0.8.10-protocol-u41-pre-rebase 247b7f5af` ✓; codec-residue `82dd2ff49` 3/0 ✓, `preserve/0.8.10-residue-pre-rebase b90a41463` ✓; residue-u45-integration `1fb71a3bf` on `c26167c4d` ✓; `tests/helpers/continuation_budgets.py` on `b296259db` ✓; landcheck xmls present (19) ✓.
- **Cold-executable:** step 2's command is runnable after the two edits it names (drop the missing path; `-n 3` + `timeout`). Step 1 "when the coordinator says so" is precise enough. Step 5 (U-38c coordination) and 6 (real Stop after U-35b) are correctly deferred.
- **Durability:** `landcheck/run.sh`, `bin/uv` shim, keyless logs — ignored (N9). The lease patches are durable via `a8aeab905` (S2), so nothing of protocol's is held only in the residue lane dir.
- **Consistency:** W5 (wrong test path at :202); S2 (open question already resolved by runtime); the "Playwright through `bin/uv`" guidance agrees with u30's own-venv rule. Agrees with ledger rows 3 and 9.
- **Verdict: READY WITH FIXES.** Mutations: fix :202 path; close the lease-patch question with a pointer to `a8aeab905`; copy `landcheck/run.sh` (corrected) and `bin/uv` into the handover snapshot.

### U-45 lane — `u45-fix-report.md` § COLD START

- **Git:** `c26167c4d`, 9/0 ✓; c1–c9 subjects ✓; `u45-base` @ `d7ad18596`, `u45-bisect` @ `b89febd7a` (c3) as stated ✓; all three trees clean ✓; `preserve/0.8.10-u45-pre-u38-rebase f824d69bd` ✓.
- **Cold-executable:** steps 1–5 have commands, an acceptance criterion written before the run (the c8/c9/c10 captures), and the five new c9 ids by name. The `chain3.sh` pointer is stale (S5) but the copy exists; `u45pairs.py`, which `chain3.sh` needs, is in the *bisect* tree's lane dir — the report does not say so.
- **Durability:** N5. The report's own hazard 4 argues the lane dir will be retired; its proposed durable copy (memory notes) is not one (I5).
- **Consistency:** agrees with the officer gate on every number (44 literals/10 files; 24→22 messages; conditions 1/2 vs clauses (a)/(b)) and with ledger row 4 (c10 owed, pair chain c1→c10, keyless vs target, sweep).
- **Verdict: READY WITH FIXES.** Mutations: update the `chain3.sh` pointer and name `u45pairs.py`'s location; copy `chain3.sh`, `u45pairs.py`, `dictscan.py`, `f4control.py`, `affected-files.txt` and the 11 `p2-*.txt` into the handover snapshot (they are the baseline the landing chain compares against).

### U-45 officer — `u45-officer-gate.md` § COLD START

- **Git:** base `d7ad18596` ✓; `ea96096e1` not an ancestor of the branch ✓ (`git merge-base --is-ancestor ea96096e1 codex/0.8.10-u45-fix` → no); patch-id chain and net-diff checks are written out with commands ✓.
- **Cold-executable:** yes — every verification is given as a command, deliberately because the scratchpad harnesses are gone. The resumer note at :1095 (extract every `FunctionDef`, not a fixed list) is exactly the kind of hazard a cold reader needs.
- **Durability:** nothing load-bearing is held outside the report itself (its harnesses are already lost and reproduced inline).
- **Consistency:** W6 — the lead paragraph says c9 is uncommitted WIP; the body says committed. Otherwise consistent with the lane report and ledger.
- **Verdict: READY WITH FIXES.** Mutation: rewrite :1057-1064 and :1123-1125 to the committed state.

### u45-firstcompose (U-46, Task 0 owner) — `u46fix/EVIDENCE.md` § COLD START (+ `u46-report.md`)

- **Git:** `codex/0.8.10-u46-fix` @ `aa044516b`, 4 commits T1/T2/P1/(b) ✓, 19 behind (base `582cc93c2`) ✓; `preserve/0.8.10-u46-b-wip 36dca5e94` ✓, diff = 3+/1− in the fencing file ✓; `codex/0.8.10-u46-diagnosis` @ `6af0328e3` 13/80 ✓, `bf7d75ba5`/`a40c87fda` on it ✓, doc + instruments tracked ✓; u45-doc `40f67d02f`, u45-profile `5932af0e9` ✓; worktrees `u46-fix`/`u46-doc` at the stated tips ✓; evidence files named in the table exist (`tool_b.json`, `plantcheck.json`, `plant2_*`, `t1_*`, `t2_*`, `suite_b.log`) ✓.
- **Cold-executable:** steps 1–6 are concrete (preserve ref → rebase → tool run → plant-2 classification → commit wording → §8 rev 3 → implement (a)). Step 2's `timeout 7200` needs a recorded grant (I3). Step 4 names the ids the hang→red wording must list.
- **Durability:** N6, S3 (instrument drift: the committed `corpus_compare.py`/`u46oracle.py` are not the versions that produced the P1 evidence). The Mythos rulings are ignored files.
- **Consistency:** agrees with RESUME-FIRST 0.2–0.4 and ledger row "U-46 diagnosis doc lands with or before (a)". U1: nothing here supports the "holds the duration data" default for Task 0.1.
- **Verdict: READY WITH FIXES.** Mutations: commit the three Mythos ruling files and the evidence-producing instrument versions onto `codex/0.8.10-u46-diagnosis` (beside `u46-instruments/`), or state the provenance; record the 7200 s grant; give Task 0.1 a real first step (U1).

### carve-partition — `overlay-report.md` § COLD START (coordinator-written)

- **Git:** every row verified (reserve-repins order and 7/0; overlay-fixes 4/0 with `51f8b7872` the only new delta; physical-audit-fakes 1/0; tier-analysis 1/0 and path; isinstance-pairs 2/0 = `506561cd4`+`afaedadc0`; carve-partition 4/138; `preserve/0.8.10-overlay-fixes-pre-rebase-20261025 b81fd8678`) ✓.
- **Cold-executable:** landing order is explicit; the rulings (layer 2 ADMIT, layer 3 DEFERRED) are recorded with the controls that must stay red. "Re-measure everything on the target tip" is the right instruction for a note written from messages.
- **Durability:** the tier-analysis doc is tracked on its branch ✓. The 65-id physical-audit measurement "was not frozen" (STATUS 07:45) — the lane's own evidence is not referenced by path.
- **Consistency:** O1 (superseded "copies"); U3 (6 lease-adapter ids with no artefact). Matches ledger rows 1, 2, 14.
- **Verdict: READY WITH FIXES.** Mutations: O1 wording; ask carve at resume for the lease-adapter ids/log and the physical-audit before/after logs; the lane itself must confirm this coordinator-written section when it resumes.

### design-runtime — `runtime-report.md` § COLD START (coordinator-written)

- **Git:** `a8aeab905` 1/0, tree == `4c5e08d2d` ✓; `codex/0.8.10-runtime-u35b` 22/0 ✓; `7074ce4b8`/`e77a20c70` on it ✓; budgets helper byte-identical to protocol's (`1cf04ef7`) ✓; `runtime-u37` worktree at `a8aeab905`, clean ✓; 11 runtime trees all clean ✓; `preserve/0.8.10-runtime-u35b-674026773`, `-pre-u35b-split ee77a1ff9` ✓.
- **Cold-executable:** the 273-file attribution is named but not listed (where is the 273-file set? the lane's `.claude/lanes/runtime/` presumably — not cited). U-38c conditions are explicit. "Rebase onto 506561cd4 and re-derive pins with the tools if the sessions pins move" is actionable.
- **Durability:** the lease rule is in `a8aeab905` ✓ (S2 resolves protocol's worry). The 273-file list and the PG logs are lane-dir only.
- **Consistency:** agrees with ledger row 7, §5 (P1 fix approved, U-38c approved) and protocol's line-coordination rule. M7 (branch `codex/0.8.10-runtime-u37` unnamed).
- **Verdict: READY WITH FIXES.** Mutations: cite the 273-file list's path; name the `runtime-u37` branch and its intent; the lane confirms the section at resume.

### T5e peer "0.8.10 - Assistant" — `T5e-metadata-writer.md` § COLD START

- **Git:** `6c22638f3` 1/7 on `dcb742ecc` ✓; worktrees `t5e` @ `6c22638f3`, `t5e-base` @ `dcb742ecc`, clean ✓; service.py pin `57be012e…` = sha256 of the file at the tip ✓; `ttdiff.py`, `ttdiff.txt`, `tt2-{base,tip,ctrl}.json`, `sweep.txt`, `t5f-inventory.txt` present ✓.
- **Cold-executable:** the RESUME ORDER (506561cd4 first → rebase → re-derive both pins with the tools → then verify) is exactly what the ledger prescribes, with commands that honour the hard rules; step 2 is correctly left open pending the coordinator's ruling.
- **Durability:** `ttdiff.py` (N9).
- **Consistency:** M4 — its owed ruling is absent from the ledger. "Report body still owed" vs PAUSE-PROTOCOL § 3 (the body is the report) — acknowledged by the lane. The owed-citations list makes the body writable cold.
- **Verdict: READY WITH FIXES.** Mutations: add the ruling to ledger row 5; copy `ttdiff.py` into the snapshot.

### issue327-light — `issue327-rest-report.md` § COLD START

- **Git:** `20408a1ec` 23/0 ✓; pairs and fold SHAs on the branch ✓; `preserve/0.8.10-issue327-rest-pre-target-rebase 98b090715` ✓; helper worktrees as stated ✓; `scripts/tool_contracts.py` recoverable from `1530e7610` with the stated sha ✓.
- **Cold-executable:** step 1's rebase command is correct (and the `--onto … 9df1d29e6` trap is called out); step 2 cannot be run cold (N4: `rebind.py`/`status_pass.py` in a scratchpad, not described in the commit); steps 4–7 are runnable.
- **Durability:** N4.
- **Consistency:** O2; matches ledger row 8 (2 R5 findings, pairs, folds, U-17 Astra). The unowned MCP audit defect is in ledger §7 ✓. The 5 `test_pipeline_planner.py` reds are cross-referenced by u30 ✓.
- **Verdict: READY WITH FIXES.** Mutations: copy `rebind.py` and `status_pass.py` into the lane dir now and commit them beside the dossier instruments on the branch (or into the snapshot); soften O2.

### u02-engine — `u02-engine-report.md` § COLD START

- **Git:** `430b3bd46` 5/0 ✓ (subjects match); worktrees clean ✓; `preserve/0.8.10-u02-pre-rebase d58fed13d`, `preserve/0.8.10-u02-369734ba4` ✓; `$L/arch-idsig-diff.txt`, `astra-u02-followups.md`, `touched-tip6.log`, `abandon-pin-base.log` present ✓.
- **Cold-executable:** the optional re-comparison has full commands under the hard rules; the landing list and PG files are enumerated; the d937ac4ca ruling is argued with evidence and the "if ruled otherwise, what changes" list.
- **Durability:** N8 (pair parser transcript-only).
- **Consistency:** U2 (SHA label); the inherited-reds table is the best source for ledger §2 and agrees with it. Matches ledger row 6.
- **Verdict: READY WITH FIXES** (closest to READY). Mutations: name `7f058738c` beside `d937ac4ca`; write the parser into the lane dir (or describe its rule in two lines).

### composer-carries, U-30 — `u30-report.md` § COLD START

- **Git:** `f8fc444c9` 2/0 ✓ (two `wip` commits, red at tip as stated); `u30-base-20261025` @ `d7ad18596` ✓; both trees clean ✓; `.venv` symlinked in both (as warned) ✓; `L/` files present ✓.
- **Cold-executable:** the next step is "read three gitignored reports and apply their plans" — executable only while `.claude/worktrees/u30-20261025/.claude/lanes/u30/` survives (N2). Q1 is recorded in full; Q2 correctly routed to the maintainer with both options.
- **Durability:** N2 — the most load-bearing gitignored material in any lane.
- **Consistency:** W7; I3 (`-n 4`). Agrees with ledger row 11 and §5/§6. The U-22 conflict hazard (`prepare_bounded_pipeline_state`) is stated on both sides.
- **Verdict: READY WITH FIXES.** Mutations: commit the three bucket reports, `reachability.py`, `a9.sh`, `deleted-names.json` as a docs/tooling WIP commit on `codex/0.8.10-u30` (e.g. `docs/release/u30-instruments/`, the pattern U-46 used) — the branch is already WIP, so this costs nothing; fix :86 and :235.

### composer-carries, U-22 — `u22-report.md` § COLD START

- **Git:** `d0dcfc321` 8/7, fork `dcb742ecc` ✓; `c338a79cd` not an ancestor ✓; `codex/0.8.10-composer-carries` @ `0f4cd8aed` ✓; `preserve/0.8.10-u22-seam2-pre-rebase 67b6519c3` ✓; `PARKED-section7.md`, `cap-green2.log`, `cap-e2e3.log` present ✓.
- **Cold-executable:** step 1 (plain rebase after U-30, preserve ref first, expected conflict named) ✓; the d0dcfc321 split is reasoned and actionable ✓.
- **Durability:** N3.
- **Consistency:** matches ledger row 12 and STATUS 06:14/08:08 (U-30 → seam 2 → 16 KiB → seam 4 incl. §7).
- **Verdict: READY WITH FIXES.** Mutation: commit `PARKED-section7.md` onto `codex/0.8.10-u22` (docs path) or into the snapshot.

### exceptions — `exceptions-status.md` (no COLD START)

- **Git:** branch `ddc1ec9e8` 9/0 ✓ (the file says `85f709968`, 3 commits — W3). All 9 commits are timestamped 06:08–07:40; the last 6 (`3035bfeeb` 06:37 onward) post-date the 06:30 pause order, `748d548db` (06:22) does not. `preserve/exceptions-pre-rebase-20261025 edac53b88` ✓. Worktree clean ✓; the 2 hung and 3 wide runs are gone ✓.
- **Evidence actually present (the ledger says none):** `…/exceptions-20261025/.claude/lanes/`: `exceptions/` (Astra review `astra-branch.md` + brief, 7 Sol briefs + 7 hand-off notes, F1/F2-F3 red/green/gates logs), `f1-u18/` (43 files, 14 mutation controls), `f2-f3-20261025/` (60, M01–M08…), `u20-f5-a/` (report.md: 62 focused, 131 codec, every new test mutated), `u20-part-b-20261025/` (9 tests, 4 mutants), `u20-f4-part-c/` (25 tests, 4 mutants; "the nominal R5 site requiring reviewer adjudication"), `f6-primary-rejection/` (67), `u20-f7-part-e/` (35).
- **Open and unrecorded:** Astra #1 Major (F5 evidence loss at `service.py:3186/2985`), #3 Minor (F6 `diagnostic_reference`), the F4 "nominal R5 site requiring reviewer adjudication", the `tool_batch.py:1019` carrier-model half of #2. No Claude review of parts B–E; no keyless diff; no landing-rule chain; no PG; the fencing/reserve files never finished.
- **Cold-executable:** no — there is no next-step list. From git plus the notes a coordinator can reconstruct one: (1) read `astra-branch.md` and the five hand-off notes; (2) disposition #1 and #3 (fix or record as release defects with an owner); (3) adjudicate the F4 R5 site; (4) landing-rule pair sets per commit over the named selection, `-n 3`, `timeout 2400`, both hang files ignored; (5) keyless set-diff vs `d7ad18596`; (6) the whole-tree gates for its 19 files.
- **Durability:** N7, N10, M6.
- **Consistency:** W1, W2, W3, S4 against the ledger.
- **Verdict: NOT READY.** Mutations: the coordinator writes the COLD START section into `exceptions-status.md` from the material above (branch, 9 commits with the Sol/Claude split, evidence dirs, the four open items, the six next steps); copies `astra-branch.md` and the hand-off notes into the release lane dir / snapshot; adds the two Astra findings to ledger §7 with "owner: exceptions, before landing".

### Summary across lanes

| Lane / report | Tip claims | Next steps cold-runnable | Load-bearing material only in ignored/scratch paths | Verdict |
|---|---|---|---|---|
| design-protocol / `protocol-report.md` | ✓ | yes (after 2 named edits) | run.sh, uv shim | READY WITH FIXES |
| U-45 lane / `u45-fix-report.md` | ✓ | yes | chain3.sh, u45pairs.py, p2 captures | READY WITH FIXES |
| U-45 officer / `u45-officer-gate.md` | ✓ (lead paragraph stale) | yes | none | READY WITH FIXES |
| u45-firstcompose / `u46fix/EVIDENCE.md` | ✓ | yes (7200 s needs a grant) | out/ evidence, Mythos rulings, instrument versions | READY WITH FIXES |
| carve-partition / `overlay-report.md` | ✓ | yes | lane's own measurements not cited | READY WITH FIXES |
| design-runtime / `runtime-report.md` | ✓ | yes (273-file list uncited) | 273-file list, PG logs | READY WITH FIXES |
| T5e / `T5e-metadata-writer.md` | ✓ | yes (step 2 awaits a ruling) | ttdiff.py | READY WITH FIXES |
| issue327-light / `issue327-rest-report.md` | ✓ (O2) | step 2 no | rebind.py, status_pass.py (scratchpad) | READY WITH FIXES |
| u02-engine / `u02-engine-report.md` | ✓ (U2 label) | yes | pair parser (transcript) | READY WITH FIXES |
| composer-carries U-30 / `u30-report.md` | ✓ | only while the u30 lane dir exists | 3 bucket reports + 2 instruments | READY WITH FIXES |
| composer-carries U-22 / `u22-report.md` | ✓ | yes | PARKED-section7.md | READY WITH FIXES |
| exceptions / `exceptions-status.md` | ✗ (stale tip, 3 of 9 commits) | no section | Astra review, 7 Sol notes, 8 evidence dirs | **NOT READY** |

## Recommended mutations, prioritised (the coordinator decides; none applied here)

1. **Make the handover durable (N1, I5).** Commit a docs-only snapshot on the target: `docs/release/0.8.10-handover/{HANDOVER-LEDGER,RESUME-FIRST,AGENT-BRIEF,PAUSE-PROTOCOL,HANDOVER-LEDGER-review,COLD-START-REVIEW}.md` plus each lane report (or at least its COLD START section) and the small instruments named in N2–N5, N9 (`run.sh`, `bin/uv`, `ttdiff.py`, `chain3.sh`, `u45pairs.py`, `dictscan.py`, `f4control.py`, `rebind.py`, `status_pass.py`, `reachability.py`, `a9.sh`, `PARKED-section7.md`, the three u30 bucket reports, `astra-branch.md` and the Sol notes). Not STATUS's logs, not the 54 MB corpora. `git check-ignore` confirms the path is tracked. One commit, by the coordinator (the only committer on the target), subject `docs(release): 0.8.10 pause handover snapshot 2026-10-25`. Add one line to `docs/release/0.8.10-custody-handover-prompt.md § 1` pointing at it, so a reader of tracked docs finds Task 0.
2. **Backup push (M1):** `scripts/branch-safety-check.sh --intent push --allow-protected`, push, cancel the triggered runs as johnm-dta, log it. Do this after #1 so the snapshot is in the backup.
3. **Exceptions lane (W1–W3, S4, M2, N7):** write its COLD START section as drafted above; add Astra #1/#3 to ledger §7; copy `astra-branch.md` + hand-off notes into the lane dir.
4. **Ledger corrections:** S1 (composition_init count), U2 (`7f058738c`), U3 (lease-adapter ids or "re-measure"), M4 (T5e ruling), M5 (worktree table), M7 (`runtime-u37` branch), M8 (three hazards), W4 (stamp), O1 wording, I4 ("09:19").
5. **RESUME-FIRST (U1, U4):** replace "already holds the duration data" with the real first step (`--durations=0` run with both hang files ignored, slot required); cite 29%/43%.
6. **AGENT-BRIEF (I1, I2):** `-n 3` at :60/:77/:83; align the KNOWN HANG section with the hard rules.
7. **Lane notes:** protocol :202 path (W5) and lease-patch closure (S2); officer :1057/:1123 (W6); u30 :86/:235 (W7, I3); EVIDENCE.md instrument provenance (S3) + 7200 s grant (I3); u45-fix chain3.sh pointer (S5); issue327 O2; u02 U2 + parser (N8); runtime 273-file list path; sol-f1 dangling note (M6).
8. **Branch-side durability (N2, N3, N4, N6):** WIP commits of the u30 reports/instruments on `codex/0.8.10-u30`, `PARKED-section7.md` on `codex/0.8.10-u22`, `rebind.py`/`status_pass.py` on `codex/0.8.10-issue327-rest`, the Mythos rulings + evidence-producing instrument versions on `codex/0.8.10-u46-diagnosis`. Each is a docs/tooling-only commit on an already-WIP branch.
9. **`0.8.10-work-packages.md` (M3):** refresh from the ledger or stamp it "superseded by HANDOVER-LEDGER.md 09:35"; then move to `docs/release/` or delete before the merge, as already planned.
10. **Housekeeping, low:** remove the two source-shaped scratch copies in the exceptions lane dir (N10) or note them as copies; note the VS Code Playwright `test-server` (pid 2879262) is the maintainer's IDE, not a lane process.

## Readiness verdict

**READY WITH FIXES. Takeover confidence: 6 / 10** (8 / 10 once mutations 1–3 are applied).

Why not higher: fidelity is excellent — every SHA, count, order and ahead/behind in the ledger and in 12 of 13 lane notes checks out against git, the queue's first nine rows are executable cold, every tree is clean and nothing is running. But (i) the ledger's one material falsehood sits on the lane with the least documentation — "UNREVIEWED" hides a Major Astra finding in Continuation code that nobody owns; (ii) the entire handover, and the instruments eight lanes need for their first step, exist only under a gitignored path that AGENTS.md schedules for deletion, with no pointer from any tracked document; (iii) the promised backup push did not happen, so ten landed target commits are single-host. None of these is hard to fix; all of them would be invisible to a fresh coordinator reading what is in git.

Why not lower: Task 0 is specified well enough to start (owner, order, done-criterion), the rulings of 10-25 are recorded in one place and agree with STATUS, every lane but one wrote next steps with commands, and the hazards sections (stale `--onto` bases, `uv run`, `pgrep -f`, exit 5 on a missing path, instrument controls) are the ones that actually bit this release.

Counts: WRONG 7 · STALE 5 · OVERSTATED 2 · UNSOURCED 4 · OMITTED 8 · NOT-DURABLE 10 · INCONSISTENT 5 — **41 findings**.

`date` at finish: Sun Oct 25 10:05:42 AM AEDT 2026 (post-advisor corrections applied; first stamp 10:01:38)
