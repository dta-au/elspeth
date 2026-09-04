# Agentic tool interface — making the composer's real contract legible at the point of decision

Date: 2026-09-04. Status: design, revision 2. Branch: `plan/agentic-interface`,
branched off `release/0.8.0` @ `cbae1ef0c`.
Revision 2 corrects revision 1's central factual error: revision 1 said the
rejection's instance facts are "computed and discarded". They are computed
into a purpose-built type AND projected to the planner; what is missing is
persistence. The correction is marked in place under §Layer 1 rather than
silently rewritten, because the wrong version shipped in `cbae1ef0c` and an
implementer may have read it.
Origin: operator framing during the `strany/tool-result-envelope` landing —
"the big win in this line of effort is getting the composer reliable tool
calls", with the north star stated as **agentic as a first-class interface**
and the bar set at **exemplar, not merely fixed**. A prose remedy in the
planner brief is explicitly out of scope as the primary answer.

Related tickets: `elspeth-e405ad7cd2` (tool-result envelope, landed — the
return-arrow exemplar this document mirrors), `elspeth-15c60e7c66`
(rejection instance facts, filed 2026-09-04), `elspeth-42f8e9e66f`
(prevalidation error-code flattening), `elspeth-15b400881f` (no
`guaranteed_fields` authored downstream of the source).

## THE FOUNDING OBSERVATION IS UNSOUND — read this first

**Measured 2026-09-04, and it retires the evidence this whole document was
built on.** `_candidate_shape_hash` does not distinguish what its name and its
`_value_free_shape` helper imply. Running it over a tutorial-shaped candidate:

| mutation | hash |
|---|---|
| rename a node id and its references | **same** |
| re-point a route (rewire `input`) | **same** |
| fix a sink alias | **same** |
| flip `schema.mode` fixed → flexible | **same** |
| **swap the plugin entirely** (`llm` → `coalesce`) | **same** |
| add a schema field | differs |
| add a node | differs |

`_value_free_shape` maps every string to the literal `"string"`. Stated
exactly, because the first draft of this paragraph overstated it and was
corrected by measurement:

- **Visible:** the `node_type` set (transform → gate DOES differ), the presence
  or absence of a key, node counts and sequence lengths.
- **Invisible:** plugin identity, node ids, route targets, sink aliases,
  `schema.mode`, and every other string VALUE.

So the hash is not structure-blind — it is IDENTITY-blind. It answers "is this
the same arrangement of the same kinds of things", not "is this the same
graph".

**Therefore "attempts 2 and 3 share a shape hash" means only: same node count,
same field count, same nesting.** It licenses NO inference about values versus
structure. Every claim in this document of the form "the first repair changed
only VALUES inside an identical structure" is withdrawn, as is every
downstream claim about what the model believed, assumed, or was shown. A
repair that renamed every node, re-pointed every route and swapped a plugin
would have been invisible to this instrument.

**It also inverts the reading.** `guided_reviewed_name_shadowed`,
`guided_route_target_unknown` and `guided_output_alias_collision` all prescribe
RENAMES or ROUTE SWAPS — exactly the edits measured invisible above. So a model
following good guidance can perform a large, correct, structural repair and
leave the hash unchanged: the signature read as blindness is equally the
signature of competence.

**Correction to the first version of this paragraph, which claimed those three
"mask to `validation_error`". They do not**, and the peer session caught it.
Each is constructed with an explicit `error_code=` AND a `connectivity={...}`
fact payload (`guided/planning.py:2794`, `:2814`, `:3048`) and each is
catalogued (`tools/generation.py:1103`, `:1114`, `:1125`). They announce
themselves properly.

That correction NARROWS the hunt rather than weakening it: whatever produced
`validation_error` at attempt #2 was therefore NOT one of these — they would
have shown their own code — so the population to chase is the codeless
rejection constructors. "An address without a fault" survives intact and now
has a smaller search space.

**Retracted with it:** the claim, committed earlier in this branch, that the
one-turn structural repair at attempt #4 is behavioural proof the contract
facts were forwarded. Both withholding branches are undiscriminated — this
time not because the record is missing, but because the instrument never had
the resolution that was assigned to it.

**What survives, each verified without reference to the hash:** the staged
validator against a repair budget of exactly 2; the
`guided_amend_contract_violation` violations computed and never sent;
`validation_error` as the default for a codeless rejection and
`explain_validation_error` disclaiming it; and the withholding rule, which is
a property of the predicate, tested directly, and never depended on the walk.

