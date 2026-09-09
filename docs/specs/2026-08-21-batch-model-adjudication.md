# Batch model adjudication: window, group, and completeness unit

**Date:** 2026-08-21
**Status:** Adjudication complete; implementation not started
**Question:** Given ELSPETH's DAG / state-engine / plugins architecture, what is the *most correct* way to implement batches?

## Verdict

**The batch model is under-modelled, and it is silently wrong today** (see §0). "Aggregation node + trigger" implements a WINDOW. The correct move is a **new first-class barrier primitive** — the structural twin of `row_union` — carrying a completeness policy over a group whose cardinality comes from the engine for expand groups and from declared row data for data-defined groups (§7).

Two independent architects reached the barrier verdict separately; they split only on where identity and cardinality live. §7 is **the author's reconciliation of that split, not a panel finding** — it is the one load-bearing claim in this document with no independent review behind it.

## 0. REFRAMED — first-class rows is binding doctrine, and it changes the verdict

> **This section was rewritten 2026-08-21 after the maintainer recalled a governing prior decision the panel judged without knowing about. The earlier framing — "a latent correctness bug in released behaviour" — was too strong and is withdrawn.**

**ADR-020** (Accepted, 2026-05-06) retired both batch-LLM transforms for breaking *"per-row attribution within a single run: every output traceable to source data, configuration, and code version, with `explain(recorder, run_id, token_id)` proving complete lineage"* (`:31`). And decisively, at `:62`, it names what it KEPT and why:

> *"`batch_replicate` and `batch_stats` are synchronous, single-run, **per-row attributable**, and serve statistical-aggregation use cases that ELSPETH's audit framework supports natively."*

A batch aggregation was retained **on the ground that rows remain individually attributable through it**. So first-class-rows is not merely a defensible reading — it is within-batch doctrine, recorded, and the surviving plugins were kept because they honour it.

### What that does to the findings

Under first-class rows, **a batch is a window over independent rows, and independence is the point.** A member that fails has its own terminal record and its own lineage; nothing is concealed. An aggregate over the survivors is an honest aggregate over the rows that exist.

So:

- **"Silently wrong" is the wrong word. "Unlinked" is the right one.** The systems review said this in its own terms without drawing the conclusion: answering *"why did A-77 fail?"* requires a human to join two unlinked audit records. Unlinked is a far weaker claim than silent, and it points at a linkage improvement rather than a correctness defect.
- **The count-trigger contamination is the window working as specified.** `count: 5` promises "flush every 5 arrivals" and keeps that promise. A batch holding 99 of A plus one of B is only a *defect* if you believed the window was a group — which is the §1 conflation, not a separate bug. (The systems review was careful here and explicitly declined to claim a window shift; that caution was correct.)
- **`best_effort` must remain the DEFAULT.** Any design making completeness a precondition would overturn ADR-020's retained conops, break every existing count-triggered pipeline, and — per §13 — convert tolerable loss into a late hard abort.

### What survives the reframe, unchanged

1. **The three-concepts finding (§1).** Window ≠ group ≠ completeness unit is structural and independent of which policy is default.
2. **`examples/batch_aggregation` (elspeth-1b31c6da3a).** Not a policy question: the config *promises* a per-category total and delivers a per-window subdivision. A promise the config makes and does not keep.
3. **Completeness is inexpressible.** This is the real gap, and it is a missing capability rather than a broken behaviour. There is no way to say "this set must be whole" even when you know it must be.
4. **`batch_data_quality_report`'s denominator.** Wrong on its own terms under any row model — a plugin whose *subject* is data quality should not exclude upstream failures from its denominator. Worth its own ticket independent of all of this.

### The shape the maintainer was reaching for already exists

`coalesce_policy` is `require_all | first | quorum | best_effort`. **First-class-rows IS `best_effort`; completeness is `require_all`, opted in per aggregation.** So this is not a reversal of the prior decision — it is the opt-in that decision left unexpressible, using the vocabulary the tree already has (§8). Both cases are real: a statistical batch over independent rows wants `best_effort`; a document reassembly wants `require_all`.

**Priority consequence:** the evidence points *down* on the default-behaviour question and remains up only on the narrow "cannot express completeness" gap. The material below retains the full severity analysis because it is exactly what a `require_all` opt-in must handle — read it as *what breaks when completeness genuinely matters*, not as a catalogue of live defects.

