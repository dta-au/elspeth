# Barrier scopes: structural failure propagation for correlated token groups

**Date:** 2026-08-21
**Status:** PROPOSAL — **REVIEWED, THREE BLOCKERS. Not buildable as written.**

## BLOCKERS (systems stress test, 2026-08-21 — all verified against source)

**The worked example in "Failure propagation" below is unrepresentable in the current token model.** Three independent blockers, each sufficient on its own.

### B1 — Opening an inner scope DESTROYS outer-scope membership

Step 2 of the example ("`A-1` forks into two branches") deletes `A-1`'s membership in the outer group, in memory *and* durably:

- `engine/tokens.py:295-304` — `fork_token` constructs `TokenInfo(row_id, token_id, row_data, branch_name, fork_group_id)`. **`expand_group_id` is not passed.** Fork children of an expand child carry `expand_group_id=None`.
- `engine/tokens.py:364-369` — `coalesce_tokens` drops `branch_name`, `fork_group_id` **and** `expand_group_id`, so the token leaving an inner coalesce does not recover outer membership either.
- `core/landscape/data_flow/tokens.py:575, :584` — the fork replay predicate **asserts** `child.expand_group_id is None` and raises `AuditIntegrityError("fork_token: divergent fork replay")`. Inheriting the group is a Tier-1 audit-integrity change, not a field addition.
- **The engine already rejects the analogous shape, for this exact reason.** `core/dag/builder.py:1516-1527`: *"A nested fork replaces the enclosing branch identity and terminalizes its parent before the enclosing row_union can receive or durably lose that branch, so the union group can never be satisfied."* And `:1462-1472`: *"A require-all union cannot span fork generations."* Mirrored in the composer as `row_union_nested_fork_invalid` (`web/composer/state.py:6655-6664`).

**Consequence:** `count(*) WHERE expand_group_id = ?` — the exact-extent query this design leans on — is exact **only for a scope containing no fork**. Build item 4 ("reject partial overlap") is backwards: the work is *permitting nesting at all*, which needs a scope-membership stack as a first-class token field (`scope_path`) preserved by every token-minting primitive, plus updated replay predicates, tokens table and canonical form. The alternative is flat-only scopes in v1 — which deletes outward propagation, the design's core mechanism.

### B2 — One claim may stage exactly ONE loss record; nesting needs two

`engine/scheduler_drain.py:996-998` raises `OrchestrationInvariantError(... "one claim loses at most one branch. Processor bug.")`. The seam rests on it: *"A branch belongs to at most ONE barrier"* (`processor.py:3139-3141`). But `expand_token` sets `branch_name=parent_token.branch_name` **and** a fresh `expand_group_id` (`tokens.py:449-456`), so an expand child inside a fork branch is simultaneously a coalesce member and a scope member. Add a scope arm and both stage → Tier-1 crash on the next disposition.

**Note the pairing: fork-then-expand breaks the single-loss invariant (B2); expand-then-fork loses identity (B1). Both directions of the required nesting are broken, for different reasons.**

### B3 — A wedged scope produces a run that LOOKS resumable and never is

ADR-038 closed the token-stranding gap, but its resumability discriminator is **source lifecycle, not barrier state**. The matched pair of tests makes it explicit — `tests/integration/audit/test_contract_violation_token_outcomes.py:255` (source `exhausted` ⇒ resumable ⇒ sweep must NOT fire) and `:290` (source `loading` ⇒ not resumable ⇒ all tokens abandoned).

A wedged closer aborts at `orchestrator/leader_drain.py:472-490`, which fires **after** end-of-source — so the source is `exhausted`, the run looks resumable, the abandonment sweep does **not** fire, members keep `(NULL, BUFFERED)` with `closure='open'`, and **the operator is offered a resume.** The resume re-drives into the identical wedge: the member that reached terminal without the closer learning is still terminal, durably and idempotently, so the closer can never be satisfied on any resume.

ADR-038's guard sentence — *"the sources are complete and checkpoints exist, so a resume can still decide these tokens"* — is precisely the false premise here. The original stranding was an honest dead end; this is a **dishonest live end**. Fixing it requires a third resumability input ("a barrier holds members no resume can satisfy") in `RecoveryManager.can_resume` and the ADR-038 predicate — not in the build list.

