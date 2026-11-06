# Independent review of HANDOVER-LEDGER.md

Reviewer: independent Fable session (read-only; no edits, no git writes, no tests).
Reviewed: 2026-10-25 05:21–05:40 AEDT (`date` at first read and at the last edit).

## Scope and method

- Two versions of the ledger were read: mtime `05:21:44` and mtime `05:28:27`.
  The coordinator edits it concurrently. Every finding below names the line as it
  stands in the **05:28:27** version unless marked otherwise.
- The target moved while the review ran: `d7ad18596` (U-38) landed at 05:23:14.
  Lane tips `u22`, `u46-fix`, `t5e`, `issue327-rest` and `runtime-u35b` also moved
  between 05:18 and 05:26. Facts the ledger states "at `a2cbca5b0`" were checked at
  `a2cbca5b0`; where the live value differs, it is given as a STALE finding so the
  next refresh has the number.
- Sources: `git` (read-only), `STATUS.md` (274 → 278 lines during the review),
  `AGENT-BRIEF.md`, `AGENTS.md`, the lane reports in the lane dir
  (`runtime-report.md`, `protocol-report.md`, `u45-fix-report.md`,
  `u45-officer-gate.md`, `u45-part1-redteam.md`, `work-at-risk-0023.log`).
- Instrument controls run: `git rev-parse origin/release/does-not-exist` → fatal
  (a missing ref does not echo); `codex/0.8.10-nonexistent-control` → MISSING;
  `grep -c def test_this_does_not_exist_zzz` → 0; set-intersection control of a
  file list with itself → 13 (so an empty intersection is a real empty).

## Verified correct (no finding)

| Claim | Command | Result |
|---|---|---|
| §2 target tip `a2cbca5b0` (at write time) | `git rev-parse --short=9 release/0.8.10-assembly` (05:21) | `a2cbca5b0` |
| §2 `origin/release/0.8.10-assembly` = `5297041d6`, behind | `git rev-parse`; `git rev-list --left-right --count origin/...` | `5297041d6`; `0 9` (origin is an ancestor, 9 behind at `a2cbca5b0`) |
| §2 139 ahead of `origin/main` (at `a2cbca5b0`) | `git rev-list --count origin/main..a2cbca5b0` | 139; `origin/main` = `main` = `3b28d537b` |
| §3 all 25 SHAs, order, subjects | `git log --reverse --format='%h %s' fa9e91575^..a2cbca5b0` | 25 commits, identical order, every subject matches its row; nothing between `fa9e91575` and `a2cbca5b0` is missing |
| §3 `b36679965` = TC-004 | `git log -1 --format=%b b36679965` | body opens "U-14 / #327 TC-004" |
| §3 verification lines | STATUS 04:42 (656 passed, 1 skipped, exit 0); STATUS 05:19 (417 passed, exit 0) | match |
| §4 tips + ahead/behind at `a2cbca5b0` for runtime-u38-land, runtime, protocol, u45-fix, u46-fix, u22, t5e, overlay-fixes, u02-engine, exceptions | `git rev-list --left-right --count a2cbca5b0...<branch>` | all ten match the ledger's `(ahead/behind)` |
| §4 u45-fix "based on U-38 tip `ea96096e1`" | `git merge-base --is-ancestor ea96096e1 codex/0.8.10-u45-fix` | yes; branch = `ea96096e1` + c1..c7 (`44a97bcde c43922dd2 8598d9066 6e12d739b 358bcca8b 33737d863 ed2297a82`) |
| §4 (05:28) U-45 rebase line "landed U-38 is a different patch, so it won't drop out" | `git cherry release/0.8.10-assembly ea96096e1 cd90c8034` | `+ ea96096e1` (not patch-equivalent, correct). Files U-38 changed between `ea96096e1` and the landed patch ∩ files U-45 c1..c7 touch = ∅ (control 13), so the rebase should be clean |
| §4 `cb9cb3bf8` on overlay-fixes; `9df1d29e6` on issue327-rest; `cb28a0058`/`866989025` under `preserve/0.8.10-u02-pre-rebase` | `git merge-base --is-ancestor` | yes / yes / both yes |
| §4 u45-firstcompose "P1 done" | `git log release/0.8.10-assembly..codex/0.8.10-u46-fix` | `036a708f5 perf(fencing): … (U-46 P1)` present |
| §6 `a3b0e2f86` on target, not on main | `git merge-base --is-ancestor` ×2 | on-target; not-on-main |
| §6 lanes > 10 GB and gitignored `src/elspeth` copies | `work-at-risk-0023.log` lines 118–119 | `[WARN] 50+ src/elspeth/__init__.py under gitignored paths`; `[WARN] .claude/lanes exceeds 10.0G` |
| §7 `continuation_primary.py:165`/`:278` | `grep -n ':278\|:165' u45-fix-report.md` | both sites cited (lines 181, 205, 568) |
| §8 11-of-12 guard audit at `dcb742ecc` | STATUS 05:04 | matches |
| §1 `--allow-protected` exists | `grep -n allow-protected scripts/branch-safety-check.sh` | lines 22, 58, 72 |
| §1 both xfails exist | `grep -n` def + `pytest.xfail(` | `test_session_db_mutation_authority.py:21005` def, `:21040 pytest.xfail(`; `test_allowlist_loader_unification.py:180` def, `:214 pytest.xfail(` (both are imperative xfails inside the body, not markers) |
| §1/§5/§8 rules vs AGENT-BRIEF / AGENTS.md | read both | landing rule present in AGENTS.md § Test Verification Policy (added by `a2cbca5b0`, +18 lines); xfail names, signing, capacity, KNOWN HANG, `--ignore` instruction, push/CI-cancel procedure (STATUS 02:21/02:32) all match |
| §9 peer socket | `ls /run/user/1000/cc-socks/` | `2922602.sock` present |
| §10 preserve refs | `git for-each-ref refs/heads/preserve/ \| wc -l` | 20; every listed name/glob resolves; `preserve/0.8.10-p4-prototype-v152-20261024` → `897dabeb8` "wip(0.8.10): preserve p4-owner-prototype-v152 source snapshot" |

