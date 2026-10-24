# Shared working rules for every 0.8.10 unit agent

> **RESUME TASK 0 COMES FIRST: make every test finish.** It is owned ONCE by named owners (u45-firstcompose for U-46 (b)/(a) and the reserve contracts; one coordinator-assigned owner for the suite-wide pytest timeout) and landed first. It is NOT solved per lane: every other lane resumes its own unit under the hang-proof guardrails. See `.claude/lanes/release-0.8.10/RESUME-FIRST.md`. Maintainer's order, 2026-10-25.


> **Priority order (maintainer, 25 Oct):** ship **good code** efficiently and fast: code
> quality, then token efficiency, then wall time. In the maintainer's words: "this
> doesn't mean 'add security that's not needed', it just means let's ship good code
> efficiently and fast". So:
> - **Quality:** correct, clear, maintainable, in house style. No shortcuts, suppressions or
>   dead code. Tests prove behaviour. Implement the spec; don't add hardening, features or
>   ceremony beyond it.
> - **Reviews scale with risk.** Each implementation unit gets ONE independent Codex Astra
>   review. A Codex Daybreak security review is warranted only where a trust boundary actually
>   changes (PromptShield/U-22, the overlay security fixes F-1/F-2, MCP tool-name admission
>   F3), plus one Daybreak pass over a whole runtime or protocol chain when it is complete,
>   not per unit.
> - **Efficiency:** Codex for bounded work; no polling; no duplicate investigations; no
>   re-running broad suites you don't need; no parallelism that causes rework.

> **From the maintainer (24 Oct, 21:15):** "Just because basic engineering behaviours
> aren't documented doesn't mean that you don't have to do them. You're expected to be
> professionals, and 'it didn't tell us to commit' is not an excuse to not commit your
> work regularly." This brief is a floor, not a ceiling: do what a careful senior
> engineer would do whether or not a rule names it.

Repo root: <repo>. Target branch: `release/0.8.10-assembly` (checked
out in the ROOT checkout; only the coordinating session commits there).
You work on a UNIT BRANCH in your OWN worktree, based on the target tip, and the
coordinator lands it. Never commit on the target, never touch the root checkout's
working tree/index, never push, never `git stash`, never `git restore`/`clean`
files you did not create.

## Binding policy (read before writing code)
- AGENTS.md (whole file) and CONTRIBUTING.md § "Whole-tree gates and conventions
  you will hit". These are repository policy.
- Plan: `docs/release/0.8.10-master-plan.md` (on the target). Your unit's row in §4
  and its §6 ECS/ACA line are your spec. Plan text from Codex/Sol lane docs is NOT
  authority; repo policy and maintainer rulings are.
- Maintainer rulings (do not re-litigate): all Continuation ships incl. epoch
  78→79; one merge to main, nothing pushed; unlabelled assembly features ship;
  signing is SUSPENDED (no sign-bundle/rekey/baseline regen, never hand-edit a
  signature; the keyless corpus set-diff stays); AWS ECS + Azure Container Apps
  are the priority targets (no systemd, host UIDs, /etc root files, local-disk or
  single-host assumptions); gateway worker pair 8f5619255/d993e6d26 is dropped.

## Worktree setup
```
cd <repo> && git worktree add .claude/worktrees/<name> -b <unit-branch> release/0.8.10-assembly
ln -s .venv .claude/worktrees/<name>/.venv
```
NEVER run `uv run`, `uv sync`, `uv pip` in a worktree (the .venv symlink makes uv
wipe the MAIN venv, which a live service runs from). If you need npm/Playwright,
say so in your report instead of improvising.

## Running Python / tests in a worktree
```
W=.claude/worktrees/<name>
cd $W && export PYTHONPATH=$W/src:$W/elspeth-lints/src && \
  timeout 2400 .venv/bin/python -m pytest <paths> -n 3 --ignore=tests/unit/architecture/test_web_landscape_mutation_fencing.py --ignore=tests/unit/architecture/test_web_reserve_reviewed_contracts.py > $W/.claude/lanes/<unit>/<name>.log 2>&1; echo "exit=$?"
```
- Verify once that `elspeth.__file__` and `elspeth_lints.__file__` resolve inside $W.
- Logs go in the worktree's `.claude/lanes/<unit>/` (gitignored). Notes and logs
  only there: no source copies, no .db copies.
