# Systems-thinking position — composer async operation storage (A/B/C/D)

Panellist: systems thinker. Tree: `release/0.8.1` @ `6506f6a7f`, read-only.
Protocol: `meta-sme-protocol:sme-agent-protocol`.

## Position: **B — separate table, shared codec** (parameterized, not as-is)

Confidence: **Moderate-High**. The structural argument for keeping the
compose job in its own table is strong and well-evidenced. The "shared
codec" half is correct in direction but the specific function
(`operation_receipt_request_hash`) cannot be reused unmodified — see
Reason 2 and the fact-check below.

## Variables identified

- **Takeover surface**: which kinds of in-progress rows a reservation
  function will reclaim past lease expiry. State: currently 2 kinds
  (`session_fork`, `state_revert`), both takeover-eligible. Trend under each
  option: A adds a third kind that must be the *first* non-takeover-eligible
  kind ever admitted to that surface; B/C/D leave the surface at 2 and open
  a second, structurally separate surface with zero takeover-eligible kinds.
- **Fence models bound to one row**: how many distinct "who owns this row"
  mechanisms exist. State: 1 today (`lease_token`/`lease_expires_at`/
  `attempt`, receipts-only). Compose's `running` fence (R3) is a second,
  incompatible mechanism (the SOL triple, no separate lease expiry —
  contract.md "Job-lease liveness == SOL liveness"). Trend under A: 2
  mechanisms cohabit 1 table, both able to mutate overlapping columns.
  Under B/C/D: the SOL fence lives in its own table, alone.
- **Combinatorial branch count in `_validate_row`**: currently a 55-line
  kind×status function (`operation_receipts.py:93-148`) already branching on
  2 kinds × 3 statuses with per-kind locator rules. State: measured at
  2 kinds today. Trend under A: 3 kinds, 4 statuses (`queued` is new — a
  status receipts has never had), 1 codec, 1 CHECK-bundle, 1 validator,
  1 caller surface all owned by whichever team last touched compose or
  fork/revert.
- **Traffic share on the shared surface**: which kind dominates volume.
  State: fork/revert are user-initiated, occasional. Compose is every
  `/messages` and `/recompose` call, including the tutorial's Build path
  (BRIEF.md:53-54, `TutorialFreeformShell.tsx:112`) — the highest-volume,
  highest-scrutiny caller in the codebase. Trend under A: compose becomes
  the dominant tenant of a table named and originally scoped for
  session-lifecycle receipts.

## Feedback loops

