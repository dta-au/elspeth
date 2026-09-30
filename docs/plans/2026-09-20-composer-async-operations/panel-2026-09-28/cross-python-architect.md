# Design panel cross-examination — CODE STRUCTURE lens (python-architect)

**Panellist:** python-refactoring-architect
**Opening position:** C — separate table, own codec (high confidence)
**Panel result:** systems-thinker B, leverage B′, solution-architect B′, tech-critic B′,
db-architect B, audit-integrity B, sre B. codex: forwarded (not a substantive position).
**Re-verified at:** `release/0.8.1` @ `6506f6a7f`, read-only.

Every other substantive panellist rejects A and D and lands on "separate table" — the axis I
was assigned to judge most directly (code reuse, kind-branch smell, refactor-vs-rearchitecture).
Nobody disputes the separate-table conclusion. The live disagreement, once A and D are off the
table, is narrow: **does the request/response hash codec get extracted into one shared,
parameterized function now (B/B′), or does compose get a byte-for-byte duplicate codec now,
with extraction deferred (C, my opening position)?** That is squarely a CODE STRUCTURE question,
so I re-examined it hardest.

Before the rebuttals, I re-verified the load-bearing code-structure claims that recur across
five positions, since if they're wrong the whole B-vs-C argument collapses:

- `OperationReceiptKind = Literal["session_fork", "state_revert"]` — confirmed exactly at
  `src/elspeth/web/sessions/protocol.py:264`. Closed two-value union, as claimed by
  systems-thinker, leverage, solution-architect, tech-critic, db-architect.
- `operation_receipt_request_hash` (`operation_receipts.py:39-53`) requires (a) `strict=True`
  and `extra="forbid"` on the request DTO's `model_config`, (b) a field literally named
  `operation_id` on that DTO, (c) the hardcoded schema string
  `"session-operation-receipt-request.v1"` (`:36`). All three confirmed by direct read.
- Today's `SendMessageRequest` (`schemas.py:143-158`) derives from `_RequestModel` (non-strict,
  `schemas.py:66`), not `_SessionOperationRequest` (the strict base at `:72`, used only by
  `ForkSessionRequest`/`RevertStateRequest`), and carries `client_request_id: UUID`
  (`schemas.py:154`) — **not** `operation_id`. `RecomposeRequest` (`schemas.py:162-164`) carries
  `expected_user_message_id: UUID` — also not `operation_id`. `_GuidedOperationRequest`, which
  `contract.md:158,161` names as the base class, does not exist anywhere in `schemas.py` — grep
  confirms zero hits.
- `message_ingress_receipts_table` (`models.py:464-487`) exists, is append-only (no-update/
  no-delete triggers, `models.py:1596-1633,1832-1852`), and is keyed by the same
  `client_request_id` that `SendMessageRequest` already carries.
- Session schema epoch is `71` at **both** `models.py:59` (`SESSION_SCHEMA_EPOCH`) and
  `schema.py:45` (`_COORDINATION_HARD_CUT_EPOCH`) — two distinct constants, currently equal by
  coincidence, both stale against `contract.md`'s "68".

So: **calling `operation_receipt_request_hash` for compose, literally as B's brief text and
`contract.md`'s "reuse `operation_receipt_request_hash`" wording describe it, is not possible
today.** It would fail on all three preconditions at once. This is tech-critic's and
db-architect's sharpest point, and it is fully confirmed, not merely plausible.

## Rebuttals, by panellist

### systems-thinker (B)
No rebuttal on the facts — their T02 read (hardcoded schema string, closed Literal kind) matches
mine exactly, independently arrived at. One structural sharpening, not a disagreement: their
write-up frames the extraction as something that touches `operation_receipts.py` itself gaining
compose-awareness. It doesn't need to. The safe shape (per tech-critic/solution-architect/
leverage, and matching my own "optional addendum" in my opening position) is a **third, neutral
module** that both `operation_receipt_request_hash` and `composer_operation_request_hash` call,
with `operation_receipts.py`'s public surface and behavior byte-identical afterward. That keeps
their own Meadows framing (rules vs. structure) pointed at the right target — the shared codec
is Level-10 structure (an invariant no future caller can accidentally violate by copying the
wrong schema string), not a rules-patch inside the receipts module.