### The wedge set is NOT empty

The premise that `_notify_barrier_of_lost_branch` is *"THE single seam every early-exit path calls"* **is false.** Seam callers are 10 sites / 9 reasons. Terminal dispositions that never reach it:

1. **`(TRANSIENT, BATCH_CONSUMED)` for every non-representative buffered token in a transform-mode flush** (`processor.py:1621-1652`) — recorded atomically inside `expand_token`, no seam call anywhere in `_apply_transform_route`. **This is the normal success path.** The largest member of the wedge set is not a failure at all.
2. **`QUARANTINED_AT_SOURCE` inside a non-empty flush** (`:1643-1646`). The *empty*-emission path does call the seam (`:1441-1446`); the non-empty one does not. Pure asymmetry, no stated rationale.
3. **Held-sibling failures written inside the barrier executors** — `coalesce_executor.py:841, :1054, :1254` call `record_token_outcome(FAILURE, UNROUTED)` directly, bypassing the seam. These are exactly the tokens outward propagation must report on.

**The cascade concern is CONFIRMED, not inference.** A closer's failure is emitted *inside the executor*, which has no `_pending_branch_losses` slot, no claim to ride, and no reference to any enclosing closer — and `processor.py:3270` confirms the executor already wrote those outcomes itself. **Build item 2 cannot be built on the record-then-notify discipline it cites as its model.** It needs a durability channel the design does not name.

### Also material

- **Triggers are the single largest omission.** Rejecting `count`/`timeout`/`condition` on a scope closer is correct (`triggers.py:141`, `:157` — arrival-counted, so the buffer backfills) and precedented (`builder.py:1391-1404`). But that leaves only `end_of_source`, so a scope over a 10k-document run buffers everything before the first result. `builder.py:1348`'s "group-aware triggers are the production follow-up" is therefore **load-bearing, not complementary** — a `require_all` scope is unusable without it. A `timeout` on a closer is worse than wrong: it converts a liveness bug into a silently short group.
- **Survivors are recorded under a false cause.** A live sibling runs to completion, bills in full, then meets §E.3a at intake and is released alone as `late_arrival_after_merge` (`barrier_coordination.py:447-479`). Under `require_all` that is 99 records blaming survivors for *when they arrived* rather than why they failed — and per B3 the run containing them may present as resumable. The aggregation side has no `late_arrival` arm at all.
- **Composer parity is worse than "4 edge-walking sites".** The composer independently reimplements the row_union guards this design must *lift* (`state.py:6655-6664`, `:6666-6699`, plus a regex at `tools/generation.py:489`). Runtime permitting nesting while the composer still rejects it is composer-red / runtime-green — the *opposite* drift direction from `elspeth-ae83a6b60c`, where the authoring loop gets a rejection with no runtime error to repair against.
- **`best_effort` cannot express member failure at all.** `validated_quarantined_indices` range-checks every index against `buffered_token_count` (`aggregation_result.py:14-51`), so an absent member is structurally unrepresentable; and `:83-84` forbids quarantine actions in `passthrough` mode — the mode the row_union guard *recommends*.
- **Sinks inside a scope** contradict SESE: a member whose terminal is a *successful* sink write leaves through something other than the closer. Precedent is a flat ban (`processor.py:487-492`).
- **No duplicate-member guard on aggregation.** `accept_adopted_row` appends; coalesce checks (`coalesce_executor.py:875-883`). "Identity-set equality against a roster, never arithmetic on a count" is a precondition, not advice.

---

**Author:** maintainer (transcribed into ELSPETH vocabulary)

## Vocabulary note

The maintainer proposed this using placeholder terms. Mapped to existing ELSPETH vocabulary:

