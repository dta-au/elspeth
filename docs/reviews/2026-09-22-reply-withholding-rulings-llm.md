# Reply-withholding rulings — LLM-behaviour review — 2026-09-22

Reviewer read of `fix/composer-reply-withholding` at the worktree HEAD
(`e93cfd643`). Scope: an LLM-behaviour recommendation on the six decisions
left open by `docs/reviews/2026-09-22-composer-reply-withholding-review.md`
("Left for decision" + the deadline sibling), under the two non-negotiable
composer invariants in `AGENTS.md` § Composer invariants: (1) the LLM does
the job, no server-side authoring of pipeline structure; (2) no
tutorial-special paths. Server-side validation, rejection, and redaction of
what the planner produces are permitted; none of the recommendations below
propose synthesizing or routing pipeline structure server-side, and none
introduce a tutorial-only branch.

Read-only review. No source or test file was edited, no suite was run, no
commit was made. Every claim below is grounded in a cited `file:line`; where
I infer rather than verify, or where frequency is unmeasured, that is stated
explicitly — I did not invent a number.

---

## Case 2 — Planner discards prose on first build

**Recommendation: NARROW.** Keep the `DECLINE:` marker and the
`_PROSE_NUDGE_BUDGET = 2` nudge mechanism unchanged. Change only the terminal
`MALFORMED_RESPONSE` fallback: when the nudge budget is spent and no escape
hatch is available, persist the model's last prose reply as a recoverable
`composer_withheld_reply` audit row (extend `WithheldReplyOrigin`,
`src/elspeth/web/composer/withheld_replies.py:26-31`, with e.g.
`"planner_prose_nudge_exhausted"`) instead of discarding it, using the
`_persist_withheld_reply` mechanism this branch already built
(`src/elspeth/web/composer/service.py:3324-3352`). Do not change what the
user sees by default.

**What I verified, not what the review doc implied.** The review's framing
("no channel for a clarifying question") undersells an escape valve that
already exists. `_parse_response_tool_calls`
(`src/elspeth/web/composer/pipeline_planner.py:1611-1648`) does reject
unmarked prose as `PROSE_REPLY`, and the loop
(`pipeline_planner.py:4217-4245`) nudges up to twice. But past that budget,
if `model_config.escape_hatch_model` is available (it is essentially always
configured — `service.py:4308,4718,5206` always pass
`self._settings.composer_advisor_model`, a required non-optional `str`,
`src/elspeth/web/config.py:358`), the loop engages the hatch
(`pipeline_planner.py:4234-4237`, `_engage_escape_hatch` at `:4141-4146`). The
hatch turn (`is_hatch_turn`) calls with `allow_text_reply=True` and
**no** marker requirement (`:4189-4195`), and any non-tool text there is
accepted and **published verbatim** as `PlannerDeclined(decline_text=raw_text)`
(`:4291-4303`). So a genuine clarifying question written as free prose, on an
ordinary ML narration failure, usually *does* reach the user via the hatch —
it just arrives framed as a decline rather than a question.

The true silent-loss window is narrower than "any first-build prose": it
fires only when `hatch_spent` is already `True` — i.e. the hatch was already
consumed earlier in the *same* planning session (e.g. on an earlier
`REPAIR_EXHAUSTED` or `RESPONSE_TRUNCATED` exhaustion) — and the model then
prose-replies again past the nudge budget. Only then does
`_hatch_available()` (`:4138-4139`) return `False` and the loop raise
`PipelinePlannerError(..., code="MALFORMED_RESPONSE")`
(`:4239-4242`) with the text gone.

