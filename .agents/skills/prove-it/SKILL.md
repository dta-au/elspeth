---
name: prove-it
description: >
  Use before telling the user that any work is complete, fixed, passing,
  landed, or merged — including "done", "all green", "committed", "the bug no
  longer reproduces" — and whenever the Stop hook blocks a session with a
  prove-it reason. Also use when reviewing another agent's completion claim.
  Applies to code, tests, commits and merges in any git repository.
---

# prove-it — a completion claim is a hypothesis until it survives falsification

**You do not report success. You report a verdict file.** A claim of
completion is decomposed into assertions, each one is attacked by an
instrument that cannot see your working directory, an adversarial reviewer
tries again, and the result is written to `.verify/<timestamp>.md`. If any
assertion is unproven the verdict is FAIL and your message to the user says
exactly which claims **could not be substantiated** — never that the work is
done.

The Stop hook in `.claude/settings.json` enforces this: a session that changed
files or history cannot stop until its newest work is covered by a PASS
verdict or an explicit, reasoned withdrawal. It is per-session (keyed on
`CLAUDE_CODE_SESSION_ID`), so other sessions' claims are invisible to yours.

The mechanism is `python .agents/skills/prove-it/prove_it.py` (`--help`). From
another repository call it by absolute path and point it at that repository with
`--repo <path>`; the claims and verdicts land in that repository's `.verify/`.

## Step 1 — Decompose the claim into falsifiable assertions

Write the claim in one sentence, then list assertions a hostile reader could
check. Every assertion has a kind and a payload:

| Kind | Payload | What proves it |
|------|---------|----------------|
| `test:` | `<command>` | exits 0 in a **fresh worktree at the branch tip** |
| `commit:` | `<sha> on <branch>` | `git rev-parse` resolves it and it is an ancestor of the branch tip |
| `mutation:` | `<test cmd> :: <fix path>[,<path>] [@ <base>]` | with the fix reverted, the test goes **red** |
| `exit0:` / `nonzero:` | `<command>` | exit code in a fresh worktree (e.g. a repro script that must now exit 0, or must now fail) |
| `file:` | `<path>` | present at the branch tip, not merely on disk |

```bash
P="python .agents/skills/prove-it/prove_it.py"
$P claim --claim "parse_price accepts thousands separators" \
  --assert "test: pytest tests/test_parse.py -q" \
  --assert "commit: 2d97ccf on main" \
  --assert "mutation: pytest tests/test_parse.py::test_thousands -q :: pricekit/__init__.py @ 2d97ccf~1" \
  --assert "nonzero: python -c 'import pricekit; pricekit.parse_price(\"x\")'"
```

Include a `mutation:` for **every claimed fix** and a `commit:` for **every
claimed commit**. A claim with no mutation assertion for a fix is incomplete,
not lightweight.

## Step 2 — Run the deterministic verifier

```bash
$P verify --claim <claim-id>       # exit 0 only on PASS; UNREVIEWED and FAIL exit 1
```

What it does, and why the working directory is never the instrument:

- `test:`/`exit0:`/`nonzero:` run in a **fresh detached worktree** at the
  branch tip. A test that passes only because of uncommitted edits, a stale
  cwd, or leftover build output fails here, and the evidence says so
  ("working directory has N uncommitted changes").
- `commit:` is `git rev-parse` + `git merge-base --is-ancestor`; a sha that is
  on a different branch, or does not exist, is unproven.
