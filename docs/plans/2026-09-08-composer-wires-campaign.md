# Composer Wires Campaign Implementation Plan

**Custody and consolidation update — 2026-09-12.** Resume this existing campaign
on branch `composer-wires-consolidated`, in
`.claude/worktrees/composer-wires-consolidated`, with custodian
`codex-composer-wires-custodian`. The fixed release input is `release/0.8.1` at
`cb772071b23cfd94757094f4f71643744542eeab`. The predecessor checkpoint
`a1ab1b25ef3ec0397b6bb96df8f9a19723c4ec75` preserves the older worktree's five
changed paths before the newer campaign overlay. This identifies the recovery
inputs; it does not certify campaign acceptance or a merge into the release.

**Execution authorized — 2026-09-12.** Following consolidation at
`e5060442893b64a2718e186c3987ce25b17cc74d`, the operator requested completion of
the full campaign with regular checkpoints: MODEL; READ and ADMITTED;
diagnostics and count applicability; discovery responses; sparse presence and
persisted errors; remaining response/evaluation, scorecard and integrated/live
acceptance. Implementation and checkpoint commits are authorized, superseding
the September 8 commit ban. Keep the September 10
[pause checkpoint](2026-09-10-composer-wires-campaign-checkpoint.md) as historical
evidence. Its interrupted implementation, prior reviews and exact test results
remain useful; its paused-work, original checkout and commit instructions no
longer control execution. Historical no-model alternatives and pending
contract decisions below have been replaced by the accepted rulings in this plan.

## Outcome and scope

Every planner argument must be accounted for across six wires: advertised schema
(SHIPPED), actual owned argument admission (MODEL), consumption (READ), redaction
admission (ADMITTED), planner teaching (TAUGHT), and operator projection
(FRONTEND). Close internally chosen response shapes and the response/evaluation
residue already included in the campaign. Finish with derived per-tool accounting,
integrated gates and the planned live acceptance evidence.

The historical 42 tools and 104 knobs are dated measurements, not fixed totals.
Derive the universe from `get_tool_definitions()`. Extend the existing
`composer_wire_census.py`, `tools/schema_contract.py` and owning tests;
do not add a parallel registry or reporting framework.

The scope authority remains this plan and epic `elspeth-54bd0b84cd`. Use
[the existing explore-and-pin methodology](../agents/explore-and-pin-methodology.md)
for bounded census, verdict, structural repair, behavioral probes and review.
During implementation, use the repository's existing execution skills,
read CONTRIBUTING's whole-tree gates before code edits, and retain one
implementation owner across overlapping Composer/session files. Independent
read-only preparation and reviews can run in parallel.

## Accepted decisions to preserve

- Every shipped tool, including empty-input tools and advisor interception,
  validates complete original public input into an owned model. A manifest model
  is redaction evidence, not proof of handler admission. Missing models,
  unresolved provenance, selected-key reconstruction and duplicate/missing
  catalogue entries must remain visible failures. Do not import the older
  provisional `model_wire_fence.json` as an approved exception.
- Close internally chosen roots and fixed nested records. Preserve only
  evidence-backed dynamic leaves with explicit value grammars. Malformed
  provider output does not require hidden object-string coercion; genuine outer
  transport JSON decoding and legitimate string content remain supported.
- Follow JSON Schema numeric semantics: finite integral numbers such as `1.0`
  are integers; reject booleans, numeric strings, nonintegral/nonfinite numbers
  and invalid ranges. `list_models.limit` has minimum 1 and omitted default 50,
  with no new upper bound. Preserve omission versus supplied null as advertised.
- Structurally malformed arguments use the safe `ARG_ERROR` evidence summary.
  Structurally valid but inapplicable mode/count pairs use an ordinary failed
  ToolResult, preserving permitted redacted attempted values. Reject that
  applicability defect before source/blob preparation; historical state stays
  readable and repairable.
- Seam 4.4 option (a) is approved: after validation and redaction, retain
  recursively supplied fields, including explicit nonsensitive null, and omit
  absent fields. Replacement cards always disclose that omitted settings may
  reset, even when supplied-argument comparisons are empty. Do not ask for this
  choice again or simulate effective defaults in the frontend.
- Preserve exact published display and private invocation authority. The existing
  inline-blob null equivalence belongs only to semantic redacted dispatch
  comparison, including its persisted constructor and all five service sites.
  Sparse presence and structured persisted errors share one session epoch cutover;
  re-read both current epoch pins rather than hard-coding the historical 53→54.