| Placeholder | ELSPETH term | Status |
|---|---|---|
| "span" | **barrier scope** | **NEW** — the tree has no term for the region between an opener and its barrier |
| "span transforms" | **scope opener** (a `creates_tokens=True` node, ADR-015) and **scope closer** (the bound barrier) | opener/barrier exist; the *pairing* is new |
| "family" | **token group** — `fork_group_id`, `expand_group_id`, `join_group_id` (`contracts/identity.py:22-23, 37-38`) | exists |
| "family scramble mode" | `policy: require_all` | **exists** — `config.py:994`, `Literal["require_all","quorum","best_effort","first"]` |
| "first class mode" | `policy: best_effort` (default) | exists; preserves ADR-020 |
| "babysitting" | **membership accounting** — the barrier holds until every member has arrived or reached a terminal disposition | new behaviour, existing vocabulary (`TerminalOutcome`, `TerminalPath`) |
| "control edge" | **scope binding** — an ASSOCIATION, not an edge | **NEW term.** Do NOT call it "barrier binding": that already denotes the per-token carried barrier NAME (`token_traversal.py:250-266`, the `coalesce_name`/`row_union_name` a child inherits through expansion so an unsatisfiable expansion fails closed). Two meanings at the exact seam where both are live is a defect. |
| "going home" | reaching the bound barrier — by arrival, or by a DIVERT the barrier is notified of | the notification is the missing piece |
| "shunted to quarantine" | `TerminalPath.QUARANTINED_AT_SOURCE` / `AggregationMemberAction.QUARANTINE` | exists (`engine/aggregation_result.py:65-68`) |

## The premise

**A barrier scope is a single-entry/single-exit region of the DAG, opened by a token-creating node and closed by a barrier bound to it at compile time.**

Two properties define it:

1. **Compile-time binding.** The scope opener holds a reference to its closer, established at build. Direct precedent: `processor.py:483-486` already keeps `_branch_to_coalesce` and `_branch_to_row_union` as build-time maps, with an overlap check at `:488-494` rejecting a producer bound to two barriers. This proposal adds a third map of the same shape.

2. **Well-nestedness.** A scope may contain another scope entirely, or be disjoint from it — never partially overlap. Tokens may fork and coalesce freely *inside* a scope, but no token may leave it except through its closer. (This is the SESE / properly-nested-region property from compiler literature, so the static check is a known problem, not an invention.)

## Failure propagation — the core of the proposal

**Failure walks the scope stack outward. It does not escape to an arbitrary sink.**

The maintainer's worked example:

1. Row `A` enters a scope opener and expands into `A-1 .. A-100` (one token group).
2. `A-1` forks into two branches — an **inner** scope, closed by a coalesce.
3. Branch `~A-1` fails. It DIVERTs **to the inner coalesce**, not to an outside quarantine sink.
4. The inner coalesce applies its policy. Under `require_all` it concludes the whole of `A-1` has failed.
5. The coalesce then DIVERTs **outward to its own bound closer** — the outer barrier accounting for the 100-member group.
6. The outer barrier applies its policy. Under `require_all`, the entire group and its originating row are quarantined.

This is structured exception handling over the DAG: a scope is a try region, its closer is the handler, `require_all` is rethrow-to-enclosing-handler, and `best_effort` is catch-and-continue. The policy vocabulary already exists at every closer.

**Consequence: `on_error` inside a scope is determined by scope structure rather than named freely.** That makes error routing checkable at build instead of author-supplied per node.

## Why this is not fighting the architecture

Four pieces already exist:

**1. Typed edges, including a non-data edge kind.** `RoutingMode` is `MOVE | COPY | DIVERT` (`contracts/enums.py:155-172`). DIVERT's own docstring: *"These are structural markers in the DAG — rows reach these sinks via exception handling, not by traversing the edge."* A control edge already exists in this graph.

**2. DIVERT-into-a-non-sink is already anticipated, defended and tested.** `core/dag/schema_validation.py:1181-1193`, verbatim:

> "REACHABILITY, stated honestly: `build_execution_graph` cannot currently produce a DIVERT edge INTO a transform. Error routing in ELSPETH is terminal — every DIVERT edge the builder creates lands on a SINK... It is kept as defence-in-depth for the public `add_edge` surface — which tests and **any future 'route errors into a repair transform' topology** use — and pinned by `test_divert_only_predecessor_is_not_checked`."