**The method failure, stated because it is the fourth in one investigation:**
`_candidate_shape_hash` and `_value_free_shape` read as "structure-preserving,
value-dropping". Two agents reasoned from that reading for a day. Nobody ran
the function. A name is not a specification, and a hash's resolution is a
property you MEASURE, never one you infer from its identifier — see
[[derive-a-log-columns-semantics-from-its-writer]], of which this is the
sharper case: it is not just the column's semantics but the function's
discriminating power.

## Superseded: the misattribution correction that preceded it

*Retained because it shipped, and because its structural finding survives.*
The LLM-specialist seat first found a misreading of WHICH attempt the audit's
code belongs to. That correction stands on its own terms and is recorded
below; its conclusions about values-only repair do not, per the section above.

**1. The attempt audit's shape hash records the candidate SUBMITTED at that
attempt, not the one the rejection was answering.** `pipeline_planner.py:3915`
parses the terminal tool call, takes `arguments["pipeline"]`, hashes it, and
passes it to `trail.begin_attempt`. So attempt #3's code is the code the
values-only repair EARNED, not the code it was answering.

Re-reading the walk correctly:
- #2 submitted a candidate → rejected `validation_error`
- #3 submitted a values-only edit ANSWERING `validation_error` → earned
  `locked_input_extras`
- #4 repaired `locked_input_extras` **structurally, in one turn, accepted**

**2. `locked_input_extras` was therefore never the problem, and its facts were
forwarded.** `web_scrape` is a transform, so it is model-authored, so
`withholding.contract` is False — and the one-turn structural repair is the
behavioural proof. The withholding question this document spends three
sections on is settled, and settled the way the mechanism predicted. Both
branches are no longer live; the forwarded one is confirmed by behaviour.

**3. The wasted turn was `validation_error` — a rejection carrying no repair
information at all.** A rejection constructor that passes no `error_code`
resolves to the literal string `validation_error` (`pipeline_planner.py:2101`,
`:2165`, `:2315`, `:2446`), and the entry the model receives then carries
component, severity, `error_code` and `error_class` and nothing else. The
envelope advertises `explain_validation_error`, which for such a code replies
that it "does not match any known validation message or closed error_code"
(`tools/generation.py:1862`) — the server disclaiming its own token.

The seat's formulation is the one to keep: **a values-only edit is correct
inference from an address without a fault.** The model was told WHERE and not
WHAT, so it changed the only thing an address licenses you to change.

### The structural defect this exposes

Graph validation is unreachable while any component rejection stands.
`tools/sessions.py:1619` returns the collected component failure before spec
construction, with the reason stated in the code: whole-state checks need a
COMPLETE component set or they report artefacts. Validation is therefore
STAGED, and the staging is invisible to the model.

`composer_planner_repair_budget` defaults to exactly **2**
(`web/config.py:312`).

**So a candidate carrying one Stage-1 defect and one Stage-2 defect costs two
repair turns BY CONSTRUCTION, against a budget of two. Zero margin.** No
teaching change and no instance-fact change alters that arithmetic. This is
the defect the tutorial's ceiling was actually measuring, and it is an
interface-shape problem of exactly the kind this document is about: the model
cannot see that it is being validated in stages, so it cannot know that fixing
everything it was told about still leaves a second gate it has not been shown.

### What this costs the rest of the document

The three-layer architecture below stands, but its motivating example does
not. Layer 1 (persist what was projected) keeps its own justification — the
record IS lossy — but must no longer be sold on the tutorial's repair thrash.
The `guided_amend_contract_violation` finding two sections down is unaffected
and remains the strongest concrete instance: facts computed, tested,
documented as the repair payload, and never sent.

## Problem

The composer asks the planner to satisfy a contract it is never shown.

Two different contracts govern a pipeline proposal:

1. **The declared contract** — each tool's `json_schema`, describing that
   plugin's own options. Static, honest, and visible to the model.
2. **The accept/reject contract** — inter-node field agreements. Does the
   consumer accept the fields the producer emits? That lives in the *graph*
   and appears in no tool definition.

The model is shown (1) and judged on (2). The refusal's *persisted* record
names a code and nothing else; whether the model itself was shown the facts
behind that code is, today, unrecorded — see the layer-1 correction below,
which is the difference between two very different defects.

### Measured evidence

A tutorial walk on `release/0.8.0` @ `51b43a770` (measured by a peer session
2026-09-04, `tutorial_run_id` `66907bb0-4433-4d0e-882c-1a5a7600cc1f`)
graduated — 12 transitions, terminal `completed`, 3/3 substantive rows, 0
discarded — while taking four planner attempts against a ceiling of two:

| # | phase | outcome | codes | candidate shape hash |
|---|---|---|---|---|
| 1 | discovery | `discovery_executed` | — | — |
| 2 | candidate | `candidate_rejected` | `validation_error` | `a63068f5a5` |
| 3 | repair | `candidate_rejected` | `locked_input_extras` | `a63068f5a5` |
| 4 | repair | `accepted` | — | `49f300e14a` |

Attempts 2 and 3 **share a candidate shape hash**. That hash is value-free
by construction — `_candidate_shape_hash` (`pipeline_planner.py:1094`) hashes
`_value_free_shape` (`:1066`), which keeps kinds, key layout and sequence
sizes and drops every value. So the first repair changed only *values* inside
an identical structure, and earned a different code for it; the structural
change that satisfied the contract came only on the second repair.

That is the signature of correct hypothesis-elimination against
under-specified feedback. The model was told "invalid" by a contract whose
shape-level requirement it could not see, so it assumed a value was wrong —
because values are what it was shown. It is not a weak-model problem.

### Why the feedback is under-specified

`locked_input_extras` (`state.py:5423`, code string `:5465`) is one code
covering a family of violations: a consumer whose input schema is locked
received fields the producer emits and it does not accept.

*Which* field, from *which* producer, against *which* locked consumer, **is**
computed — into `SchemaContractDetail` — and **is** projected to the planner
at `pipeline_planner.py:2541`, subject to a `withholding.contract` custody
check. What no downstream reader can recover is what was actually sent: none
of it is persisted. So "the feedback is under-specified" is a hypothesis this
document cannot yet assert, and the layer-1 correction below states the
measurement that would settle it. The claim that survives without that
measurement is narrower and still sufficient to act on: the *record* is
under-specified, and that is why nobody can tell.

Separately, `_prevalidate_transform_for_context` (`tools/_common.py:3278`)
does not validate against the real graph despite its name. It constructs a
**synthetic** `CompositionState` — a fabricated `csv` source with
`schema: {mode: observed}` feeding the candidate transform — and validates
the plugin's options in isolation. A call therefore passes prevalidation on
contract (1) and dies later on contract (2), with the error surfacing at
composition rather than at the call that caused it. It also flattens its
result to a single string (`:3338`), discarding the structured
`error_code`/`message` split that `_failure_result` already accepts
(`:1243`, `error_code` parameter at `:1247`) — the subject of
`elspeth-42f8e9e66f`.

## The sharpest instance: facts computed, tested, documented — and never sent

**Found by the systems-thinker seat 2026-09-04, verified independently before
being recorded here.** This displaces `locked_input_extras` as the headline,
and it is a cleaner defect than anything above because nothing about it is
uncertain.

`_bind_guided_revision` (`guided/planning.py:3116`) computes one fact record
per amend-contract breach — `_record(kind, **facts)` at `:3223`, nine kinds,
in discovery order. The dataclass docstring (`:330`) states their purpose
outright:

> "the facts are what let a repair name the offending node instead of
> re-guessing the whole contract"

and pre-clears their custody in the same breath: "Every value is either a node
id the provider authored or already sees in `current_state`, a closed
violation kind, or an option/field KEY — never a reviewed option value."

They are computed. They are unit-tested. They are documented as the repair
payload. They are custody-cleared by their own author.

**And they are dropped one line after they are returned.**
`service.py:4156` reads `pending_revision_rejection = binding.rejection_code`
— the code alone. `GuidedRevisionBindingResult.violations` has no Python
consumer anywhere in `src/` (verified by grep; the only other `.violations`
hits are an unrelated class in `contracts/declaration_contracts.py` and
frontend harness TypeScript). What the model receives instead is built at
`pipeline_planner.py:2027`: `component="pipeline"`, and the fixed sentence

> "The candidate did not satisfy a surface-specific semantic obligation."

That is not a persistence gap and not a custody decision. The facts never
reach the wire at all, and the sentence that replaces them names neither the
node, nor the kind, nor the field.

**This also corrects the "one code over nine kinds" framing** used earlier in
this document and in `elspeth-15c60e7c66`. The nine kinds belong to
`guided_amend_contract_violation` (`rejection_code: Literal[...]`,
`guided/planning.py:340`), NOT to `locked_input_extras` — which is a narrow
single-rule code that already carries its facts in `SchemaContractDetail`.
Both the ticket and this spec inherited the misattribution.