## Findings

### WRONG

**W1 — §4 composer-carries, "Lands when / State": `needs git rebase --onto <target> c338a79cd`.**
Measured: `git merge-base --is-ancestor c338a79cd codex/0.8.10-u22` → no. The branch already forks at `dcb742ecc` (`git merge-base release/0.8.10-assembly codex/0.8.10-u22` → `dcb742ecc`); the 04:21 instruction was executed. `git rev-list --count c338a79cd..codex/0.8.10-u22` → **12**, and `merge-base(c338a79cd, u22)` = `ba4b25ee2`, so running the ledger's command now would replay the target's own `2d41a085a`…`dcb742ecc` picks plus the lane's 7 commits onto the target.
Correction: "Seam 2 is already rebased (fork `dcb742ecc`); land with a plain `git rebase release/0.8.10-assembly` (or `--onto <target> dcb742ecc`)."

**W2 — §4 design-protocol, "Lands when": `fold 30c562483 into its parent`.**
Measured: `git merge-base --is-ancestor 30c562483 codex/0.8.10-protocol` → no. `git log release/0.8.10-assembly..codex/0.8.10-protocol | grep 'means 1'` → `44f56e823 fix(composer): a missing Composer-Operation-Version means 1`. This is the third SHA for that commit (`e054efb77` → `30c562483` → `44f56e823`); `protocol-report.md:87` already says "`30c562483` today".
Correction: name it by subject ("the planted-mutation fix 'a missing Composer-Operation-Version means 1', currently `44f56e823`") and note the lane pins `preserve/0.8.10-protocol-u41-pre-rebase` before folding (protocol-report.md:87).

**W3 — Header: "Current as of: 2026-10-25 ~05:45 AEDT".**
Measured: `date` → 05:23 at first read; `stat` → ledger mtimes 05:21:44 and 05:28:27; STATUS 05:19 "HANDOVER LEDGER started … measured at a2cbca5b0"; the target moved at 05:23:14. The stamp is in the future relative to every edit, and STATUS (21:45 entry) already recorded that estimated stamps "ran ahead".
Correction: stamp the actual write time from `date`, and keep "Measured at target tip" as the authoritative anchor.

### STALE

