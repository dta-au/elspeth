# Reply-withholding rulings — systems review — 2026-09-22

Companion to `docs/reviews/2026-09-22-composer-reply-withholding-review.md`,
which catalogued eight sites where the composer removes or replaces the
model's reply. That review fixed two and left six for ruling. This one asks a
different question: what structure keeps producing this class of defect, and
which intervention on that structure is worth a one-developer pre-release
project's time.

**Read at** `fix/composer-reply-withholding` HEAD `e93cfd643`. The companion
review's line numbers were taken at `7d981d6cf`; three commits have landed
since, so every citation below is a fresh read at `e93cfd643` and the numbers
do not match the companion document's.

**Read-only.** No source or test file was edited, no suite was run, nothing
was committed.

**Measured vs inferred.** Counts in the Cost inventory were produced by the
commands in the appendix, each with a control. Everything about *frequency in
production* is unmeasured and is flagged as such; no number in this document
describes how often a case fires.

---

## A. The system that keeps producing this

### A.1 The generating goal

One sentence in the tree generates almost all of the cost. From
`src/elspeth/web/composer/service.py:10968-10972`:

> Advisor-MODEL findings remain withheld on these branches (R2-F13: raw
> provider findings never enter the composer's published prose or
> validation-wire surfaces — scoped deliberately: a flagged model's subsequent
> TOOL CALLS can still write derived text into pipeline state the user
> inspects, and that state channel is uncontained by design,
> elspeth-25f7b757e7 A4).

R2-F13 is a **goal** in Meadows' sense (level 3), not a mechanism: *the
advisor's findings are internal*. Everything in the advisor cohort is
downstream of it. The primary model's prose is withheld not because the prose
is wrong but because the prose was written in a context that contained the
findings and may quote them — the rationale stated at
`src/elspeth/web/composer/no_tool_policy.py:396-401` and repeated in the
builder docstring at `service.py:8433-8443`.

Note the asymmetry the comment itself admits. Containment is **absolute on the
prose channel** and **acknowledged uncontained on the state channel**: a
flagged model's tool calls can write derived text into pipeline state the user
reads. So the system already tolerates the leak it is paying to prevent, on a
channel that carries more of the model's judgement than a chat bubble does.
That asymmetry is the single most important fact in this review. It is not an
argument that R2-F13 is wrong — it is an argument that its *price* has never
been set against its *coverage*.

### A.2 Loop 1 — Fixes That Fail (the notice-patching loop)

```
  a gate must not publish the reply
        │
        ▼
  replace it with backend copy ──────────────┐
        │                                    │ (reinforcing)
        ▼                                    │
  the copy makes a claim that is false       │
  on some state shape                        │
        │                                    │
        ▼                                    │
  split the notice on that shape,            │
  add a constant + a recognizer arm ─────────┘
```

This loop is visible as a dated trail, not as a hypothesis.
`no_tool_policy.py` cites **12 distinct ticket ids in 27 places** (appendix
A.1). Read in order they are a sequence of the same event: a notice asserted
something the turn had not established, and the repair was a new shape rather
than a different mechanism.

- `service.py:11019-11032` (elspeth-5403f346c0): the blocked validation zeroed
  every readiness axis, reporting a *green* build as authoring-invalid under a
  "Runtime preflight failed" header. Fix: a fourth validation shape.
- `service.py:1360-1368` (elspeth-88592f5be7): `runtime_preflight is None`
  means "not computed", and it was riding the success disjunct — publishing
  "The pipeline is configured and ready." for a turn in which nothing
  validated. Fix: a fifth branch.
- `service.py:8474-8480` (elspeth-2ae50afcd1 facet B): an absent preflight
  published a "Runtime preflight failed" header asserting a failure the turn
  never produced. Fix: a sixth branch.
- `service.py:1405-1414` (elspeth-66717f0c99) and `:1427-1440`
  (elspeth-5a372d3267): "ready for the required review" over stages the strict
  ledger never reached. Fix: a masked re-validation plus two more branches.
- `service.py:8575-8584` (elspeth-25f7b757e7 N1): one uniform
  `flagged_unrepairable` copy asserted "No pipeline change is needed" over
  red, absent and handoff preflights. Fix: shape-awareness on all four.

Each repair was correct. None of them reduced the number of ways the next
notice can be false, because each added a shape. The balancing loop that would
stop the growth — *publish the model's own words, which cannot make a false
backend claim because they are not a backend claim* — is exactly what R2-F13
closes off on this cohort. The defended path in
`src/elspeth/web/composer/no_tool_finalize.py` is that balancing loop: every
exit appends and `enforce_augmentation_prefix_invariant` proves it
(`no_tool_finalize.py:230-234`, `:300-304`, `:314-318`, `:373-377`). Its
docstring records why the earlier full-replacement synthesizer was removed
(`:174-182`, `:266-283`, elspeth-861b0c58f5, elspeth-9cfbad6901). Replacement
then re-grew at the exits that do not pass through it — which is the defining
signature of Fixes That Fail: the fix is real, and it is applied at one point
in a structure that has many.