**Consequence for the layering below.** Layer 1 was scoped as persistence.
For THIS defect persistence is not the fix and would not help: the remedy is
to project the violations the binder already computed, on the custody terms
its own docstring already establishes. It is the same "carry the fact
structurally" discipline, at a surface where the fact is not merely lossy in
the record but absent from the conversation.

## The pattern already exists in this codebase, at two surfaces

This design is not proposing a new mechanism. It is proposing that a proven
house pattern be applied to the surface that lacks it.

### Exemplar A — the tool-result envelope (landed, `elspeth-e405ad7cd2`)

`tests/unit/web/composer/test_tool_result_envelope_gate.py` walks the AST,
derives every `data.*` key the composer's tools actually ship to the model,
and requires each to be either **taught** or **explicitly fenced**. An
unaccounted key fails the build. The return arrow — what the model receives —
is a first-class, mechanically-enforced interface.

### Exemplar B — the durable rejection carrier (landed, `elspeth-3e28029d2f`)

Schema epoch 49 added `composition_rejection_events`
(`sessions/models.py:1333`), described in the epoch log as a "durable
session-side record of composer mutation-tool rejections — the unredacted
reason the planner saw, keyed to session + the composition state current at
rejection". Operator ruling 2026-09-02: session data, not Landscape data.

Its columns are exactly the right shape: `tool_call_id`, `tool_name`,
`error_code`, `message`, `planner_payload`. Its single insert site
(`sessions/service.py:6611`) persists a refused mutation's reason unredacted
alongside its redacted tool row, linked to `current_state_id` because a
rejection commits no state of its own.

**The gap:** that path matches `record.tool_call_id` against a refused
*mutation tool* row. The planner's repair loop rejects candidates inside
`pipeline_planner`, not through the session mutation-audit path — so the
rejected `emit_pipeline_proposal` candidates never reach the carrier. The
peer measurement found zero rows for all four attempts above, and confirmed
the rejected candidates are absent from the planner attempt audit entirely;
only the single accepted terminal call is recorded.

So ELSPETH built the right durable carrier, ruled on it at operator level,
and shipped it — and the surface that most needs it does not write to it.

## Architecture — three layers

Ordered by ambition. Each is independently deliverable and each is a strict
improvement; layer 3 is the exemplar move.

### Layer 1 — persist the rejection facts that were already sent

**Revision 1 correction, measured 2026-09-04 after first publication.** The
framing below originally said the instance facts are "computed and
discarded". That is wrong, and wrong in a way that would send an implementer
to build a carrier that already exists. The corrected statement:

- The facts **are** computed, as a purpose-built structured type.
  `SchemaContractDetail` (`state.py:1246`) carries `producer`, `consumer`,
  `missing_fields`, `extra_fields`, and its docstring states the exact
  rationale this document argues for: "A bare `schema_contract_violation` is
  not repairable within the repair budget: the planner must know WHICH edge
  failed and WHICH fields are missing." `_locked_input_extras_error`
  (`state.py:5423`) populates it.
- The facts **are** projected to the planner. `pipeline_planner.py:2541`:
  `if entry.contract is not None and not withholding.contract:` →
  `projected["contract"] = entry.contract.to_dict()`.
- The facts are **not persisted**. Measured on tutorial run
  `a9767adc-523e-4f6c-a8d4-f4f78776bb95`: `locked_input_extras` occurs
  exactly ONCE in the whole session database, in the `planner_attempt_audit`
  envelope's `rejection_codes` array. `composition_rejection_events` has zero
  rows. The repair loop runs inside a single `compose()` call and its
  intermediate tool results never become chat messages.

So the observability gap is **narrower and sharper** than first written: we
cannot tell what the model was shown, because what it was shown is not
recorded. That is a persistence defect, not a derivation defect.

**ANSWERED IN PART, 2026-09-04 — the rule is determinate and now pinned.**
The question "was `withholding.contract` set?" has no single answer: it is
**edge-dependent**, and the rule was measured by exercising
`_allowlisted_candidate_feedback` directly and pinned in
`test_schema_contract_detail_withholding_follows_the_participants_not_the_entry`.

| failing edge | contract detail |
|---|---|
| model-authored → model-authored (`llm` → `field_mapper`) | **forwarded**, `extra_fields` included |
| producer is the guided-reviewed **source** | **withheld** |
| consumer is the reviewed **sink** | withheld — but by the entry-own check, not the participant arm |
| entry component unattributable | withheld (fail-closed) |