**S1 — §2 "Target tip `a2cbca5b0`", "Commits ahead of origin/main 139"; §3 ends at `a2cbca5b0`; §5 item 1 "U-38".**
Measured at 05:30: `git rev-parse --short=9 release/0.8.10-assembly` → `d7ad18596` ("feat(composer): U-38 production continuation policy from settings", 05:23:14; body: cherry-picked from `d22419b28` ← `f135a2b04`). `git rev-list --count origin/main..release/0.8.10-assembly` → **140**. `git rev-list --left-right --count origin/release/0.8.10-assembly...release/0.8.10-assembly` → `0 10` (origin 10 behind, 05:40). STATUS 05:23 records the landing and that U-45 + design-protocol were told to rebase.
Correction: add the `d7ad18596` row to §3 ("U-38, from design-runtime `60953a830`; masquerade getattr ×5 removed via `model_dump`; keyless 0/0 (3302); attribution vs `582cc93c2`: no green→red; composition_init 20F→3F, the 3 = U-45 §10 defects"), update §2, strike §5 item 1.

**S2 — §4 design-runtime row.**
(a) Branch: ledger names `codex/0.8.10-runtime-u38-land @ d22419b28` as the candidate. `runtime-report.md:10` (mtime 04:25, before the ledger) says "**Tip to land: `60953a830` on `codex/0.8.10-runtime-u38-final`**", and STATUS 05:23 landed from `60953a830`. Measured: `git patch-id --stable` of `d22419b28` and `60953a830` → both `595253a30c2f` (same patch, 10 files +582/−27; `60953a830` is a rebase of `d22419b28` onto `dcb742ecc`), so the content is not wrong, but the named branch was superseded an hour before the ledger was written.
(b) "A masquerade fix is being added: … 5 `getattr` sites … The attribution table is owed." Measured: `git show ea96096e1:tests/unit/web/composer/test_continuation_policy_settings.py | grep -c 'getattr('` → 5; the same at `d22419b28` and `60953a830` → **0**. `runtime-report.md:73` marks the probes "**fixed** in `d22419b28`" and carries the attribution table. Both were done by 04:25.
(c) The lane's live branch is omitted: `codex/0.8.10-runtime-u35b @ bedb06605`, 4 WIP commits at 05:25:51, **0 behind** `d7ad18596` (already rebased onto the landed U-38). `codex/0.8.10-runtime @ ee77a1ff9` (7/18) is the older WIP line; `git cherry` shows only 4 of its 7 are unlanded.
Correction: row → "U-38 LANDED `d7ad18596`. Live work: `codex/0.8.10-runtime-u35b @ bedb06605` (4/0), U-35b WIP. Older WIP `codex/0.8.10-runtime @ ee77a1ff9` (4 unlanded of 7)."

**S3 — §4 issue327-light "`codex/0.8.10-issue327-rest` @ `154bec495` (18/11)".**
Measured: `git log --format='%h %ci' -4 codex/0.8.10-issue327-rest` → `154bec495` 05:13:27, `a2749804b` 05:18:15, `494475ed2` 05:23:37. The ledger's first mtime is 05:21:44, so the tip was already `a2749804b` when it was written. Now `494475ed2`, 20 ahead / 12 behind `d7ad18596`. STATUS 05:19 adds a landing condition the row lacks: "`9b7a5a7b6`+`0870e6f2a` land together".
Correction: update tip/counts at refresh; add the pair-landing condition.

**S4 — §4 u45-firstcompose "`codex/0.8.10-u46-fix` @ `10893144b` (4/18) … (b) is WIP with a placeholder constant".**
Measured: STATUS 05:21 "(b) budget APPROVED = 10,000 eval-count (p99.9 653, ~15×)". Branch tip `36dca5e94` 05:23:46 "fix(fencing): retention budget set from the measured corpus: 10,000 evaluations (U-46 (b))", 5 ahead / 19 behind `d7ad18596`. The ledger's 05:28 edit did not pick this up.
Correction: "(b) constant approved and committed (`36dca5e94`); lands next. (a) design `bf7d75ba5` under Mythos review (STATUS 05:29)."

**S5 — §4 peer T5e "`de2072a1a` (1/6; WIP)".**
Measured: branch tip `6c22638f3` 05:23:33 "fix(sessions): every composition state carries its pipeline metadata", 1 ahead / 7 behind. STATUS 05:26: "T5e squashed to ONE commit 6c22638f3 (NOT NULL in epoch 79, record rule, sweep incl 49 raw inserts); full web selection + keyless + testcontainer running." STATUS 05:28 also approved a follow-on unit **T5f** (runs CHECK constraints) for the same peer.
Correction: update tip; add T5f to the peer's Unit(s) and to §5 after T5e.