The sink restriction is a **builder-level config gate** (`validate_on_error`, `builder.py:1139`), not a property of the graph model. And `_live_predecessors`' rule — *"a predecessor counts as live when ANY of its edges is non-DIVERT"* — is already the semantics a closer needs when one predecessor reaches it by both a data edge and a divert edge.

**3. Half the propagation already works.** For a coalesce closer, step 3-4 is shipping behaviour: `_notify_coalesce_of_lost_branch` plus `require_all` returning `_fail(f"branch_lost:{...}")` (`engine/coalesce_policy.py:148-151`). What is missing is step 5 — a closer failing *outward*. Today a coalesce failure writes `FAILURE`/`UNROUTED` (`processor.py:3271-3290`) and terminates, because there is no notion of an enclosing closer.

**4. The barrier-agnostic loss seam was built for exactly this.** `_notify_barrier_of_lost_branch` (`processor.py:3121-3155`) is documented as *"THE single seam every early-exit path calls"*, built after commit `136b5e3cb` so *"a future barrier kind is wired by editing one method"*. It dispatches to coalesce and row_union arms and has none for aggregation.

## THE MISSING HALF — group-scoped membership (review finding, 2026-08-21)

**The build list below specifies outward failure PROPAGATION while presupposing a group-scoped barrier MEMBERSHIP it never requires.** Membership accounting appears in the vocabulary table and then never appears in what must be built. That gap is load-bearing.

A barrier's buffer is one flat list per node — `node.tokens.append(token)` (`executors/aggregation.py:283-288`) — and the flush window is cut from arrival counters (`_build_flush_window` derives `batch_size`/`row_start`/`row_end` from `node.accepted_count_total`, `:431-440`). Tell that closer "group A failed" and it **cannot identify A's survivors among the buffered tokens**, and **cannot un-emit A's members that flushed in an earlier window.**

Two precedented doors. Naming them is the reviewer's ruling; choosing between them is the maintainer's:

1. **Group-scoped buffers** keyed `(node_id, group_key)`. Expensive — this is the "one buffer per node to M" cost — and `quarantined_indices` becomes group-relative (`engine/aggregation_result.py:14-51`), the seam where a bug mis-quarantines a sibling group's rows.
2. **Force the implicit `end_of_source` trigger on any scope closer** — precisely the ROW_UNION GROUP-INDIVISIBILITY GUARD's existing remedy (`core/dag/builder.py:1343-1349`). Cheap, precedented, and unbounded memory.

## THE FRAME IS WRONG PRIOR ART — structured concurrency, not exception handling

The try/catch analogy fails on a bigger axis than concurrency: **there is no unwind and no join.**

A `raise` destroys the frames between throw and handler; the failed computation stops. Here, "the entire group is quarantined" stops nothing — the other 99 members keep executing while the decision propagates. So under `require_all`, *"the group fails"* does not mean the group fails. It means **the group is retroactively marked failed after being paid for in full.** That reclassifies the billing bullet under "Known costs" from a tradeoff into a semantic gap.

The correct prior art is structured **concurrency** — a Trio nursery, `StructuredTaskScope.ShutdownOnFailure` — whose two defining mechanisms are **cancellation of live siblings** and **a join point where the scope's outcome becomes known**. The proposal has neither.

*The concurrency objection dissolves, though:* "the enclosing closer" is well-defined **statically** — a build-time property of scope nesting, not of any token's position — so N tokens at N depths is not a problem. What is genuinely unaddressed is the CASCADE: the record-then-notify discipline (`processor.py:3191-3210`) "rides the branch token's own disposition transaction", and an inner closer failing outward must record N sibling losses with **no single token disposition to ride**. *(UNVERIFIED — inference, read from that docstring rather than a traced call path.)*

**The open question no amount of design closes: is `require_all` without cancellation a policy worth shipping?**

## What must be built