`_entry_withholding` (`pipeline_planner.py:2286`) sets `contract` when the
entry's own component is config-owned OR when any participant of the contract
is (`_contract_participant_refs`). `_derive_finalizer_owned_refs` (`:853`)
marks a component config-owned by structural diff of authored against
finalized, so in the guided lane the reviewed source and sink are owned and
the model-authored transforms are not.

**One narrowing that follows, and one that does NOT.**

The narrowing that holds: `locked_input_extras` is emitted only for NODE
consumers — sinks emit `sink_locked_extras`, a different code, from
`_sink_locked_extras_error` (`state.py:5474`) — so the only way this detail is
withheld is a **source producer**.

**A second narrowing was attempted here and is WITHDRAWN.** Revision 2 argued:
the extras are `web_scrape`'s six emitted fields, so `web_scrape` is the
producer, so the producer is model-authored, so the detail was forwarded — and
therefore the planner was shown `content` and `fingerprint` by name and the
work moves to the brief. That chain breaks at its first step, and the break was
found by the peer session that supplied the evidence.

The field-set reading comes from the **accepted** graph, attempt #4. Attempts 2
and 3 carry candidate shape hash `a63068f5a5` against the accepted `49f300e14a`,
and `_candidate_shape_hash` is value-free — so the rejected candidates
demonstrably had a different TOPOLOGY, not merely different values. If the
second repair is what introduced `web_scrape`, then on attempts 2 and 3 the
locked consumer's producer may have been the reviewed `csv` source directly,
which IS config-owned, which withholds. The observability branch is fully live.

This is worth naming as a method failure and not just a wrong answer: it is a
claim about the ACCEPTED candidate extended to the REJECTED ones, which is the
same class of error as revision 1's — a measurement of one thing carried into a
conclusion about another. That the ticket's headline finding is *precisely
that the rejected candidates differed structurally* makes it worse, not better.

**Corrected position: both branches are live and neither is favoured.** What
survives is the RULE, which is determinate and pinned. What does not survive is
any claim about which arm applied on this walk. The topology of the rejected
candidates is not merely uncertain, it is unrecoverable from persisted data —
so the persistence work is not one way of settling this, it is the only way,
and it must persist the projected detail AND the rejected shape.

The two branches, neither favoured: as written below.

- **Withheld** — the model was blind to `extra_fields` and the defect is
  custody scoping. Note the standing hazard: the `_allowlisted_candidate_feedback`
  docstring records that the predecessor candidate-global predicate "made
  guided repair permanently blind — the guided binder ALWAYS mutates the
  candidate" (elspeth-5904b1683a). A per-entry successor can still withhold
  too much.
- **Forwarded** — the model received the offending field names and still
  needed a further turn. Then this is not an observability defect at all, and
  layer 1 buys nothing; the work moves to the planner brief and to layer 3.