- Response contracts do not grant disclosure permission. Preserve D4 preview
  audit withholding, restricted context projection, preview refusal and schema
  budget checks. Unknown external key names remain opaque; no raw-name or
  key-hash substitute for positional labels is approved.
- New diagnostic guidance uses direct machine-code records. Freeze expanded
  legacy regex membership and surviving order while retaining required prose
  compatibility. Earlier 139-row/118-code figures were invalid AST counts;
  neither those nor the later mixed-source experiment define the freeze.
- Prioritize correctness and useful teaching before performance optimization.
  No provider bypass, tutorial-special path, new broad fence, global signature
  clearance or weakened admission/disclosure is authorized by this plan.
- **Source review ruling — 2026-09-13:** retire the specialized rootless inline
  interpretation widget and its acceptance requirement. Use the existing generic
  source proposal review, preserving the provider-authored proposal's ID through
  browser approval, persistence and replay. Proposal approval does not replace
  the required execution content review for LLM-authored source data. This ruling
  supersedes widget acceptance in earlier campaign references.

## State at the interrupted checkpoint

These are **September 10 historical results**, not fresh verification of the
consolidated tree. Subsequent MODEL changes overlapped earlier reviewed files.

| Component | Historical state | What remains |
|---|---|---|
| Initial MODEL census | Reviewed bounded census; unfinished admission later extended it | Finish MODEL behavior, typing, mutation proofs and fresh review |
| Frontend projection registry | Reviewed; source-derived dispatch guard rejects joint fixture/export omission | Integrated frontend/Python fixture regression |
| Shared option decoder | Implemented | Sparse presence plus generic rootless source proposal review acceptance |
| Prospective approval effects | Reviewed declaration-derived implementation and card wording | Integrated behavior and text regression |
| Response-envelope measurement | Reviewed structural repair | Preserve derived coverage through remaining response work |
| Teaching ownership | Reviewed own-context lexical gate | Semantic teaching review and final integration |
| Error-twin retirement | Reviewed; validation entries authoritative, independent repair data retained, failed discoveries uncached | Regression after MODEL and later contracts; unresolved shutdown warning remains evidence |
| Universal MODEL admission | Unfinished; latest protocol repair had not been retested | Milestone 1 |
| READ, complete ADMITTED, restricted responses, sparse presence, structured errors, remaining residue, scorecard/live trial | Unfinished | Milestones 2–8 |

The controller's September 12 consolidation checks reproduce the immediate
feedback gap: `exit=1; 103 passed; 4 failed`, with the four
`test_structural_feedback_teaches_actual_json_types` cases expecting
`caught.value.expected` to include `actual JSON objects and arrays` but receiving
`a valid value`. The final historical protocol change therefore did not clear
this selection. The fresh 18-file typing selection found 13 findings: the ten
known cast/unreachable findings plus three AST-narrowing findings in the census
script. Subsequent bounded five-file housekeeping removed redundant casts,
duplicate model-enforced encoding validation and AST narrowing defects. Its
focused evidence is `exit=0; 97 passed`, followed by the identical parent mypy
rerun: `exit=0; Success: no issues found in 18 source files`. This is checkpoint
typing hygiene, not semantic MODEL acceptance.

The controller also recorded `exit=1; 1 failed; 6 passed` for mock discipline
plus session attribute contracts. The failure is
`test_no_unspecced_direct_mock_constructors`, identifying bare `MagicMock()` in
`tests/unit/web/composer/test_tool_model_wire_parity.py:43`; the six session
contract tests passed. Fix the mock to its real collaborator during MODEL work.
No integrated campaign acceptance follows from these focused checks.

Focused frontend ProposalDiff/ToolCallCard/projected-tool tests then recorded
`exit=0; 75 passed`. The first launch's two subprocess `EPERM` failures were
sandbox limitations; the successful repeat ran outside that sandbox.

Normal checkpoint hooks also exposed four exception-channel violations. The
rejection-feeder and prospective-effect authorities operate on owned, already
admitted values; their invariant failures now raise `AssertionError` rather than
the planner-argument `ValueError` channel. Six expected-exception cases failed
against the previous code, then the focused proposal/rejection suites passed:
`exit=0; 84 passed`. Composer incremental checks, Ruff and scoped mypy passed.
Two synthetic user-home paths in redaction tests were replaced with neutral
server paths for commit hygiene; that file's tests passed `exit=0; 88 passed`.