0. **Group-scoped membership at the closer** — see "THE MISSING HALF" above. Nothing else works without it.
1. **A third build-time binding map** (`_opener_to_barrier`), alongside `_branch_to_coalesce` / `_branch_to_row_union`, with the same overlap rejection. **RULED: an ASSOCIATION, not an edge mode.** `get_branch_to_coalesce_map()` (`core/dag/graph.py:914`) returns a plain dict derived from build-time branch records and handed to the processor; no `RoutingMode` value carries it. A fourth `RoutingMode` would be the expensive mistake — persisted (`contracts/enums.py:167`), inside the canonical topology hash (`core/canonical.py:295`), and forcing an arm onto 64 discriminating sites, each a fail-open default waiting to happen. The cheap path: a build-derived map that never enters the canonical form, plus — only where a scope is *declared* — an ordinary `RoutingMode.DIVERT` edge whose target happens to be a barrier. Because such an edge exists only in scoped pipelines it is additive to the hash, so the "absent, not present-and-null" hazard dissolves.
2. **Outward failure propagation from a closer** — a barrier whose policy fails may DIVERT to its enclosing closer rather than terminating. This is the genuinely new mechanism.
3. **An aggregation arm on the loss seam**, plus a durable ledger modelled on `coalesce_branch_losses` (`core/landscape/schema.py:1037-1064`) with its own natural key — staged *before* the in-memory notify and *unconditionally*, per the record-then-notify discipline at `processor.py:3191-3210`, so a follower with no in-process executor still records it in the same transaction as the disposition.
4. **Build-time nesting validation, VALIDATED NOT ENFORCED.** Reject partial overlap and reject a token path leaving a scope other than through its closer — but **only for declared scopes.** A global SESE constraint fails the AGENTS.md posture test outright: it would reject pipelines that declare no scope and gain nothing, improving reliability for zero pipelines while degrading supportability for all of them, and the error names the wrong node (`schema_validation.py:197-199`) — landing hardest on the composer's authoring loop. The house pattern is local: `builder.py:1343-1349` fires only for aggregations reachable *from a row_union*. Note the understated cost: "no token may leave except through its closer" is not an overlap check but a **reachability walk from opener to every terminal** — tractable only because it is bounded to declared scopes.
5. **Extend `validate_on_error`** to permit a scope closer as an `on_error` target inside a scope.

## What this preserves

**ADR-020 is untouched.** `:62` retained `batch_replicate` and `batch_stats` because they are *"synchronous, single-run, per-row attributable"*. `best_effort` remains the default; a batch stays a window over independent rows unless a scope is declared. `require_all` is opt-in per barrier. This adds the expression that decision left unavailable — it does not reverse it.

**It also sidesteps a live pincer — conditionally.** When an entire group fails, the *engine* performs the outward divert, so the closer plugin is never invoked. Both horns of the all-quarantined trap:

- emitting → `OrchestrationInvariantError` (*"emitted output but all buffered tokens were quarantined"*, `executors/aggregation.py:548-551`) — verified verbatim;
- emitting nothing → `PluginContractViolation` (*"returned success status but neither row nor rows contains data"*, `executors/aggregation.py:526-531`).

**CITATION CORRECTED.** An earlier draft named `ZeroEmissionSuccessContractViolation` as the second horn. That was wrong: it is raised only at `engine/executors/can_drop_rows.py:143` and caught only at `engine/executors/transform.py:550`, and `aggregation.py` has **zero** declaration-contract wiring. ADR-020 corroborates independently — ADR-013 *"is enforced at the per-row pre-execution dispatch site, which a batch-aware transform never visits."* So "no ADR-012 relaxation needed" is true only because ADR-012 was never on this path. (Four agents asserted the wrong exception; agent consensus is not verification.)

**And the escape is CONDITIONAL on the membership mechanism below.** It holds only while every member is still buffered. Under a `count` trigger a group spanning two flushes has already been through the plugin and reached a sink — an outward divert cannot un-emit it.

## Known costs