### A.3 Loop 2 — Shifting the Burden (filters over brief quality)

The symptomatic fix is an output filter; the fundamental fix is the prompt or
the protocol the model is given. The system has done both, which is what makes
this diagnosable rather than speculative.

- Fundamental fix, taken: `guided/chat_solver.py:123-132` names the cause
  precisely — `solve_step_chat` attaches no tools but its system prompt is the
  same per-step skill that frames the model as a tool-caller, so the model
  narrates a tool call as literal text. `_ADVISORY_NO_TOOLS_ADDENDUM`
  (`:133-146`) overrides the framing for that call. That is the brief being
  fixed.
- Symptomatic fix, retained and still load-bearing:
  `_require_prose_assistant_message` (`:228-250`) still rejects the whole
  reply on a substring match, and it fires at eight call sites, of which only
  the one at `:4955` is covered by the addendum (applied at `:4895`). The
  tool-equipped `resolve_source` / `resolve_sink` / management paths at
  `:2782`, `:2970`, `:3034`, `:3121`, `:3694`, `:4135` and `:4696` have no
  equivalent addendum.

So the burden has been shifted back *once*, on one path, and the filter
remains the only mechanism on the other seven. The addiction signature — the
filter grows while the brief stays unexamined — is present but partial.

### A.4 Loop 3 — Rule Beating (the model routes around the gate)

`pipeline_planner.py:1743-1752` records the model defeating the nudge:

> Names the terminal tool: the generic "call a declared tool" wording was
> satisfiable by any cheap discovery call — the live repro showed the model
> answering each nudge with a discovery call and prosing again, never reaching
> the terminal tool before the nudge budget spent.

This matters beyond case 2. Any new channel offered to the model — a marker, a
field, an escape hatch — will be used to satisfy the letter of the gate unless
taking it costs the model something it wants. It is the constraint every
recommendation in section B has to survive.

### A.5 Where the notice pairs sit in the structure — the cost inventory

All measured (appendix). These are the *stock* that loops 1 and 2 accumulate,
and the reason the next withholding decision is expensive:

| Artefact | Count | Location |
|---|---|---|
| Withheld-twin notice constructions (`X = Y_PUBLISHED + " " + ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE`) | 13 | `no_tool_policy.py` |
| `_PUBLISHED_`-named constant definitions | 30 | `no_tool_policy.py` |
| `_ADVISOR_PUBLISHED_BARE_SUFFIXES` registrations | 13 | `no_tool_policy.py:796-816` |
| `_ADVISOR_PUBLISHED_WRAPPED_SHAPES` registrations | 4 | `no_tool_policy.py:817-822` |
| `_AugmentationBranch` literal members | 8 | `no_tool_policy.py:699-708` |
| Distinct ticket ids cited | 12 (27 citations) | `no_tool_policy.py` |
| Distinct ticket ids cited | 44 | `service.py` |

Add the gate obligations that ride each one, from
`CONTRIBUTING.md:272-307` ("Gate: wire-shape templates"): a new backend suffix
must be built through `_wrapped_diagnostic_template`, must add a round-trip
case to a **hand-maintained** list that an AST scan checks
(`CONTRIBUTING.md:278-280`), must be registered in
`_canonical_trusted_suffix_segments` with a matching `_split_wrapped_diagnostic`
arm — and if that registration is missed the gate "fails closed and publishes
your operator notice as model prose, silently" (`CONTRIBUTING.md:289-291`).
Every END-gate notice must exist as a **pair**, and "What decides the member is
whether advisor findings entered the model's context this turn
(`advisor_repair_context_introduced`), never the block's `reason`"
(`CONTRIBUTING.md:305-307`).

That last line is the tell. Choosing between two copies of the same sentence
is now a documented, gate-adjacent obligation: the AST scan itself checks that
every template has a round-trip case (`CONTRIBUTING.md:278-280`), and the
pairing rule sits beside it as a written requirement that a new blocked notice
ship "both members, both registered" (`:300-307`). The project has paid to
formalise the maintenance of a doubling that exists only because the prose
channel is contained absolutely. **The 13 pairs are not the disease; they are
the correctly-engineered symptom of a goal whose price has never been
totalled.**

### A.6 Why the same chokepoint keeps failing

`AGENTS.md` records two prior composer-invariant violations (`b073d248e`,
`9700470e2`), both latency fixes on the rootless/tutorial entry path, the
first undetected for 26 days "because the gate counted calls per walk". The
withholding sites cluster on the same chokepoint for the same reason: the
terminal exit of a turn is where every policy — advisor sign-off, preflight,
repair budget, compose deadline — has its last chance to act, and it is the
only place where acting means destroying something the model produced. A gate
placed there is structurally tempted to fail *toward removal*, because removal
always satisfies the gate and publication never certainly does.

---

## B. The six rulings