- `mutation:` first runs the command on the UNMODIFIED tree (it must exit 0:
  a command that cannot run, or that is red anyway, proves nothing), then
  reverts the fix and requires the test to FAIL AT AN ASSERTION **as reported
  by the runner**: a pytest command is launched with `-p prove_it_red_plugin`,
  which records every call-phase report whose OUTCOME is `failed`, and RED
  means that report carried a `pytest.fail` or an `AssertionError` raised in a
  test file (the test module, a `python_files` match, or a `conftest.py`). A
  marked or imperative xfail is reported skipped and never counts; an
  `AssertionError` raised inside production code is a crash — the test
  asserted nothing itself. Exit codes and output text are never consulted —
  every runner reports an uncaught exception as exit 1 too, and the test
  controls its own output, so a test that prints a verdict and then crashes
  is a crash. A test that merely imports what the fix
  adds is "crashed rather than failed" and unproven; if the fix adds a module
  or attribute, assert it exists (`importlib.util.find_spec`, `hasattr`) so
  the assertion is what fails. Under any runner other than pytest the failure
  kind is **not measurable** and the mutation stays unproven with exactly that
  reason: the green-to-red flip is observed but not certified. An in-place
  verdict records a sha256 per reverted path, and `review` refuses to record
  a verdict if any of those bytes changed since `verify` — re-verify first.
  The record
  is proof against MISTAKES (a crash mistaken for a failure), not against a
  test that deliberately forges it — the test runs in the plugin's process
  and could append to the report file itself; deliberate fraud is what the
  adversarial reviewer reads the test source for. Every run
  prove-it launches sets
  `PYTHONDONTWRITEBYTECODE=1` (a stale `.pyc` of the same size and second
  would execute the fix's code after the revert). A symlink path is refused —
  name the real file. An uncommitted
  fix is reverted **in place for the named paths only**, from a byte snapshot,
  and the snapshot is written back and compared afterwards — unrelated edits
  are never touched, and no `git checkout --` or stash verb runs (this
  repository hard-blocks stash after silent work loss; never work around that
  block). A committed fix names its pre-fix base with `@ <base>` and is
  reverted inside a fresh worktree, so the checkout is untouched.
- Every assertion's evidence names the instrument and the tree it ran on.

The verdict file is `.verify/<timestamp>.md`: header lines (`claim:`,
`session:`, `verdict:`, `review:`), one PROVEN/UNPROVEN line per assertion
with evidence, an **Unproven** section, and the review section. Deterministic
success is `UNREVIEWED`, not PASS.

## Step 3 — Spawn the adversarial reviewer

Spawn a fresh subagent whose only goal is to **falsify** the assertions. Give
it the claim file (`.verify/claims/<claim-id>.json`) and the verdict file, and
this brief:

> Your job is to prove these assertions false. Do not run anything in the
> working directory: create your own fresh worktree or clone at the branch tip
> and work there. For each assertion look for the way it could pass for the
> wrong reason: a test that does not exercise the fix, a mutation that went red
> for an unrelated error, a commit that is on the branch but not the fix, a
> repro script that fails before reaching the bug. Write your findings to a
> file, one section per assertion, ending with a single line `VERDICT: PASS` or
> `VERDICT: FAIL`, and return one line.

Record what it found:

```bash
$P review --claim <claim-id> --verdict PASS|FAIL --findings <reviewer file>
```

A reviewer PASS cannot rescue a deterministic FAIL; a reviewer FAIL turns a
deterministic pass into FAIL. Only both together produce `PASS`.

## Step 4 — Report the verdict, never the claim

| Verdict | Your message to the user |
|---------|--------------------------|
| PASS | The work, with the verdict path. |
| FAIL | "Could not be substantiated:" followed by every unproven assertion and its evidence, verbatim from the Unproven section. Then what you will do next. |
| UNREVIEWED | Not reportable. Finish Step 3. |

If you cannot substantiate a claim and will not fix it now, withdraw it so the
record shows you did not claim success:

```bash
$P withdraw --claim <claim-id> --reason "A3 unproven: the mutation did not go red; test does not exercise the fix"
```

Withdrawal releases the Stop hook; it does not make the work verified, and
your message must say the work is incomplete.

## The Stop hook

`stop_hook.py` reads the session transcript — and every subagent transcript
under `<session>/subagents/agent-*.jsonl`, so delegated work counts once it
is handed back — the parent's tool_result for a foreground `Agent` call, or a
teammate's idle notification (work still in a running subagent's hands must
not re-arm the gate on every yield the parent makes while waiting; a subagent
whose metadata names no hand-back channel is counted in full; one killed
before handing back is the documented fail-open) — for work signals:
Edit/Write/NotebookEdit on a path inside a worktree of this repo, `Workflow`,
any MCP tool whose verb is not in a small read-only allowlist, and Bash
commands that commit/merge/push/pull/reset git state or use a git alias,
copy/move/remove/rewrite files, run a formatter, fixer or package manager, a
filigree CLI verb outside its read set, `elspeth run --execute` or a canonical
script with `--execute`, redirect output to a path, or run inline Python that
writes or shells out. Redirects to `/dev/null`, `/tmp`, a `scratchpad`
directory or a shell variable are logs, not work: that is the hook's one
deliberate fail-open, and the Bash side is a heuristic that will always have
gaps. The hook reads **no prose**. It blocks the stop whenever a work signal
is newer than this session's last PASS verdict or withdrawal, whatever the
final message says, and its block reason names the exact next command. A
session that is waiting, blocked, or honestly not done releases itself with
`withdraw --reason '<why>'` (no `--claim`), or `withdraw --claim <id>` for a
filed claim — withdrawal is the honest exit, and the message to the user then
says the work is not verified. (A classifier over the final message was
tried; measured on 1507 real sessions it gated 58 % of honest messages and
released 6 % of real claims, and the operator ruled it out on 2026-09-09.)
After five blocks since the session's last release
(a PASS or a withdrawal; doing more work does not re-arm it) the hook
releases the session with a loud "NOT verified" message so a stuck session
cannot loop forever — that release is a safety valve, not a verdict.
`$P status` shows this session's claims and verdict.

## Rationalizations that produce false completions

| Thought | Reality |
|---------|---------|
| "I ran the tests and they passed." | In the working directory, with whatever was on disk. `verify` runs them at the branch tip in a fresh worktree; if they only pass here, the work is uncommitted or the cwd is stale. |
| "The fix is obviously right, mutation is overkill." | A test that stays green with the fix reverted is not testing the fix. Thirty seconds. |
| "I committed it, I saw the hash." | `git rev-parse` sees it too, and also whether it is on the branch you named. |
| "The reviewer is just going to agree." | Then it costs one subagent. When it does not agree, you were about to report a false completion. |
| "I'll withdraw to get past the hook." | Fine — and your message then says the work is incomplete and why. Withdrawal is honesty, not a bypass. |
| "This is a docs-only change." | Then the assertions are `file:` and `commit:`. Still file them. |

## Red flags — stop and run `verify`

- The word "done", "complete", "fixed" or "green" in a draft message with no
  verdict path next to it.
- A test run whose cwd is the checkout you edited in.
- A claimed fix with no `mutation:` assertion, or a claimed commit with no
  `commit:` assertion.
- A verdict of UNREVIEWED being treated as a pass.
- Reading a reviewer's one-line return instead of its findings file.