### leverage (B′)
No rebuttal — this is the position I found myself converging on independently. Their sharpest,
verified point (operation_id vs. `client_request_id`/`message_ingress_receipts` identity
unification as a precondition to T02) is confirmed by my own read above and is *not* a code-
structure question per se (it's an identity/product ruling), but it directly gates whether the
codec extraction's `operation_id`-field precondition can even be satisfied by the real DTOs. I
accept this as a hard precondition on T02, ahead of either B or C.

### solution-architect (B′)
No rebuttal on substance. I checked one thing I initially misread and am correcting here rather
than in the document's fact-corrections section, since it's my own error, not the brief's:
`operation_receipts.py`'s docstring ("Mode-neutral immutable request and response bindings for
fork/revert") is not self-contradictory. "Mode" in this repo means guided-vs-freeform UI mode
(the brief itself: "a mode-neutral facility `session_operation_receipts` has replaced
`guided_operations`"), not operation *kind*. The docstring correctly says the module works
under either UI mode, for exactly two kinds — that is consistent with `_validate_identity`'s
closed Literal, not in tension with it. I do not have an independent code-structure argument
from solution-architect's position beyond what I already reach through tech-critic's point
below.

### tech-critic (B′)
No rebuttal — this is the most precise statement of my own lens's conclusion in the panel:
"B as stated ('reuse operation_receipt_request_hash') can't be built without changing the
receipts contract," with the exact three preconditions I independently re-verified above. Their
prescribed move — "lift the normalisation policy into a neutral function where the tag is a
parameter and each facility keeps its own tag and kind vocabulary" — is precisely the
behavior-preserving Extract Method I'd have specified myself: same public wrapper signatures,
one new private/neutral core, a golden-vector byte-identity test pinning the receipt wrapper's
output unchanged. By my own Refactor/Rearchitect framework this is a **Refactor**, not a
Rearchitecture, of `operation_receipts.py`: its public API (`operation_receipt_request_hash`'s
signature and every byte it returns for existing callers) is unchanged; only an internal helper
moves. My opening position's hesitation about "rearchitecting a just-shipped module" does not
actually apply to this narrower move — I was arguing against sharing the *lifecycle*
(reserve/renew/settle/takeover/events), which nobody in the panel proposes merging, not against
sharing 15 lines of hash normalization.

### db-architect (B)
No rebuttal on the code-structure-adjacent claim (mutation-authority widening under A: three
authorities gaining receipts write-access under option A, `test_session_db_mutation_authority.py`
pin at `:12212-12215`) — this independently confirms my own T04/A-rejection reasoning from a
different angle (schema/authority-gate surface vs. my lifecycle/takeover-branch surface); the two
arguments are complementary, not overlapping, and both point the same direction (reject A). Their
T02 phrasing ("pass the schema string and a kind from each caller's own closed union as
parameters. Do not widen `OperationReceiptKind`") is the detail that resolves a concern I would
otherwise raise: if the shared core took an unvalidated `kind: str` from an arbitrary caller,
that would be exactly the kind of un-nominally-typed boundary ADR-032 warns against. I should be
precise about what "closed union" buys here, though: `OperationReceiptKind` is a `Literal`
today, which is a *static*-typing gate only — `operation_receipt_request_hash` performs no
runtime check on `kind` itself (the runtime check lives in `_validate_identity`, called from
`reserve_operation_receipt`, not from the hash function). So the real requirement for T02 isn't
"the shared core validates `kind` at runtime" (it doesn't need to, and shouldn't duplicate that
check); it's that each wrapper stays *typed* against its own closed `Literal` at its own call
boundary (mypy-enforced, not runtime-enforced), so a caller cannot pass a compose kind to the
receipt wrapper or vice versa without a type error. Worth stating that precisely in the T02
consequence rather than implying a runtime guard that isn't there.

### audit-integrity (B)
No rebuttal on their central point (receipts already has a working event log + gated authority
that a from-scratch `composer_async_operations` does not, per the plan as currently drafted —
`T03.md`'s settle path nulls `claim_owner_instance_id`, and there is no
`composer_async_operation_events` table named anywhere in Task 3). This is a real, independently
verified plan defect (I did not check it myself in my opening position — I take it as fact,
consistent with their file:line citations and the contract.md Table section I read, which indeed
names no events table for `composer_async_operations`). It is a lifecycle/audit-completeness gap,
not an argument for merging tables — their own conclusion agrees B, not A.

Their T05/T06 proposal is squarely code-structure and I did skip it in my opening position, so I
engage it directly here: put the start CAS and terminal CAS row-mutation logic in connection-
taking helpers inside `composer_operation_authority.py`, called from `start_composer_async_
operation` (repository.py, T05) and `complete_/fail_composer_async_operation` (service.py, T06),
citing `SessionServiceImpl.settle_fork_operation_receipt` → `settle_operation_receipt(conn, ...)`
(`service.py:6551-6557` calling the module function at `service.py:1172`) as precedent. I checked
the precedent directly: it holds. `settle_operation_receipt` is a bare, connection-taking module
function; `settle_fork_operation_receipt` is a thin service-layer wrapper around it; the mutation-
authority gate's writer scan (`test_session_db_mutation_authority.py:12241-12242`) finds the write
by the function symbol `settle_operation_receipt` and the test's own mapping table
(`:12223`) attributes it to `"SessionForkAuthority"` via the *wrapping* qualified name
`SessionServiceImpl.settle_fork_operation_receipt._sync`, not by where the SQL happens to sit.
So a connection-taking `_advance_composer_operation_to_running_on_connection(conn, ...)` /
`_settle_composer_operation_terminal_on_connection(conn, ...)` pair in the authority module,
called from both T05's and T06's composite-transaction sites, is a sound move by this repo's own
precedent — it avoids the CAS `UPDATE ... WHERE status='running' AND claim_token=... AND
session_operation_{id,lease_token,epoch}=...` clause being written out twice in two files that
currently have no shared code at all. I'd endorse it as a code-structure improvement over the
plan as drafted.

One overclaim I'd correct: "the TablePolicy drops from three authorities to one" doesn't follow
automatically from sharing the connection-taking helper. In the receipts precedent, the *shared*
low-level function (`settle_operation_receipt`) still gets attributed to a *specific* authority
name per call site by the test's own symbol-to-authority mapping (`settle_fork_operation_receipt`
→ `SessionForkAuthority`) — the gate keys on the calling method's qualified name, not the shared
helper's. Under the same rule, `start_composer_async_operation` and `complete_/
fail_composer_async_operation` would each still need their own entry in that mapping (plausibly
`SessionOperationAuthority` and `SessionComposerOperationTerminalAuthority` respectively, as
`contract.md` already names them), even after the row-mutation SQL is centralized in one shared
helper. Sharing the helper is real code reuse; collapsing the authority *count* is a separate
claim about the gate's attribution rules that needs checking against the gate itself before it's
asserted as a consequence, not assumed from the code layout.

### sre (B)
No rebuttal, but their T02 line ("reuse `operation_receipt_request_hash` through a kind-agnostic
signature") is the loosest phrasing of the codec-sharing point in the panel — it doesn't name
the extraction target (a new neutral module vs. widening the existing function in place) or the
byte-identity test that makes the move safe. Converges on the same substance as leverage/
solution-architect/tech-critic once that detail is supplied; no disagreement, just less
code-structure precision than the others, which is expected outside their lens.

One point worth flagging as an owner sign-off item rather than a panel-adopted consequence:
leverage's T06 proposal to change the F-B2/C2 review-pass decision (`contract.md:46`) from a
negative cancel-fencing predicate to a positive one at `_require_session_operation_context_on_
connection` (service.py:970) is a real code-structure change to an already-decided review-pass
item, not a restatement of the table question. I have no rebuttal to its correctness (D12's
"lease can outlive a terminal job" scenario is a genuine gap in a negative predicate), but since
`contract.md`'s "Review-pass decisions... override everything above and below" language marks
F-B2/C2 as already settled by the owner, this specific proposal should go back to John as a named
amendment request, not get silently folded into "what the panel recommends" alongside the
table-placement question this panel was actually convened to answer.

## What changes my mind

**The B-vs-C axis.** My opening position called the codec-shape extraction "genuinely optional...
a same-day, low-risk micro-extraction... does not argue for a shared table" — and left it
optional specifically because I weighted "don't touch a module that shipped yesterday
(`7001600fe`)" against the reuse payoff. Re-reading `operation_receipts.py` against the actual,
current `SendMessageRequest`/`RecomposeRequest` DTOs sharpens what's actually being chosen
between: `contract.md`'s own wording for option B ("reuse `operation_receipt_request_hash`...
instead of a duplicate codec") is not achievable as literally written — calling the existing
function for compose fails all three of its preconditions today. That does not make the
extraction *mandatory* under every letter — under a strict reading of C nothing touches receipts
at all, my opening position included, and that remains a coherent, low-risk choice on its own
terms. What it changes is the comparison I'm actually making: the real choice is not "B (already
buildable) vs. C" but "B′ (extract now, with a byte-identity test pinning the receipt wrapper's
output unchanged) vs. C (write a second, independently-maintained byte-for-byte duplicate hash
function, on an audit-bound binding, starting today)." Stated that way, the extraction is a
same-connection, behavior-preserving Refactor by my own Refactor/Rearchitect framework — the
receipt wrapper's public signature and every byte it returns for existing callers is unchanged,
proved by the test — which is the lowest-risk category of change available. Weighed against
audit-integrity's drift-hazard point (two hand-maintained copies of an audit-bound normalization
rule, ADR-046), the extraction now, tested, is the better of the two real options open to me, not
a forced move.

**What does not change my mind:** the case against D (a full generalized "client-minted session
operation" core spanning identity + request hash + terminal replay + event log +
lifecycle/takeover semantics) stands exactly as I argued it originally. The dividing line is
branching: the codec core has **no kind-conditional logic at all** — `kind` is folded uniformly
into every caller's hash input, so there is nothing to special-case. Terminal replay, the events
table, and the takeover-vs-never-takeover rule **do** differ by lifecycle (receipts rebuilds and
compares a locator; compose stores and re-verifies a full JSON blob; receipts takes over an
expired lease, compose structurally cannot). Pulling those into one shared core is exactly the
kind-branch smell the brief asked me to test for, and nobody in the panel — including the B′
positions — actually proposes sharing that part. B′'s scope is the codec only.

The brief asked specifically what's shareable "without coupling lifecycles" among codec, hash
helpers, and event log — I answered codec above; the other two need the same test applied
directly, not deferred:

- **Event log.** I read `_append_event` (`operation_receipts.py:237-269`) and `_validate_events`
  (`:170-200`) again with this question in mind. Neither is reusable as code. `_append_event` is
  bound to `session_operation_receipt_events_table`'s exact column set — no
  `claim_owner_instance_id`, no `session_operation_*` triple, nothing compose's events would need
  to record. `_validate_events` hardcodes `events[0]["event_kind"] == "claimed"` as the only
  legal opening event and a three-status (`in_progress`/`completed`/`failed`) terminal-matching
  rule (`:183-184, :193-200`) that has no `queued`/`running` equivalent. Same verdict as the
  lifecycle machinery generally: the *pattern* (append-only, sequence-ordered, terminal-hash-
  checked-on-read) is worth copying, matching audit-integrity's recommendation to build a
  `composer_async_operation_events` table using the receipts *pattern*; none of the code is
  reusable as-is, and nothing here changes the codec-vs-lifecycle line I drew above.
- **Response hash.** `operation_receipt_response_hash` (`:56-62`) takes a Pydantic response DTO
  and re-validates it `strict=True` before hashing its `model_dump(mode="json")`.
  `contract.md:119`'s `composer_operation_result_hash(result_json: str)` takes already-serialized
  canonical text, not a DTO — a different input shape, because compose stores the full JSON blob
  and re-verifies it on read (T12: "Read validates `result_sha256` and the strict DTO again"),
  where receipts never persists the payload at all. leverage's claim that
  `stable_hash(json.loads(result_json))` must equal `operation_receipt_response_hash(dto)` for a
  `_StrictResponse` is a real equivalence worth pinning (both ultimately hash a strict, canonical
  `model_dump(mode="json")`), but it is a **test** obligation (a golden-vector fixture proving the
  two entry points agree on the same DTO), not evidence for a shared *function* — the DTO-in vs.
  text-in signatures are legitimately different because the two callers hold different things at
  the point they need the hash. I'd recommend the equivalence test in T02 without recommending a
  merged function.

## Final position: **B′ — separate table (`composer_async_operations`), plus one narrow,
parameterized shared hash-normalization core extracted in Task 2**, not a byte-for-byte
duplicate codec, and not full option D.

This changes my opening recommendation from C to B′, converging with the rest of the panel
(systems-thinker/db-architect/audit-integrity/sre's plain B and leverage/solution-architect/
tech-critic's B′ are the same table decision; the B/B′ naming split in the panel is itself just
about how explicitly the codec extraction is specified, which is no longer a live disagreement
once T02 must do it either way).

### Consequences for T02–T06, updated from my opening position

- **T02** (owned types + codecs): **changed from my opening position.** Extract a private,
  parameterized core — e.g. `_strict_request_hash(*, schema: str, session_id: UUID, kind: str,
  request: BaseModel)` — into a small neutral module (not inside `operation_receipts.py`, to keep
  that module's diff to zero beyond the internal call it makes into the new helper).
  `operation_receipt_request_hash` becomes a thin wrapper passing `"session-operation-receipt-
  request.v1"` and its own `OperationReceiptKind` Literal (typed at that call boundary, per
  db-architect); `composer_operation_request_hash` becomes a thin wrapper passing
  `"composer-operation-request.v1"` and `ComposerOperationKind`. A golden-vector test asserts the
  receipt wrapper's output is byte-identical to today's `operation_receipt_request_hash` for a
  fixed set of inputs — this is the safety proof that makes the move a Refactor, not a behavior
  change. Precondition, ahead of this task: John rules on `operation_id` vs. the existing
  `client_request_id`/`message_ingress_receipts` identity (leverage/solution-architect/
  tech-critic's point, independently confirmed above) — the DTOs must actually carry a field
  named `operation_id` satisfying the shared core's precondition, or the wrapper cannot be called
  as designed. `SendMessageRequest`/`RecomposeRequest` must also move off `_RequestModel` onto
  `_SessionOperationRequest` (or an equivalent strict base) to satisfy the strict/forbid
  precondition — today they do not.
- **T03**: unaffected by this position change — same as my opening position (new table, own
  TablePolicy, no rewrite of the receipts CHECK bundles). I defer to audit-integrity's and
  db-architect's plan-defect findings (no events table named, settlement nulls forensic columns,
  stale epoch/anchor citations) as out of my lens but worth carrying into the consolidated
  record.
- **T04**: unaffected by this position change — `claim_next`/reaper machinery remains net-new
  code regardless of the codec decision, exactly as I said originally.
- **T05/T06**: unaffected by this position change — same reasoning as my opening position.

### Fact corrections carried forward from re-verification (all confirmed against the tree)

- `contract.md:158,161`'s `_GuidedOperationRequest` base class does not exist in `schemas.py`;
  the strict base actually available is `_SessionOperationRequest` (`schemas.py:72`).
- `contract.md:161`'s claim that `RecomposeRequest` is "NEW... only `operation_id`" is stale:
  it already exists (`schemas.py:162-164`) with `expected_user_message_id: UUID`, not
  `operation_id`.
- `SendMessageRequest` already exists with a required `client_request_id: UUID`
  (`schemas.py:154`), backed by the immutable `message_ingress_receipts` table
  (`models.py:464-487`), on the exact same `POST /messages` route the plan targets. Neither the
  brief nor the spec/plan folder mentions this facility (grep for `client_request_id` and
  `message_ingress` under the plan folder returns nothing) — it is a real omission that bears
  directly on T02's `operation_id` precondition, independently surfaced by four other panellists
  and confirmed here.
- `contract.md:39`'s "epoch bump... 68 at plan time" is stale; the tree is at epoch 71 in both
  `models.py:59` and `schema.py:45` (two distinct constants, currently coincidentally equal), so
  the next free epoch is 72.

## Confidence Assessment

High on the narrow B-vs-C codec question (direct code read against the exact DTOs and the exact
hash function, cross-verified by three independent panellists using the same file:line evidence).
Moderate-high on the overall B′ recommendation, since it inherits the separate-table conclusion
that seven of seven substantive panellists reached independently from different lenses (db/
queueing, audit, operations, systems dynamics, and code structure) with no dissent on that half
of the question.

## Risk Assessment

- Low risk in the position change itself: it converges with, rather than diverges from, the
  panel, and the mechanism (Extract Method + golden-vector test) is the lowest-risk category of
  refactor in my own framework.
- The identity-unification precondition (operation_id vs. client_request_id) is the one place a
  wrong call could cascade: if John rules that these stay two separate ids, T02's shared-core
  precondition ("a field named `operation_id`") needs a different resolution (e.g., the shared
  core takes the id field name as a parameter too, or compose's DTOs gain a distinct
  `operation_id` alongside the existing `client_request_id`). That is an owner decision, not a
  code-structure one, and I flag it rather than resolve it.

## Information Gaps

- I did not re-verify the PostgreSQL-specific claims (SKIP LOCKED contention, CHECK reflection
  behavior) in the DB-architect and SRE positions — outside my lens, and already covered by
  panellists whose assigned lens it is.
- I did not independently verify audit-integrity's claim that Task 3 names no
  `composer_async_operation_events` table; I read `contract.md`'s Table section (Task 3) directly
  during this cross-examination and confirm it names no companion events table, which is
  consistent with their finding, but I have not read the full `T03.md` task file they cite by
  line number.

## Caveats

Scoped strictly to CODE STRUCTURE / reuse, as in my opening position. Does not re-litigate R2/R3,
the freeform-only scope ruling, or any other owner ruling, all treated as fixed inputs. This
cross-examination changes my recommendation on the narrow codec-sharing question (C → B′'s codec
scope) but does not change my original reasoning against A or against full D, both of which stand
unmodified and unchallenged by the rest of the panel.
