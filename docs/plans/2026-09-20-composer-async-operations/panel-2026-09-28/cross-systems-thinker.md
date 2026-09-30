# Cross-examination — systems thinker

Panellist: systems thinker. Tree: `release/0.8.1` @ `6506f6a7f`, read-only except
this file. Protocol: `meta-sme-protocol:sme-agent-protocol`. Opening position:
`position-systems-thinker.md` — **B** (moderate-high), a separate
`composer_async_operations` table with a parameterized shared codec.

## A fact I missed, verified first

Five other panellists (leverage, solution-architect, tech-critic, sre,
db-architect) independently flagged that my opening position — and the
brief itself — never mentions `message_ingress_receipts`. I verified this
directly rather than taking the panel's word for it:

- `src/elspeth/web/sessions/models.py:464` — `message_ingress_receipts_table`,
  PK `(session_id, client_request_id)`, `UniqueConstraint("user_message_id")`,
  immutable (no update/delete triggers per the panel's trigger inventory).
- `src/elspeth/web/sessions/schemas.py:154` — `SendMessageRequest.client_request_id: UUID`,
  a required field on the exact route (`POST /messages`) this plan is adding
  a second client-minted id (`operation_id`) to.
- `src/elspeth/web/sessions/schemas.py:162-165` — `RecomposeRequest` has
  **only** `expected_user_message_id: UUID`. It inherits from `_RequestModel`,
  not `_SessionOperationRequest`, and has no `operation_id` field today. (I
  confirm the panel's own fact-corrections here are accurate — nobody
  actually claimed it carries both; I misread it on first pass and re-verified
  before writing this file.)
- `_SessionOperationRequest` (`schemas.py:72-77`, the strict base carrying
  `operation_id`) is used today by exactly two classes:
  `ForkSessionRequest` and `RevertStateRequest` (`schemas.py:367,400`) —
  fork/revert only. Neither `SendMessageRequest` nor `RecomposeRequest` uses it.
- Introduced by `8630db9b8`, 2026-09-27 — one day before this panel convened,
  in the same epoch-69→71 series that built the receipts table itself.

This is correct and material. I'm folding it into my leverage-point analysis
below rather than treating it as a footnote, because it changes which
leverage point is highest in the system, not just which facts are cited.

## Cross-examination

### vs. python-architect (C, high) — the position that most differs from mine

python-architect's structural argument for a separate table is identical to
mine (takeover-vs-never-takeover, `OperationReceiptTakenOver` at
`operation_receipts.py:314-353`) — no daylight there. The divergence is
narrower: whether the *codec* should be shared (my B) or duplicated
byte-for-byte (their C), on the ground that "even reusing the receipts
machinery buys no code reuse there... the entire authority surface is net-new
regardless of which table backs it."

That's true of the *authority* (claim/renew/settle machinery) — I don't
dispute it, and neither does anyone else on the panel. It is not true of the
*codec*, which is a much narrower unit: a strict+forbid check, an
`exclude={"operation_id"}` dump, and a `stable_hash({schema, session_id,
kind, request})` envelope. Two independently-maintained implementations of
"canonicalize a client-minted request the same way" is exactly the failure
mode this repo has already paid for once: a rule expressed identically in
two places that quietly diverges (the one-element-`IN` PostgreSQL CHECK
reflection defect the brief itself cites, BRIEF.md:57-58 — not the same bug,
but the same *class*: two nominally-identical rule-copies with no structural
mechanism forcing them to stay identical). In Meadows terms, a shared
canonicalization function is a Level-6 (information flow) fix — one place
that knows the rule. A duplicate codec plus a pinned golden-vector test (what
several B′ proponents also require) is a Level-5 (rules) fix — two places
that must independently keep agreeing, checked only if someone remembers to
run the test and nobody "improves" one side without noticing the other.

**Partial concession**: the practical gap here is narrower than my opening
position implied. If a golden-vector byte-identity test is mandatory
regardless of B or C (and most of the panel treats it as mandatory either
way — even the shared-codec camp wants a pinned fixture), then C's actual
drift exposure is bounded by that test, not by convention alone. I still
prefer B (a genuinely shared, parameterized function) because it removes the
chance to diverge at the point of the next edit rather than catching it after
the edit lands, but I no longer treat this as a first-order risk on the same
scale as the table-separation question. It's a real but secondary point of
disagreement, and reasonable people (this panel is proof) land on either side
of it once table separation is settled.

### vs. the identity-unification finding (leverage, solution-architect,
tech-critic, sre, db-architect) — this is the point that changes my mind

I did not consider `message_ingress_receipts` in my opening position at all.
Having verified it's real (see above), the systems reading is not "add one
more fact to the pile" — it's that the pattern I flagged as a *future* risk
in my own "strongest argument against B" section is already underway:

> "if the answer is 'give it its own table' every time, ELSPETH accumulates
> N structurally similar tables... a maintainability tragedy-of-the-commons
> on the schema-gate surface."

`message_ingress_receipts` (epoch 69) and `session_operation_receipts`
(epoch 71) are two bespoke, independently-built client-minted-identity
facilities, built in the same short window, with different replay semantics
(ingress: reject an exact duplicate with 409; receipts: take over and
re-attempt) and zero shared code between them — the ingress writer
(`service.py:1396-1420`) is a plain PK-conflict insert with no request-hash
codec at all, structurally simpler than, not a variant of,
`operation_receipts.py`'s strict-DTO hashing. Adding a third
(`composer_async_operations`, with its own `operation_id`) without first
asking "is this the same identity as the one already sitting on this exact
route" is not a hypothetical 12-18-month drift — it is choosing to write the
third instance of a pattern that has already independently repeated once,
one day before this panel sat down to discuss it. That is the commons
dynamic from my opening position's "strongest argument against B" — not the
R1 Success-to-the-Successful loop, which is about traffic skewing one
shared table's shape, a different archetype — already measured, not
projected.

This re-ranks my own leverage-point table. I filed "shared codec" under
Level 6 as a nice-to-have alongside the Level-10 table-separation decision.
It is not a nice-to-have: resolving whether `operation_id` and
`client_request_id` name the same request is the single highest-leverage
open question in front of this plan, because it is upstream of both the
table decision (a compose row keyed on a second, unrelated id sitting next
to an ingress receipt keyed on the first id, on the same route, is dual
acceptance under the owner's own "no dual acceptance" doctrine) and the
codec decision (there is nothing to canonicalize-the-same-way if the two
sides don't agree what "the request" identity even is). I adopt this as a
precondition on my final position, not an addendum.

### vs. audit-integrity (B, high) and sre (B, moderate) — the terminal-row
forensic-nulling disagreement

These two don't disagree with me on the table question, but they disagree
with each other on a real point my own archetype analysis bears on:
audit-integrity wants a compose event log (citing ADR-046 evidence-loss
risk from `claim_owner_instance_id` being nulled at settlement); sre argues
that's unnecessary ceremony because the turn's real audit trail lives in
`chat_messages`/`llm_calls`, and claim churn is "transport telemetry, not
audit evidence."

I verified the underlying claim independently rather than taking either
side's word for it:

- `src/elspeth/web/sessions/models.py:320-324` — the comment on
  `session_operation_fences_table`, the structurally nearest existing table
  (also a claim/fence row, also has an `owner_instance_id`), states as a
  design rule: *"Release never nulls forensic authority; `released_at` alone
  discriminates an inactive row."*
- `docs/plans/2026-09-20-composer-async-operations/T03.md:834-849` — the
  actual `ck_composer_async_operations_status_bundle` CheckConstraint. Its
  `completed` arm (:843) and `failed` arm (:847) both apply
  `_COMPOSER_ASYNC_OPERATION_CLAIM_IS_NULL` (defined :716, `"claim_token IS
  NULL AND claim_owner_instance_id IS NULL AND claim_expires_at IS NULL"`),
  so the claim triple — including `claim_owner_instance_id` — is required
  null on every settled row. `attempt` is a separate, non-nullable column
  (:744) that the bundle does not touch and is not reset at settlement; my
  first draft of this file wrongly folded it into the same claim I make
  about `claim_owner_instance_id` and has been corrected.

That is a direct contradiction of the precedent this exact codebase already
set for the nearest analogous table, sitting in the plan's own current
draft. This is not audit-integrity's speculative future risk and not sre's
"unnecessary ceremony" — it's my own Eroding-Goals archetype, materialized
today, in T03 as written, before compose has shipped a single line. The
mechanism is exactly what I described in my opening B1 loop: a developer
extending a shared *pattern* (claim/lease columns null at terminal is the
majority shape elsewhere in this schema — receipts' own `lease_token`/
`lease_expires_at` are required NULL on `completed`/`failed`,
`models.py:794-801`) under the
gravity of "that's how the similar-looking thing nearby does it," without
noticing the one sibling table that deliberately does the opposite for a
stated forensic reason.

I side with audit-integrity on the narrow fix, not the broad one: T03 should
stop nulling `claim_owner_instance_id` at terminal (the `completed`/`failed`
arms of `ck_composer_async_operations_status_bundle`, T03.md:843,847),
matching `session_operation_fences`' established convention, so "who last
claimed this row" survives settlement. `attempt` is unaffected either way —
it's a non-nullable counter the bundle never nulls. I don't go as far as
recommending
audit-integrity's full parallel event-log table — sre's point that
turn-level audit evidence already has a durable home in
`chat_messages`/`llm_calls` is a reasonable scope boundary, and a full
append-only event log is a materially bigger T03/T04 commitment than a
two-column nulling change. The narrow fix closes the live archetype
instance; the broad fix is a separate, larger design question this panel's
charge doesn't obviously require settling today.

### vs. codex ("Forwarded")

Not a design position to rebut — it reports that the task was forwarded to
a background Codex run and returns no opinion on A/B/C/D. Nothing to
cross-examine; flagged for completeness only.

## Points that changed my mind

1. **`message_ingress_receipts` re-ranks my own leverage-point analysis.**
   I filed shared-vs-duplicate codec as a secondary Level-6 concern under a
   primary Level-10 table-separation decision. Verifying that a second,
   independently-built client-minted-identity facility already exists on
   the same route this plan is touching moves identity unification (is
   `operation_id` the same key as `client_request_id`?) to the top of the
   leverage stack — it is upstream of both the table and codec questions,
   and the Success-to-the-Successful/commons pattern I flagged as a future
   risk is empirically already one cycle in.
2. **The Eroding-Goals archetype I described as a 12-18-month, deadline-
   pressure risk is already visible in the plan's current draft**, not
   only in a hypothetical future edit to `reserve_operation_receipt`.
   `session_operation_fences`' own "release never nulls forensic authority"
   precedent (models.py:320-324) is contradicted by T03's current terminal
   CHECK bundle (T03.md:716-818). This doesn't change which option I
   recommend, but it changes the urgency and concreteness of one plan
   consequence.

## Points I maintain against the panel

- **B over A remains unchanged and, if anything, strengthened.** Nobody on
  the panel — including python-architect, whose codec position (C) differs
  most from mine — argued for A. The takeover-vs-never-takeover structural
  conflict (`operation_receipts.py:316-353` vs. the spec's "never taken
  over" bar) is independent of the identity-unification question above:
  even after `operation_id`/`client_request_id` are reconciled, receipts'
  unmarked-default-is-takeover behavior and compose's must-never-take-over
  requirement still cannot cohabit one CHECK bundle without a kind-
  conditional a future editor must remember. That argument is untouched by
  anything raised against it. It has a second, independent leg I verified
  but did not use in my opening position: `decide_and_soft_archive`
  (`repository.py:722-734`) raises a Tier-1 `AuditIntegrityError` for any
  in-progress receipt whose kind is outside `{"session_fork",
  "state_revert"}`. Under A, a live compose row makes archive a hard
  failure, contradicting the plan's own D7 ("no archive refusal"). Under B,
  archive never sees a compose row at all, and D7 holds by construction —
  the same structural-vs-conditional shape as the takeover argument, on a
  second invariant.
- **B over C, on the codec, with the concession above already folded in.**
  A shared, parameterized canonicalization function is a Level-6 fix; a
  duplicate codec plus a pinned conformance test is a Level-5 fix that
  depends on the test being kept in the loop of every future edit to either
  side. I still prefer B but no longer treat this as first-order relative
  to the table-separation and identity-unification questions.

## Final position

**B′**: a separate `composer_async_operations` table (unchanged from my
opening — the takeover invariant becomes structural, Level 10, rather than
a conditional inside a shared reservation function, Level 5), with two
amendments I now treat as preconditions rather than nice-to-haves:

1. **Before T02**, resolve whether `operation_id` is the same key as the
   existing `message_ingress_receipts.client_request_id` on the same route.
   This is the highest-leverage open question in the plan — it is upstream
   of both the table shape and the codec question, and the alternative
   (three independent client-minted-identity facilities on overlapping
   routes with no shared rule) is the commons dynamic I flagged as a future
   risk, now visibly already one cycle underway.
2. **In T03**, do not null `claim_owner_instance_id` on a terminal compose
   row (the `completed`/`failed` arms of
   `ck_composer_async_operations_status_bundle`, T03.md:843,847). Match the
   "release never nulls forensic authority" convention
   `session_operation_fences` already established (models.py:320-324) for
   the structurally nearest table in this schema. `attempt` is unaffected —
   it is a non-nullable counter the bundle never touches.
   This closes a live instance of the Eroding-Goals archetype in the
   current draft, not a projected future one.

On the codec (B vs. C): prefer a shared, parameterized canonicalization
function over a duplicate codec, but treat this as a secondary,
bounded-risk disagreement with python-architect once a golden-vector
conformance test is mandatory either way — not a reason to withhold
agreement with the rest of the B′ camp.

---

## Confidence Assessment

**Overall Confidence:** Moderate-High, unchanged in direction from the
opening position, strengthened on the identity-unification point by direct
verification.

| Claim | Confidence | Basis |
|---|---|---|
| `message_ingress_receipts` exists as described, independent of receipts | High | Direct read, `models.py:464-487`, `schemas.py:154`, commit `8630db9b8` confirmed via `git log` |
| `RecomposeRequest` has only `expected_user_message_id`, no `operation_id`, and does not inherit `_SessionOperationRequest` | High | Direct read, `schemas.py:143-166`, grep confirms single definition |
| `session_operation_fences`' "release never nulls forensic authority" precedent is real and is contradicted by T03's current terminal CHECK bundle | High | Direct read of both `models.py:320-324` and `T03.md:716-818` |
| Identity unification is the single highest-leverage open question, ranked above table/codec | Moderate | Systems reasoning from verified facts (three independent client-minted-id facilities on overlapping routes); a genuine judgment call about relative leverage, not itself a measured fact |
| The codec B-vs-C gap narrows once a conformance test is mandatory either way | Moderate | Reasoned concession, not independently measured against a drafted C-shaped implementation |

## Risk Assessment

**Implementation Risk:** Medium, unchanged from opening — B′ still requires
the T02 codec-parameterization work plus, now, an explicit pre-T02 identity
ruling that isn't yet scoped as a task in the plan folder at all.
**Reversibility:** Difficult once the epoch cut ships (unchanged; this
remains a one-time schema decision under the owner's no-dual-acceptance
doctrine).

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| The identity question is left unresolved and `operation_id` ships as a fourth-independent client-minted key alongside `client_request_id` | High | Medium — the plan folder currently has zero references to `message_ingress_receipts` per every panellist's independent grep | Block T02 on an explicit owner ruling, as the B′ camp already recommends |
| T03 ships with the current terminal-nulling CHECK bundle, losing "who last claimed this row" on settlement | Medium | High — it's already in the current draft, not a hypothetical | Fix before T03 lands: stop nulling `claim_owner_instance_id` at terminal (`attempt` is a separate, non-nullable column already unaffected) |
| B/C codec disagreement is never actually resolved and both camps ship independently-drifting implementations | Low-Medium | Low if a conformance test is mandated either way (as this panel converges on) | Pin the test regardless of B-vs-C outcome |

## Information Gaps

1. No drafted A-shaped or C-shaped implementation exists to inspect directly
   for either the CHECK-bundle entanglement claim (B2, opening position) or
   the codec-drift-bound claim above — both remain reasoned from verified
   component facts, not from a built artifact.
2. Whether the owner intends `message_ingress_receipts` and the compose job
   to be reconciled by removing one (per solution-architect's "exactly one
   of job.user_message_id or the ingress receipt is kept as authority") or
   by a lighter cross-check is not something this panel can resolve —
   it's the ruling this position recommends be obtained before T02.
3. No tracker/issue search was performed for prior precedent on this exact
   shared-vs-split question in this codebase (same gap as the opening
   position; still out of this panel's read-only scope).

## Caveats & Required Follow-ups

### Before relying on this analysis
- [ ] Confirm the owner's ruling on `operation_id` vs. `client_request_id`
  before any T02 work starts — this position treats that ruling as a
  precondition, not a parallel task.
- [ ] Re-verify `T03.md`'s terminal CHECK bundle and `session_operation_fences`'
  comment have not changed between this read (`6506f6a7f`) and whatever
  commit implementation starts from.

### Assumptions made
- That the panel's convergent recommendation to resolve identity before
  writing new schema is itself sound systems advice (upstream problems
  should be resolved before downstream ones are built on top of them) —
  this is a general systems-engineering premise, not specific to this
  codebase, and I did not independently stress-test it against a
  counter-scenario where deferring the identity question is actually safer.

### Does not account for
- The engineering cost/schedule impact of adding a pre-T02 identity-ruling
  step to the plan — this is a sequencing recommendation from the systems
  lens, not a scoped estimate; the solution-architect and db-architect
  positions are better placed to size it.
- Whether `message_ingress_receipts` should itself be retired at the epoch
  cut (solution-architect's Option 1) or kept as the authority
  (Option 2) — this position states the question needs resolving, not
  which answer is correct.

### Recommended next steps, in order
1. Obtain the owner's ruling on operation identity (this position's
   precondition 1) before scoping T02.
2. Apply the two-column terminal-nulling fix to T03 (this position's
   precondition 2) regardless of the identity ruling's outcome — it's a
   narrow, low-cost fix to an already-live archetype instance.
3. Proceed with B′ (separate table, shared parameterized codec) for the
   table/codec question once 1 and 2 are settled.
