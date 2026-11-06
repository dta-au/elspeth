# Gate 1 six-message findings — 20 October 2026

Live Composer session: [Case Study CSV Summary Pipeline Setup](https://elspeth.foundryside.dev/#/d7fd25a7-efe6-46b3-86bb-728cb4312112).
Session ID: `d7fd25a7-efe6-46b3-86bb-728cb4312112`. Investigation requires
authenticated access. The findings below refer to this session; message sequence
numbers identify its database transcript rows.

Status: the original C1–C6 authoring run is complete. All six public operations
completed and matched the reviewed prompts. The saved graph is connected, but
the semantic Gate 1 contract has not passed. A separately reviewed seventh
cleanup message has also completed and saved corrections; its final Advisor
review was refused by the provider's context-window limit. The original
six-message baseline is retained separately.
No pipeline execution or review approval is part of this run. John owns the test
suite.

The deployment was rebuilt and restarted from `codex/composer-fixes` at
`a1ee28202f1df8eb17094c9a76b89d10124204cc`. C1 and C2 passed the independent
authoring checks. C3 saved a connected arithmetic stage and disclosed the
following limitation. C6's final saved state is
`8bf42ecb-03ce-4570-9cf0-4a8a442dc340`, version `54`; its final reply is message
sequence `488`. Independent review measured all sixteen FINAL configuration
checks true, including the direct writer-to-fork connection, thirty Summary
fields, eighteen ordered Detail questions and ten Detail headers. These
observations do not establish execution readiness.

The seventh request is operation `72f92760-359a-445b-ac7b-a00e55a6ce93`; it
completed in `296` seconds. Its final saved state is
`16d27437-eb3c-4038-b601-700f5d27ca3b`, version `67`. The detailed Composer
response was withheld at transcript sequence `563`; the visible response at
sequence `566` reports saved checkpoints and an uncleared Advisor review.

F1–F6 below describe the original six-message baseline. The later disposition
table records what the seventh request changed; original findings are retained
to distinguish that baseline from the cleanup result. Independent final custody
checks found exactly seven authoring operations, zero pipeline runs and zero
human review attestations for this session.

## F1 — Upstream weighted floating-point arithmetic can break the inclusive boundary

The authored weighted calculation uses binary floats before
`decimal_within(calculated, recorded, 0.05)`. That helper compares the decimal
spellings of its numeric inputs; it cannot reconstruct an exact result from an
already rounded weighted sum.

A synthetic diagnostic, using no applicant data or pipeline execution, confirmed:

- Band values `1, 1, 1, 1, 3` produce `1.2999999999999998` under the authored
  `0.05/0.35/0.30/0.15/0.15` expression; the exact weighted result is `1.3`.
- Against a recorded score of `1.35`, the exact weighted result is within the
  inclusive `0.05` boundary, but the current composed calculation returns false.
- The helper itself correctly accepts the exact `1.0`/`1.05` boundary and rejects
  `1.0`/`1.0501`. The limitation is the composed arithmetic, not the comparison's
  inclusive operator.

The Composer disclosed this risk and added neither an epsilon nor an input
restriction. Exact numeric fidelity remains unresolved; an approved graph must
not be represented as satisfying that rule without resolving it.

Code evidence: `src/elspeth/core/expression_parser.py:137–155` and the published
plugin guidance at `src/elspeth/plugins/transforms/value_transform.py:697–698`.
Private measured evidence: `.claude/lanes/gate1-live-six-20261020/numeric-boundary-diagnostic.json`.

## F2 — Final readiness wording contradicts disclosed limits

The C3 response correctly discloses the numeric limitation and unverified
failure-case identity coverage, then states:

> Execution readiness is blocked only by pending reviews.

That sentence overstates readiness. The end Advisor nevertheless returned
`clean`/`ADMIT`. Ordinary prompt and prompt-injection review cards are expected;
approving them cannot settle the separately disclosed numeric limitation or
unverified requirements.

Private evidence: C3 final assistant sequence `171`, Advisor sequence `170`, and
`.claude/lanes/gate1-live-six-20261020/reviewer/C3-review.json`.

This contradiction recurs in C6's final reply, sequence `488`: it discloses
required gaps and initially refuses readiness, then says the only execution
blocker is the twenty-two pending cards. The final Advisor returns `clean`/`ADMIT`
at sequence `487`. F1, F3 and F4 remain in the final six-message baseline.

## Verification item — Failure-case identity coverage

C3 says it has not checked whether the failure report distinguishes two cases
sharing an application ID. Preserve this as an open verification item, not a
confirmed missing capability. The retained failure envelope and disposition
reason support have been inspected; no private case has been executed.

Whole-report duplicate handling and excluded-case counting are assigned to later
stages. Their deferred status at C3 is not, by itself, a finding.

## F3 — C4 misapplies the unavailable-rationale exception

The final C4 draft retains two inconsistent implementations of the adopted rule:

- All six C1 assessment system prompts require every rationale to differ from
  every other rationale without preserving the exception for accurate repeated
  unavailable-answer rationales.
- The `circumstance_assessment` and `result_assessment` checks named
  `c1_rationales_distinct_in_section` skip the entire three-question uniqueness
  check when any one verdict is unavailable. That also permits the remaining two
  assessed questions to share a rationale. The exception belongs to unavailable
  answers, not to every other answer in the same section.

These are confirmed final-draft prompt/check findings, separate from the
whole-report checks assigned to later stages. Recheck their status after C5/C6;
no correction had been applied at the C4 checkpoint.

C5 adds a native whole-report duplicate batch and writer admission check that
catch prohibited duplicates among cases reaching that boundary. This mitigates
the local bypass downstream; it does not correct the saved prompt wording or
local predicate. Whether an earlier local rejection hides another implicated
participant from the complete-report comparison remains a verification item.

Private evidence: final C4 state `bfadbb76-8bb6-499f-b501-4be08f62b28c`, version
`35`, and `.claude/lanes/gate1-live-six-20261020/reviewer/C4-review.json` plus
`C4-snapshot.json`, including literal saved predicates and all six prompts.

## F4 — Earlier C2 blank-input refusal blocks C4's unavailable-answer path

All four saved critical-assessment nodes require `case_study` and omit the
supported blank-input allowance. The six new C1 nodes include that allowance.
For a present empty or whitespace-only string, the first C2 node returns
`required_input_field_blank` and routes to failed rows before any C1 node runs.
The later allowance cannot make that earlier rejection reachable by C1.

This was independently checked against the saved configuration and the actual
LLM transform's pre-dispatch code, as well as the contract's unavailable-answer
rules. It does not assert that any uploaded private case contains a blank value,
and no private case was executed.

The completion Advisor withheld sign-off on this issue. C4 finished with a saved
connected draft, twenty ordinary pending review cards, and an uncleared Advisor
gate. Normal review approvals do not resolve the upstream configuration problem.

Code evidence: `src/elspeth/plugins/transforms/llm/base.py:290–295` and
`src/elspeth/plugins/transforms/llm/transform.py:158–180,2000–2002`.
Private evidence: the final C4 snapshot and its independently reviewed
`advisor_blank_input_defect` record.

## F5 — Required report checks remain incomplete in the six-message result

The saved writer does not mechanically enforce the four-sentence maximum; its
final reply discloses that only word count and a two-sentence minimum are checked.
It also discloses that the required visible count of excluded source cases has
not been implemented. These remain acceptance gaps. The model's statement that
no supported configuration can produce that count has not been independently
established; inspect native source/run metrics before claiming a product
capability is absent.

Failure identity for distinct cases sharing an application ID, actual nullable
CSV serialization, and paired report behavior when Detail fails after a case
reaches Summary remain unexecuted verification items. They are not demonstrated
runtime failures. The actual nullable `float?` schema spelling is supported;
declaring it an unsupported type would be incorrect.

Private evidence: final reply sequence `488`,
`.claude/lanes/gate1-live-six-20261020/reviewer/C6-review.json` and the preserved
`six-message-final-graph.json`.

## F6 — Final Advisor review encountered a timeout and provider filtering

The first C6 end-review provider call timed out after `60003` ms. Its retry was
recorded as `malformed_response` with finish reason `content_filter`, not a proven
JSON parsing failure. That retry reports `861530` prompt tokens in provider usage
metadata; this is not an independently measured input token count. A separate
second checkpoint pass subsequently returned `clean`/`ADMIT` at sequence `487`,
and the public C6 operation completed in `372` seconds.

Preserve this operational finding separately from graph configuration defects;
neither root cause nor a graph-level repair is established by this evidence.
Relevant transcript sequences are `478`, `481`, `483` and `487` in the linked
session. Private evidence is in the C6 review bundle.

## Seventh-message disposition

These are saved configuration changes, not an executed pipeline acceptance.
The exact reviewed cleanup body was submitted once in the same linked session;
the UI trimmed its single trailing newline. C1–C6 remain byte-for-byte matches
to the original reviewed inputs.

| Finding | After the seventh message |
| --- | --- |
| F1: weighted score / inclusive boundary | Repaired in the saved expression and independently checked on synthetic values using the actual owned expression evaluator. |
| F2: readiness wording | The six-message overclaim remains recorded. The seventh visible reply withholds completion; no readiness clearance was granted. |
| F3: unavailable-rationale exception | All six C1 prompts corrected; local uniqueness checks removed in favor of the existing native whole-report comparison. Broader early-rejection coverage remains open below. |
| F4: blank case text | All four C2 nodes now explicitly allow blank `case_study`; missing/null/wrong-type refusal and failure routes remain. End-to-end behavior has not been executed. |
| F5: report checks / visible excluded count | Still incomplete in the final saved configuration. Native capability absence is not established by the Composer's claim. |
| F6: transient C6 Advisor failures | Preserved as an operational finding; C6 subsequently obtained `ADMIT`. C7 has a separate terminal provider failure below. |

The new overall calculation uses whole-number weights followed by one division:
`(5*C + 35*A + 30*R + 15*E + 15*S) / 100`. No epsilon, rounding operation or
new input restriction was introduced. Controlled synthetic evaluation covered
all `3125` mapped-band tuples against seven selected recorded values each
(`21875` pairs), including both inclusive and outside boundaries: no calculated
decimal-spelling or Boolean mismatch was observed. The original failing example
now produces calculated `1.3`, difference `0.05`, and a positive inclusive
comparison against recorded `1.35`. This finite diagnostic does not establish
every arbitrary recorded float or pipeline runtime behavior.

The corrected C1 prompts preserve accurate repeated unavailable-answer text and
distinguish genuinely unavailable text from weak readable evidence. The native
`batch_duplicate_annotator` and writer input admission check remain. Removing
the local uniqueness rejections prevents those particular checks from hiding
cases before the complete-run comparison. Other earlier failures can still
exclude a case from that comparison; complete selected-run participation is not
proven merely by the presence of a batch plugin.

## F7 — Cleanup introduces finite-input overflow in the difference field

The seventh message separately changes `overall_score_difference` to
`abs(integer_weighted_numerator - 100 * recorded) / 100`. This multiplication
can overflow even when the recorded input and the required absolute difference
are finite. With all mapped bands equal to `1`, recorded `1e307` or `1e308`
causes the actual expression evaluator to raise `ExpressionEvaluationError`;
the ordinary unscaled absolute difference remains finite. A `1e306` control
succeeds. The contract does not impose a `1..5` restriction on the recorded
overall score, and no new restriction should be invented to conceal this case.

This is a controlled expression counterexample, not an observed applicant-row
or pipeline failure. It does not reopen the repaired inclusive YES/NO boundary.
Last-digit floating-point noise in ordinary displayed differences is separately
a numeric limitation, not a new exact-display contract defect.

Private evidence:
`.claude/lanes/gate1-live-six-20261020/reviewer/C7-numeric-actual-expression-diagnostic.json`.

## F8 — Seventh-message completion blocked by actual Advisor context-window refusal

The final Advisor provider call, `b73601dc-7274-4a3e-a9fd-1432cc5856ab`, is
recorded at transcript sequence `560` as `bad_request_error` with error class
`ContextWindowExceededError`, after `2508` ms. The checkpoint at sequence `562`
is `unavailable`, and final publication is withheld. The operation's
`completed` status therefore means it settled with saved changes, not that its
completion review passed.

This identifies the actual provider refusal. It does not establish which
context components caused the excess or justify dropping required evidence.
The earlier C6 token-usage metadata is relevant diagnostic context, not a
substitute for measuring this request's full composition.

## F9 — Paired reports, full duplicate participation and source-case counts remain unproven

The saved fork sends a full record independently to Summary and Detail. The
seventh response identifies that a Detail-path failure can be routed to failed
rows after Summary has accepted the case; the current draft does not provide a
paired all-or-neither report admission guarantee. No such failure was executed
in this run. The Composer's claim that no supported configuration can satisfy
the guarantee has not been independently established; do not invent atomicity
or change the contract to call the current topology compliant.

Likewise, cases rejected by other earlier checks do not reach the native
whole-report rationale comparison. The local-check removal fixes one such
boundary but does not establish complete all-participant coverage. A surviving
case sharing a rationale with an earlier rejected case is an open coverage
scenario, not an observed duplicate run.

The draft still does not expose the required distinct excluded-source-case
count. Counting Detail rows, fork tokens or individual failure reasons would
not meet that requirement. The failure report's carried source/run/row identity
is relevant to a supported counting mechanism, but actual retained-file
identity, distinct counting and paired-report disposition remain unexecuted.

## Other recorded limits and controls

- The Detail expansion emits an advisory list-shape warning because the builder
  does not declare its output field as a list to the explode step. The saved
  builder actually supplies eighteen question dictionaries; the warning alone
  is not proof of a malformed list or execution failure.
- The existing duplicate mechanism compares the selected run; coverage of
  earlier appended report rows is not demonstrated. No append behavior was
  added by this authoring run.
- Semantic accuracy of category facts, findings, and the writer's prohibition
  on new judgments is not established by mechanical checks or prompt self-review.
- The retained failure sink uses `require_all_rows`; configuration inspection
  supports refusing a failure-report write failure. That refusal and actual
  duplicate/blank/nullable behavior have not been exercised end to end.
- Normal prompt and prompt-injection review cards are expected workflow state,
  not defects. Twenty-two remain pending; none was approved by this task.
- The service remains active and the public `/api/health` endpoint returned
  `200` with `{"status":"ok"}` after the seventh request. Service health is
  separate from Gate 1 acceptance.
- A concurrent commit advanced the checkout to `24a02a1e29a8cf23c5574591ad2d314379ebfbdd`
  while this run was in progress. Its only changed path versus the rebuilt
  commit is `tests/unit/web/test_azure_container_apps_runbook_contract.py`;
  production source and the running service PID remained unchanged. This task
  made no production source/config edits and launched no test suite.

Private evidence is retained under
`.claude/lanes/gate1-live-six-20261020/`, including independent C1–C7 reviews,
the original six-message graph/baseline, actual tool-call evidence and numeric
controls. No private applicant content is reproduced in this findings file.