Each gives a verdict, the specific change, the Meadows leverage level, second
-order effects, and the measurement that would show it worked. Where a
recommendation is a **product or policy decision rather than an engineering
one**, it says so — a systems review can price a ruling but cannot make it.

### CASE 2 — Planner discards prose → **NARROW**

**Verified.** `_parse_response_tool_calls`
(`pipeline_planner.py:1611-1648`) admits a no-tool-call text reply only when
`allow_text` is set and the content leads with the case-sensitive `DECLINE:`
marker (`_marked_decline_body`, `:1594-1608`); everything else raises
`PROSE_REPLY`. The loop (`:4217-4245`) nudges twice
(`_PROSE_NUDGE_BUDGET = 2`, `:1710`), then goes to the escape hatch or raises
`MALFORMED_RESPONSE`. The planner's entire roster is read-only discovery plus
one terminal tool (`planner_tool_definitions`, `:1497-1521`) whose only
parameters are `pipeline` and `claimed_deferred_intent_ids`
(`planner_terminal_tool_definition`, `:1474-1494`). There is no channel for a
question.

**Recommendation.** Add a second protocol marker — `QUESTION:` — that takes
the same publish path the `DECLINE:` body already takes, and persist the
nudged prose through the withhold seam (section C.1) on the way to the
`MALFORMED_RESPONSE` exit.