## 0b. Severity analysis (what a `require_all` opt-in must handle)

### The corruption has TWO shapes, and they split on the trigger

**Under `count: N` the batch is NEVER short — it BACKFILLS.** `TriggerEvaluator.record_accept` increments `_batch_count` once per **arrival** (`engine/triggers.py:141`) and the latch fires at `_batch_count >= config.count` (`:157`); `accept_adopted_row` appends on arrival only (`engine/executors/aggregation.py:283-288`). A lost member never arrives, so the counter never advances for it and the buffer keeps filling **from the next logical group**.

So for the developer's own case — A expands to A-1..A-100 into `count: 100` — the flushed batch holds **99 children of A plus one child of B**. Honest denominators, no short read, no counter mismatch, no audit anomaly. This is membership **contamination**, and it is undetectable by construction.

**Under `timeout_seconds` / `end_of_source` you get the short batch** — the mean over 99 of 100. End-of-source at least makes absence definitive (`orchestrator/leader_drain.py:452-462`, `:479-490`), but definitive absence is not *observed* absence.

### Nothing catches either shape, and the reason is systemic

Every consumer emits a `batch_size` derived from `len(rows)`. `AggregationBatchContext`'s fields are all derived by `_build_flush_window` from `node.accepted_count_total` (`aggregation.py:431-440`), which increments once per arrival. **`batch_size` looks like an audit field but is a tautology — the plugin reporting the length of a list it was handed.** There is no reference point anywhere in the flush path that the engine did not derive from arrivals. `expected_output_count` — the only knob whose name suggests otherwise — validates rows *emitted*, not inputs received (`aggregation.py:541`, `processor.py:1562-1567`).

### Consumer sweep: 13 batch-aware plugins, ZERO guards, exactly 1 immune

`is_batch_aware = True` on the 12 `batch_*` plugins plus `report_assemble` (`:135`); `runtime_factory.py:126-132` proves the flag is the gate.

| Class | Plugins | What goes wrong |
|---|---|---|
| **Wrong ANSWER** (holds under every trigger) | `batch_top_k`, `batch_outlier_annotator` | top_k ranks by arrived counts (`:276`, `:283`) so a loss **changes which labels appear**; outlier derives mean+stdev from the arrived set (`:396-401`) so **other rows flip in/out of outlier status** — losing an extreme value tightens stdev and manufactures outliers |
| **BIASED, not noisy** | `batch_effect_size` (`:339`), `batch_paired_preference` (`:443`), `batch_drift_compare` (`:294`,`:319`), `batch_experiment_compare` (`:306`) | two-cohort comparisons where loss correlated with a variant hits ONE cohort; `paired_preference` **amplifies** — losing one half silently discards its intact partner too. Differential loss reads as genuine drift. Produces a confidently wrong **decision** |
| **Anti-correlated with truth** | `batch_data_quality_report` | `missing_rate = missing_count / batch_size` (`:279`, `:286`, `:293`). An upstream row failure **is** a data-quality event; removing it from numerator *and* denominator means **the worse the data gets, the better this report says it is.** The plugin an operator would reach for to detect this is structurally incapable of seeing it |
| **Magnitude-only** | `batch_stats`, `batch_distribution_profile`, `batch_threshold_summary`, `batch_classifier_metrics` | wrong number, right shape |
| **Naming hazard** | `report_assemble` | `line_count`/`line_start`/`line_end`/`lines_seen_total` (`:297-300`) are flush-window ordinals honestly described as pagination in its docstring — but the names invite reading them as source positions. "lines 1-5 of 5" over source records 1,2,3,4,6 |
| **IMMUNE** | `batch_replicate` | per-row fan-out, no cross-row arithmetic. The only one |

`batch_stats`'s own diligence sharpens the point: it refuses to fabricate `count=0/sum=0` for skipped values and names each skipped row in an audit record (`:397-401`, `:487`) — real care about data missing *within* the batch, structural blindness to data missing *from* it.

### It is not reachable-in-principle. It is the only shipped posture.

**All 17 example configs containing `aggregations:` carry `on_error: discard`** — verified, zero exceptions. That is the quarantine arm: `error_sink == "discard"` → `QUARANTINED_AT_SOURCE`, terminal, never passed to `create_continuation` (`engine/token_traversal.py:295-347`). Every shipped aggregation example is configured so that an upstream failure silently vanishes from the batch.

