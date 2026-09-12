# Composer Wires Campaign checkpoint — 2026-09-10

> **Historical pause record.** On 2026-09-12 the operator authorized custody,
> consolidation on one branch, and an updated completion plan. Continue from
> `composer-wires-consolidated` and the
> [living campaign plan](2026-09-08-composer-wires-campaign.md).
> The pause, worktree and commit restrictions below describe September 10;
> they do not override that later authorization. Test results remain historical.

Paused at the operator's request. Implementation agents have released ownership;
all task-owned processes have exited and no temporary mutation remains. Do not
resume implementation until the operator requests it.

The work is **uncommitted and not ready for integration**. Branch
`composer-wires-campaign` remains at
`1278c5c215fc8f749d53fd16d57413e416b93a00`, based on `release/0.8.1`, in
`.claude/worktrees/composer-wires-campaign`. Nothing is staged. The shared release
checkout and older `soft-type-burndown` worktree were preserved. The standing ban
on commits remains in force.

The [campaign plan](2026-09-08-composer-wires-campaign.md) remains the scope
authority. This checkpoint distinguishes reviewed component work from the
unfinished changes present at the stopping point. Reviews and tests below bind
to their recorded intermediate snapshots; later MODEL edits overlap some of
those files, so they do not establish that the current integrated tree passes.

| Component | Status at pause | Recorded evidence |
|---|---|---|
| Initial MODEL census | Original bounded census reviewed; now extended by unfinished MODEL implementation | Original 29 focused tests; actual handler admission distinguished from redaction manifest models |
| Frontend registry and option decoder | Registry repair reviewed; decoder implemented; widget completion still depends on sparse presence | 340-test mutation selection passed before/after restoration; removing a tool from export and fixture still failed the frontend guard; Python fixture gate passed 44 tests without Node |
| Prospective effects | Reviewed implementation | Independent 378 tests; same 74-test selection caught incorrect blob effects and passed after restoration |
| Response-envelope measurement | Reviewed structural repair | 37 additional measured fields; 13 behavioral mutants caught with the same 92-test selection; independent 92-test pass |
| Teaching ownership | Reviewed implementation | Frozen 255-test pass; three mutations caught/restored; 104 argument knobs had own-context lexical teaching at that snapshot, not proof of semantic usefulness |
| Error-twin retirement | Reviewed implementation; final cleanups complete | Validation entries are authoritative; repair metadata preserved; failed discoveries uncached; real blob redaction and route fallbacks covered. Consumer 2,929, integration 67, focused PostgreSQL 13, final frozen 560, refined 20 and independent refined 20 all passed |
| Universal MODEL admission | **Unfinished, not ready for review** | Last measured census resolved 42/42 tools; intermediate 212-test selection passed. Latest preparation selection had 103 passed and four failed; subsequent feedback fix is untested. Ten new mypy findings remain |
| READ, complete ADMITTED, restricted response contracts, sparse presence, structured persisted errors, remaining residue and scorecard | Not implemented/completed | Preparation is available; see resumption order below |

The 560-test error-retirement selection reported one unresolved warning in
`test_cancellation_during_sync_tool_waits_for_result_audit_persist`: asyncio's
executor did not finish joining threads within 300 seconds. The process did
eventually exit 0 with unchanged source hashes. Inspection found low direct
relevance to the changed rejection/cache branches, but did not establish its
cause or prove it pre-existing.

Error-retirement mypy remained red with 1,365 errors versus 2,369 in its exact
saved baseline, with zero new normalized per-file/message entries. This differs
from the **new MODEL typing defects** below; do not describe those as inherited.

**Current MODEL changes and known unfinished work**

The halted MODEL task has 21 changed paths against its captured pre-task bytes:
20 modified and one added. The controller verified every current file against
the agent's checkpoint hashes. This is a source checkpoint, not verification
that the implementation is correct.

Implemented so far:

- Complete owned input models, including shared empty-input admission, on the
  shipped handler paths; derived MODEL parity in the existing schema-contract
  authority. Its 42/42 result proves bounded admission provenance and root-name
  parity, not every control-flow branch or full schema equivalence.