**S6 — §4 composer-carries "`codex/0.8.10-u22` @ `451eec473` (6/6)".**
Measured: tip `4916ff479` 05:21:44 "feat(composer): one universal prepared-request cap at the physical seam", 7 ahead / 7 behind `d7ad18596`. This looks like the "16 KiB `prompt_template` crash fix" the row lists as a later item, which would change the row's sequencing ("U-30 first" was the ruling, STATUS 03:33/04:21).
Correction: update tip; confirm with the lane whether `4916ff479` is the prompt_template fix and whether U-30 is still first.

**S7 — §6 "`.claude/lanes/` is over 10 GB".**
Measured: `du -sh .claude/lanes` → **151G** (the "10 GB" is the WARN threshold from `work-at-risk.sh`, not a measurement). 13 WARNs in `work-at-risk-0023.log` across 111 worktrees, including 50+ `src/elspeth/__init__.py` copies under gitignored paths in the root checkout and in `composer-batch-qualification-20261023` and `security-boundaries-20261020`.
Correction: "151 GB (du, 05:25)" and name the three trees with 50+ source copies.

**S8 — §4 U-45 row (05:28) "Owed: red-team only".**
Measured: `u45-part1-redteam.md` (mtime 05:18) already covers `ea96096e1..33737d863`, confirms F1/F2 fixed by `ed2297a82` ("Verified at current HEAD … 194 passed"), and leaves **F3 open** (see O1). So the red-team has run; what is owed is F3's disposition and, if the lane wants it, a confirmation pass at `ed2297a82`.
Correction: "Red-team done (`u45-part1-redteam.md`): F1/F2 fixed by c7; F3 (low) needs a disposition before hand-over."

### OVERSTATED

**O1 — §7 "Six independent guard-key enumerations existed. Fix: U-45 part 1."**
Measured: `u45-part1-redteam.md` § F3 (low, confirmed): the drift-guard detector in `tests/unit/contracts/test_prompt_guard_provenance_thaw.py:132-156` inspects only `ast.Set/Tuple/List` literals, so two `or`-chain enumerations in `continuation_message_family_builders.py:161/:232` are invisible and the gate reports zero. `grep -n -i 'BoolOp\|or-chain\|or chain' STATUS.md u45-officer-gate.md u45-fix-report.md` → no match: no disposition exists anywhere. The red-team itself notes a counter-argument (per-key logic, no fifth key), but "Fix: U-45 part 1" states the instrument closed while it has a confirmed blind spot.
Correction: "Fix: U-45 part 1 (drift guard); red-team F3: the guard misses `or`-chain enumerations at `…family_builders.py:161/:232` — disposition owed."

**O2 — §5 item 4 "U-46 (b) … which restores gate verdicts"; §4 "(b) lands first; it unblocks every gate verdict".**
Measured: STATUS 05:21 "target goes RED with ~806 named fail-closed rows (not hang) until (a)". A verdict returns, but it is red.
Correction: "(b) replaces the hang with a RED verdict (~806 fail-closed rows) until (a) lands; (a) is what turns it green."

**O3 — §4 design-protocol "Also owns … 2 `composer_stream.py` trust-tier findings".**
Measured: `protocol-report.md:54-66`: the keyless result at `247b7f5af` is **one** finding re-keyed (R6 tuple → `except TimeoutError:`), already narrowed and justified as the honest form; the other item is "**Flagged, not changed (pre-existing)**": `BoundedComposerStreamResponse.__call__` catches `AuditIntegrityError` on our own data and turns it into a quiet close, with a recommendation to remove it and no owner. STATUS 04:02 says only "composer_stream trust-tier now", no count.
Correction: "composer_stream.py: 1 keyless finding re-keyed and justified; 1 pre-existing defensive `AuditIntegrityError` catch flagged with a removal recommendation, unowned (see §7)."