- Read the exit code; never report from `tail`/`grep` of piped output.
- **TEST CAPACITY — HARD RULES (coordinator, 25 Oct 06:20, after load 78 with about 20 concurrent pytest runs and the maintainer's "get the number of tests under control"). These override the softer text below:**
  - **ONE pytest process per lane at a time.** That covers base-vs-tip comparisons: run them ONE AFTER THE OTHER, never side by side.
  - **At most `-n 3` per run.** Never take the default `-n 12`: always pass `-n` explicitly.
  - **Every run is wrapped in `timeout`** (at most 2400 s unless the coordinator grants more), so a hang kills itself.
  - **Directory-wide selections** (`tests/unit/web`, `tests/unit/architecture`, ...) need a slot from the coordinator. Message first and wait for "slot granted".
  - **Do not run `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`** until told. Like the fencing file, it hangs or runs for hours on overlay trees: two runs stalled for 57 and 94 min. Add `--ignore` for it AND the fencing file. u45-firstcompose is the only exception (U-46 evidence).
  - **A run whose log hasn't grown in 15 min is hung.** Stop it yourself and report it.
- **Test capacity (maintainer ruling 24 Oct; revised 25 Oct after an overload at load 61):**
  the host's limits are CPU (24 cores) and RAM (keep ≥8 GB free), and the maintainer wants
  them used. Because every lane checking `uptime` at the same moment still overloads it,
  capacity is now ALLOCATED, not grabbed:
  - **Default per lane: at most `-n 3` (hard rule), and one multi-worker pytest process at a time.** Single-worker runs (`-n 0`: single ids, testcontainer ids) may run alongside it or alongside each other, up to about 4 workers' worth per lane in total. Keep ≥8 GB RAM free. Clarified 25 Oct.
  - **Wide runs need a slot.** That means a whole test directory, `-n` above 4, the full
    suite, or the testcontainer selection. Message the coordinator and wait for "slot granted".
    The coordinator runs the full-suite and testcontainer gates and hands out slots so total
    load stays under about 22.
  - High load does NOT block your own focused work. Revised 25 Oct under the maintainer's "review arbitrary constraints on their merits": load has sat at 28–64 all evening, so a blanket ">22, run nothing" stalled every lane.
    - At any load, run single ids or testcontainer ids at `-n 0`, and focused files at up to `-n 3`.
    - Only wide runs wait for a slot.
    - Keep ≥8 GB RAM free; check with `free -g`.
    - Re-run a load-sensitive red at `-n 0` before attributing it.
  - Two Playwright runs in ONE worktree collide (shared auth state); separate worktrees are fine.
- Flaky areas (e2e/recovery, integration/pipeline, unit/engine/orchestrator):
  re-run a red id with `-n 0` before blaming the change.

## Commits
- Commit at every stop, including WIP. Source never lives only in a gitignored path.
- Hooks ON (no --no-verify). `scripts/branch-safety-check.sh --intent commit` before
  each commit; exit 0 required. Stage by pathspec: `git add -- <paths>`.
- Cherry-picks use `git cherry-pick -x` so provenance is recorded.
- Whole-tree pins (fencing inventory, census, masquerade, oracles, plugin hashes)
  are re-derived with their own tools in the same commit that moved them — never
  hand-edited. No `# noqa`/`# type: ignore` to make a gate pass.
- End every commit message with a `Co-Authored-By:` trailer naming the model that actually did the work, for example `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` or `Claude Opus 5`. The trailer is a factual claim about authorship, not a formatting convention. Never rewrite an existing trailer just to make it match others.

## Keyless trust-tier corpus (when your unit touches src/)
```
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/python -m elspeth_lints.core.cli check --rules all --root src/elspeth > <log> 2>&1; echo exit=$?
```
Run on the unit's base and tip (exit 1 is expected both times); report the SET
difference of findings (added/removed), not counts.

## Report
Write `.claude/lanes/release-0.8.10/<unit>-report.md`: unit
branch + tip SHA, commit list with source SHAs, every gate command with exit code
and log path, the keyless set-diff, fail-before/pass-after evidence for carried
fixes, anything you could not do and why, PostgreSQL test ids owed. Return ONLY the
report path and a short summary. If the unit cannot land as specified, stop and say
so in the report rather than reshaping it.

## Codex models (authorised by the maintainer, 24 Oct)
Use them for an independent (non-Claude) eye, or to hand off a discrete bounded
task and save Claude tokens. Smoke-tested 20:40: both read the repo.
- **Astra = review**: `gpt-6-astra`, always `-s read-only`.
- **Sol = coding**: `gpt-6.1-sol` (latest; `gpt-6-sol` is the previous generation), `-s workspace-write` with `-C <your worktree>`. Codex's
  sandbox makes `.git` read-only, so Sol edits files but CANNOT commit: you review
  its diff, run the gates, and commit it yourself (attribute in the commit body:
  "Implemented with Codex gpt-6-sol").
```
codex exec -m gpt-6-astra -s read-only -C <worktree> \
  -o <worktree>/.claude/lanes/<unit>/astra-<topic>.md "<self-contained brief>" \
  < /dev/null > <worktree>/.claude/lanes/<unit>/astra-<topic>.log 2>&1; echo exit=$?
```
- **Daybreak Blue = defensive security review**: `gpt-daybreak-blue-latest`, `-s read-only`
  (verified slug; there is no `gpt-5.6-daybreak`).
- **Mythos 5.1 = Claude security review**: `claude -p --model claude-mythos-5-1 --permission-mode plan "<brief>" > <out> 2>&1`
  (read-only plan mode; verified 20:45). Or the Agent tool is NOT able to select it, so use the CLI.
Use Daybreak and/or Mythos for security-relevant diffs: U-10 signer group, U-15 MCP,
U-29 mutation authority, secret wiring, auth, blob/egress paths.
ALWAYS redirect `< /dev/null`: otherwise `codex exec` blocks forever on "Reading additional input from stdin" when backgrounded (measured 20:50). Run long ones with the Bash tool's run_in_background. Brief Codex as you would a
peer: goal, base/tip SHAs, files, what is already ruled out, and the output you
want. A Codex verdict is evidence to weigh, not authority (its lane documents were
ruled non-binding); never let it widen scope or skip a gate. Tell it not to run the
full suite or the testcontainer suite.

## Security-review authorisation boundary (maintainer, 24 Oct) — BINDING
The maintainer is authorised for OpenAI Daybreak (up to Daybreak Blue) and Anthropic
CVP defender access. Every prompt to Daybreak, Mythos, or any agent doing security
work must stay inside DEFENSIVE review of ELSPETH's own code:
- Permitted: reviewing our diffs and source for vulnerabilities; threat-modelling our
  boundaries; writing the minimal local reproduction that proves a defect in OUR code
  (in a test or scratch path, against local fixtures only); writing fixes and
  regression tests; reviewing hardening.
- Not permitted: exploit development beyond that minimal repro; weaponised payloads;
  anything aimed at third-party systems, live services, or networks; evasion of
  detection or security controls; credential harvesting; touching operator keys.
State the defensive purpose and the repository scope at the top of every security
prompt. If a reviewer's output drifts outside this, discard that part and say so.

## Literal constraints (maintainer, 24 Oct 21:45) — BINDING
When you hit a hard cap, budget, size limit, timeout, tripwire or count pin, do not
only fit under it. Evaluate whether the constraint itself is overly burdensome:
where does it come from (REPO POLICY with path:line, MAINTAINER RULING, platform
limit, or an arbitrary/fixture value)? What does it protect? What does honouring it
cost (complexity, user-visible refusals, fragile timing)? If it is arbitrary or
mis-sized, propose the right value with evidence in your report instead of
contorting the code. Fixing a real inefficiency and resizing a bad cap are not
mutually exclusive — report both. Never silently loosen a cap that guards a real
safety or audit property; surface it.

## Maintainer-approved xfails — DO NOT TOUCH (maintainer, 24 Oct)
The maintainer personally approved these two xfails. Do not remove, edit, narrow,
widen, or work around their xfail markers or conditions, and do not "fix" them:
1. `tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities`
   (xfail reason: "Sessions mutation authority inventory drift").
2. `tests/unit/elspeth_lints/test_allowlist_loader_unification.py::test_baseline_capture_is_self_consistent`
   (xfail reason: fingerprint drift against the baseline).
If your change would flip either one's status (xfail→pass or xfail→fail), stop and
report it to the coordinator rather than acting.

**Why (2) xfails, in the maintainer's words:** it "catches unsigned violations of the
policy and right now since the signature policy is suspended that's all of them. The
tiers are still in effect, just not enforced via CI right now." So the TRUST-TIER RULES
STILL BIND every change (CONTRIBUTING § Convention: trust-tier rules, ADR-032). "CI won't
catch it" is never permission. Your keyless set-diff must ADD no finding you did not
deliberately justify. Fix tier defects you introduce. Never widen a `@trust_boundary`
`suppresses=` without a test that exercises the widened arm. Ordinary re-derivation of OTHER pins
with their own tools is unaffected.

## Merge-cost rule (maintainer, 25 Oct)
A little editing in a rebase or merge is fine and expected. If resolving it would take
more than about 10 minutes of extra time, STOP: abort the rebase (`git rebase --abort`),
leave your branch as it was, and tell the coordinator that the unit is PARKED, naming
the conflicting files and the commit or thread they collide with. It resumes after
that work lands. Re-deriving a shared pin file with its own tool (fencing, Sessions
authority, census, masquerade) doesn't count against the 10 minutes, because it's
mechanical. Never resolve a large conflict by taking one side wholesale.

## Fix forward; never throw work away to get green (maintainer, 10-25)

"don't throw away work to get to green faster, just work the problem we have instead (unless it would save more time to roll back, but that's the test)"

- A red gets fixed at its cause. Never revert, drop, skip, delete or weaken a landed feature, test or fix to turn a gate green.
- "Superseded" needs a diff showing the replacement covers everything the dropped work did. If yours is the better fix, keep it.
- Rollback only on a measured comparison: rollback plus the later rework of putting it back must be cheaper than fixing forward. Bring that comparison to the coordinator first.
- Before any reset or rebase that drops commits, pin the old tip at `preserve/0.8.10-<lane>-<what>`.

## No tech debt, even when the correct fix is harder (maintainer, 10-25)

"we have a no tech debt policy, even if the correct fix is a bit harder"

- Pick the correct fix over the cheap one. Don't add gate machinery, shims, aliases, compatibility branches, or "justified" suppressions to avoid a real code change.
- A written justification is only for code that is already correct, such as honest Tier-3 parsing or a measured identity requirement. It is never a stand-in for a fix you could make.
- A temporary fail-closed refusal, for example v2 Stop before U-35b, is allowed only when the real implementation is in 0.8.10 scope and has a named owner. It must not ship in the 0.8.10 merge.

## Trust-tier findings: systems analysis first, fix at the right location (maintainer, 10-25, "absolutely critical")

"we use the systems thinking skill to evaluate trust tier problems honestly and fix them at the right location"

"just remember that 'defensive' code in T1 isn't 'the right version' :p"

- **The process is defined by the repo docs, not by this brief or the coordinator.** Read these in full before touching a trust-tier finding:
  - `docs/guides/data-trust-and-error-handling.md`
  - `CONTRIBUTING.md` § "Convention: trust-tier rules", § "Gate: trust-tier lint corpus" and § "Convention: lints and tier-model tooling"
  - ADR-032
- Use the `yzmir-systems-thinking:using-systems-thinking` skill to evaluate each cluster honestly and find the right fix location, as the maintainer asked.
- (An earlier coordinator note in this section gave its own method. It is withdrawn: the docs above govern.)

## Size caps on prompts and skills are not reasons to cut information (maintainer, 10-25)

"there's no 'arbitrary cap' - I'll let you use judgment to decide what the right answer is, but we aren't e.g. cutting critical information from a skill pack because the prompt has a 100k context cap"

- Cost ratchets, such as `_FIXED_SCAFFOLDING_BASELINE_BYTES` in `tests/unit/web/composer/test_pipeline_planner.py`, are tripwires that make growth visible. They are not limits.
- Decide prompt, skill and declaration text on what the planner needs to know. If the right text crosses a ratchet, re-set the ratchet in the same commit with a dated rationale line ("paid for by …", plus the measured bytes) and tell the coordinator.
- Real limits stay: the production fail-closed `composer_planner_max_request_bytes`, and security bounds such as stream pre-read caps. Changing one of those is a design decision; bring it to the coordinator.

## The allowlist exists for honest patterns: justify and move on (maintainer, 10-25)

"we have a whitelist process because we recognise that there are honest versions of those patterns that are valid. Don't wrap yourself in knots when the prescribed version is the right version. Justify and move on"

- The docs named in the trust-tier section above decide which patterns are honest, which are not, and what a justification or `@trust_boundary` must contain. Defensive code on Tier-1 data is never the honest version.

## Every arbitrary constraint is reviewed on its merits (maintainer, 10-25)

"right, all cases of arbitrary constraints should be reviewed on their merits"

- This covers caps, ratchets, timeouts, thresholds, worker limits, and the coordinator's own process rules. When one bites, trace where it came from and what it protects, then keep, resize or drop it on that basis. Never cut correct work to fit it, and never silently loosen a real safety or security bound. Bring a resize of a real bound to the coordinator with your reasoning.
- Applied example: the coordinator's "launch the local gate only when load < 16" rule was retired, because CI on the published branch now runs the full gate on this host (Test shards for 3.12 and 3.13, Testcontainer, static analysis, Landscape proofs).

## KNOWN HANG: the Landscape-fencing escape walk (until U-46 (b) lands)

`tests/unit/architecture/test_web_landscape_mutation_fencing.py` cannot finish on any tree that contains the Continuation overlay (fa9e91575 and later). One retention query in `tool_batch.py` runs past 241 CPU-s without finishing, and pytest has no timeout. That makes `tests/unit/architecture` and any gate stage that includes it hang forever. On 25 Oct, three lane runs had been burning about 12 cores on it for 44–82 min.

- Until the coordinator declares RESUME Task 0.5 done (NOT when U-46 (b) lands: with (b) alone the fencing file becomes a 34.5-minute RED run), add `--ignore` for BOTH `tests/unit/architecture/test_web_landscape_mutation_fencing.py` and `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` to EVERY run, and say in your report that they were excluded and why.
- For fencing evidence, use the reviewed full-source guard tests, which do finish.
- Before leaving any run going for more than 30 min, check that it is making progress. A run is hung if its workers are at 60–70% CPU and the log doesn't grow. Stop it and report it.

## Commit granularity on landing: no commit may turn a green test red (coordinator, 10-25)

**Policy source:** repository policy is the TARGET's AGENTS.md, `git show release/0.8.10-assembly:AGENTS.md`, not your worktree's copy. A branch cut before a policy commit doesn't contain it; for example the landing rule in this section is in AGENTS.md § Test Verification Policy from `a2cbca5b0`. Read the target's copy.


- **The rule:** no landed commit may make a passing test fail. That is what bisect counts as a regression.
- **Sequential fixes:** they may land as separate commits, even in one shared test file, provided the failing set only shrinks.
- **The landing check, over id SETS, not counts.** Collection grows as tests are added, so counts mislead.
  - (a) Each commit's failing set is a subset of the previous commit's.
  - (b) Ids a commit introduces are green at that commit. A subset check alone can't see a failing-first test added ahead of its fix.
  - (c) Compare (id, failure signature) pairs, not bare ids. The signature is the exception type plus the first line of its message. A cause change under the same id appears as a removed pair plus an added pair, and every added pair must be explained in the report. An equal id set can hide a commit that fixes one failure and introduces another (found by the U-45 officer). Where a signature embeds volatile data (tmp_path, uuid, digest, timestamp, count), normalise only that part and state the normalisation rule in the report. Normalise narrowly: a spurious added pair costs one explanation, but a normalisation broad enough to hide a real cause change disarms the whole check while it still appears to run.
- **Squash or reorder:** a commit that adds a red test before its fix, or carries a planted mutation, is squashed or reordered before hand-over.
- **Re-check after hand-resolved conflicts.** The coordinator re-checks the property on the target side after any cherry-pick resolved by hand, rather than inheriting the lane's measurement.