- Strict JSON scalar and supplied-null handling, closed owned trigger records,
  positive `list_models.limit` with default 50, and nonempty `clear_source` name.
- Removal of the undeclared `sha256_override` while preserving the stored blob
  hash, and removal of nested object-string repair while preserving genuine
  outer transport decoding and legitimate string content.
- Owned public advisor arguments, separate backend checkpoint formatting, and
  public validation before exhausted-budget refusal.
- A final untested change to `protocol.py`'s closed error projection. Four real
  boundary tests showed that earlier caller-level repair text was discarded;
  the fixed JSON-type instruction now resides at that canonical authority.

Next repairs within this task:

1. Remove nine redundant casts introduced by the now-generic validation helper:
   six in `tools/transforms.py`, two in `tools/outputs.py`, one in
   `tools/sessions.py`. Remove the unreachable encoding branch in
   `tools/blobs.py`. These are new defects: the saved pre-task 18-file mypy
   selection passed; the subsequent 17-file selection failed with these ten
   findings. Repeat an identical 18-file selection including `protocol.py`.
2. Re-run the exact 107-test preparation selection and existing error-closure
   tests after the final protocol change. Verify safe canonicalization,
   idempotence and actual planner-visible feedback; no raw invalid values,
   unknown keys or exception causes may leak.
3. Finish advisor state/version/accounting and valid-exhausted-budget controls;
   typed formatter equivalence, scrubbing and size checks; remaining
   schema/behavior cases; and safe feedback examples for LLM review.
4. Run final Ruff/format/contracts checks. The intermediate contracts checker
   passed after repinning to 2,704 soft occurrences, 382 files and 64
   boundary-parsed sites; it has not checked the final protocol edit.
5. Freeze, run the broad owned selection, then perform the planned
   baseline/mutant/restored proofs. **No MODEL mutation campaign, performance
   measurements or fresh implementation reviews have run.** Inspect avoidable
   repeated validation in the upsert/splice wrappers without removing real
   public admission boundaries.

Do not restore files from HEAD or the earlier error snapshot: that would erase
other campaign work. The task-only delta uses saved pre-MODEL current bytes.

The immediate preparation test selection, to run only after resumption and the
necessary repairs, is below. Start in the primary checkout; the first command
enters the campaign worktree. Record its exit code separately from its log:

```bash
cd .claude/worktrees/composer-wires-campaign
PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src" \
LITELLM_MODE=PRODUCTION LITELLM_LOCAL_MODEL_COST_MAP=True \
  .venv/bin/python -m pytest \
  -o "pythonpath=$PWD/src $PWD/elspeth-lints/src" -n 0 \
  tests/unit/web/composer/test_tool_model_wire_parity.py \
  tests/unit/web/composer/test_coerce_stringified_json_object_args.py \
  tests/unit/web/composer/test_advisor_tool.py::test_f3a_advisor_rejects_non_list_recent_errors \
  tests/unit/web/composer/test_blob_inline_tools.py::TestWireBlobInlineRef::test_authors_marker_with_authoritative_pinned_hash \
  tests/unit/scripts/test_composer_wire_census.py \
  > /tmp/composer-wires-campaign/model-admission/resumed-focused.log 2>&1
result=$?
printf '%s\n' "$result" > /tmp/composer-wires-campaign/model-admission/resumed-focused.exit
```

**Decisions and corrections to preserve**

- Replace internally chosen open shapes with precise owned contracts. The
  restricted-discovery review found no compelling reason for arbitrary outer
  response data. Producer-owned contracts referenced by declarations are the
  agreed direction; this response repair is **not yet implemented**. Specific
  dynamic schema/configuration/data leaves retain explicit value grammars.
