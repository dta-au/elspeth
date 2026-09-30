# Advisor structured output: review fixes (implementation prompt)

Paste everything below the rule into the session that owns
`fix/advisor-structured-output`.

---

You own branch `fix/advisor-structured-output` in
`.claude/worktrees/advisor-structured-output`. A read-only review of `e4ad02c72`
(base `4afd73169`) found the defects below. Fix them on this branch as new
commits. Do not merge, push, or sign anything.

## Before you start

- If the gate you launched is still running, let it finish and record its
  `summary.txt` in the implementation report as-is. Do not edit the tree while
  it runs: a moved tree makes that run `frozen=NO` and worthless.
- Use TDD for every item. Write the test, run it, and show that it fails on an
  assertion, not on an import or name error. Then make it pass.
- Worktree imports: export
  `PYTHONPATH=<worktree>/src:<worktree>/elspeth-lints/src` and check that
  `elspeth.__file__` points into the worktree.

## 1. The boot probe must detect a schema that is accepted and then ignored

**Defect.** `boot_probe.py` tells the advisor
`Return exactly this JSON object: {...}`. Any model that follows instructions
returns that JSON whether or not `response_format` was enforced, so the probe
cannot detect an ignored schema. The plan required it to.

**Fix.**
- Use a neutral prompt that would naturally get a prose answer, for example
  "This is a configuration check. Reply with ok." Do not describe or quote the
  JSON shape anywhere in the probe request.
- Pass when the reply is **schema-valid** (`AdvisorResponseAdmission.schema_valid`).
  Do **not** require semantic acceptance. A model under an enforced schema,
  asked to "reply with ok", can legitimately return CLEAN with a note. The
  parser rejects that combination, and failing boot on it would be a false
  rejection.
- Prose, invalid JSON, a tool-call-only reply or empty content must still raise
  `ComposerBootConfigError`.

**Tests.**
- Positive control: an enforced-looking reply (schema-valid JSON, including
  CLEAN with a non-null note) passes.
- Negative controls: an `ok` prose reply fails, and a fenced reply
  (```` ```json ... ``` ````) fails.
- A structural assertion that the probe's outgoing `messages` contain none of
  the schema field names (`verdict`, `findings`, `note`, `steps`, `category`).
  Show this assertion goes red against the current prompt.

## 2. The advisor probe must not silently time out

**Defect.** `_COMPOSER_BOOT_PROBE_TIMEOUT_SECONDS = 5.0` (`app.py:194`) wraps
the advisor probe. The probe now sends `reasoning_effort` (medium on the
deployed `openrouter/z-ai/glm-5.3`) and the full
`composer_advisor_max_completion_tokens`. A timeout is non-fatal
(`composer_boot_probe_transient_failure`), so on this deployment the conformance
check may never run. This is unmeasured: measure it before you choose a fix.

**Measure first.** Find a recorded source of real advisor call latency: LLM
call audit rows in a session DB, telemetry, or journal lines. **Do not read
`deploy/elspeth-web.env`, and do not make live provider calls with deployment
credentials.** Report the source, the sample size, and p50/p95. If no source
exists, say so and do not guess.

**Fix.**
- Give the advisor role its own boot-probe timeout, sized from that
  measurement. Keep the planner's 5.0 s unchanged.
- Do **not** shrink `max_tokens` for the probe. A reasoning model can spend a
  small budget entirely on reasoning and return empty content. That would fail
  boot falsely under item 1.
- Keep the request options identical to runtime: the probe exists to exercise
  the real checkpoint request.
- If the right timeout adds noticeably to boot time, stop and ask John before
  choosing it.
- A timeout stays non-fatal. That boot policy is existing and deliberate.
- For the advisor role, the timeout warning must say plainly that
  structured-output conformance was **not** verified this boot. Add a distinct
  field or event, not only prose, so an operator can alert on it.

**Tests.**
- The advisor timeout is used for the advisor role and the planner timeout for
  the planner role.
- The advisor timeout warning carries the "conformance not verified" signal.
  The planner timeout warning does not.

## 3. An advisor 400 must say what it can actually know

**Defect.** Every advisor-probe `BadRequestError` is reported as
"structured-output capability request rejected". The request also carries
temperature, seed, reasoning and the OpenRouter `provider` routing object, so
the label can be wrong. The provider's error text is dropped for the advisor,
but kept for the planner.

**Fix.**
- Use a neutral message that names what was sent: model, and whether
  temperature, seed, reasoning effort, `response_format` and `provider` routing
  were present.