**LLM-behaviour reasoning.** The nudge budget itself is evidence-backed:
the comment at `pipeline_planner.py:4201-4211` cites a 2/2 live repro
(elspeth-b1e85829e9) where the model narrated the plan as prose at low
reasoning effort exactly on the turn the terminal call was due, and
`candidate_reasoning_effort` fixed it — a genuine, measured mitigation, not a
guess. That argues for keeping the nudge exactly as-is. What it does not
justify is destroying the text once the nudge (and the hatch) are spent. A
model that is still producing free text after two mechanical "call a tool"
nudges and one from-scratch senior-advisor turn is not narrating out of
habit anymore — at that point unmarked prose is much more likely to be the
model trying to say something it has no channel for (a genuine blocker,
disagreement with a constraint, or a garbled decline it couldn't format).
Keeping the text costs nothing structurally: it is the model's own verbatim
words, kept as a non-rendered audit row exactly like cases 1/3/4/5 on this
branch, not surfaced as new UI. This does not touch either composer
invariant — no structure is authored or bypassed, only text custody.

**Main risk.** None to invariant compliance; the risk is scope-creep of the
`WithheldReplyOrigin` vocabulary for a case that may be vanishingly rare
(double-exhaustion within one plan), and the cost of maintaining one more
audit-row shape.

**Measurement.** `prose_nudged` attempt rows are already emitted per the
review's "Not measured" section. Count `trail.finish_attempt(..., "prose_nudged" | "guard_fired", ...)`
records where a later `MALFORMED_RESPONSE` on the SAME plan has
`hatch_spent=True` at the time of the fatal `PROSE_REPLY`. If that count is
near-zero across a production sample, this is not worth doing; if nonzero,
recoverability is worth the small vocabulary addition.

**Confidence: medium.** High confidence in the code-path reading (the hatch
is the real safety valve, not a `DECLINE:`-only channel); medium on whether
the narrow double-exhaustion case is worth the change, because — as the
original review states — production frequency for this exact tail is
unmeasured and I did not invent a number for it.

---

## Case 5 — Advisor repair terminal (replaced even on CLEAN)

**Recommendation: KEEP, unconditionally, including the CLEAN-re-review
branch.** Do not narrow.

**What I verified.** The advisor findings block injected into the primary
model's own context is explicitly framed as **untrusted** content —
`_ADVISOR_FINDINGS_UNTRUSTED_BEGIN`/`_END` (`service.py:10766-10767`) fence
it, and the same injection carries an explicit output-contract instruction:
`_ADVISOR_OUTPUT_CONTRACT_CLAUSE` (`service.py:10773-10777`) tells the model
*"The end user has NOT seen these findings; your final reply is shown to
them and must state only the outcome — never reference, quote, or rebut the
advisor."* Despite that instruction being present at injection time,
`_replace_advisor_repair_public_result` (`service.py:1341-1429` and
onward) unconditionally replaces the model's terminal prose with fixed
backend copy whenever `advisor_repair_context_introduced` is set — including
when the next checkpoint returns CLEAN (docstring at `:1346-1352`
states this explicitly: "even when the next checkpoint returns CLEAN").

**LLM-behaviour reasoning.** This is the textbook shape of a prompt-injection
compensating control, and the code already treats it that way (the
`UNTRUSTED` fence is the tell). An instruction embedded in-context that says
"don't repeat/quote this" is a *soft* control: models comply with such
instructions unreliably, and the more directly relevant content sits in
context, the more likely paraphrase or leakage becomes under ordinary
sampling variance, not only under adversarial pressure — this is the same
reason system-prompt-secrecy instructions are known to fail routinely.
Relying on `_ADVISOR_OUTPUT_CONTRACT_CLAUSE` alone and skipping the
template-replacement enforcement would be exactly the anti-pattern the
untrusted-content fencing is designed to avoid. A CLEAN verdict is a fact
about the **pipeline state** (did the mutation resolve the finding), not
about whether the model's prose happens to paraphrase, quote, or allude to
what it read — those are independent axes, so gating replacement on CLEAN
would reintroduce exactly the leak class the fence exists to prevent, for
cases where the fix happened to work.

This also does not violate the composer invariants: no pipeline structure
is withheld, changed, or server-authored — only chat prose, and the
published copy is drawn from a fixed, pre-approved vocabulary
(`ADVISOR_REPAIR_SUCCESS_PUBLIC_MESSAGE` etc., `no_tool_policy.py:487-513`),
which is the same "trusted suffix over untouched state" posture the
`no_tool_finalize` augmentation invariant uses elsewhere in this codebase
for the equivalent, already-adjudicated concern.

**Main risk.** Information loss on the common path: a genuinely helpful,
non-leaking summary of what the model changed is discarded on every repair
that touched advisor context, even when nothing sensitive was ever at risk
of surfacing. Repeated over multiple turns, the user sees the same 1-2
canned sentences ("The pipeline is configured and ready.") regardless of
what actually happened, which can read as evasive rather than safe.

**Measurement.** This branch already keeps the replaced text as a
recoverable `composer_withheld_reply` audit row (Resolution status: "5 |
Recoverable"), so the risk is a UX-quality question, not a data-loss one.
Concrete instrument: sample rows with
`origin="advisor_repair_terminal"`/`"advisor_repair_tool_turn"` and check
(by a human read or a cheap classifier prompt) what fraction actually
reference, quote, or paraphrase advisor-finding language versus are
generic "done" summaries. If that fraction is near zero across a real
sample, that is evidence a *future*, separately-adjudicated narrowing
(publish on CLEAN only, still replace on FLAG) could be safe — but that is
a ruling change the review doc already flags as such, and this review does
not recommend making it now without that evidence.

**Confidence: high** that unconditional KEEP is the correct default given
the untrusted-content fencing and the explicit "even on CLEAN" design intent
already recorded in the docstring; **medium** on whether a future,
evidence-backed narrowing would ever be safe — that requires the sampling
measurement above, which I did not perform (no production data available in
a read-only review).

---

## Case 6 — Planner staging (server-authored proposal card)

**Recommendation: CHANGE, but conditionally — measure first, then decide.**
Two-step plan:
1. Add a low-cost instrument: whenever the terminal `emit_pipeline_proposal`
   provider response carries non-empty `content` alongside its tool call,
   record that fact (a counter, or a field on the existing planner-attempt
   telemetry) rather than silently dropping it as today.
2. If measurement shows it happens at a non-trivial rate, thread that
   `content` string through to `_stage_pipeline_plan` and publish it as an
   **augmentation** to the fixed `PIPELINE_STAGED_*` status line — never a
   replacement — mirroring the `finalize_no_tool_response` augmentation-prefix
   invariant (`message.startswith(raw_assistant_content)`) that the review
   doc cites as this codebase's own defended pattern.

**What I verified.** The inline comment's claim — "this surface's model
emitted a tool call, never prose" (`service.py:5076`) — is not just
unmeasured, it is currently **unmeasurable through this code path even if
false**: `_assistant_tool_calls_message` (`pipeline_planner.py:1785-1797`)
keeps the provider's `content` field only in the planner-**internal**
`messages` list used for subsequent provider turns. `PipelinePlanResult`
(`pipeline_planner.py:1340-1367`) — the object actually returned to
`_stage_pipeline_plan` — has no field to carry that text at all. So even if
a provider *did* return prose beside the terminal tool call, today's code
has architecturally nowhere to put it before it reaches the staging site;
the claim in the comment cannot currently be falsified by observing
`_stage_pipeline_plan`'s inputs. I also confirmed the proposal card's own
`summary`/`rationale` are separately, unconditionally server-authored
(`build_tool_proposal_summary`, cited by the review at
`proposals.py:37`) — that part is correct and untouched by this
recommendation.

**LLM-behaviour reasoning.** Whether providers attach `content` beside a
terminal tool call is a live behaviour question, not an architectural
guarantee — OpenAI-style function calling and Anthropic tool-use both
permit a short accompanying text segment in the same turn, and
reasoning-capable/instruction-following models are known to emit a brief
acknowledgment or caveat alongside a tool call even when told not to,
because the prompt (`PLANNER_TERMINAL_INSTRUCTION`,
`pipeline_planner.py:1730-1733`) instructs *what tool to call* but never
explicitly forbids accompanying text. Treating "never prose" as an
assumption baked into the data model (rather than a measured fact) is the
exact pattern the review doc's own methodology warns against ("state no
conclusion by inference" — this is CLAUDE/AGENTS doctrine, not just my
opinion). Fixing the *prompt* (explicitly forbidding accompanying text) is
one alternative, but that would be trying to suppress a behaviour that may
carry genuine signal (a caveat the model wants to raise) rather than
capturing it — the augmentation pattern is the safer default because it
preserves the model's words without granting it authority over structure.

**Composer-invariant check.** This is explicitly compliant: the proposal's
`pipeline` argument, `summary`, and `rationale` remain 100% server-projected
from validated fields exactly as today; a threaded model note would ride
alongside as clearly-labeled, unverified model prose — the same posture
`DECLINE:` bodies already have. No structure changes hands server-side.

**Main risk.** If exposed by default without measurement, model commentary
next to a server-authored proposal card could contradict or editorialize
over what was actually staged (e.g. the model claims something about the
plan that the projected `pipeline` argument does not reflect), which is a
believability/trust risk distinct from a security one — the user might
reasonably (and wrongly) treat the model's aside as authoritative over the
card.

**Measurement.** Exactly the one named above: a counter of
content-bearing terminal turns / total terminal turns on this surface,
collected before any UI change. The review doc already names this as the
single biggest "Not measured" gap for this case; I did not fabricate a
number for it, and none is available from a read-only code review.

**Confidence: medium-high** that the "never prose" comment is unverified
and that the current code path could not even prove it wrong if it wanted
to (both are things I checked directly in the source); **medium** on
whether surfacing the text is worth doing, which is exactly why the
recommendation is measure-then-decide rather than change-now.

---

## Case 7 — Guided scaffold-leak guard

**Recommendation: KEEP as written.** Do not narrow the substring match to a
line-leading tag, as the review doc's "Left for decision" section floats.

**What I verified.** `_require_prose_assistant_message`
(`src/elspeth/web/composer/guided/chat_solver.py:228-250`) rejects the whole
reply if it contains any of `<tool_call`, `</tool_call`, `<tool_response`,
`</tool_response` anywhere, case-insensitively (`:242-249`). The class
comment (`AssistantScaffoldLeakError`, `:214-225`) documents two **live,
observed** incidents (tutorial `resolve_source`, 2026-07-03; live-guided
advisory reply, 2026-07-03) where the model wrote "a pseudo agentic
transcript as literal text," in one case a full 2.8KB replay persisted
verbatim into a tutorial chat history and rendered to the learner. That
description — a large embedded replay, not a leading marker — is decisive
against the review's proposed narrowing: a line-leading-only match would
have missed exactly the incident that motivated the guard, since embedded
mid-reply scaffolding was what was actually observed, not a header line.

**Root cause, and why the fix is already partly in the prompt.** The
comment block at `chat_solver.py:107-132` correctly diagnoses this as a
prompt-priming artifact, not a hallucination in isolation: `solve_step_chat`
(Phase A, advisory-only) never attaches tools, but its system prompt is
`load_step_chat_skill(step)` — the **same** skill used by the tool-equipped
resolve calls, and the shared `base.md` unconditionally frames the model as
"you build the pipeline by calling tools." A model primed that way, given no
real `tools` parameter on the wire, narrates a tool call as literal text
because the system-prompt framing drives generation independent of what the
API actually exposes — a well-documented LLM behaviour (framing in the
prompt dominates over what's technically callable). This confirms the
task's framing exactly: the guard is catching a symptom of a prompt defect.
Critically, the prompt-level fix **already exists** for the one call site
that genuinely has no tools: `_ADVISORY_NO_TOOLS_ADDENDUM`
(`chat_solver.py:133-146`) is a fresh system message overriding the
tool-caller framing, and it is wired into `solve_step_chat`
(`chat_solver.py:4892-4895`). So "the fix belongs in the prompt" has already
happened where it is addressable; the substring guard is legitimate
defense-in-depth for whatever slips past that addendum (model
non-compliance with the addendum itself, or any future no-tools call site
that forgets to apply it) — not a workaround standing in for a missing
prompt fix.

**LLM-behaviour reasoning on false-positive risk.** The four marker strings
are angle-bracket XML/pseudo-tool syntax that would essentially never appear
in legitimate prose about CSV sources, sink formats, or transform options —
the guided wizard's actual subject matter — unlike, say, a
general-purpose coding assistant where a user might legitimately paste or
discuss XML tags. The risk surface for an innocent match here is narrow by
construction of the domain, which is a case-specific argument for keeping
the wide (substring, whole-reply) match rather than a general one.

**Main risk.** A reply that happens to literally contain one of the four
marker substrings for an innocuous reason (e.g. a user pasted example
tool-call XML and asked the model to comment on it, and the model echoed
it back) is rejected wholesale with the generic `_SCAFFOLD_LEAK_MESSAGE`
("didn't pass a quality check…", `src/elspeth/web/sessions/_guided_step_chat.py:313-317`)
rather than a more specific one.

**Measurement.** `status=SYNTHETIC_UNAVAILABLE` with
`error_class=AssistantScaffoldLeakError` is already the discriminating
audit signal (both the original review and the code confirm only
`error_class` distinguishes this from ordinary unavailability). Count
occurrences of that `error_class` and spot-check a sample of the rejected
content: if a nontrivial share are not genuine scaffold leaks (i.e. would
have been legitimate replies), narrow **then**, with the sample as
evidence, rather than pre-emptively weakening a guard that has two
documented live catches and zero documented false positives so far.

**Confidence: high.** Both the guard and its already-applied prompt-level
root-cause fix (the addendum) are directly read, and the two cited live
incidents both describe embedded/bulk leakage rather than line-leading
leakage, which directly contradicts the premise of the proposed narrowing.

---

## Case 8 — Guided shape/commit rejection copy

**Recommendation: KEEP.**

**What I verified.** `_MODEL_SHAPE_REJECTED_MESSAGE`, `_COMMIT_REJECTED_MESSAGE`,
`_PAIRED_SOURCE_NOT_APPLIED_MESSAGE`, `_PAIRED_SINK_NOT_APPLIED_MESSAGE`, and
`_DEFERRED_MANAGEMENT_REPAIR_MESSAGE`
(`src/elspeth/web/sessions/_guided_step_chat.py:290-345`) replace the
model's own `assistant_message` only when its accompanying tool arguments
were rejected. Confirmed both are gated on **repair exhaustion**, not
ordinary rejection: `_guided_step_chat.py:680-683` ("this branch fires only
at repair exhaustion") and `:933` ("self-repair in-Send, so this too fires
only at repair exhaustion") — i.e. the in-loop repair already tried and
failed to get a self-consistent tool-call + prose pair before this copy is
used. Each message is explicit about **what did not happen** ("I couldn't
apply that configuration, so I didn't change your pipeline" / "your
pipeline source is unchanged"), and none of them assert anything about what
the rejected tool call was trying to do.

**LLM-behaviour reasoning.** Reply and action are coupled on purpose here,
and that coupling is the *correct* call, not merely a defensible one.
Publishing the model's original `assistant_message` verbatim next to a
rejected tool call would almost certainly produce a **false completion
claim**: the model's prose was written on the premise that its own tool
arguments would be accepted ("I've set your source to X..."), and if X was
then rejected, the sentence becomes an affirmative lie about pipeline state
— worse than case 1's "reply discarded, nothing said," because here the
user would be *told something false happened* rather than simply not told.
This is the same class of hazard the codebase's own `no_tool_finalize`
augmentation-prefix invariant exists to prevent in the opposite direction
(hiding a real reply); case 8's fixed copy is the correct mirror-image
guard for a reply whose premise did not hold. Because this only fires at
repair exhaustion — a genuinely rare terminal case, not a routine
substitution — the cost of the fixed copy is paid rarely.

**Main risk.** The model's own (mistaken) explanation of what it was
attempting is lost, which has some diagnostic value for understanding a
repeat failure pattern (e.g. is the model consistently misunderstanding
a particular field).

**Measurement / optional follow-up (not required by this ruling).** The
same `WithheldReplyOrigin`/`_persist_withheld_reply` mechanism already used
for cases 1/3/4/5 on this branch could be extended to persist the rejected
`assistant_message` + rejected tool arguments as a recoverable audit row
here too, without changing what the user sees — consistent with how every
other resolved case on this branch kept the words while keeping the
publication fixed. This is an optional, small follow-up, not something the
review asked me to decide, and I am flagging it rather than recommending it
outright since it is additional scope.

**Confidence: high.** The coupling rationale is sound on its face (false
completion claim risk) and independently confirmed by the repair-exhaustion
gating, which shows this fires rarely and only after the loop already tried
to reconcile action and prose.

---

## Deadline sibling — compose-deadline expiry loses a completed final reply

**Recommendation: CHANGE, small and scoped.**
1. Persist the model's completed final reply (`assistant_message.content`)
   via the existing `_persist_withheld_reply` mechanism
   (`service.py:3324-3352`), using a new `WithheldReplyOrigin` value (e.g.
   `"compose_deadline_expired"`), at the raise site inside the terminal
   no-tool advisor gate before `_AdvisorCheckpointComposeDeadlineExpired` is
   raised (`service.py:6772`, and the advisor-checkpoint-call raise at
   `:8762` when reached from the same gate).
2. Separately, correct the user-facing error text. `ComposerConvergenceError.__str__`
   (`src/elspeth/web/composer/protocol.py:513-517`) is a **fixed** string:
   *"The LLM kept making tool calls without producing a final response."*
   That sentence is factually wrong for this trigger. It reaches the client
   verbatim: `_handle_convergence_error` sets `"detail": str(exc)`
   (`src/elspeth/web/sessions/routes/_helpers.py:3131`) in the HTTP 422 body.

**What I verified.** Every raise site of
`_AdvisorCheckpointComposeDeadlineExpired` that is reachable from
`_evaluate_terminal_no_tool_advisor_gate` (the gate that runs after the
model has already produced its no-tool terminal `assistant_message`) occurs
strictly **after** that reply exists: the `outstanding_findings`
verification raise at `:6772` sits inside the pending-interpretation-handoff
branch of the terminal gate, and the advisor-checkpoint-call raise at
`:8762` sits inside `_run_advisor_checkpoint`, which that same gate calls.
Both are caught by generic `except _AdvisorCheckpointComposeDeadlineExpired:`
handlers (`:5915`, `:6230`) that convert straight to
`ComposerConvergenceError.capture(budget_exhausted="timeout", ...)` — and
`capture`'s signature (`protocol.py:501-531`) has no parameter for
assistant prose at all, so the completed reply is unrecoverable by
construction once this path is taken. I also checked the **other**
raise site of the same exception, at `:7272` inside
`_maybe_run_early_checkpoint` — that one is structurally different: it fires
mid-loop after a **tool-call** turn (not a no-tool terminal reply), is
caught locally, and only sets a flag (`early_checkpoint_deadline_expired`)
for the driver to handle later; there is no completed final prose at risk
there, since the turn was a tool call. I am restricting this
recommendation to the terminal no-tool-gate sites (`:6772`, `:8762` in that
context), not claiming it applies uniformly to all five raise sites — the
early-checkpoint site is a different shape and outside the "reply is lost"
description in the task brief.

**LLM-behaviour reasoning.** This is the same "false completion/failure
claim" concern as cases 1-5, mirrored: cases 1-5 are about not letting the
*model* falsely claim more than it did; this is the system falsely telling
the *user* that the model failed to respond, when a verification step (the
masked pending-interpretation re-validation, or the advisor checkpoint call
itself) is what ran out of the shared wall-clock budget after the model
had already finished. An operator or user reading "the LLM kept making tool
calls without producing a final response" will draw exactly the wrong
inference about what to do next (retry expecting a different model
behaviour) versus the correct one (the verification pipeline is slow under
this load; that is an infra/latency issue, not a model-behaviour one).
Trustworthy error copy is a Composer-adjacent trust surface, not a
cosmetic detail: this codebase already treats "the model claims more than
it did" as a defect class worth engineering effort (the entire
augmentation-invariant doctrine); a system-authored false claim about the
model's behaviour deserves the same standard.

**Composer-invariant check.** No conflict: this does not touch pipeline
authoring at all — it only changes what error text a timeout produces and
whether the model's already-generated (never-published) words are kept in
the audit trail. Nothing here routes around the provider or adds a
tutorial-special path.

**Main risk.** `ComposerConvergenceError` is a Tier-1-adjacent exception
whose attributes are deliberately frozen after construction
(`_FROZEN_ATTRS`, `protocol.py:497-499`, enforced via a custom
`__setattr__`) specifically so no intermediate layer can silently rewrite
what downstream consumers (the 422 body, the `composition_states` audit
table) see. Adding a new frozen field or a new `budget_exhausted` variant
must go through that same discipline carefully — done carelessly, it risks
an attribute-mutation bug exactly where the class was hardened against one.
This is a real but small, mechanical risk (the pattern to extend is already
established for the other 8 attributes), not a design risk.

**Measurement.** Today, the "reply completed, verification timed out" shape
is **invisible**: it is indistinguishable from any other `timeout` at the
`budget_exhausted` field, so nobody can currently tell how often it occurs
in production — which is itself the argument for the change, not something
I can measure without it. Concrete instrument once landed: count sessions
where `_AdvisorCheckpointComposeDeadlineExpired` was raised from the
terminal-gate context specifically (distinguishable once a `reason` or
dedicated `budget_exhausted` value is added) versus other timeout shapes,
and cross-reference against the new `composer_withheld_reply` rows with
`origin="compose_deadline_expired"` to confirm the reply-preservation half
actually fires when expected.

**Confidence: high** that the current error string is actively misleading
for this specific trigger (verified directly: the model's `assistant_message`
exists at the raise site, `capture()` never receives it, and `__str__` is a
fixed string that asserts the opposite of what happened) and that
persisting the reply via the existing, precedented mechanism is safe and
small; **medium** on the exact scope of the error-text fix, since choosing
the right user-facing wording may need product/copy input beyond what a
code-only review should decide — I'd land the reply-persistence half
unconditionally and treat the message-text correction as the natural next
step, not bundle a user-copy decision into this review's recommendation.

---

## Summary of confidence and what would change my mind

| Case | Recommendation | Confidence | Would change my mind if... |
|---|---|---|---|
| 2 — Planner prose | NARROW (recover text only past a spent hatch) | medium | Telemetry shows the "hatch already spent, then re-nudged" tail is genuinely ~0 in production — then KEEP as-is. |
| 5 — Advisor repair terminal | KEEP unconditionally | high | A sample of withheld rows shows near-zero actual advisor-content leakage even on CLEAN — would support a *future*, separately-ruled narrowing, not this one. |
| 6 — Planner staging | CHANGE, conditional on measurement | medium-high on the gap, medium on the fix | The content-bearing-terminal-turn counter comes back ~0% across a real sample — then the existing comment is correct and nothing should change. |
| 7 — Scaffold-leak guard | KEEP as written | high | A sample of `AssistantScaffoldLeakError` rejections shows a material false-positive rate of genuinely benign replies — then narrow to line-leading, with that evidence. |
| 8 — Guided rejection copy | KEEP | high | Evidence that repair-exhaustion rejections are common rather than rare — that would point at the repair loop itself needing work, not this copy. |
| Deadline sibling | CHANGE (persist reply; fix error text) | high on the defect, medium on wording | None identified — the current error string is verifiably false for this trigger regardless of frequency. |