**O4 — §8 "Squashed or folded landings. Lane branches won't drop their commits by patch-id."**
Measured: `git cherry release/0.8.10-assembly codex/0.8.10-overlay-fixes` → `- dda18a953 - 23d141997 - bd48a85bd - e17d93783 + cb9cb3bf8 + 85a8df41c + d6909daf2`: 4 of the "7 ahead" in §4 are already landed-equivalent and will drop on a plain rebase; only 3 are unlanded. Likewise `u23-u24`, `u53-notes`, `envelope-walker`, `t5d-*`, `u45-diagnosis` all show 0 unlanded. The hazard is real only for squashed/folded picks (`fa9e91575`, `2d41a085a`, `9aff4e441`) and for pre-landing overlay bases (see U1).
Correction: scope the hazard to squashed/folded landings and overlay-WIP bases; note that `git cherry <target> <branch>` is a reliable instrument for plain picks and give the per-lane unlanded counts. Measured at 05:40 against `d7ad18596` with `git cherry release/0.8.10-assembly <branch> | grep -c '^+'` (unlanded) beside `git rev-list --count` (ahead): overlay-fixes 3/7, u22 7/7, protocol 17/17, u45-fix 9/9, u46-fix 5/5, issue327-rest 16/20, t5e 1/1, u02 4/4, exceptions 11/12 (2 real + 9 overlay-WIP).

### UNSOURCED

**U1 — §4 exceptions "`d42337e1e` (12/25) | Paused by plan after T2. A status line was asked for. | Later".**
Measured: `git log release/0.8.10-assembly..codex/0.8.10-exceptions` → the 12 are **10 pre-landing overlay commits** (`88c9b34aa … e66fbd1f1`, the `codex/0.8.10-continuation-overlay` WIP line; fork `ae3904bd7`) plus **2 code commits at 03:44:11**: `343aa13dd fix(web): report executor unavailability as executor_unavailable … (U-18/F1)` and `d42337e1e fix(web): give composer observation faults an explicit owner … (U-19/F2-F3)`. `git show --stat 343aa13dd` touches `src/elspeth/web/app.py` — the file named as the reason for the pause (STATUS 00:39 "PAUSED: exceptions (before any code; app.py)"). The last STATUS word on this lane is 03:21 (hung run stopped). "A status line was asked for" has no STATUS entry. `git cherry` → 11 of 12 `+`: the overlay-WIP commits are **not** patch-equivalent to `fa9e91575`, so a plain rebase would replay them.
Correction: "2 real commits (U-18/F1, U-19/F2-F3, 03:44) on a pre-landing overlay base; touches `web/app.py` (overlap with protocol/runtime). Must rebase with `git rebase --onto <target> e66fbd1f1`; nothing in STATUS records whether the pause was lifted or the app.py overlap is accepted."

### OMITTED

**M1 — U-46 diagnosis is unlanded.** `git cherry release/0.8.10-assembly codex/0.8.10-u46-diagnosis` → 11 `+`, 0 `-`. `git diff --name-only release/0.8.10-assembly...codex/0.8.10-u46-diagnosis` → `docs/release/0.8.10-fencing-gate-diagnosis.md` + 6 instruments under `docs/release/u46-instruments/`; `git ls-tree release/0.8.10-assembly docs/release/` has no fencing doc. STATUS 01:52 ("diagnosis done 439d09f30") never says LANDED; STATUS 05:29 adds `bf7d75ba5` (§8, the (a) design) with a Mythos 5.1 review running (`u46fix/mythos-a-design-review.md`). The ledger's u45-firstcompose row cites only the Mythos P0 ruling. A successor would not know this durable deliverable exists or that it is off-target.

**M2 — The §9 idle-host timing run and its release-blocker consequence.** STATUS 03:23 "§9 idle run scheduled by me near hand-over"; 03:26 "§9 idle result >2.5s p95@50 pairs PG ⇒ design-runtime re-sizes U-38 policy before merge (RELEASE blocker, not U-45)". Owed by the coordinator; absent from §5, §6 and §8.