*(The expansion-into-aggregation topology is separately confirmed as genuinely new: zero example configs pair `line_explode`/`json_explode`/`blob_csv_expand` with an `aggregations:` block. So the fork is BOTH — a new-feature gap **and** an independently real released bug arriving through ordinary failure paths.)*

### And one shape fires with ZERO failures

`_group_rows` partitions **within the flush buffer only**. Any `group_by` value spanning two flushes emits **two independent aggregate rows, each formatted identically to a complete group aggregate**, with nothing distinguishing them. That is `examples/batch_aggregation` today (§1) — orthogonal to member loss, and **the completeness feature will not fix it**. The engine cuts windows by arrival count, plugins cut groups by value, and nothing reconciles the two.

*(Scope correction: the `group_by` **option** is in 3 plugins, but **5** partition the flush buffer via `_group_rows` — those 3 plus `batch_effect_size` (`:242-249`) and `batch_experiment_compare`, which partition by `variant_field`. The straddle mechanism is identical whatever the key is called.)*

## 1. "Batch" conflates three concepts

| Concept | Membership determined by | A wrong answer costs |
|---|---|---|
| **WINDOW** | arbitrary — any partition is valid | throughput only |
| **GROUP** | the data (`group_by: category`) | a wrong answer that validates |
| **COMPLETENESS UNIT** | a known expected extent | must be an error, not a smaller set |

They do not collapse into one primitive with parameters, because the failure modes differ **in kind**.

### Demonstrated in shipped code, not argued

`examples/batch_aggregation/settings.yaml` pairs `trigger: {count: 5}` (`:22-23`) with `group_by: category` (`:29`) and a `category: str` fixed sink field (`:44`), so every output row reads as a per-category total.

`examples/batch_aggregation/input.csv` holds exactly **5 electronics, 5 clothing, 5 groceries, in that order** (verified by run-length). The example is correct *only because the data was authored so window boundaries coincide with category boundaries*. Shuffle the CSV and the same config emits per-window-per-category rows still labelled as category totals — wrong answer, no error, no audit signal.

Filed as **elspeth-1b31c6da3a** (P2). This is shipped documentation teaching a capability that does not exist.

### `group_by` is structurally incapable of being a partition

All three implementing plugins say so in their own docstrings — *"group_by partitions one flushed batch and never accumulates a group across windows"* (`batch_stats.py:135`, `batch_top_k.py:88`, `batch_distribution_profile.py:116`). The engine sets the window; the plugin can only subdivide what it was handed and can never see past the boundary.

### The engine already knows a window destroys a group — for forks only

`core/dag/builder.py:1343-1349`, the ROW_UNION GROUP-INDIVISIBILITY GUARD: *"any aggregation reachable from a row_union may use only the implicit end_of_source trigger, which cannot split a group"* (`row_union_downstream_group_invalid`). The only available remedy is an infinite window. No equivalent protection exists for `group_by`, because `group_by` is a plugin option the engine cannot see.

## 2. The asymmetry is the actual hole

FORK has **two** inverses: `coalesce_tokens` (N→1 field merge, `engine/tokens.py:307`) and `row_union` (N→N group release, `core/config.py:1174`). **EXPAND has none.**

Reassembly falls to a generic aggregation that knows nothing about the expansion it is reversing. That aggregation is not under-informed — it is *differently purposed*. Informing it makes one node hold both an arbitrary-membership window and a known-extent group, reconciling their triggers. That is the §1 conflation, re-created inside a node.

An expand family is invisible to the loss seam **by construction**: `processor.py:3121-3155` dispatches two arms, both keyed on `current_token.branch_name` (`:3182`), and expand children inherit the parent's, which is `None` outside a fork (`tokens.py:454`). The seam's own docstring (`:3136-3141`) says a future barrier kind is wired by editing that one method.

**Extent is derivable, not missing.** `expand_token` creates exactly `len(expanded_rows)` children in ONE transaction (`tokens.py:426-435`), and `expand_group_id` is a durable, **indexed** column (`core/landscape/schema.py:613`). So `count(*) WHERE expand_group_id = ?` is exact from the moment any child exists. This is the honest difference from `row_union`, whose extent is build-time declared (`config.py:1202`, `min_length=2`).

## 3. Why NOT the field-contract system

The DAG has a real, ADR-backed static contract layer, and non-sink consumers — **aggregations included** — can demand fields of their parent today:

- `core/dag/schema_validation.py:180` — `consumer_required = frozenset() if to_info.node_type == NodeType.SINK else get_required_fields(graph, to_node_id)`; checked against effective propagated guarantees (`:184`), failing with `EdgeContractError`.
- `contracts/schema.py:917-926` branches on `node_type == "aggregation"` via `get_aggregation_contract_options`.
- SINKS are *excluded* from the per-edge check only because they have a dedicated pass (`:1107`) with abstention semantics (`:169-180`, elspeth-3283f2eaec).

There are **three distinct demand channels**, easily conflated: author-written demand (`required_input_fields` / `schema.required_fields`, all non-sink consumers); derived declaration (`declared_input_fields`, transform-only, participation-gated); and the sink plugin attribute (`declared_required_fields`) — the last is what `graph.py:158`'s "For SINK nodes only" refers to.

**So the mechanism exists and reaches aggregations. It is still the wrong instrument.**

**Wrong algebra.** `compose_propagation` (`contracts/guarantee_propagation.py:56-59`) is `self ∪ ⋂(participating)` — a set-lattice over *names*. You cannot intersect two expected-counts. The abstention rule (`None` skipped, `:38`) means an unknown upstream silently drops out of the constraint: sound conservatism for presence, **inverted** for completeness, where an unknown upstream must be a hard error.

**Wrong scope.** The contract is per-edge and build-time. Completeness spans a set of tokens across time. There is no edge on which "all 100 arrived" is a statement.

**Presence is not truth — and here the static check passes *precisely* when the semantics break.**

`core/config.py:1002-1011`, `union_collision_policy`, verbatim: *"'fail' treats any field-name overlap as a collision, even if both branches produced the same value — **collision detection is name-based, not value-based**."* The default is `last_wins` (`:1003`), keeping "the value from the last branch in declaration order"; `first_wins` is implemented at `engine/coalesce_executor.py:114-136`.

So when two branches carrying different expand groups meet at a coalesce and both guarantee `_expand_expected_count`, the intersection **keeps** the field (`{count} ∩ {count} = {count}`), the graph validates green, and at runtime one group's count silently overwrites the other's by declaration order.

- Today: the group is silently short.
- With a data-plane count: the group is silently short *and carries a positional-accident number asserting it should not be*. The aggregation compares 99 against a 100 sourced from the same plane that lost the row.

A mechanism whose green state is uncorrelated with the invariant's truth is not a weak guard — it is a misleading one, and it would put ELSPETH's most trusted static layer behind a claim it cannot check, degrading that layer's meaning everywhere else.

This is `feedback_declaration_tests_pin_existence_not_truth` at the architecture layer, and it collides with `feedback_stop_parsing_carry_the_fact_structurally`: **the engine is the producer.** `expand_token` mints the family and knows its exact size in one transaction. Routing that fact out through the data plane and reading it back is the parse-shaped move that doctrine exists to prevent.

### `row_id` is not a carrier either

After a transform-mode aggregation, every output row inherits the row_id of an **arbitrarily chosen representative** — `expand_parent_token = ... else non_quarantined_tokens[0]` (`processor.py:1577-1580`), `representative_token=buffered_tokens[0]` (`executors/aggregation.py:420`). Five rows in, three summaries out, all carrying input-row-0's row_id — while `contracts/identity.py:18` documents row_id as *"stable source row identity."* row_id is already overloaded across three meanings (source identity / coalesce correlation key / expand family key) and is untrustworthy as a group key downstream of a batch.

## 4. Layering

`group_by` is implemented in **3** of 12 `batch_*` plugins — `batch_stats`, `batch_top_k`, `batch_distribution_profile` — each independently reimplementing a `_reject_empty_group_by` validator, a non-finite-key guard (`batch_stats.py:319-331`, `batch_top_k.py:199-211`, `batch_distribution_profile.py:269-281`) and a first-seen-order partition helper. (Earlier scoping notes said "twelve"; the other hits in `plugins/infrastructure/base.py` are column-name contract machinery, not partition logic.)

The incoherence is not sink-vs-everything. It is that one node kind has its semantics split across two namespaces with no rule about which owns what: the aggregation's *contract* and its *partition* both live under `options:` (plugin space), while its *boundary* (`trigger`) is engine-level. Layering is coherent only if the concept a plugin owns is expressible within the plugin's horizon — and a partition is not.