An expanded proposal/rejection/declaration selection reported
`exit=1; 163 passed; 14 failed`. All fourteen failures are in
`tests/unit/web/composer/test_tool_declarations.py`, comparing declaration
definitions/descriptions. They cover create/update/delete blob, list_models,
preview_pipeline, clear_source, set_metadata, list_blobs, get_blob_metadata,
get_blob_content, inspect_source, list_secret_refs, validate_secret_ref and the
definition-map round trip. Their baseline attribution has not been measured.
Reconcile these expectations against the accepted schemas and teaching during
MODEL acceptance; do not blindly refresh expected output to turn the test green.

The historical baseline at `1278c5c21` had frontend 4,581 passing tests; backend
50,562 passes and two timing failures; PostgreSQL 388 passes and one reproducible
heartbeat timeout-classification failure (`elspeth-6feb133948`). Exact backend
serial reruns passed but did not make that broad run green. The error-retirement
selection also recorded an unresolved executor-shutdown warning. Re-measure the
fixed release input and consolidated candidate where needed; none of these
historical exceptions is a permanent waiver on the moving release.

## Remaining execution order

**Execution checkpoint — 2026-09-12.** Commit
`73f6ea15de460065845fdb6f18f5e6b1cac6efb3` repairs safe JSON guidance readback,
incremental trigger bounds, published review string bounds, declaration
expectations and the unspecced mock. Fresh-worktree verification ran the same
focused selection successfully (308 tests); five individual repair reversions
failed at the intended assertions, with independent review. MODEL acceptance
remains open. The broader Composer/MCP run exited 1 with **26 failed, 9,190
passed** and a 300-second executor-shutdown warning. Its follow-up repairs cover
annotated optional-field traversal, invalid property generators, stale tool and
edge expectations, and an unused facade export. Do not treat this checkpoint as
broader acceptance or release integration.

The repaired, frozen snapshot subsequently reports **9,221 passed, 1 warning**
in the same broad selection (exit 0, 141.18 seconds). The remaining warning is
Hypothesis ignoring an unsupported Strict annotation; explicit MODEL scalar
controls remain in the selection. The shutdown test passes unrestricted in
0.87 seconds and reproduces its teardown stall only in the sandbox, so no
synchronization workaround was added. The same 18-file mypy selection and
contract checks pass. Eight additional representative charter mutations have
green/mutant/restored evidence at `73f6ea15d`; the shared optional-field walker
has a separate discriminating regression. LLM and systems reviews found no
blocker. Advisor rejection remains safe but less field-specific; consider owned
field-specific diagnostics during the existing guidance milestone.

Key-free lint comparison against the fixed release input exposed two new raw
mapping fallbacks in pipeline proposal counts and effect classification. Both
now admit complete input through `SetPipelineArgumentsModel` and consume typed
fields; malformed selectors raise the safe argument error. The two remaining
new R5 sites are fail-closed nominal checks on owned effect/model types, retained
under the trust-domain rule. No signatures or allowlist metadata changed.
The follow-up broad run reported 9,247 passed and two incomplete test fixtures;
their storage-purity and exact registry assertions are preserved with valid
complete fixtures (407 affected tests pass). Final committed-tree verification
remains the checkpoint gate; global signing and release integration remain open.

Each milestone ends with a frozen, named source state, proportionate tests,
reconciled baseline/mutant/restored evidence and fresh review. Historical reviewed
work is retained and regression-tested rather than automatically rewritten.

### 1. Finish and accept universal MODEL admission

Use the [MODEL implementation charter](composer-wires/model-admission-implementation-charter.md)
and [ruling amendment](composer-wires/argument-model-ruling-amendment.md).
The [original field/boundary table](composer-wires/argument-model-handoff.md) and
[whole-model strictness fixtures](composer-wires/existing-model-strictness.md)
are supporting historical evidence; their pending choices are superseded.

- [x] Complete checkpoint typing housekeeping: remove the redundant validator
  casts and duplicate model-enforced encoding checks, and narrow the census AST
  values. The identical 18-file mypy rerun passed; actual MODEL acceptance remains
  open in the steps below. Preserve every real admission boundary.