- JSON Schema treats `1` and `1.0` as the same integer value. Apply that
  consistently, including `list_models.limit`; reject booleans, numeric strings,
  nonintegral/nonfinite numbers and invalid ranges. The earlier all-floats
  refusal was a planning mistake, not an operator preference. This was checked
  against the installed validator and the
  [official numeric reference](https://json-schema.org/understanding-json-schema/reference/numeric).
- Malformed arguments retain the existing safe `ARG_ERROR` audit summary.
  Structurally valid but semantically incompatible mode/count arguments must
  instead retain redacted attempted values in an ordinary failed ToolResult.
  Reject that later applicability rule before source/blob preparation, with
  ordered bounded diagnostics. Historical state stays readable.
- Precise shape admission does not grant disclosure permission. Preview
  `data`/`runtime_preflight` loss in retained audit is deliberate D4 withholding;
  ordinary provider responses follow a separate path. Unknown external key names
  can be sensitive. Do not replace opaque labels with raw names or key hashes.
- Earlier guidance counts of 139 rows / 118 codes omitted generator expansion.
  They are not accepted runtime counts. Measure expanded membership/order from
  a dependency-complete frozen snapshot before freezing legacy regex guidance.
- Sparse-presence work must preserve exact display and private authority hashes.
  Its controlling design identifies **five** semantic redacted comparisons and
  the persisted binding constructor, not just three function-level sites.
  Structured persisted error records have required message/code/component
  fields; code remains `str | None`, not a proven exhaustive enum.

**Resume order after MODEL acceptance**

Finish READ extraction/behavior and complete ADMITTED parity using their existing
authorities. Implement the direct diagnostic-code catalogue and legacy-regex
freeze before adding count-applicability guidance. Then perform the shared
count-applicability rule, producer-owned restricted response contracts, combined
sparse-presence/structured-error work, and remaining response/evaluation residue.
Compose the scorecard from the actual gate results. Recheck dependencies and
current source before each bounded implementation; use one implementation owner
at a time with fresh source reviews.

Full integrated backend, frontend, PostgreSQL and lint-corpus comparisons remain
outstanding. The frozen baseline at the same HEAD was:

- Frontend: 4,581 tests in 245 files passed.
- Default backend: 50,562 passed, two timing failures, 83 skipped, two xfailed;
  exit 1. Both exact serial reruns passed, which does not turn the broad run green.
- PostgreSQL: 388 passed, one failed, one skipped; exit 1. Held-row heartbeat
  timeout classification failed again serially; tracked as `elspeth-6feb133948`.
- Ruff, mypy and contracts passed; trust-tier lint was deliberately red. Compare
  the masked finding corpus, not zero; no global signing or allowlist clearance.

Only the exact synthetic **baseline** provider command was approved and run:
33.98 seconds, five successful provider calls, zero runs and zero proposals.
It exercised ordinary `compose_loop`, not canonical rootless entry or widget
acceptance. Standard-battery and candidate egress remain unapproved. No deployment,
session reset, signing or commit is authorized by this checkpoint.

**Evidence and prepared work**

Scratch records are under `/tmp/composer-wires-campaign/`. The working tree is
the current source; these records distinguish intermediate task snapshots.

- `model-admission/checkpoint.md`, `checkpoint-task.patch`,
  `checkpoint-manifest.json`, `checkpoint-source/`, and the saved `before/`
  contain the halted task's exact delta, commands, results and source.
- `error-twins/error-twin-handoff-v3.md` is the final reviewed error task. Its
  original mutation crashes and refined behavioral proofs remain separately
  recorded. `parent-refined.log`/`.exit` record the independent 20-test pass.
- Earlier reviewed work: `effects-handoff.md`,
  `envelope/structural/structural-handoff.md`, `taught/taught-handoff.md`.
- Resume designs: `model-admission-implementation-charter.md`,
  `argument-model-ruling-amendment.md`, `read-wire-handoff.md`,
  `admitted-scorecard-handoff.md`, `diagnostic-catalogue-implementation-charter.md`,
  `count-applicability-implementation-charter.md`,
  `restricted-response-implementation-charter.md`,
  `combined-presence-errors-charter.md`, `hidden-label-disposition.md`,
  `remaining-residue-handoff.md`, `scorecard-integration-charter.md`, and
  `contract-performance-checks.md`.

Campaign epic `elspeth-54bd0b84cd` and seam tickets remain open/in progress;
no completed-seam or landed-branch claim is made.