Do **not** solve this with a prose field on the terminal tool (that is case
6's fix). A clarifying question is the model saying it does *not* want to emit
a proposal yet; a field on the emit call cannot express that.

**Why a marker and not a tool.** A new tool in the planner roster is a
capability-manifest change (`_assert_planner_call_matches_manifest`,
`:1524`) and widens the discovery surface. The marker reuses machinery that
exists: `_marked_decline_body` is already parameterised on the marker string
(`:1594`), `allow_text` / `text_marker` already thread through
`_parse_response_tool_calls` (`:1615-1616`, `:1627-1631`), and the teaching
text has an established form (`_prose_decline_notice`, `:1755-1765`).

**Leverage: level 6 (information flows)** — it creates a channel from the
model to the user that does not currently exist. Secondarily level 5 (rules):
it changes what the planner surface admits.

**Second-order effects.**
- *Rule-beating risk is low here*, unlike the nudge. Answering a nudge with a
  cheap discovery call costs the model nothing (`:1744-1747`); emitting
  `QUESTION:` ends the turn and forfeits the build, so it is not a
  lower-effort path to the same reward.
- The marker-vs-narration ambiguity the comment at `:1586-1591` describes gets
  *worse*, not better, with two markers: a model that prefixes narration with
  `QUESTION:` now publishes narration. Mitigation: the same case-sensitive,
  must-lead, non-empty-body discipline, and the same "one nudge" fail-safe
  direction the decline marker chose deliberately (`:1601-1602`).
- A published question changes the turn's disposition on the planner surface
  from "staged / failed" to a third thing. Check the frontend's handling of a
  planner turn that produces no proposal — the `DECLINE:` path already
  establishes that shape, so this is a reuse, not a new arm, but confirm it.

**Measure.** Planner attempt rows already record `prose_nudged`
(`:4243`) and the trail records the disposition. The pair worth watching is
the ratio of `prose_nudged` attempts that reach `MALFORMED_RESPONSE` before
and after. If the marker works, that ratio falls and `QUESTION:` publications
appear; if the model beats the rule, `QUESTION:` publications rise *without*
the failure ratio falling — the signature of narration wearing the marker.

**Standing caveat.** The companion review is right that whether a first-build
text reply should reach the user is a product decision about the planner
surface. This recommendation prices it; it does not make it.

### CASE 5 — `_replace_advisor_repair_public_result` → **KEEP** (recoverability already landed)

**Verified.** `service.py:1341-1465`. Seven branches, all fixed copy, all
setting `raw_assistant_content` to `None` or `""`. Applied at both loop exits
(`:7161-7171`, `:7514-7524`) purely on the sticky
`advisor_repair_context_introduced` flag; the last checkpoint's verdict is not
consulted. The docstring states the rule plainly: the prose "was generated
after the model received an internal advisor finding, so it is not safe as a
human or persisted transcript surface **even when the next checkpoint returns
CLEAN**" (`:1349-1352`).

**Recommendation: keep the withholding.** The companion review frames the
CLEAN-re-review case as a candidate for narrowing. It is not, and the reason
is worth stating because it will come up again: CLEAN-ness of the *re-review*
is orthogonal to the *leak*. The findings entered the model's context before
the prose was written; whether the repair later satisfied the advisor says
nothing about whether the prose quotes what the user never saw. Narrowing on
CLEAN would be a rule that fails open on exactly the case it is meant to
catch.

Recoverability is already fixed on this branch: `_persist_withheld_reply` runs
before the replacer at `service.py:3399-3404`.

**Leverage of what landed: level 6 (information flows)** — the words now exist
in a durable, non-rendered place. It does not touch the structure.

**The residual, and its honest price.** The success branch publishes "The
pipeline is configured and ready." (`:1394-1402`) in place of the model's
summary of *what it changed*. The user loses the changelog, which is the part
of the reply with the most operational value. A mitigation exists that does
not touch R2-F13: `_AdvisorReviewState` already carries
`successful_mutating_actions` (`_compose_loop_carriers.py:123`), which is
server-derived from tool evidence, not from prose, so reporting it is not
server-side authoring of pipeline structure and does not violate composer
invariant 1 (it is the same class of act as
`build_tool_proposal_summary`, `proposals.py:37`).

Be clear about what that is: **a mitigation that still shifts the burden**. It
replaces the model's account of its work with the server's account of the
model's work. It is better than "configured and ready" and worse than the
model's words. And it is not free — the success branch is currently a bare
constant with no suffix-over-prose shape, so adding a detail-bearing notice
means a new wire-shape template, a round-trip case, a recognizer arm and a
`_PUBLISHED_` twin (`CONTRIBUTING.md:282-307`). That is the fourteenth pair.

**Recommendation on the residual: do not build it as a fourteenth pair.** If
the changelog is wanted, it belongs to the structural question in section C.2,
not to another notice.

**Measure.** `AdvisorTerminalPublication` rows already carry `branch`
(`service.py:1390-1392` and siblings). Count rows by branch; `repair_success`
is the population that loses a changelog. That count, against the count of
`composer_withheld_reply` rows with `origin="advisor_repair_terminal"`, is the
size of the problem — and it is now countable, which it was not before this
branch.

### CASE 6 — Planner staging publishes only server copy → **CHANGE**

**Verified.** `_stage_pipeline_plan` (`service.py:4925-5104`) publishes one of
four `PIPELINE_STAGED_*` constants (`:5082-5097`). The proposal card carries
no model words either: `build_tool_proposal_summary` (`proposals.py:37-128`)
authors both halves — the rationale is the constant "Requested by the current
composer turn." (`:43`) and the summary is a node count (`:51-59`).
`PipelinePlanResult` (`pipeline_planner.py:1340-1367`) has no field for model
prose, and `_assistant_tool_calls_message` (`:1785-1788`) keeps any `content`
returned beside the tool call in planner-internal history only. On the planner
surface the only model-authored text that can reach the user is a `DECLINE:`
body.

**Recommendation.** Add an optional `assistant_message` string property to the
terminal tool's schema, carry it on `PipelinePlanResult`, and publish it as
the staging message with the existing staged copy appended as a trusted
suffix.

**This sidesteps the review's "measure first" gate, and that matters.** The
companion review defers case 6 pending a measurement: do real providers return
`content` beside the terminal proposal call? That measurement **cannot be made
from existing rows** — `ComposerLLMCall`
(`contracts/composer_llm_audit.py:148-178`) stores request hashes, token
counts and `reasoning_content` and has no response-text field, which is the
same fact that made cases 1 and 3 unrecoverable. So the gate as posed is
unsatisfiable without first building an instrument. A producer-side field does
not depend on provider behaviour at all: the model is *told* it may fill it,
so the channel exists whether or not that provider would have volunteered
`content`. Prefer the producer-side discriminator to the structural inference
— the same principle the tree already applies at
`CONTRIBUTING.md:305-307` (decide on `advisor_repair_context_introduced`,
never on the block's `reason`) and at `service.py:1377-1379` ("The
discriminator is the producer's own marker, never the preflight shape").

**Leverage: level 6 (information flows)**, with a level 10 (stock-and-flow)
component — it adds the structural carrier for prose that the planner surface
currently lacks end to end.

**Cost, honestly.** Three cheap sites and one expensive one.

Cheap, and bounded by an existing precedent — `claimed_deferred_intent_ids` is
already a second top-level property beside `pipeline`, so a third is not a new
shape:
- schema: `pipeline_planner.py:1484-1492`
- payload model: `_PlannerTerminalPayload`, `pipeline_planner.py:~1430-1441`
- the closed key set: `allowed_terminal_keys = {"pipeline",
  "claimed_deferred_intent_ids"}`, `pipeline_planner.py:4363-4364`
- the carrier: `PipelinePlanResult`, `pipeline_planner.py:1340-1367`
- publication: `_stage_pipeline_plan`, `service.py:5081-5104`

Expensive: turning the staged copy into a **suffix over model prose** is a new
wrapped-diagnostic template, a hand-maintained round-trip case, a
`_canonical_trusted_suffix_segments` arm and a `_split_wrapped_diagnostic` arm
(`CONTRIBUTING.md:274-295`), plus the `raw_assistant_content` pairing that
`routes._composer_history_content` reads *structurally* — the comment at
`service.py:5072-5080` explains why the green arm keeps `None` and the
non-green arms carry `""`, and a fourth state (prose present, suffix appended)
must fit that discriminator. Budget the gate work, not the field.

**Second-order effects.**
- The staged announce stops being byte-predictable, which may move pinned
  bytes in any golden that captures it (`CONTRIBUTING.md:309-323`). Grep for
  the `PIPELINE_STAGED_*` constants in `tests/` before starting.
- Prose authored by the planner is Tier-3 text on a surface that currently
  carries none. It must go through the same publication path as any other
  model prose, not into the proposal `summary`/`rationale` fields, which are
  server-authored by contract and are read by the review card.
- Do not let the field become a route around invariant 1: it carries *words*,
  never structure. The pipeline stays in `pipeline`.

**Measure.** After the change, the share of staged proposals whose
`assistant_message` is non-empty is directly countable, and it answers the
original question honestly — it measures what the model does when given the
channel, which is the thing that actually matters, rather than what some
provider happened to return when there was nowhere to put it.

### CASE 7 — Guided scaffold-leak guard → **NARROW**

**Verified.** `_require_prose_assistant_message`
(`guided/chat_solver.py:228-250`) lowercases the whole reply and rejects it if
it contains any of `<tool_call`, `</tool_call`, `<tool_response`,
`</tool_response` (`_TOOL_SCAFFOLD_MARKERS`, `:116-121`). The user receives
`_SCAFFOLD_LEAK_MESSAGE` (`sessions/_guided_step_chat.py:313-317`), whose
comment explains the copy is deliberately not the unavailability wording.
Status is `SYNTHETIC_UNAVAILABLE` with `error_class` carrying the distinction
(`:309-312`).

**Recommendation.** Narrow the match from "anywhere in the reply" to "at the
start of a line" (after optional whitespace), and record the rejected text
through the withhold seam.

**The residual is real, not dead.** The upstream cause is named at `:123-132`
and was fixed for one path by `_ADVISORY_NO_TOOLS_ADDENDUM` (`:133-146`,
applied at `:4895`). The guard fires at eight sites; the other seven —
`:2782`, `:2970`, `:3034`, `:3121`, `:3694`, `:4135`, `:4696` — are
tool-equipped `resolve_source` / `resolve_sink` / management calls where that
addendum is deliberately *not* applied, and correctly so: those calls
legitimately have tools and must not be told they have none (`:130-132`). So
the filter is still the only mechanism on the large majority of its call
sites.

**Why line-leading and not deletion.** A leaked transcript is a
line-structured artefact — the observed failure was "a 2.8KB replay of an
invented list_sources/build_source loop" (`:110-112`). A legitimate mention
inside a sentence is not. ELSPETH ships an `llm` transform, so a guided user
configuring one can plausibly write a sentence containing `<tool_call>`; under
the current substring rule that reply is destroyed. This is unmeasured — no
production frequency is claimed — but the class is reachable by construction,
not hypothetical.

**Leverage: level 12 (a parameter)** for the match narrowing — genuinely low
leverage, which is the honest assessment: it reduces a false-positive class
without changing the structure. The recoverability half is level 6.

**Second-order effects.**
- Narrowing makes the guard weaker against a leak that is embedded mid-line.
  Accept it: the guard is a register check, not a security control, and the
  fail-safe direction here is "publish a reply that mentions a tag" rather
  than "destroy a reply that discusses one".
- A mutation control is required before trusting the change: feed the guard
  the 2.8KB transcript shape it was built for and confirm it still goes red.
  A narrowed matcher that silently matches nothing returns exactly what a
  correct one returns when there is nothing to find (`AGENTS.md`, Claims Must
  Be Measured).

**Recoverability, and the trap in it.** `ComposerChatTurn`
(`contracts/composer_llm_audit.py:325-370`) holds
`user_message_hash`/`assistant_message_hash` and states explicitly that "this
record holds the hash only, never the raw text (the raw text is Tier-3 user
input or LLM output and must not enter the audit row)" (`:342-346`). That rule
is about the **Landscape audit record** and must not be weakened. The withheld
-reply row is a different store: it is a session `chat_history` row written
through `sessions.add_message` (`service.py:3345-3352`), a table that already
holds the user's and the model's text. `role="audit"` rows are excluded from
the chat view and from prompt history unconditionally
(`sessions/routes/_helpers.py:1569-1594`). So the guided lane can gain
recoverability without touching the hash-only contract — and that distinction
must be stated in the change, or a reviewer will read it as a violation.

**Measure.** `error_class` already distinguishes
`AssistantScaffoldLeakError` from transient failures on the chat-turn row.
Count those rows before and after; a narrowing that fixes false positives
lowers the count while the positive control still fires.

### CASE 8 — Guided shape/commit rejections → **KEEP**, with recoverability

**Verified.** The fixed-copy replacements live at
`sessions/_guided_step_chat.py:290-345` and are returned from the exception
handlers — `_MODEL_SHAPE_REJECTED_MESSAGE` at `:703`, `_SCAFFOLD_LEAK_MESSAGE`
at `:451`/`:667`/`:923`/`:1100`, `_PAIRED_SOURCE_NOT_APPLIED_MESSAGE` at
`:569`, `_DEFERRED_MANAGEMENT_REPAIR_MESSAGE` at `:431`/`:639`. The comment at
`:673-684` confirms these fire only at in-Send repair exhaustion.

**Recommendation: keep the coupling.** The companion review's rationale is
correct and is the strongest of the eight: a rejected action's explanation
describes a change that did not happen. Publishing it would be the
false-completion-claim failure that
`no_tool_finalize.py:174-182` exists to prevent, in a lane that has no
augmentation invariant to catch it.

**There is also a mechanical limb worth recording.** At these exits the
model's prose is *not in scope*. `_require_prose_assistant_message` raises
with a message naming the marker and the tool
(`guided/chat_solver.py:245-249`); it does not attach the rejected value. So
"publish it anyway" is not a small change — it is a change to how the
rejection travels.

**Leverage: level 6 (information flows)** for the recoverability half; the
ruling itself is a KEEP and moves nothing.

**If recoverability is wanted — the trap.** Carry the rejected prose as a
**typed attribute on the exception**, never in `str(exc)`. The handler at
`:686-696` logs `error_detail=str(exc)`, and `_safe_frame_strings`
(`:348-375`) exists precisely to keep Tier-3 text out of those logs: "No
source lines or local-variable reprs that could carry Tier-3 data." Folding
the prose into the exception message would route model output straight into
structured logs and violate the logging policy. An attribute the handler reads
and passes to the withhold seam does not.

**Measure.** The same `error_class` population as case 7, split by exception
type. If the seam lands, `composer_withheld_reply` rows with a guided origin
become the count.

### DEADLINE SIBLING — completed reply lost to the compose deadline → **CHANGE** (record, do not republish)

**Verified.** `_AdvisorCheckpointComposeDeadlineExpired`
(`service.py:745`) is raised from two places:
`:8753-8762`, when the shared compose budget expires before any advisor
attempt ran, and `:6766-6772`, when it expires before the masked
re-validation of a pending-handoff preflight can run. Both are caught and
converted to `ComposerConvergenceError.capture(..., budget_exhausted="timeout")`
at `:5915-5924` and `:6230-6239`. `ComposerConvergenceError`
(`composer/protocol.py:472-512`) has no prose field.

**Recommendation.** Call the withhold seam at both `except` sites before
raising. **Verified in scope at both**: `assistant_message` is bound at
`service.py:5875` (`assistant_message = completion.message`) before the try
that ends at `:5915`, and is a parameter of the enclosing terminate phase at
`:6006` for the `:6230` site; `session_id` and `session_operation_context` are
in scope at both (`:6033` for the second). Add a
`"compose_deadline_expired"` member to `WithheldReplyOrigin`
(`withheld_replies.py:26-31`). That is one call per site plus one Literal
member.

**Do not republish it, and specifically do not route the deadline into the
advisor `unavailable` notice.** That reuse looks free — the notice pair
already exists and was extended by `e93cfd643` — but it contradicts the code's
own distinction at `:8754-8756`: "If no advisor call ran, this is a compose
timeout rather than an advisor verdict/provider failure." Telling the user the
advisor was unavailable when the truth is that the request ran out of time is
exactly the class of false claim that loop 1 has been repairing for twelve
tickets. It would also need `outstanding_findings` to grow a third state,
because `None` there currently *asserts* verification
(`service.py:8445-8451`).

**Leverage: level 6 (information flows).** It makes an existing loss
observable without changing what anyone is told.

**Second-order effects.** A session write on a path that is already out of
time. It is a single `add_message` and the deadline is a compose budget rather
than a hard request deadline, but confirm the write is not itself inside a
cancelled scope before shipping. If it is, record it on the route side after
the exception is caught.

**Measure.** Rows with `origin="compose_deadline_expired"`. If that count is
material, the ruling to reconsider is the compose budget, not the notice.

---

## C. The highest-leverage interventions

### C.1 Generalize the withhold seam so every exit passes through it — DO THIS

**Not a new subsystem.** `e93cfd643` and `ccd8b37b7` already built it:
`_persist_withheld_reply` (`service.py:3324-3352`) writing a
`role="audit"` row whose envelope comes from
`withheld_replies.withheld_reply_envelope` (`withheld_replies.py:34-44`), with
a closed `WithheldReplyOrigin` Literal carrying four members (`:26-31`) and
four live call sites (`service.py:3399`, `:5416`, `:6798`, `:7178`). The
module docstring states the design rule that makes it safe: it is deliberately
not a `composer_control_message`, because
`replay_composer_control_message` decodes control rows back into provider
context and a withheld reply must never re-enter it (`:9-15`). That is
confirmed by the reader: `role="audit"` is unconditionally excluded
(`sessions/routes/_helpers.py:1580-1581`).

**The generalization** is small and specific:

1. Make it a free function taking the sessions service, so the guided lane
   (`sessions/_guided_step_chat.py`) and the planner path can call it.
   `withheld_replies.py` is already a standalone module with no service
   dependency, so only the persisting half needs to move.
2. Extend `WithheldReplyOrigin` with the remaining exits:
   `compose_deadline_expired`, `planner_prose_nudged`,
   `guided_scaffold_leak`, `guided_shape_rejected`.
3. Call it at each exit identified in section B.

**Gate cost — this is the part worth knowing before committing.**
- Attribute contracts (`CONTRIBUTING.md:130-152`): **untouched**. No
  `getattr`/`hasattr` is introduced.
- Wire-shape templates (`CONTRIBUTING.md:272-307`): **untouched**, as long as
  no user-visible copy changes. This is the whole point of preferring a record
  over a republication — the expensive gate is the one that governs *notices*,
  and a non-rendered row is not a notice.
- Masquerade baseline (`CONTRIBUTING.md:188-210`): untouched, same reason.
- Soft-mapping census (`CONTRIBUTING.md:154-186`): **untouched unless the
  generalization introduces a soft mapping**. The current signature carries
  none — `_persist_withheld_reply` (`service.py:3324-3331`) takes `origin`,
  `content`, `session_id` and `session_operation_context`, builds the envelope
  internally, and `withheld_reply_envelope` returns `dict[str, str]`, which is
  not a counted form. A free function taking the sessions service plus those
  same parameters adds no site. If a future signature does grow a
  `Mapping[str, Any]`, the pin fails in either direction — re-pin with
  `python -m scripts.check_contracts --write-census` in the same commit.
- Sessions database mutation authority (`CONTRIBUTING.md:463`): every new call
  site must thread the turn's `session_operation_context`; the existing
  implementation already raises `TypeError` without it
  (`service.py:3343-3344`), which is the right shape to copy.
- Tests: expect the `test_advisor_checkpoint.py` envelope assertion
  (`tests/unit/web/composer/test_advisor_checkpoint.py:4555`) to gain
  siblings, and expect guided-lane tests to need the sessions fake to accept
  an `audit` write.

Files touched: roughly `withheld_replies.py`, `service.py`,
`sessions/_guided_step_chat.py`, `pipeline_planner.py` (or its service-side
caller), the census pin, and their tests. No frontend change — the row is not
rendered.

**Why this is the top intervention.** It converts an unobservable stock into a
measured one. Everything else in this document is a ruling that should be made
on evidence, and there is currently none: the companion review's "Not
measured" section says cases 1 and 3 "leave no row that says prose was
dropped". After this, one query — `composer_withheld_reply` rows grouped by
`origin` — prices every remaining decision in section B. **Leverage: level 6
(information flows)**, rising to **level 5 (rules)** if a test pins the set of
exits that may discard a reply without recording it, and **level 4** if that
pin is a whole-tree AST scan in the style the repo already uses.

**Do not over-build it.** No signing, no manifest, no sidecar, no approval
chain — `AGENTS.md` § Project delivery posture rules those out for a project
-internal record. A row and a Literal.

### C.2 Put a price on R2-F13 and ask John to rule — DO THIS SECOND

The thirteen pairs, the `prose_withheld` flag threaded through eleven
composers in `no_tool_policy.py`, the sticky
`advisor_repair_context_introduced`, the model-facing disclosure row
(`service.py:6804-6816`), the user-facing disclosure sentence, the AST gate
that keeps the pairs in sync, and four of the eight catalogued withholding
sites all exist to serve one goal: **raw advisor findings never reach the
user**.

The question is not "is that goal right". It is: *given that the same
findings' influence already reaches the user uncontained through the state
channel — as `service.py:10970-10972` states in the tree — is absolute
containment on the prose channel worth the structure it has grown?*

Three positions, so the ruling is a choice and not a leading question:

- **Keep R2-F13 as is.** Then the pairs are the correct price and section B's
  KEEPs stand. Nothing further to do beyond C.1.
- **Keep containment, publish a derived changelog.** The server reports
  `successful_mutating_actions` in place of the model's summary. Costs one new
  registered notice; delivers most of the lost operational value; still shifts
  the burden from the model's account to the server's.
- **Surface advisor findings to the user.** Then prose that quotes them is not
  a leak, `prose_withheld` collapses to a constant, and thirteen pairs become
  thirteen notices. This is **level 3 (goals)** and by far the largest
  structural saving available — and it is a product ruling with security and
  UX consequences this review is not positioned to make.

Note what this review deliberately does *not* claim: that any prior ruling
points toward the third option. It is offered as a priced alternative, not as
a direction the project has already chosen.

**Recommended sequencing.** C.1 first, unconditionally — it is cheap, it
violates nothing, and it produces the evidence. Then bring the counts to the
ruling in C.2 rather than the argument.

---

## D. What not to do

1. **Do not let the server synthesize a clarifying question, a decline, or a
   plan summary.** Composer invariant 1 is absolute: "ELSPETH must never
   synthesize, template, route, match, or otherwise derive pipeline structure
   server-side in place of the planner", banned "regardless of what it is
   called (sketch, recipe, router, fallback, fast path, synthesis)". A
   server-authored "did you mean…?" on the planner surface is exactly this.
   Case 2's fix is a *channel for the model's words*, never words the server
   wrote.

2. **Do not add a tutorial-only arm to any of this.** Composer invariant 2 and
   ADR-031. The guided lane is where the temptation lives, because the
   scaffold-leak failures were observed on tutorial sessions
   (`guided/chat_solver.py:220-224`). A defect visible in the tutorial is a
   defect in the composer.

3. **Do not add a prose field to `ComposerConvergenceError`.** Its attributes
   are frozen after construction precisely because the instance flows into the
   422 response body and, when `partial_state` is non-None, into the immutable
   `composition_states` audit table (`composer/protocol.py:474-482`). Carrying
   model prose there widens an audit contract to solve a recoverability
   problem that a session row solves for one line.

4. **Do not delete the prose nudge.** It was added for a measured reason:
   elspeth-b1e85829e9, "2/2 live repro", the model narrating instead of
   emitting at low reasoning effort, with the sticky-effort fix recorded at
   `pipeline_planner.py:4200-4215`. A `QUESTION:` channel sits beside the
   nudge; it does not replace it.

5. **Do not route any of this on a natural-language regex.**
   `no_tool_finalize.py:121-122` states the rule — "no regex on
   natural-language text governs the routing-shape decision" — and `:358-361`
   explains the one permitted use (content grounding, additive suffix, never
   gate routing). A "does this reply look like a question?" classifier is the
   tempting shortcut for case 2 and is banned by this rule as well as by
   invariant 1. The marker is a mechanical discriminator; a classifier is not.

6. **Do not "fix" case 5 by narrowing on the CLEAN re-review.** Section B
   gives the reasoning: it fails open on the exact case the rule exists for.

7. **Do not fold the withheld text into an exception message or a log line.**
   Case 8's note: `error_detail=str(exc)` at
   `sessions/_guided_step_chat.py:693` goes to structured logs, and
   `_safe_frame_strings` (`:348-375`) exists to keep Tier-3 text out of them.

8. **Do not add a fourteenth notice pair without pricing it.** Every new
   backend suffix costs a template, a hand-maintained round-trip case, a
   recognizer arm, a `_split_wrapped_diagnostic` arm and a `_PUBLISHED_` twin,
   and a missed registration "fails closed and publishes your operator notice
   as model prose, silently" (`CONTRIBUTING.md:289-291`). That is not an
   argument never to add one; it is an argument to count them.

9. **Do not treat this document as evidence of frequency.** No case here has a
   measured production rate. C.1 exists to change that.

---

## Appendix — measurements

All run at `e93cfd643` in this worktree. Each count is paired with a control
that must return nothing, per `AGENTS.md` § Claims Must Be Measured.

**A.1 — notice pairs and citation trail** (`src/elspeth/web/composer/no_tool_policy.py`)

```
grep -cE "^_[A-Z0-9_]*_PUBLISHED_[A-Z0-9_]*(: Final)? = " …   → 30
grep -cE "\+ \" \" \+ ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE" … → 13
grep -cE "^_ZZZ_NOT_A_CONSTANT = " …                          → 0   (control, exit 1)
grep -oE "elspeth-[0-9a-f]{10}" … | sort -u | wc -l           → 12
grep -coE "elspeth-[0-9a-f]{10}" …                            → 27
grep -coE "elspeth-zzzzzzzzzz" …                              → 0   (control, exit 1)
```

Registration tables read directly: `_ADVISOR_PUBLISHED_BARE_SUFFIXES` 13
entries (`:796-816`), `_ADVISOR_PUBLISHED_WRAPPED_SHAPES` 4 entries
(`:817-822`), `_AugmentationBranch` 8 members (`:699-708`).

**A.2 — ticket citations in `service.py`**: 44 distinct ids, same instrument
and control.

**A.3 — withhold seam**: 4 origins (`withheld_replies.py:26-31`), 4 call sites
(`service.py:3399`, `:5416`, `:6798`, `:7178`), found by
`grep -rn "_persist_withheld_reply" src/`.

**A.4 — scaffold guard call sites**: 8 (the definition at `:228` excluded), at
`guided/chat_solver.py:2782, 2970, 3034, 3121, 3694, 4135, 4696, 4955`. The
`:4955` site is the only one covered by `_ADVISORY_NO_TOOLS_ADDENDUM`, applied
at `:4895`; the other 7 are uncovered.

**A.6 — `prose_withheld` threading**: `grep -c "prose_withheld: bool"
src/elspeth/web/composer/no_tool_policy.py` → 11, control
`"prose_withheld: zzz"` → 0 (exit 1).

**A.5 — deadline scope check**: `assistant_message` bound at
`service.py:5875` before the `:5915` handler; a parameter at `:6006` for the
`:6230` handler; `session_operation_context` at `:6033`.

**Not measured, by name**: how often any of the eight cases fires in
production; whether real providers return `content` beside the terminal
proposal call; whether any user has ever lost a reply to the case-7 substring
match.