- [x] Run the preparation selection within the broader owned Composer suite after
  the final protocol feedback repair. The earlier September 12 rerun had four
  feedback failures; diagnose the
  actual exception-to-planner authority rather than assume wording placement
  fixed it. Test actual planner-visible canonicalization, idempotence, bounds and
  secret/unknown-key/cause withholding, not just caller-level text.
- [x] Finish advisor state/version/budget/accounting controls, valid exhausted
  budget behavior, typed formatter equivalence/scrubbing/size checks and remaining
  schema/nullability/requiredness cases. Complete original input must reach owned
  public admission before budget or provider effects.
- [x] Repeat a consistent typing scope including `protocol.py`, Ruff/format and
  contracts. Compare the identical target list with an explicitly named baseline;
  the historical before/after 18-versus-17-file mismatch cannot prove parity.
- [x] Replace the known unspecced mock with a spec for its real collaborator and
  rerun the mock-discipline gate alongside affected MODEL tests. The checkpoint's
  three-test gate passes; restoring the old parity test exposes the constructor.
- [x] Resolve the fourteen measured declaration-definition expectations in
  `tests/unit/web/composer/test_tool_declarations.py`, checking behavior and
  teaching against the approved contract. The identical declaration selection
  reports 93 passed at the checkpoint and 14 failed / 79 passed with its
  expectations restored from the named consolidation base `e50604428`.
- [x] Freeze and run the broad owned Composer selection, representative charter
  mutations, and fresh spec/quality, LLM-feedback and systems reviews. Evaluate
  avoidable repeated validation only after correct public paths are proved.

### 2. Finish READ and complete ADMITTED parity