## 5. Cost

`row_union` is the price tag: **68 src files, 1,149 mention-lines, 86 test files, ~69 commits**; the executor alone landed at 451 + 266 test lines (`e94293d93`) and stands at 703 lines today.

*UNVERIFIED — inference:* the third barrier should be materially cheaper than the second. The loss seam was generalised precisely because adding the second kind left every early-exit path unnotified, and journal intake, adoption CAS, restore-from-journal and the end-of-source sweep are now barrier-generic. Genuinely new work: extent derivation, an `expand_group_id` arm on the seam, config + builder guards, resume.

**Zero forced change for the ~13 existing aggregation consumers and shipped examples.** A new node kind is additive; every existing aggregation stays a window. That is the strongest argument for a new primitive over remodelling — and also why `examples/batch_aggregation` stays wrong until someone fixes it.

## 6. What this is NOT

`builder.py:1348` names "group-aware count/timeout/condition triggers" as a production follow-up. That is an (a)-shaped fix for a **different sub-problem**: stopping a window from splitting a group a barrier *already assembled*. It cannot touch the expand case, because nothing assembles the expand family in the first place. Keep it as a complement, not a substitute.

A completeness predicate cannot be a fifth trigger without re-conflating window and group. **Triggers say "flush now"; completeness says "this set is wrong."**

Remodelling aggregation wholesale is refutable cheaply: nothing found shows the window model is *wrong*, only that it is the only thing available.

## 7. The one live disagreement, and how it resolves

Two independent architects agreed on nearly everything and split on **one axis: where group identity and cardinality live.**

**Agreed by both, independently:** three concepts are conflated (window / partition / completeness unit) and completeness does not exist; the loss-seam asymmetry is a consequence rather than a principle and a third arm is unobstructed; `group_by` is 3 plugins not 12; zero forced change for existing consumers; the coalesce verdict matrix should be reused; identity must not go on `PipelineRow`; the window/trigger primitive is legitimate and stays.

**The split:**

- **Engine-owned** (`expand_group_id`): immune to the data plane entirely. But it only works for groups the engine *mints* — i.e. expand groups.
- **Row-data + contract** (`key_field` / `size_field`, demanded via ADR-007): works for any group the *data* defines, which is what "universally needed for batches" actually requires. But a `size_field` riding in row data is exposed to the coalesce collision in §3.

### The resolution (AUTHOR'S RECONCILIATION — neither agent proposed this; unreviewed)

**Cardinality source is a property of the group KIND, not a design choice.**

The closure table makes this explicit — a barrier can only run a completeness policy if the engine can know, at a finite point, that no further member will arrive:

| Group kind | Closed at | Cardinality source |
|---|---|---|
| coalesce / row_union | config time | `len(settings.branches)` — `engine/coalesce_policy.py:141` |
| **expand group** | **expansion time** | **engine — all N children inserted atomically** (`engine/tokens.py:426-457`) |
| **data-defined group** | never, without a declared size | **row data — only the producer can know** |
| field-derived partition | never | none — cannot carry a completeness policy |

So `size_field` should be **optional**, with the engine-derived extent used when the group is an expand group. For an expand group, engine-derived is strictly better: exact, immune to the coalesce collision, and needs no contract. For a data-defined group, row-data is the *only* possible source, and the ADR-007 demand is what makes it safe.

That is not a compromise between the two designs — it is the observation that they were answering for different group kinds. Both mechanisms are needed; neither is sufficient alone.

**The architect's strongest counter, recorded because it stands:** *"moving the claim into the engine does not make it truer. Only the expander can count the pages of a PDF."* True for a data-defined group. **Not** true for an expand group, where `expand_token` creates exactly `len(expanded_rows)` children in one transaction — there the engine's count is the ground truth, not a restatement of a plugin's claim.

### The five obligations — and why contract-only fails

Producer-stamped row data solves exactly one of these:

| | Row data + contract | Engine |
|---|---|---|
| 1. Detection — barrier knows 99 ≠ 100 | solves it | not needed |
| 2. **Boundary** — the group *is* the batch | **cannot** | required |
| 3. **Liveness** — bounded wait for the absent member | **cannot** | required |
| 4. Accounting — which member, why, audit lineage | cannot | required |
| 5. Disposition — terminal outcomes for the survivors | cannot | required |