**M3 — Q-stage requirements recorded in STATUS but absent from §2 "Gate state".** (i) `npm ci` in the qual worktree — 22:42 "FINAL GATE MUST provision node_modules via npm ci" (10 environment reds in gate #1); (ii) the DURABLE GREEN milestone includes "a git bundle refreshed on /mnt/data" (00:38); (iii) pre-push "review of the security docs for open-finding exploit detail" (22:59); (iv) "REVIEW AT Q" of the U-05c `interpretation_state.py` widened `suppresses=` (22:31; maintainer 23:04 "cf. the open U-05c item").

**M4 — Unit ownership missing from §4 "Unit(s)".** U-24 (seam 3 + seam 6 acceptance suites, post-overlay) → composer-carries (STATUS 00:40 T3); U-17 + MCP F3 ('<unregistered>' + sha256; `ComposerToolInvocation` has no digest field) → issue327-light (00:17; a live worktree `issue327-f3-20261025` on `codex/0.8.10-issue327-f3` exists at `494475ed2`); U-44a `title_provider`/`title_application` orphan pulled into U-45 scope (04:56).

**M5 — Unowned release-defect candidate from the protocol lane.** `protocol-report.md:66`: `BoundedComposerStreamResponse.__call__` converts `AuditIntegrityError` (raised by `authorize()` on our own data) into a 503 / quiet close — "defensive handling of our own data", recommendation to remove, left alone as pre-existing. Under the maintainer's 10-25 trust-tier rulings (brief § "'defensive' code in T1 isn't 'the right version'") this belongs in §7 with an owner.

**M6 — The root report `0.8.10-work-packages.md`.** STATUS 20:05: maintainer request, "refresh at every landing/milestone … move to docs/release/ or delete before the merge"; 00:40: it holds the thread plan. It exists (`ls` → present), is untracked, and §1 "Where the details live" does not list it or its disposition obligation.

**M7 — Open maintainer questions where "defaults proceed" (STATUS 22:03, 22:06, 22:13, 21:59).** Revert during a live request → refuse; Stop with an in-flight provider call → abandon-and-reconcile; a provider call cut by kill/drain → fail rather than retry; the judge MCP reader caps (24 calls / 400 lines) and the 2026-09-09 shell ruling (`judge.py:2291`); and the U-02 `d937ac4ca` INTEGRITY QUESTION ("is abandon-on-non-resumable right? a real row loss = STOP") — u02-engine's 4 commits pin the ABANDONED record (`369734ba4`, `df850a4d6`) but STATUS has no ruling. These predate the 6-hour window but remain open and §6 lists none of them.

## Resolved between the two reads (not counted)

- §6 "Whether his other 10-25 rulings get tracked wording" — removed by 05:28 (STATUS 05:26: maintainer says they are already in AGENTS.md/CLAUDE.md).
- §7 `_StrictResponse` "six … execution unverified" → "all seven … AST-checked, control 255 `frozen=True` files" (STATUS 05:21 correction). Verified present in the 05:28 version.
- §4 U-45 row gained the correct rebase form `git rebase --onto d7ad18596 ea96096e1 codex/0.8.10-u45-fix` and the B6 result (STATUS 05:28; `u45-fix-report.md:827-869` "0 differences across all 31 shapes"). Verified correct (see table above).

## Post-dated items for the next refresh (after 05:21, not counted as errors)

- STATUS 05:28 T5f approved (runs CHECK constraints; own unit after T5e; epoch 79 or 80).
- STATUS 05:29 U-46 (a) design `bf7d75ba5`, Mythos 5.1 review running; flagged deviation from rule 2 (set-valued origins incl. None).
- STATUS 05:21 "target AGENTS.md is authoritative over worktree copies (branches cut before `a2cbca5b0` lack the landing rule)" — a §8 hazard.
- STATUS 05:19 carve-partition told to stop its U-46 blow-up measurement (U-46 = u45-firstcompose); U-13 `9d4abc9a7` projection gap fixed.
- `codex/0.8.10-runtime-u35b` @ `bedb06605` (4 WIP commits, 05:25, 0 behind).

## Verdict

**CORRECT WITH FIXES.**

Every SHA, count and ordering the ledger states *at `a2cbca5b0`* is right, the preserve refs are right, and the rules match AGENT-BRIEF/AGENTS.md. The errors are two rebase/fold instructions that point at SHAs no longer on their branches (one of which would replay 12 commits if run as written), a future-dated stamp, a cluster of tips that moved within minutes of writing, two state lines stated more strongly than their source, and seven omissions — the most consequential being the unlanded U-46 diagnosis branch, the §9 idle-run release blocker, and the exceptions lane's pre-landing overlay base.

Counts: WRONG 3 · STALE 8 · OVERSTATED 4 · UNSOURCED 1 · OMITTED 7 — 23 findings.