Use the [READ handoff](composer-wires/read-wire-handoff.md) and
[ADMITTED handoff](composer-wires/admitted-scorecard-handoff.md), corrected by the
accepted closure ruling above. For READ, the later
[argument-model ruling amendment](composer-wires/argument-model-ruling-amendment.md#practical-read-after-typed-models)
explicitly supersedes the handoff's semantic-use analyzer: measure original-input
field extraction, and prove operational consumption separately with behavioral
tests. This corrects the stronger obsolete requirements copied into the initial
resumption plan; it does not lower the accepted evidence standard.

**Implementation checkpoint — 2026-09-12.** The live CLI measures 42 tools with
no missing, extra or unresolved READ fields. ADMITTED measures 30 closed
declarative policies and 12 type-driven models, covering the same 42 tools and
104 advertised knobs. These are extraction and root-name relations; separate
behavior tests check selected results, persisted state and mocked advisor requests.
The shared blob preparation helper now receives explicit typed fields, removing
its bulk-mapping conversion without a census exemption. Unsupported wrappers,
deferred bodies, overridden validation and captured-callable aliases fail visibly.

A frozen combined Composer/MCP and touched blob-integration run recorded
`exit=0; 9365 passed; 1 warning`. Subsequent census-only typing and deferred
MODEL-admission refinements recorded `exit=0; 177 passed`, with five-file mypy
and Ruff passing. Independent runtime controls reproduced the original false
attribution cases and then passed against the repaired, unchanged source.
ADMITTED's related selection recorded `exit=0; 468 passed; 1 warning`; its actual
closure-policy mutation failed and the restored selection passed. The warning
is the existing Hypothesis Strict-metadata limitation, not a new fixture warning.
Fresh-worktree checkpoint proof remains recorded separately under `.verify/`;
full repository, PostgreSQL, moving-release and live acceptance remain later work.

- [x] Reuse the actual callable catalogue and source provenance utilities for
  READ. Bind instances to complete original-input admission, and collect direct
  declared field accesses and literal raw reads along supported owned helpers.
  Preserve extraction sites alongside unresolved provenance. Do not label these
  static extractions as operational consumption.
- [x] Add the derived READ relation and provenance controls: foreign/independent
  model instances, unused nested functions, ambiguous aliases/copies and whole
  model dumps cannot manufacture coverage. Unsupported paths remain explicit
  findings; a bulk dump never implies every field is extracted.
- [x] Add meaningful behavior probes showing representative field changes alter
  intended state, selected results, or the request at a mocked boundary. Cover
  optional branches and defaults. Dead-read/logging-only positives are not
  consumption evidence. No general semantic-use graph or shadow field reads.
- [x] Extend `test_tool_argument_wire_parity.py` to include closed-empty policies,
  type-driven models and actual accepted/emitted wire names. Check exact universe
  equality before per-field comparisons; do not intersect away missing endpoints.
- [x] Tighten internally chosen open policies under the existing ruling and
  prove existing sensitive-value handling. ADMITTED and MODEL remain different
  authorities; name preservation does not authorize value disclosure.
- [x] Run representative missing-endpoint, empty-allowlist, input-alias,
  serialization-alias, removed-use and advisor-interception mutations.

### 3. Introduce direct diagnostic guidance and freeze legacy regex growth

Use the [diagnostic catalogue charter](composer-wires/diagnostic-catalogue-implementation-charter.md).

- [x] Capture the dependency-complete expanded legacy records, code order and
  first-match relationships from the accepted post-MODEL snapshot. Refuse
  unresolved generator expansion; record ordered occurrences, not a set/count.
- [x] Add direct immutable code-guidance records and derive exact lookup,
  fuzzy/help vocabulary and producer coverage without a second code inventory.
- [x] Preserve required legacy prose behavior, Expected hints and terminal
  guidance. Reject new/replaced/reordered regex rows, duplicate direct codes and
  accidental authority collisions. Do not delete fallback before a separate
  complete uncoded-producer census justifies it.
- [x] Point teaching/terminal consumers at the unified record authority and run
  behavioral baseline/mutant/restored lookup and ordering checks.

Implemented against accepted `9ab705381`: the dependency-complete capture contains
144 ordered regex occurrences and 123 seed codes. Exact lookup now uses immutable
records; new quarantine and count guidance reaches planner feedback from actual
producers. Review found and retained existing nonseed guided, diff and coalesce
codes with their original first-match guidance. Public prose and Expected hints
remain supported. Literal-producer compatibility was compared with the accepted
snapshot; dynamic forwarding remains outside that bounded comparison, so this
does not establish that uncoded producers are absent.

### 4. Enforce aggregation count applicability across authoring/runtime surfaces

Use the [count charter](composer-wires/count-applicability-implementation-charter.md)
and [audit-ordering constraints](composer-wires/expected-output-count-audit-ordering.md).

- [x] Add the smallest pure authority to the existing `OutputMode`, rejecting
  passthrough plus non-null count. Preserve omitted/null Composer transform
  defaults and runtime's existing null-mode restriction.
- [x] Apply it after structural admission in upsert and full set_pipeline,
  before source/blob resolution/preparation. Collect bounded diagnostics in input
  order, preserve state/version and permitted redacted attempted values.
- [x] Apply the same rule in runtime settings, state validation, YAML import and
  public export/lowering; keep historical hydration readable. Register new
  guidance through milestone 3 without growing legacy regexes.
- [x] Test actual audit serialization, pre-custody call boundaries, import/export
  round trips and unchanged transform/passthrough executor behavior.

The shared rule and adapters preserve historical hydration and authored optional
values. Actual handler plus audit-wrapper tests distinguish structural ARG_ERROR
from semantic unsuccessful results recorded as successfully dispatched; permitted
mode/count arguments survive the real audit serializer. This is not full-service
persistence acceptance. Runtime rejection parity adds one generated mirrored site;
the soft-mapping census is unchanged. Both authoring schemas teach applicability.

Combined Composer/MCP, census, runtime parity, enum/configuration, aggregation and
blob-provenance selection: `9535 passed, 1 warning`, exit 0. Eight-file mypy passes.
The first run exposed two stale test expectations (the grown enum stand-in and
generic hatch classification), repaired with their original assertions preserved
or strengthened. Whole-repository, PostgreSQL, release integration and live
acceptance remain in the final campaign checkpoint below.

### 5. Close producer-owned restricted discovery responses

Use the [response implementation charter](composer-wires/restricted-response-implementation-charter.md).

**Implementation checkpoint — 2026-09-13.** All 19 live discovery declarations
now select producer-owned response contracts. Actual planner and tool-batch
dispatch admit before successful audit, re-admit cached owned values, and
serialize the admitted value. Restricted current-state context is detached
before provider work; schema/state projection, refusal, and budget decisions
remain independent. Reviews exercised real producer bytes, malformed data,
cached root and nested corruption, and post-await context mutation.

The frozen Composer/MCP regression selection exited 0 with 9,919 passed and
one warning; all 5,442 captured inputs were unchanged. Six alternating local
performance runs preserved 37 fixture wires and semantic controls. The largest
measured initial-admission increase was the 100-plugin fixture: local egress
350 to 1,171 microseconds, excluding handler, database and provider work.
A subsequent narrow refinement removed duplicate cached-default traversal;
33 focused tests and the same 37 parity controls passed. Fresh committed
verification must cover that final refinement before this milestone is
accepted. Whole-repository, PostgreSQL, release-integration and live-provider
acceptance remain outstanding campaign gates.

- [ ] Reconcile the historical producer table against the live discovery registry.
  Select contracts from declarations, owned beside producers; admit real
  successful variants and permitted absence/failure families only.
- [ ] Close fixed roots and nested records while preserving legitimate dynamic
  leaf grammar, nominal ownership, list roots, insertion order and omission.
  No arbitrary object/Any/BaseModel/JSON root fallback or giant trial union.
- [ ] Preserve independent error authority and exact disclosure/projection
  precedence, cache eligibility and current state/schema budget on reuse. Build
  the final restricted envelope once without later ambient mapping mutation.
- [ ] Exercise real producer variants, corruption, current-surface cache reuse,
  restricted/full-state selectors, preview refusal and schema budget boundaries.
  Prove actual wrong-field/wrong-type mutations fail for intended reasons.
- [ ] After correctness freezes, use the [local performance checks](composer-wires/contract-performance-checks.md)
  for adapter construction/traversal costs. No performance target weakens admission.

### 6. Implement sparse presence and structured persisted errors together

Use the [combined charter](composer-wires/combined-presence-errors-charter.md),
with the [detailed presence fixtures](composer-wires/frontend-presence-handoff.md).

- [ ] Re-read both session schema epoch pins at the incorporated release state;
  advance them together for these two persisted changes. Test fresh and stale
  temporary SQLite/PostgreSQL stores. No actual session store reset, backfill,
  legacy reader or historical rewriting is part of implementation.
- [ ] Preserve sparse argument display through sensitive summarization,
  publication, row/event restoration and frontend comparison. Responses retain
  existing default-inclusive redaction. Replacement caveats are unconditional.
- [ ] Share the narrowly scoped semantic-redacted hash helper between the
  persisted dispatch-binding constructor and all five service comparisons.
  Exact display, draft, audit-payload and private invocation hashes remain exact.
- [ ] Carry frozen required-key `{message, error_code, component}` records through
  composition-state persistence, strict decoder/HTTP projection, guided replay,
  frontend and RGR scoring. Code remains `str | None`; preserve null/empty
  semantics, message-only pending digest authority and guided disclosure.
- [ ] Prove explicit inline_blob:null and omission through first durable guided
  dispatch, settlement, rejection, retry and recovery, with non-normalized-field
  tampering controls. Run frontend fixture/decoder/humanizer/component checks
  and PostgreSQL persistence/epoch proofs.

### 7. Close the remaining response and evaluation residue

Use the [residue handoff](composer-wires/remaining-residue-handoff.md) and
[hidden-label disposition](composer-wires/hidden-label-disposition.md).

- [ ] `elspeth-5e81b50f2e`: remove the redundant destination data.note; retain the
  existing `quarantine_unknown_output` error once, with direct guidance and
  independent server-owned metadata notes preserved.
- [ ] `elspeth-72ce6749ac`: keep legitimate dynamic keys private; document and
  test mapping-local input-order labels through real redaction/audit persistence.
  Approved fixed vocabulary may retain names; unknown external names/hashes may not.
- [ ] `elspeth-c00e6d9795`: finish immutable restricted construction in milestone 5.
- [ ] `elspeth-6aa477c78e`: make battery approval observability explicit; redacted
  status is not evidence approval was absent or a mutation definitely applied.
  If a minimal status carrier is needed, identify its actual producer/capture
  seam and prove current disclosure policy permits it. Full canonical payload
  capture would require a separate disclosure decision.
- [ ] `elspeth-9e76d9436b`: consume actual structured HTTP state from milestone 6
  with producer-backed fixtures; preserve correct mocked-harness behavior.
- [ ] Reconcile already reviewed error-twin, envelope-census and teaching repairs
  with their owning tests. Do not redo historical fixes or silently drop their
  independent repair/failure/cache contracts.

### 8. Derive the scorecard and complete integration/live acceptance

Use the [scorecard charter](composer-wires/scorecard-integration-charter.md).

- [ ] Extend `composer_wire_census.py --scorecard` using each gate's actual census
  authority. Join every wire onto the live registered universe after exact-set
  and duplicate checks. Missing/unresolved cells fail honestly, not as N/A.
- [ ] Distinguish root/schema relation, static attribution, value redaction,
  lexical teaching, generated frontend membership and executed behavioral proof.
  The Python fixture consumer remains independent of Node; Vitest proves the
  runtime dispatch relation. A table is not a test execution receipt.
- [ ] At the frozen integrated candidate, run backend default and serial
  PostgreSQL suites, full frontend tests/typecheck/lint, Ruff/mypy/contracts and
  key-free lint-corpus comparison. Account for each new failure or binding delta.
- [ ] Prepare the existing [standard battery](../../evals/composer-standard-battery/battery.md)
  plus one scenario per repaired seam, including silently dropped knobs and
  explicit-approval cards. Reuse the existing Composer harnesses. Prepare exact
  baseline/candidate commands, model/settings and bounded inputs before seeking
  any still-required provider-egress approval.
- [ ] After authorized trials, record repair turns, calls per transition,
  unknown-key placeholders and approval-card rows. Rootless source acceptance
  requires per-transition provider-call scrutiny; the old single baseline
  transition did not exercise that path.
- [ ] In a rootless session, render the real provider-authored source proposal
  through the existing generic review surface. Reload the session, approve that
  same persisted proposal ID in the browser, and verify settlement and replay
  against that ID and its original invocation authority. Do not synthesize a
  replacement proposal or require the retired interpretation widget to appear.
- [ ] Retain the execution content review for LLM-authored source data after
  source proposal approval. Verify that proposal approval alone does not satisfy
  that review and that execution follows its existing acknowledgement path.
- [ ] Close campaign scope only when current gate results and live evidence
  account for every tool and each residue item. Reverify tracker/HEAD status
  before reporting completion; no inferred fence or incomplete scorer counts.

## Commands and evidence discipline

These are future execution commands. Start in the primary checkout for worktree
selection; use its explicit resolved path thereafter. Use existing `.venv`
dependencies; do not rebuild or install into the shared environment.

```bash
campaign_root="$(git rev-parse --show-toplevel)/.claude/worktrees/composer-wires-consolidated"
cd "$campaign_root" && export PYTHONPATH="$campaign_root/src:$campaign_root/elspeth-lints/src"
export LITELLM_MODE=PRODUCTION
export LITELLM_LOCAL_MODEL_COST_MAP=True
campaign_logs="$(mktemp -d /tmp/composer-wires-resume.XXXXXX)"
cd "$campaign_root" && .venv/bin/python - <<'PY'
from pathlib import Path
import elspeth
import elspeth_lints
root = Path.cwd()
for module, relative in ((elspeth, 'src'), (elspeth_lints, 'elspeth-lints/src')):
    origin = Path(module.__file__).resolve()
    print(f'{module.__name__}: {origin}')
    assert origin.is_relative_to(root / relative)
PY
```

Stop if provenance fails. Run the preserved immediate MODEL selection, capturing
its exit separately. Read the complete log before making a result claim.

```bash
cd "$campaign_root" && .venv/bin/python -m pytest \
  -o "pythonpath=$campaign_root/src $campaign_root/elspeth-lints/src" -n 0 \
  tests/unit/web/composer/test_tool_model_wire_parity.py \
  tests/unit/web/composer/test_coerce_stringified_json_object_args.py \
  tests/unit/web/composer/test_advisor_tool.py::test_f3a_advisor_rejects_non_list_recent_errors \
  tests/unit/web/composer/test_blob_inline_tools.py::TestWireBlobInlineRef::test_authors_marker_with_authoritative_pinned_hash \
  tests/unit/scripts/test_composer_wire_census.py \
  > "$campaign_logs/model-preparation.log" 2>&1
result=$?
printf '%s\n' "$result" > "$campaign_logs/model-preparation.exit"
cat "$campaign_logs/model-preparation.exit"
```

Add the actual owning protocol/error-closure tests after reading their current
names. For final broad gates use the canonical wrapper; its detached launcher
exit is not the suite result. Poll its printed `.done` path, then read
`summary.txt` and the corresponding logs. A `frozen=NO` run is not evidence.

```bash
cd "$campaign_root" && scripts/full-suite-gate.sh --execute --detach \
  --root "$campaign_root" --log-dir "$campaign_logs/integrated" \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Frontend commands use existing package scripts and record separate exits.

```bash
for campaign_stage in typecheck lint test; do
  cd "$campaign_root/src/elspeth/web/frontend" && npm run "$campaign_stage" \
    > "$campaign_logs/frontend-$campaign_stage.log" 2>&1
  result=$?
  printf '%s\n' "$result" > "$campaign_logs/frontend-$campaign_stage.exit"
done
```

Use `-n 0` for single tests and PostgreSQL; respect sibling suite capacity.
Each gate mutation records identical baseline/mutant/restored test identities,
applied change, intended behavioral failure, explicit exits and restored source.
Collection/import crashes are not semantic kills. Control new instruments with
known-positive/negative examples and mutate the thing a gate claims to protect.

Compare failures against the same named comparison revision with identical test
selection. Serial passes for timing-sensitive failures remain separate evidence.
Compare key-free trust-tier findings with positions masked and binding changes
explained; do not compare to zero, hand-edit signatures, hold signing keys or
launch global restaging. Preserve necessary contract census changes honestly.

## Release integration cadence and completion boundary

### Assigned repair: uploaded-source planner participation

John assigned the upload-source planner bypass found during ACA/VANguard review
to this campaign on 2026-09-12. It is an additional completion requirement.
The route's `_prepare_step_1_uploaded_source_bind` selects a plugin and answers
the source schema form from inspection facts; the upload branch bypasses
`provider_runner`. Keeping committed sources empty until confirmation does not
remove those server-authored decisions.

- [ ] Pass structured upload inspection facts and custody references through the
  real planner and its tools; apply only the planner-authored source proposal.
- [ ] Preserve uploaded content custody, validation, inspection review and user
  confirmation, atomic settlement, replay, and failure behavior. Add no
  server-authored fallback or tutorial-specific path.
- [ ] Test provider participation for each authoring transition, including a
  conversation that has already called the provider. Verify unsuccessful
  planner outcomes leave the upload available and do not author pending intent.
- [ ] Reconcile the release session's guided-plan lease cleanup, guided-chat
  integrity propagation, and admission refusal changes before integration.
  The release session retains ACA/VANguard custody; this campaign owns the
  planner bypass repair in its separate worktree.

The originating review measured ACA `55cbf9cc8` against release `e0ee148c2`.
Those are historical inputs; refresh the landing revision and route/test diff
before applying the integration. Independent adversarial review must examine
the actual authoring decision boundary, not just a conversation-wide call count.

ACA/multi-replica enhancements continue on `release/0.8.1`. Work against the fixed
incorporated revision while completing a bounded milestone; avoid rebasing under
an active writer or frozen test run. At milestone checkpoints and before final
acceptance, measure upstream commits and exact changed paths, incorporate the
chosen release revision deliberately, and rerun affected proofs.

During consolidation the local release advanced to
`d1b473c8f846f9ed7dc1da1d6c2dd6641abfed8a`. Git measured six newly reachable
commits and 40 changed paths since the fixed input, with no direct overlap with
the captured campaign paths plus `tools/outputs.py` housekeeping. Those release
changes are not incorporated here. This is a dated path comparison, not proof
that dependencies cannot interact; refresh it before the next integration.

Deployment-only changes may be separate, but session service, schema epoch,
durable dispatch/recovery, guided replay and PostgreSQL contention are real
possible overlaps. Re-read their current contracts after integration, especially
before milestone 6. Preserve unrelated release work and do not switch the shared
checkout or clean older worktrees merely because committed ancestry is merged.

The earlier provider approval covered one exact synthetic baseline command.
When execution resumes, prepare reversible implementation/tests and concrete
live commands or operational cutover before any final approval actually needed.

Campaign exclusions remain the previously separated design work: deep_thaw
`elspeth-10f818998e`, byte ledger `elspeth-6089bfa8fa`, TS mirrors
`elspeth-91133e850b` / `elspeth-e25f8f7530`, operator tier-model worklist
`elspeth-1eca86caa9`, `elspeth-8b0b6e5bb9`, `elspeth-f4c71c3e8e`,
`elspeth-919cd29876`, and Wave-1 residues `elspeth-4ddaee2202`,
`elspeth-c8f8318203`, `elspeth-caae752e11`, `elspeth-6bcc0e7ee9`.
New evidence that one is a necessary dependency must be explained explicitly;
filing follow-ups is not a substitute for the campaign's actual deliverable.