**Boundary is the fatal objection to contract-only.** `check_flush_status` cuts on the window (`engine/executors/aggregation.py:1017`, fired at `engine/barrier_coordination.py:395`) and `accept_adopted_row` appends to one flat per-node buffer (`:283`). Under `trigger: {count: 64}` a 100-page document arrives as 64-of-100 and 36-of-100 in **two separate flushes** — both incomplete, both wrong, and the plugin cannot see across a flush because `_snapshot_flush_inputs` rebuilds inputs from `node.tokens` alone (`:399-421`). *A field in a row cannot tell the engine where a group ends.*

**Liveness is the second.** Drop the count trigger and the group holds to end-of-source — the only point where absence is definitive (`orchestrator/leader_drain.py:430-462`). For a 10k-document run that means buffering everything before the first result. Detection at EOF is detection too late.

## 8. The policy is already written — it is wired to the wrong barrier

The developer's rule ("A-24 fails ⇒ all of A fails") exists verbatim today as `require_all` on LOSS, `engine/coalesce_policy.py:148-151`:

```python
# event is CoalesceEvent.LOSS: ANY lost branch = immediate failure
return _fail(f"branch_lost:{','.join(sorted(lost_branches.keys()))}")
```

with `expected_count = len(settings.branches)` at `:141`. Parameterising `expected_count` is the whole policy change. Keep the failure strings byte-exact — they are hashed into Landscape.

**Disposition differs from coalesce, deliberately.** Coalesce writes `FAILURE`/`UNROUTED` (`processor.py:3271-3290`); the developer asked for *quarantine*. A group-loss failure should write `FAILURE`/`QUARANTINED_AT_SOURCE` for every survivor — the pair aggregation already uses (`engine/aggregation_result.py:65-68`) — with a `group_loss:{key}` error hash routed through the aggregation's `on_error`.

**Liveness cannot wedge:** `expected = arrived + lost + live`, all three known.

## 9. Three implementation hazards that would bite silently

These are the highest-value findings for whoever builds this.

1. **Node ids are content hashes.** `builder.py:226-230` — `hashlib.sha256(canonical_json(config))[:12]`, and node ids are FK targets across `batches`, `token_outcomes`, `node_states`. A `group` key added to `agg_node_config["options"]` must be **conditional**: an absent key contributes nothing to the canonical form so every ungrouped aggregation keeps a byte-identical id, whereas `"group": null` would not. Prefer a read-side union inside `contracts/schema.py:917-926` over injecting into the config dict at all.
2. **`AggregationBatchContext.to_dict()` emits a fixed 8-key set** (`contracts/node_state_context.py:276-287`), and its `require_int` validators reject `None`. Extending it changes audit bytes for **every** aggregation including ungrouped ones — the difference between additive-in-principle and additive-in-practice at the wire-shape gate. Use a sibling `AggregationGroupContext`, `None` when ungrouped.
3. **`batches.expansion_group_id` already exists** (`core/landscape/schema.py:2022`) and is the *output* side. A new input-side column must not reuse that name — call it `input_group_key`. Live naming hazard.

Also: model a durable `aggregation_member_losses` table on `coalesce_branch_losses` (`core/landscape/schema.py:1037-1064`) with a **different** natural key `(run_id, node_id, group_key, token_id)` — do not overload the existing table. A third arm on `_notify_barrier_of_lost_branch` inherits every existing early-exit call site for free (retry exhaustion, filter drop, quarantine, error routing, gate discard, batch-flush drop). That is the largest single saving in the design.

## 10. Where the real cost sits

Semantically additive; structurally not. One file does most of the work: `engine/executors/aggregation.py` goes from one buffer per node to M. `_AggregationNodeState` keyed by `(node_id, group_key)`; `open_batch_membership` (`:223`), `get_buffer_count` (`:828`), `get_barrier_scalars` (`:842`), `restore_from_journal` (`:874`) and `TriggerEvaluator` (`:203`) all become group-scoped. `quarantined_indices` becomes group-relative (`aggregation_result.py:14-51`) — the seam where a bug could mis-quarantine a sibling group's rows.

`leader_drain.py:479-490`'s terminal-guarantee assertion still holds **provided** `flush_remaining_aggregation_buffers` iterates groups and `get_aggregation_buffer_count` sums them. It is a fail-closed abort, so a missed update wedges the run rather than degrading quietly — verify explicitly rather than assuming.