**Precedent for the withheld branch, already adjudicated once.** Run
`06c9ec49` (2026-07-29) is the same failure shape at a different code:
withholding the validator's message made an exactly-repairable
`plugin_options_invalid` rejection unrepairable — the planner "burned every
repair on the static enrichment's profile-alias hypothesis, and declined with
a confabulated cause — twice, in two sessions." The remedy was to forward the
detail, on the custody argument that the planner already holds that content
verbatim. `SchemaContractDetail` was built with the same argument
pre-made ("forwarding them does not re-open the message redaction
boundary"), so if it is being withheld here, the precedent is directly on
point.

The work, restated: persist what was projected, so the question above is
answerable from data on the next walk rather than from code reading. The
durable carrier for it exists (Exemplar B).

This is the same discipline as
[[stop-parsing-carry-the-fact-structurally]]: three defeated parses mean the
fact should have been carried, not re-derived. Here the fact IS carried on
the wire and is simply never written down, so the defeated party is the later
reader rather than the model — which is why the remedy is persistence, not
derivation.

**Expected effect on the measured walk — conditional, and the condition is
the unresolved one.** IF the contract detail was withheld, attempts 2 and 3
collapse to one: the model spent a turn eliminating the value hypothesis
because nothing told it the problem was structural. IF it was forwarded, this
layer changes nothing about the walk and only makes the next walk
diagnosable. Do not quote the first branch as the expected benefit until the
withholding question is answered — that is exactly the overstatement this
document's own "not verified" discipline exists to prevent.

### Layer 2 — the error arrives at its cause

`_prevalidate_transform_for_context` should validate against the real
composition state, not a synthetic single-node fabrication. A call that will
be refused at composition should be refused at the call, where the model can
still attribute the failure to the argument it just chose.

This subsumes `elspeth-42f8e9e66f`: once prevalidation is contextual, the
flattened `str | None` return is plainly the wrong carrier, and the
`(code, message)` pair that `_failure_result` already accepts becomes the
obvious shape.

### Layer 3 — representable implies valid

The terminal tool's argument schema should exclude what the server will
reject.

**The seam already exists and is already threaded.**
`planner_terminal_tool_definition` (`pipeline_planner.py:1438`) injects
`deep_thaw(selected.schema)` as the `pipeline` parameter, taking a
`PlannerTerminalContract` (`:931`). `planner_tool_definitions` (`:1461`)
plumbs `terminal_contract` through and additionally selects the discovery
subset from `policy.discovery_tool_names`. The tool spec is already
understood to vary: `_assert_planner_call_matches_manifest` hashes
`tools_spec_hash` and fails closed if it shifts mid-call — a guard nobody
writes for a constant.

Today the contract is always `canonical_planner_terminal_contract()` (`:958`),
a fixed full-document schema. Deriving it from the live graph's field
contracts puts the constraint in the type rather than in a paragraph, which is
what "first-class interface" means: the model cannot emit a proposal that
violates a locked input schema, because the schema will not encode one.

## The boundary that keeps layer 3 legal

Stated explicitly, because an unbounded version of layer 3 violates composer
invariant 1 ("the LLM does the job; no composer path bypasses the provider").

> The terminal schema may encode **constraints** the server would enforce
> anyway — locked input schemas, accepted field sets, declared field
> contracts. It must never encode **choices** — which transform, what order,
> what prompt, what model.

Narrow the schema past that line and the server has authored the graph, which
is banned regardless of what the narrowing is called. The distinguishing test
is *constraint vs. choice*: a constraint removes proposals the server would
have refused; a choice removes proposals the server would have accepted.

Per AGENTS.md the invariants explicitly permit server-side *validation,
rejection, and redaction* of what the planner produces. Layer 3 is rejection
moved earlier — from after the call to the shape of the call — and it is
legal exactly and only while it stays on the constraint side of that line.

**An exemplar ships this test as a gate, not as a comment.** The natural form
mirrors Exemplar A: derive the set of constraints the terminal schema
narrows, and require each to correspond to a server-side rejection rule that
would have fired anyway. A narrowing with no corresponding rejection rule is
a choice wearing a constraint's clothes, and fails the build.

## Verified, and not verified

Verified in-tree on `strany/tool-result-envelope` @ `b74783a5e`, 2026-09-04:

- Tool *declarations* are static. `ToolDeclaration` is a frozen dataclass;
  `name` / `description` / `json_schema` are fixed at the declaration site and
  the derivation helpers are pure functions holding no module state — called
  out as deliberate at `tools/declarations.py:247` to avoid an import-order
  trap. The operator's initial framing ("tools with arguments changing") is
  therefore not true at the declaration layer; the variance is real but lives
  in the terminal contract and the discovery subset.
- The terminal tool's `pipeline` parameter schema is request-selected
  (`:1438`–`1451`).
- `composition_rejection_events` exists, is durable, has exactly one insert
  site, and is not reached by planner candidate rejections.
- `_prevalidate_transform_for_context` builds a synthetic state.

Not verified, and not claimed:

- **Whether the planner brief already teaches the field-contract rules in
  prose.** It may partly close the legibility gap. This must be measured
  before layer 1 is scoped, or the layer-1 benefit will be overstated.
- **Whether the varying terminal schema is causally implicated in any
  observed failure.** The `tools_spec_hash` guard suggests the risk was
  considered. No measurement links it to the repair thrash.
- **Whether the values-only repair at attempt 3 is causally about
  `guaranteed_fields`.** The peer session was explicit that shape-hash
  equality plus the accepted graph's `field_mapper` options is *consistent
  with* that story and does not isolate it. With
  `composition_rejection_events` empty there is no diff that could settle it —
  which is itself the argument for layer 1. Anything stronger than "consistent
  with" in a ticket comment is an overstatement.

## Sequencing

Layer 1 first, and not only because it is cheapest. It is the layer that makes
the other two measurable: until rejections carry instance facts, neither the
effect of contextual prevalidation nor the effect of a narrowed terminal
schema can be observed in the data. `elspeth-15c60e7c66` filed the
observability half as **blocking** the diagnosis of the repair half rather
than as a nice-to-have beside it, which is the correct dependency direction.