- **`RoutingMode` is persisted** (`contracts/enums.py:167`, "Stored in the database"). A fourth value — or a scope-aware DIVERT target — is a persisted-vocabulary change with replay implications. The most expensive decision here to revisit.
- **Edges are in the canonical hash.** `core/canonical.py:259, :295` emits `{"mode": edge_data["mode"].value}` per edge. A binding must be *absent* from the canonical form for unbound pipelines, not present-and-null, or every existing graph's hash moves.
- **Edge-walking surface: 46 sites in 8 files**, but 39 of them (85%) in `dag/schema_validation.py` (17), `dag/graph.py` (15), `dag/guarantees.py` (7). 64 sites across 13 files already discriminate on `RoutingMode`.
- **Guarantee propagation must exclude the binding.** `dag/guarantees.py` currently forces *abstention* for a DIVERT in-edge, reasoning that "a divert payload is an error envelope, not the producer's declared" schema. A binding carries no payload at all and wants full exclusion — a simpler case, but it must be made explicitly.
- **Composer parity.** 4 edge-walking sites in `web/composer/` against 4 recorded runtime/composer drift incidents (`elspeth-5a372d3267` → `elspeth-3619b8774f`; `elspeth-41bcaa882e`; `elspeth-0b14977817`; and `elspeth-ae83a6b60c`, a recorded composer-green / runtime-red divergence at a barrier node). Both walkers must land in one commit with a parity test.
- **Billing.** Failing a 100-member group on one member's failure does not un-bill the 99 provider calls already made.

## DESIGN REVIEW FINDINGS (2026-08-21) — verified

### `size_field` / `key_field` do not survive. §7 of the adjudication is MOOT, not wrong.

A barrier scope **requires a token-creating opener**. A CSV carrying `document_id` that the engine never expanded has no opener, so the data-defined case falls **outside** the scope model entirely — not inside it with a different cardinality source. §7's taxonomy is fine; its output ("`size_field` should be optional") imports the coalesce-collision hazard and the `_mapping_target_is_guaranteed` forgery hazard for **zero delivered capability**, because nothing in the scope machinery can consume a size with no closer bound to it.

**Ship engine-derived only. Defer `size_field` completely.** Data-defined groups stay `best_effort`, which preserves ADR-020 exactly. If ever wanted, they need an explicit group-opener primitive that mints a group id — not a field contract.

**Deleted outright from any plugin-level plan:** `size_field`/`key_field` stamping, the ADR-007 demand, a reserved-name registry, the `field_mapper` forgery defence, composer parity for the contract path, and the `audit_fields` trap. All contract-route machinery with no consumer.

### FOURTH HAZARD — a third arm breaks a stated invariant

`processor.py:3139-3141`, verbatim: *"A branch belongs to at most ONE barrier — enforced at build time and re-checked in this constructor — **so at most one arm yields results**."* `WorkItem.__post_init__` encodes it (`engine/work_items.py:52-57`: `OrchestrationInvariantError` if a token targets both a coalesce and a row_union).

A token can be simultaneously a fork branch **and** an expand child, so a third arm keyed on `expand_group_id` means two arms co-fire and `results.extend(...)` accumulates from both. That is **correct under nesting and exactly what this design wants** — but the docstring and the WorkItem invariant both say otherwise. Rewrite the docstring in the same commit. Leave the WorkItem check alone (the expand binding rides `TokenInfo`, not the WorkItem), but flag it as a trap for anyone later tempted to express the scope binding as a WorkItem field.

### Outward propagation is NOT a new mechanism — and it is LEADER-ONLY

It is a **re-entrant call to `_notify_barrier_of_lost_branch`** from the coalesce-failure arm (`processor.py:3265-3290`), keyed on the consumed tokens' `expand_group_id` — which already rides `TokenInfo` (`contracts/identity.py:39`) and survives the codec. Well-nestedness forbids cycles, so depth is bounded by nesting depth. The third arm costs **nothing at the 9 existing call sites** (8 in `token_traversal.py`, 1 at `processor.py:1442`) — every one already holds the key.

**The sharp constraint:** the seam returns early for followers — `if self._coalesce_executor is None or not notify_in_memory: return []` (`processor.py:3216-3218`) — **after** the unconditional `BranchLossSpec` stage. The outer loss derives from `outcome.failure_reason`, a policy verdict only the leader computes. So "stage unconditionally" cannot hold one level out.