## 11. The precedent: this is the same bug, one barrier kind earlier

Commit **`136b5e3cb`**, *"fix(engine): notify row_union when a fork branch is lost"*:

> `_notify_row_union_of_lost_branch` had zero callers: every early-exit path (retry exhaustion, filter drop, quarantine, error routing, gate routing, gate discard, batch-flush drop) named the coalesce notifier directly, so adding a second barrier kind silently left all seven unnotified. A lost branch left its sibling blocked until the end-of-source sweep, recorded as `row_union_incomplete_at_flush` — indistinguishable from a genuinely incomplete group.

The remedy was the barrier-agnostic seam `_notify_barrier_of_lost_branch`, built so "a future barrier kind is wired by editing one method" (`processor.py:3136-3137`). **Aggregation is that future barrier kind and the seam is still missing its arm.**

The project's own doctrine already rules on this. `docs/architecture/barrier-machinery.md` is titled *"Barrier Machinery: Aggregation and Coalesce Are Structural Twins"* (2026-06-11) and its paired-change checklist opens with: *"Assume the bug exists on both sides until you have evidence"* (`:70`). ADR-028 records as ACCEPTED that the pair the abstraction targets is aggregation + coalesce. By the repo's own standard this asymmetry has been an open drift finding the whole time.

**Follow the durable branch-loss ledger pattern.** `processor.py:3202-3210` stages a `BranchLossSpec(...)` **before** the in-memory notify and **unconditionally**, so a follower with no in-process executor still writes the durable row in the same transaction as its disposition; the leader replays at intake via `list_unadopted_coalesce_branch_losses`. Reasons are bounded category tokens (`String(64)`) with detail hashed — because an unbounded repr once overflowed the column on PostgreSQL and killed a run (`token_traversal.py:311-318`, elspeth-74b795208f).

## 12. Three traps for whoever builds this

**The `audit_fields` trap — put this in front of the designer first.** A designer stamping completeness metadata will reach for `audit_fields` by name: it *is* provenance, it *is* engine bookkeeping, it *is* not user-facing API. That single choice **disables both enforcement limbs**. Build time: `get_effective_guaranteed_fields()` excludes audit_fields, so it never enters `producer_guaranteed`, and `validate_single_edge` skips entirely when `consumer_required` is empty (`schema_validation.py:182`). Runtime: `_allowed_declared_fields` unions audit_fields so no extras violation fires (`executors/schema_config_mode.py:78-83`), but `required_output_fields` excludes it (`:162`) so `missing` can never see it. **The field may vanish from any row and nothing notices.** So the contract route works *only* if the metadata is declared as `guaranteed_fields` — i.e. as stable user-visible API the user may legitimately rename or project. Declared as what it honestly is, it is invisible. This is the tension the contract route relocates rather than resolves.

**Rename catches loss but not FORGERY.** `field_mapper` is thoroughly rename-aware, so renaming the field away removes it from the guarantee set and raises `EdgeContractError`. But `_mapping_target_is_guaranteed` (`:347-358`) means `{"some_other_col": "_expand_expected_count"}` **produces a build-valid guarantee carrying arbitrary user data** — clean rename, no overlap, `_reject_overlapping_rename_graphs` does not fire. There is no global reserved-name registry. And strict validation (`aggregation.py:373`) catches the *type*, never the *number*: a wrong integer is accepted silently.

**Topology fails closed — at a supportability cost.** An abstaining arm returns `EffectiveGuaranteeVote(fields=frozenset(), participated=False)` (`guarantees.py:196-208`), so `missing = consumer_required` → `EdgeContractError`. Right polarity: **a guarantee cannot be silently lost by topology at a non-sink consumer.** But it makes currently-runnable pipelines **un-buildable** — any multi-source queue, `nested`/`select` coalesce, or observed-schema arm upstream — and the error names the *immediate producer*, not the sibling arm that abstained (`schema_validation.py:197-199`). That misdirection lands hardest on the composer's authoring loop, where an LLM planner gets a message pointing at the wrong node.