**B1: Takeover-default guard (balancing, but load-bearing on a single line)**
Path: new kind added to `session_operation_receipts` → developer must locate
and extend the takeover-eligibility check inside `reserve_operation_receipt`
→ omission silently makes a `running` compose row takeover-eligible → a
second provider turn replays against audit/session state that already
committed → spec's own bar ("a `running` row is *never* taken over" — spec
§1, "no provider replay") is violated in production, not in review.
Polarity: reserve→check (same direction, reinforcing the guard when present)
but the guard's *presence* is a manual, deletable opt-out from a shared
default — one odd link (the developer's decision) separates "safe" from
"unsafe," so this is nominally a balancing loop that depends entirely on a
human noticing, not a structural balancing loop. That distinction is the
crux of the leverage-point argument below.

**Evidence for the default-is-takeover claim**:
`operation_receipts.py:316-320` — on any expired `in_progress` row, the
function falls through to the takeover branch (`token = uuid.uuid4().hex;
next_attempt = row["attempt"] + 1; ... event_kind="taken_over"`) with no
kind-conditional gate anywhere in the function. Takeover is the *unmarked*
case; declining it is what a hypothetical `compose_message` kind would have
to add.

**B2: Two-fence CHECK entanglement (balancing loop stretched across two
authorities)**
Path: compose's `running` row needs `lease_token/lease_expires_at NULL,
SOL-triple NOT NULL` while fork/revert's `in_progress` row needs the
opposite (`lease_token/lease_expires_at NOT NULL`) → both live inside one
`ck_..._status_bundle` CHECK (or its A-widened successor) → a schema-owner
who edits the bundle for one kind must re-derive the AND/OR arms for every
other kind to avoid the exact PostgreSQL one-element-`IN` reflection trap
this repo has already been bitten by (BRIEF.md:57-58, "per-status bundles
must be AND/OR arms, no one-element IN"). Under A this CHECK is one
function's responsibility for two independently-evolving lifecycles (fork/
revert release cadence vs. compose release cadence). Under B/C/D each table
owns its own bundle; the AND/OR discipline is scoped to one lifecycle at a
time.

**R1: Success-to-the-successful on a shared table (reinforcing, dormant
under B/C/D, live under A)**
Path: compose is the highest-traffic kind → its schema/validator/authority
changes get reviewed and merged fastest (biggest blast radius, most
scrutiny, most tests) → the shared table's shape, indexes, and validator
increasingly optimize for compose's access pattern (queued admission,
capacity counting, per-second reap scans) → fork/revert's much lower-
frequency, simpler lifecycle inherits index and lock-contention costs it
never asked for (e.g. the claimable/reap index this plan needs,
`ix_composer_async_operations_claimable`, would become a shared-table index
serving a once-per-second scan for compose polling while fork/revert rows
sit inert in the same b-tree). Every future perf tune on the table is framed
in compose's vocabulary; fork/revert becomes the tenant nobody optimizes
for, then the tenant whose edge cases get "temporarily" special-cased.
Polarity: even number of reinforcing links (traffic → review priority →
shape-for-dominant-tenant → dominant tenant's traffic grows because it's now
well-served) — classic Success-to-the-Successful, described exactly by the
archetype table's "shared resource" symptom.

## Archetype match

**Primary: Eroding Goals**, on the invariant "a `running`/compose-analogous
row is never taken over."

Diagnostic question (from the protocol): *"If we gave the team 2 more
weeks, could they hit the original target under complacency, or is a
resource constraint forcing the standard down?"* — Reframed for a design
option rather than a schedule: *does the option let the invariant hold by
default, or does it hold only because of continuous vigilance against a
shared-table pressure to converge behavior?* Under A, the invariant is not
one broken by resource pressure at time of writing (the contract text is
explicit and careful — T02's adopted-deviation table even names the
takeover/non-takeover split deliberately). The erosion is *time-deferred*:
the pressure arrives at the *next* kind added after compose (the brief
itself names "fork, revert, compose, future ones" as an open-ended series),
when a developer extending the shared reservation function under deadline
pressure has a 3-in-4 chance of copying the takeover-eligible branch because
it is the majority case in the function they are editing. This is the
"pressure erodes a standard that was fine when written" shape, not
"complacency lets a standard drift" — hence Eroding, not Drifting.

Evidence:
- Does the standard require ongoing vigilance to hold, or is it structurally
  guaranteed? Vigilance, under A — see B1.
- Does violating it fail loudly (a test, a CHECK) or silently (wrong branch
  taken, correct-looking code)? Silently under A: nothing in the function's
  types stops a kind-string comparison from being wrong; the CHECK
  constraints validate row *shape*, not "this kind must never reach the
  takeover branch." Under B/C/D the guarantee is that the compose authority
  (`ComposerAsyncOperationAuthority`) *has no takeover method at all* — the
  absence of a capability, not a conditional inside a shared one.

**Secondary: Accidental Adversaries**, between the fork/revert lifecycle and
the compose lifecycle, both acting in good faith, under A.

Evidence:
- Neither lifecycle intends to constrain the other. Fork/revert's owner
  wants a simple two-status audit trail; compose's owner wants a four-status
  queue with capacity admission and a hard SOL binding. Each reasonable
  change to serve one lifecycle's need (add a `queued` status; widen a CHECK
  arm; add a `deadline_at` column with its own time-ordering CHECK) is a
  migration and a review surface the other lifecycle's owner must now also
  reason about, per the repo's own whole-tree-gate discipline (AGENTS.md:
  "one careless `getattr` turns the branch red for every sibling").
- The two lifecycles are not aware they are adversaries — this is not
  Escalation (no perceived threat, no tit-for-tat); it is two well-intentioned
  changes landing in the same structural space and each making the other's
  next change harder, exactly the "unintentionally undermining each other"
  signature.

**Checked and rejected: Shifting the Burden.** A is not a quick fix masking
a fundamental solution deferred to later — the plan text treats A/B/C/D as a
one-time, irreversible-in-practice schema decision (epoch cut, no dual
acceptance per owner doctrine), not a stopgap with an intended follow-up.
There is no "quick fix now, fundamental fix later" pairing among the four
options; they are four different fundamental designs. Shifting-the-Burden
does not fit.

**Checked and rejected: Tragedy of the Commons**, as the *primary* frame.
The shared table under A is a commons (5 owned fields become a contended
resource: table locks, index shape, CHECK complexity), and R1 above is
adjacent to this archetype's mechanism — but Tragedy of the Commons requires
multiple independent *actors each individually rational* degrading a shared
resource with no one accountable for the whole. Here there is a single
owner (John, pre-release, one-developer-becoming-several per the owner
doctrine) who can and does review every schema change; the commons dynamic
is latent, not yet active, and is better captured as the R1
Success-to-the-Successful loop (traffic-driven optimization skew) than as
an unmanaged-commons collapse. Recorded as a related pattern to watch once
multiple developers land concurrent schema PRs against the same table
(the brief's own 12-18-month, "more developers join" framing).

## Leverage points (Meadows) per option

| Option | Leverage point acted on | Why |
|---|---|---|
| **A** | Level 5 (Rules) — and only rules. The takeover exemption, the actor-comparison gap, the queued-status addition are all conditionals added to an existing rule set (the reservation function, the CHECK bundle). No structural change accompanies them: the same function, same table, same PK shape now serves a materially different lifecycle by branching harder. |
| **B** | Level 10 (Structure) for the table/authority split; Level 6 (Information flow) for the shared codec, *if and only if* the codec is parameterized rather than reused as-is (see fact-check). A new table with its own authority is a structural change: the takeover invariant becomes true by construction (the compose authority has no takeover method), not by rule. The shared codec, done right, is a lower-leverage but real win: it collapses two independently-evolving hash implementations into one information-consistency point. |
| **C** | Level 5 (Rules), reinforcing Level 10 (Structure) once but abandoning the Level-6 win B offers. The structural separation is as strong as B, but the byte-for-byte duplicate codec is a second rule set that must be kept in sync by convention alone — a self-inflicted Drifting-Goals seed for a *different* invariant (the two codecs' hashing semantics staying identical), acknowledged nowhere as a tracked obligation. |
| **D** | Nominally Level 10 (Structure) — "extract a shared core" sounds like the highest-leverage move on the table. In this codebase's concrete shape it collapses to whichever of A/B it is extracted *into*: a shared core used as a **table** both kinds share is A with an abstraction layer wrapped around the same single-table risk (B1/B2/R1 all still apply, now hidden behind a generic interface that makes the takeover-eligibility branch *harder* to audit per-kind, not easier); a shared core used as a **module** two separate tables both call is B with better-named seams. D is not a fifth position; it is A or B wearing a coat, and panel time is better spent choosing which coat B should wear (see Reason 2) than treating D as independent. |

## Three strongest reasons for B

1. **The takeover invariant becomes structural, not conventional.** Under
   B/C/D-as-B, `ComposerAsyncOperationAuthority` simply has no method that
   reclaims a `running` row past expiry — `settle_lost`, `settle_own_lapsed`,
   and `settle_lost_inactive_session` (contract.md, Authority section) are
   the only paths off an expired `running` row, and every one of them
   terminates the job rather than replaying it. Under A, the same guarantee
   depends on a kind-conditional a future editor of
   `reserve_operation_receipt` must notice and preserve, against a function
   whose unmarked default is takeover (`operation_receipts.py:316-320`).
   This is the clearest Rules-vs-Structure (Level 5 vs Level 10) leverage
   difference in the whole panel question, and the owner doctrine's own
   "no tech debt" framing favors paying the structural cost once (a second
   table) over carrying a conventional obligation indefinitely into a
   multi-developer future the brief explicitly names as coming.
2. **The two fence models cannot cohabit one table's CHECK bundle without
   entangling two authorities' release cadences (B2).** Fork/revert's fence
   is a self-contained lease triple; compose's `running` fence is bound to
   the session-wide `SessionOperationLease` by design (ruling R3) and has no
   independent expiry. A single `status_bundle` CHECK expressing both is not
   a wider version of today's CHECK — it's a second, structurally different
   invariant grafted onto the first, and this repo has already paid once for
   underestimating CHECK-bundle complexity (the cited one-element-`IN`
   PostgreSQL reflection defect, BRIEF.md:57-58). Two tables means two
   CHECK bundles, each auditable against one lifecycle at a time.
3. **Success-to-the-Successful is avoided by construction, not by
   discipline.** Compose is about to become the highest-traffic caller in
   the composer subsystem (tutorial path included). Giving it a table sized,
   indexed, and reaped for its own cadence — without that cadence pulling
   fork/revert's much rarer rows into the same hot index and scan loop — is
   the leverage-preserving move. A shared codec (parameterized) captures the
   real, narrow duplication (the strict+forbid, exclude-`operation_id`,
   schema-string hashing shape) without forcing the *storage* shape to
   converge too.

## Strongest argument against this position

The systems argument for B assumes the two lifecycles will keep diverging
in shape — but the brief's own framing ("more client-minted session
operations appear... as more developers join") could just as easily argue
the other way: a **third** future kind (say, a client-minted "compare" or
"replay" operation) will face the *same* choice again, and if the answer is
"give it its own table" every time, ELSPETH accumulates N structurally
similar tables with N authorities, N CHECK bundles, and N sets of whole-tree
gates to maintain (`test_session_db_mutation_authority.py`'s "exact
TablePolicy set, named authorities, AST-fingerprinted writer manifest" per
BRIEF.md:55-56) — itself a maintainability tragedy-of-the-commons on the
*schema-gate* surface, not the table surface. D's "generalize the core" is
the honest answer to *that* version of the problem, and if it is done as a
genuine shared base class/mixin (identity, request hash, terminal replay,
event log as reusable *code*, not a reusable *table*) rather than collapsing
to A or B as I argue above, it could out-leverage B by fixing the Rules-vs-
Structure problem for every future kind at once instead of one at a time.
My rejection of D rests on reading this specific codebase's contract
(single `Authority` classes per table, `TablePolicy` naming a table by name)
as making a code-shared-but-storage-separate D indistinguishable from B in
practice — that reading could be wrong if a later kind is common enough to
justify building the generalized base now rather than opportunistically
extracting it from B when a third kind actually appears (which is itself a
defensible, lower-risk sequencing: extract the shared core from two
existing, working separate tables rather than designing it speculatively
for kinds that don't exist yet).

## Consequences for T02–T06 (if B is adopted)

- **T02 (owned types + codecs)**: `operation_receipt_request_hash`
  (`operation_receipts.py:39-53`) cannot be reused as written — it hardcodes
  `_REQUEST_SCHEMA = "session-operation-receipt-request.v1"`
  (`operation_receipts.py:36`) into the hash payload, and its `kind`
  parameter is typed `OperationReceiptKind = Literal["session_fork",
  "state_revert"]` (`protocol.py:264`), a closed Literal that does not admit
  `compose_message`/`compose_recompose`. "Reuse ... instead of a duplicate
  codec" (option B's own wording) requires T02 to either (a) parameterize
  the schema string and widen/parameterize the kind type in a shared helper
  both codecs call, or (b) extract the strict-DTO-hashing shape (the
  strict+forbid check, the `exclude={"operation_id"}` dump, the
  `stable_hash({"schema":..., "session_id":..., "kind":..., "request":...})`
  envelope) into a function parameterized on schema string and kind, with
  `operation_receipt_request_hash` and the new
  `composer_operation_request_hash` becoming two thin callers of it. Either
  path is a small, bounded T02 change; reusing the function unmodified is
  not available and should not be assumed by whoever picks up B.
- **T03 (table, CHECKs, trigger, TablePolicy)**: unaffected in kind — a new
  `composer_async_operations` table exactly as contract.md already specifies,
  with its own `TablePolicy` and its own AND/OR status-bundle CHECK,
  untangled from `session_operation_receipts`' bundle. No change to the
  fork/revert table or its existing CHECKs, triggers, or digest inventory
  entries.
- **T04 (`ComposerAsyncOperationAuthority`)**: gains no takeover-shaped
  method by design (see Reason 1) — `claim_next`/`renew_claim` operate on
  `queued` rows only (as contract.md already specifies), and every path off
  an expired `running` row is a settle, never a re-claim. This is the
  structural guarantee the primary archetype match depends on; T04's
  implementer should treat "no takeover method exists" as a property to
  test for directly (a negative test: no code path in the authority can
  move a `running` row back to a state where a second `adopt`/provider call
  is possible), not merely an emergent property of the methods listed.
- **T05 (composite start) / T06 (composite terminal)**: both already bind to
  the SOL triple per ruling R3, independent of the receipts lease
  mechanism — under B this binding is the *only* fence the compose row ever
  has, cleanly, rather than a second fence type coexisting with receipts'
  lease columns in the same table (the B2 entanglement A would introduce).
  No functional change to T05/T06 as currently contracted; B removes a risk
  they would otherwise have inherited from a shared table, it does not add
  work to them.

## Fact corrections

- BRIEF.md's caller list for `operation_receipts.py` (line 41, "Callers in
  `service.py`, `coordination/repository.py`, `blobs/service.py`, routes
  `sessions.py`, `composer/state.py`, `routes/operation_receipts.py`") omits
  two files that also reference operation-receipt names:
  `src/elspeth/web/sessions/proposal_authority.py` (a docstring cross-
  reference to `revert_state_for_operation_receipt`, `proposal_authority.py:538`)
  and `src/elspeth/web/sessions/schema.py` (the terminal-immutability trigger
  inventory entries `trg_session_operation_receipts_terminal_immutable`,
  `trg_session_operation_receipt_events_no_update/no_delete`, `schema.py:87,
  106-108,397-402` — relevant to the brief's own "terminal-immutability
  trigger inventory (5 places)" line). Neither changes the panel's
  conclusion; both are lower-weight references (a docstring, and schema-gate
  bookkeeping) rather than functional callers, but the brief asked for
  fact-checking and these were measurably missing from its list.
- The brief's framing of B ("reuse `operation_receipt_request_hash` ...
  instead of a duplicate codec") is achievable in spirit but not literally
  as named — see T02 consequences above. This is not a correction of a
  measured fact so much as a flag that B's own option text understates the
  refactor it requires; recorded here because the panel's charge is to
  surface exactly this kind of gap.

---

## Confidence Assessment

**Overall Confidence:** Moderate-High.

| Claim | Confidence | Basis |
|---|---|---|
| Takeover is the unmarked default in `reserve_operation_receipt` | High | Direct code read, `operation_receipts.py:316-320`, no kind-conditional present |
| `OperationReceiptKind` is a closed 2-value Literal | High | `protocol.py:264`, direct read |
| `operation_receipt_request_hash` cannot be reused unmodified for B | High | Direct read of the hardcoded schema string and typed `kind` parameter, `operation_receipts.py:36,39` |
| Compose's `running` fence has no independent lease expiry, bound instead to SOL | High | contract.md "Composite start (R3)" and "Job-lease liveness == SOL liveness" text, direct read |
| A would produce the specific CHECK-bundle entanglement described (B2) | Moderate | Reasoned from the two fence shapes and the existing `ck_session_operation_receipts_status_bundle` structure (`models.py`); no A-shaped CHECK bundle has actually been drafted to inspect |
| R1 (Success-to-the-Successful) will materialize as described | Moderate | Directionally sound systems reasoning from measured traffic-share facts (tutorial path, BRIEF.md:53-54) and this repo's own review culture (heaviest-traffic code gets the most review attention); not something that can be verified pre-implementation |
| D collapses to A-or-B in this codebase's specific authority/TablePolicy contract shape | Moderate | Reasoned from `contract.md`'s one-`Authority`-class-per-table pattern and the whole-tree gate's exact-`TablePolicy`-set requirement (BRIEF.md:55-56); the counter-argument in "Strongest argument against" is itself Moderate confidence, so this is a genuine judgment call, not a measured fact |
| No existing GitHub/tracker precedent for a similarly-shaped shared-vs-split decision in this repo | Insufficient Data | Not searched; would need a tracker query beyond this panel's read-only file scope |

## Risk Assessment

**Implementation Risk:** Medium. Adopting B is not free — it requires the
T02 codec-parameterization work this brief's own option text did not
budget for, and produces a second `Authority`/`TablePolicy`/digest-inventory
entry the whole-tree gates must learn (already contracted in T03/T04 either
way, since the plan as currently written already builds
`composer_async_operations` as its own table — B changes only the codec
sharing, not the table-existence decision already made in the spec).
**Reversibility:** Difficult once epoch 68 (or whichever epoch lands this)
ships, per the owner doctrine's own "no dual acceptance" / "hard epoch cut"
framing (BRIEF.md, session schema epoch discussion) — this is a schema
decision made once, not iterated on live data.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| B's codec-sharing is attempted literally (reusing `operation_receipt_request_hash` as-is) and silently binds compose hashes under the receipt schema string / rejects compose kinds at the Literal boundary | Medium | Would be near-certain if T02 is written from the brief's option text alone, without this panel's fact-check | Parameterize schema string + kind type in T02 as described above; this position paper's T02 consequences section is the concrete spec |
| A ships and a future kind's editor copies the majority (takeover) branch under deadline pressure | Medium-High over the stated 12-18 month / multi-developer horizon | Only relevant if A is chosen against this recommendation | Choose B/structural-D; if A is chosen anyway, add an explicit per-kind takeover-eligibility test that fails closed (new kind defaults to non-takeover unless explicitly allow-listed) |
| Two separate tables (B) drift in unrelated ways that should have been shared (e.g. an audit/export tool learns fork/revert's shape and misses compose's) | Low-Medium | Possible as more kinds appear | This is exactly the case for revisiting D's shared-core extraction once a third kind exists — sequencing risk, not a reason to avoid B now |

## Information Gaps

1. **No drafted A-shaped CHECK bundle exists to inspect.** B2's entanglement
   claim is reasoned from the two fence shapes, not measured against actual
   SQL text, because A was never implemented past the option-table stage in
   this brief. A drafted A-variant schema would let this be confirmed or
   falsified directly.
2. **No tracker/issue history was searched for prior shared-vs-split table
   decisions in this codebase**, which might carry precedent (a prior ADR,
   a prior rejected PR) directly relevant to this panel's question beyond
   what this file's read-only scope covered.
3. **The real-world magnitude of R1 (Success-to-the-Successful) is not
   measurable pre-implementation** — it is a directional claim about review
   and optimization attention under load, not a number this panel can
   produce without live traffic and git-blame data on the actual (not yet
   built) shared table.

## Caveats & Required Follow-ups

### Before relying on this analysis
- [ ] Confirm this position against the plan's own contract.md, which
  already committed to a *separate* `composer_async_operations` table
  (Decision 1 in the spec, `docs/specs/2026-09-16-composer-async-operations-design.md`
  §Decisions 1) — this panel's B recommendation agrees with that existing
  decision on the storage question and narrows the open question to codec
  sharing specifically; if the panel's charge is instead to relitigate
  storage from scratch (the brief's phrasing suggests it is, given guided's
  removal changes the receipts table's framing), that agreement should be
  read as independent corroboration, not circular reasoning — the systems
  argument above was built from the code's current shape, not from the
  spec's prior conclusion.
- [ ] Re-verify `OperationReceiptKind`'s Literal and
  `operation_receipt_request_hash`'s hardcoded schema string have not
  changed between this read (`6506f6a7f`) and whatever commit implementation
  actually starts from.

### Assumptions made
- That "shared codec" in option B means genuine code reuse (a shared
  function or shared logic), not merely "the same design pattern applied
  twice" — under the latter reading B and C become indistinguishable, which
  would collapse this panel's B/C distinction. The panel brief's own wording
  ("reuse `operation_receipt_request_hash`... instead of a duplicate codec")
  supports the code-reuse reading used here.
- That the "12-18 months, more developers, more kinds" horizon named in
  this panel's brief is a real planning horizon and not a rhetorical frame —
  the leverage-point and archetype analysis is calibrated to that horizon
  specifically; a shorter horizon (e.g. "just ship compose, no more kinds
  planned") would weaken the Success-to-the-Successful and Eroding-Goals
  arguments proportionally, though not eliminate them, since compose alone
  already exercises both dynamics against fork/revert.

### Does not account for
- Query/observability ergonomics (one table to look at vs. two) — a real,
  legitimate argument for A that this systems lens does not weigh heavily,
  since it is a supportability concern better assessed by the solution
  architect or database architect panellists.
- Migration/epoch-cut engineering cost of B vs. A in absolute engineer-hours —
  this position argues from structural risk and invariant strength, not
  from implementation cost, which other panellists are better placed to
  quantify.

### Recommended next steps, in order
1. Confirm with the owner whether option B's "shared codec" is meant as
   literal code reuse (this paper's reading) or as a looser "same approach,
   two implementations" (closer to C in effect); this resolves the B/C
   framing ambiguity noted above.
2. If B is adopted, scope T02's codec-parameterization explicitly (schema
   string + kind type) rather than assuming
   `operation_receipt_request_hash` is reusable as written.
3. Add a negative test at T04 asserting no code path in
   `ComposerAsyncOperationAuthority` can move a `running` row back to a
   claimable/re-adoptable state — making the Eroding-Goals mitigation
   (Reason 1) enforced by CI, not only by design intent.