Specification:
1. **Follower:** stage only the innermost loss, unconditionally, in the disposition transaction. Never attempt an outer record.
2. **Leader (in-claim and at intake, identical code):** on a closer's FAIL verdict, stage the outer loss **before** notifying the outer executor, then recurse. `_replay_branch_losses` (`engine/barrier_coordination.py:703-760`) already reaches the point where `failure_reason` and `consumed_tokens` are both in hand.
3. **Adoption ordering:** `adopt_coalesce_branch_losses` commits before the replay loop. An outer loss staged during that loop is a new unadopted row — **iterate to fixpoint**; a one-pass intake leaves a two-level scope one drain behind its own evidence.
4. **Crash survivability:** the outer row is a **materialized derivation, not ground truth** — idempotent on `(run_id, node_id, group_key, token_id)` and always re-derivable at takeover by replaying inner losses through policy. Treating it as authoritative writes a silent-loss bug on crash between stage and commit.

### Q4 is already answered by the tree, and the answer is a hazard

`success_multi` **rejects** an empty row list (`contracts/results.py:423`, *"success_multi requires at least one row"*). Zero rows arrive only via `success_empty()`, which `token_traversal.py:198-212` records as `SUCCESS` / `FILTER_DROPPED` and **already notifies the enclosing barrier**. So an empty scope never opens — no children, no group id.

But the disposition is a **success** that must fail a `require_all` outer scope. Rule explicitly on "the expander legitimately produced nothing" vs "the group is empty and therefore incomplete". Recommended: `require_all` treats an empty expansion as a group failure; `best_effort` keeps today's behaviour. **Forbid any single-member fast path** — the degenerate case runs the N path.

### Other rulings

- **Q3 — ENFORCE, don't merely validate** partial overlap wherever a scope is declared: the outward walk's termination depends on it. (The adjudicator ruled *validate*, scoped to declared scopes, on delivery-posture grounds. These reconcile: enforce **within** declared scopes; do not impose a global SESE constraint on pipelines that declare none.)
- **Q5 — yes.** A `best_effort` inner closer returns no failure verdict, so nothing recurses. That is what makes per-level policy meaningful.
- **Q6 — dissolve it.** `AggregationNodeScalars` is for *underivable* scalars; expected membership is derivable by query on an indexed column (`schema.py:613`). Checkpointing it is exactly what manufactures the `journal_restore.py:551-560` phantom — and a phantom expected-count wedges a *healthy* batch, strictly worse than the stale latch it mirrors. **Derive on demand; never checkpoint.**
- **Canonical-hash requirement, not a dismissed worry.** `_edge_to_canonical_dict` emits a fixed five-key dict (`core/canonical.py:285-291`), so a side-table association is **invisible** to the topology hash — two pipelines differing only in scope binding would hash identically. The binding must therefore land as a **conditional node-config key on the closer**, so node id moves for scoped nodes and stays byte-identical for unscoped ones. Cheapest shape: a top-level `scopes:` list (the `row_unions:` precedent, `config.py:1963`) that the builder resolves into `_opener_to_barrier` *and* injects as a conditional closer-config key.
- **Citation slip corrected:** `node_state_context.py:276-287` is `AggregationFlushContext.to_dict()`; `AggregationBatchContext` begins at `:288`. The sibling-context hazard is unaffected.

### OVERCLAIM CORRECTED — this is not "zero forced change"

Outward propagation changes the disposition of a **scope-bound** coalesce failure from `FAILURE`/`UNROUTED` (`processor.py:3265-3290`) to a deferred verdict owned by the outer closer. Gated on scope-boundness, so unscoped pipelines are genuinely untouched — but it is a behaviour delta on a shipping path and must be stated as one.

## Open questions for review

1. Is **barrier scope** the right name, and is the opener/closer pair better expressed as node config or as a first-class `scopes:` config list (the `row_unions:` precedent, `config.py:1963`)?
2. Should a scope closer's outward divert be a new `RoutingMode`, or a DIVERT whose target happens to be a barrier?
3. Does well-nestedness need to be enforced, or merely validated where a scope is declared?
4. What happens when a scope opener's group is empty, or has exactly one member?
5. Does a `best_effort` inner scope correctly absorb a failure so the outer scope never hears? (Believed yes, and desirable — it makes per-level policy meaningful.)
6. Crash/resume: where does "expected membership" live durably? `AggregationNodeScalars` is the documented home for underivable scalars; `journal_restore.py:551-560` warns that a restored stale latch "would plant a phantom first-accept anchor... that survives into the NEXT genuine batch".