**Two-walker drift is not speculative — it has already happened four times**, each needing two independent fixes with the composer half arriving second (queue fan-in `elspeth-5a372d3267` → `elspeth-3619b8774f`; row_union `elspeth-41bcaa882e`; transparent gates `elspeth-0b14977817`, drifting the other way). And `elspeth-ae83a6b60c` is a **recorded composer-green / runtime-red divergence at a barrier node**: *"Stage 1 abstained at every coalesce while the runtime rejected the identical pipeline at build, leaving the authoring loop no error to repair."* No live divergence was found today. Any design using the field-contract system must land both walkers in ONE commit with a parity test.

## 13. Second-order effects

**The naive fix converts a silent wrong answer into a late hard abort.** If completeness becomes a flush *precondition*, a `count: N` group that lost a member never reaches N, holds to end-of-source, and hits `OrchestrationInvariantError: "Aggregation buffer for node ... still has N tokens after end-of-source flush"` (`leader_drain.py:479-490`). Every existing count-triggered pipeline that today tolerates occasional loss starts aborting — at the very end, after all cost is incurred. Bounded (`MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000`) so it exhausts rather than hangs.

**Crash/resume has no durable home for "expected".** Every durable membership record is arrivals-only. The trap is documented at `journal_restore.py:551-560`: restoring a stale latch "would plant a phantom first-accept anchor at restore time that survives into the NEXT genuine batch". A phantom expected-count fails the same way and worse — it wedges a *healthy* batch forever.

**Quarantining 99 for 1 is currently incoherent.** `error_hash = compute_error_hash(f"quarantined_in_batch:{batch_id}:{index}")` is a **positional index into the ARRIVED buffer** saying nothing about *why*. Ninety-nine survivors each carry a hash pointing at their own position while the actual cause is a different, absent token with an unrelated terminal record elsewhere. Answering "why did A-77 fail?" needs a human to join two unlinked records. `coalesce_branch_losses` is the shape that records causation — extend that, not `quarantined_indices`.

**Nested expansion loses the outer group.** Children take a NEW `expand_group_id` per expansion (`tokens.py:455`), so a token retains only its innermost group. Completeness must be checked per level.

**Fork+expand: coalesce already fails closed; aggregation does not.** `token_traversal.py:262-266` deliberately carries the barrier binding because "carrying it makes an unsatisfiable expansion (N children sharing one row_id) fail closed at the barrier", and coalesce trips duplicate-arrival detection (`coalesce_executor.py:875`). **An aggregation accepts all N without comment.** The engine already decided expansion-into-a-barrier must fail closed; aggregation is the one barrier where that decision was never enforced.

**Duplication satisfies every presence contract** and breaks arithmetic in the *opposite* direction: `arrived == expected` passes on "lost A-24, duplicated A-25". Aggregation has no duplicate-member guard — `accept_adopted_row` simply appends. **The correct check is identity-set equality against a roster, never arithmetic on a count.**

**Cost.** Per-token LLM call audit rows are written at execution time; sweeping 99 survivors at flush does not un-bill their provider calls. For a 100-way expansion with an LLM node, "all of A fails" is a 99% waste amplification on one member failure. A real tradeoff, not an argument against the rule.

## 14. Production signatures to watch for

- A `count`-triggered aggregation downstream of anything that drops rows. **No short batch will ever appear.** The only signature is `source_row_count != sum(batch_size) + terminal_failures`, and nothing computes that today.
- `batch_data_quality_report` whose `missing_rate` **improves** after an upstream change — that is rows failing before the report, not data improving.
- A/B or drift outputs where one cohort's `total_count` drifts relative to the other across flushes.
- `batch_top_k` label sets changing between runs on identical input — rank flips; tiebreak is first-seen order, so arrival-order changes alone can do it under multi-worker.
- Duplicate `category` values in a `group_by` output sink — a straddled group. **Visible in `examples/batch_aggregation/` output today, zero failures needed.**
- `OrchestrationInvariantError` mentioning "still has N tokens after end-of-source flush" after any completeness work lands.

## Related

- **elspeth-bc566ed043** — an aggregation cannot observe a failed member (failed tokens never arrive; the plugin gets no identity)
- **elspeth-4139923015** — no trigger closes a batch on expand-group completion
- **elspeth-1b31c6da3a** — `examples/batch_aggregation` teaches `group_by` as a partition
- **elspeth-0038a69467** — the PDF explode/stitch work that surfaced all of this
- **elspeth-a5b86149d4** — the row_union precedent (closed; carries the `first_class_row_union` product decision)
- **elspeth-fc499e6d03** — a second shipped-docs instance of a window trigger where a group is meant