- Keep `from exc`.
- Include provider error text only if it goes through the same handling the
  planner branch uses. First check whether the planner branch interpolating
  `{exc}` is itself acceptable under the secret-scrub rules. If it is not, say
  so in the report and fix both branches the same way. Do not leave them
  inconsistent.

## 4. Re-prompt wording must match the rejection

**Defect.** When a reply is schema-valid but breaks a contract rule (CLEAN with
a note or with steps; FLAGGED with blank findings), the re-prompt says "did not
satisfy the checkpoint schema". That is false, and it does not name the rule
that was broken.

**Fix.**
- Use two fixed, backend-authored re-prompt strings: one for schema-invalid
  replies, and one for contract-rule violations that names the rules (for
  example, "For CLEAN, steps must be empty and note must be null; FLAGGED
  requires non-empty findings").
- Never echo provider text.
- Both strings must still travel the existing contracted arguments channel.

**Out of scope.** Do **not** change whether CLEAN with a note is rejected. John
has not ruled on accept-and-discard, and the new conformance fields exist to
measure that rate first.

## 5. Sanitizer fixes (`advisor_output.py`)

Each fix needs a positive and a negative control, shown red before the fix.

- **a. Sentinel reassembly.** The fence-sentinel `.replace()` runs before the
  `Cf` strip, so `END_UNTRUSTED​_ADVISOR_FINDINGS` becomes a live
  sentinel. Measured on `e4ad02c72`. Move the replace after the
  character filter.
- **b. Bare host plus path.** `evil.example.com/login` is not redacted today.
  Redact a dotted host followed by `/` and a path.
  - Must redact: `evil.example.com/login`, `sub.evil.io/a?b=c`.
  - Must not change: `sink.path`, `csv_out.options.path`, `data.csv`,
    `outputs/results.csv`, `v1.2.3`, `row.field`, and a step id with dots
    followed by prose.
  - Bare hosts with no path, such as `evil.com`, are out of scope. They are too
    close to option paths; say so in the report.
  - Count these in `url_redactions`.
- **c. Markdown links.** `[docs](https://x.io/a)` becomes
  `[docs]([link removed]`, because the URL rule eats the closing parenthesis.
  Keep the brackets and parentheses balanced:
  `[docs]([link removed])` or `docs [link removed]`. Pick one and pin it. A URL
  that genuinely contains `)`, as some Wikipedia links do, must still be fully
  redacted inside a markdown link.
- **d. Schema `$ref`.** `category` is emitted as `$defs`/`$ref` because
  `AdvisorFindingCategory` is a PEP 695 type alias. Some constrained-decoding
  backends reject `$ref`.
  - Emit the enum inline.
  - Keep `ADVISOR_FINDING_CATEGORIES` derived from a single definition.
  - Add a test that the emitted schema contains no `$ref` and no `$defs`.

## Verification and hand-back

- Scoped: rerun the affected files: `test_advisor_output.py`,
  `test_boot_probe.py`, `test_advisor_structured_checkpoint.py`,
  `test_advisor_checkpoint.py`, the boot-probe cases in `tests/unit/web/test_app.py`,
  and the mock-discipline and masquerade gates. Record the exit codes.
- Full gate: production files change, including app boot, so rerun
  `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest`.
  Before launching, check host load and other suites, and cap workers if needed.
  Read `summary.txt` and confirm `frozen=yes`.
- PostgreSQL: run `pytest tests/ -m testcontainer -n 0` to a lane-private log
  and record its exit code. It is still owed from the first pass.
- Lints: compare the corpus against `e4ad02c72` using the
  `(path, rule, message)` multiset method, with its controls. New tier-model
  binding churn is expected; list it. Do not edit allowlists or signatures.
- Known reds: the two `test_freeform_planner_failure_translation` failures
  reproduce on the base. Attribute them again by name; any other red must be
  explained.
- Rerun `git merge-tree --write-tree release/0.8.1 HEAD` and record the release
  tip SHA and the exit code.
- Update `docs/reviews/2026-09-23-advisor-structured-output-implementation.md`
  with:
  - a section for this pass: items 1–5, the latency measurement for item 2 and
    its source, and the timeout you chose;
  - replacing the probe's "observes conformance" claim with what it now
    actually proves;
  - the new gate evidence.
- Hand back the branch HEAD, one line per item (fixed, or blocked and why), and
  the paths to the gate and testcontainer summaries.
