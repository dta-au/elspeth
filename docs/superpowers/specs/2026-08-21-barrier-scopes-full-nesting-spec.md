# Unified group lineage and barrier scopes — implementable spec

**Date:** 2026-08-21 (rev 3.2)
**Status:** SPEC — supersedes rev 2.1 of this document and the design content of
`2026-08-21-barrier-scope-proposal.md` (whose blocker analysis remains the authority on
*why*).
**Decision (maintainer ruling, 2026-08-21, in-session):** unify fork/coalesce/row_union
and barrier scopes into ONE lineage and settlement system NOW, accepting the schedule
risk, because shipping two parallel lineage representations means "locked in audit hell
in prod and never get permission to fix it." Rev 2.1's two-system design is retired
before it was built; everything from it that survives unification is carried forward
below and marked.
**Break posture (maintainer ruling, 2026-08-21, post-review):** ELSPETH is pre-1.0 with
NO shipping pipelines — the one 0.3-era pipeline was forked off as a CLI-only line for
its context. Backward compatibility with existing topologies is NOT a constraint on this
design; this is the time to make the break. The same asymmetry that motivates unifying
now applies to every narrowing below: post-launch, none of these breaks is ever
available again.
**Provenance note:** all "maintainer ruling, 2026-08-21" decisions were made in the
2026-08-21 working session; this spec is their record.

**Rev history:** rev 1 (broken by 5-lane adversarial review: member identity, in-scope
aggregator stranding, ledger key collision, advisory-only resume gate, empty-expansion
unrepresentability) → rev 2/2.1 (fixed, two-system) → rev 3 (one system) → **rev 3.1
(this): incorporates the 2026-08-21 four-lane review (solution-architect,
systems-thinker, reality-check, quality lanes) and the maintainer rulings it forced.**
The converged critical finding — three of four lanes independently — was that rev 3's
WS1 checkpoint ("zero behaviour delta") was unfalsifiable because today's tri-field
semantics are destructive while the derived accessors are preservative; rev 3.1 replaces
it with the enumerated-delta contract in §4.1a and the frozen-oracle protocol in §11.
The three model-definition gaps (pop rule vs inert inner frames, fork closure
granularity, row_union's N-token release) are resolved by rulings 22–27. **Rev 3.2
(same day, post-delta-review):** ruling 28 — a shape change inside a group must itself
be a group — replaces the generalized pop with the STRICT innermost pop rev 3
originally wanted, whose "unreachable in a valid build" claim §7 rule 5 now actually
makes true, and statically eliminates the duplicate-distinct-arrival corner the delta
review surfaced.

A note on the seam claim: rev 3 said deleting the branch/scope seam removes rev 1's
defect class. The systems lane's correction is adopted: the defect class is *relocated*
into the declarative pop/roster rules, not eliminated — a better address, because one
rule set is auditable and mutation-testable where the N-arm seam was not, but the
implementation plan must adversarially test exactly those corners (pop rule, roster
equality, frame guard) per the fail-closed-analyzer doctrine.

---

## 1. Summary

Every token-creating operation opens a **group**; every barrier is a **closer** over some
group; membership rides one token field — the **lineage path**, a stack of typed
**lineage frames**. Fork/coalesce is no longer a separate mechanism from barrier scopes:
it is the *static-roster* case of the same system.

- A **fork** opens a FORK group whose roster is the declared branch list (config-time
  static). Its closer is a **coalesce** or **row_union**. A fork is either fully bound
  (every branch flows to its one closer) or fully unbound (pure fan-out, no branch
  closes) — mixed closure is a build error (ruling 23, §7).
- A declared **scope opener** (multi-row expand) opens an EXPAND group whose roster is
  minted at runtime. Its closer is a **collector** (the new barrier kind).
- An *undeclared* expand still pushes a frame (uniform token model) but binds no closer:
  its group is inert provenance — nobody waits, nothing is staged for it. This is the
  batch posture, structurally: a window over independent rows unless a closer is bound.
  Inert expands are legal OUTSIDE bound regions only: inside any bound region, a shape
  change must itself be a declared group that closes in-region (ruling 28).
- **Settlement, loss staging, escalation, replay, and resume protection are ONE
  machinery** across all three closer kinds. The `_notify_barrier_of_lost_branch`
  docstring's claim to be "THE single seam" — false today, verified — becomes true.

What unification buys (the audit-hell payoff):

| Two-system (rev 2.1) | Unified (rev 3) |
|---|---|
| Tri-field lineage + `scope_path`, four durable representations | One `lineage_path`, one child table |
| `coalesce_branch_losses` + `scope_group_losses`, incompatible spec types | One `group_losses` ledger, one `GroupLossSpec` |
| Token-equality claim guard + frame-authenticated special case | One frame-authenticated guard |
| Three loss arms + per-arm invariants + row_union's own in-line failure path | One frame-driven settlement routine |
| Tier-1 ledger natural-key widening to dodge a collision | Collision structurally impossible (group-scoped key) |
| B3 resume protection for scope groups only | All bound groups protected, coalesce/row_union included |
| "A branch belongs to at most ONE barrier" + scope exceptions | One rule: each frame binds at most one closer |

What it costs: the audited lineage model is rewritten — TokenInfo, four audit tables,
every replay predicate, the journal codec, and every consumer of the tri-field. §9
prices it; §11 carries the risk posture and abort criterion.

## 2. Canonical vocabulary

Terms of record. **Signed off (maintainer, 2026-08-21)** — these are the strings for
errors, config keys, and the audit schema.

| Term | Definition |
|---|---|
| **group** | The token set minted by one opening operation: a FORK group (fork gate) or an EXPAND group (multi-row transform activation) |
| **lineage frame** | One `(kind, group_id, member_key)` entry: `kind ∈ {FORK, EXPAND}`; `member_key` = branch name (FORK) or member token_id (EXPAND) |
| **lineage path** | The token's stack of frames, outermost first. `()` for a source-row token |
| **member** | One identity in a group's roster: a declared branch (FORK) or a direct opener child (EXPAND). Everything a member's subtree does settles back to it |
| **roster authority** | Where a group's member set comes from: config (FORK — declared branches) or the durable group record (EXPAND). `require_all` is legal exactly where one exists (maintainer ruling) |
| **closer** | The barrier bound to a group: coalesce / row_union (FORK), collector (EXPAND). The aggregator is NOT a closer — it is a window, no roster, untouched |
| **closer binding** | The build-time group→closer association: implicit in coalesce/row_union config (as today), explicit via `scopes:` for collectors. A map + conditional closer-config key; never an edge, never a `RoutingMode` |
| **barrier scope** | The SESE region of any *bound* group — between its opener and its closer. The fork→coalesce region always was one; rev 3 makes that literal |
| **inert frame** | A frame whose group binds no closer. Pure provenance: no roster watched, no losses staged, never popped (there is no closer to pop it). Legal only outside bound regions — inside a bound region every opener must itself be bound and close in-region (ruling 28, §7) |
| **roster accounting** | The closer's identity-set ledger: minted, arrived, lost. Never arithmetic on a count |
| **settlement** | A member settles when its token arrives at the closer or a loss naming its `member_key` is durably adopted |
| **settlement propagation** | Losses/arrivals reaching the group's OWN closer (policy-independent — how `best_effort` knows everything that will arrive has) |
| **escalation** | A closer's FAIL verdict staged as one loss against the ENCLOSING frame (`require_all` semantics crossing a boundary) |
| **collector** | New barrier kind (`collectors:`), the EXPAND-group closer: roster-flushed on `end_of_group`, no trigger config, own executor. Aggregator/collector split is a standing ruling |
| **`scope_group_failed`** | Disposition reason for a member terminated because its group failed — never `late_arrival_after_merge` |

Policy vocabulary per closer kind: coalesce keeps `require_all|quorum|best_effort|first`
(untouched); row_union stays `require_all`-only v1; collector is
`require_all|best_effort` (`quorum`/`first` deferred — recorded decision, additive later).

## 3. Config surface

**Coalesce and row_union config are UNCHANGED.** Their `branches:` + name already fully
determine a FORK-group binding; the builder derives the unified binding map from the
exact config that exists today. No YAML churn for any pipeline that passes the new §7
validation; canonical hash of such pipelines does not move — **pinned by a hash-corpus
test that does not yet exist and must land as an early WS2 item: record the canonical
hashes of a representative corpus (the `examples/` settings plus composer-authored
shapes) at pre-WS2 HEAD, and assert them byte-identical after** (quality lane F7).

Pipelines that the new §7 rules *reject* (mixed fork closure, aggregators inside bound
regions) are accepted breaks under ruling 22 — migrate the offending examples/tests in
the same workstream that lands the rejection.

New lists (unchanged from rev 2.1):

```yaml
collectors:
  - name: page_stitcher           # EXPAND-group closer; SAME batch-transform plugin contract as the aggregator
    plugin: stitch_pages
    on_success: assembled_out

scopes:
  - name: document_pages          # scope_id; in the canonical form via the collector's node config
    opener: pdf_explode           # multi-row transform (creates_tokens=True); aggregators cannot open
    closer: page_stitcher         # MUST be a collector
    policy: require_all           # REQUIRED, no default
    on_group_failure: quarantine  # quarantine | escalate (escalate requires an enclosing bound group)
```

Builder resolution: ONE binding registry `group_bindings` (closer name + kind per bound
group source), from which `_branch_to_coalesce`/`_branch_to_row_union` views are derived
for the routing code that wants them. Exclusivity: each frame binds at most one closer
(subsumes `processor.py:488-499`'s pairwise checks). The collector binding is a
conditional node-config key on the collector — and it must serialize omitted-when-`None`
or the `composition_content_hash` pins redden and persisted state bindings break (the
2026-08-20 serialisation-contract discipline; canonical-form treatment otherwise
unchanged from rev 2.1; `_edge_to_canonical_dict` untouched; no DIVERT edge — DIVERT is
documented failure semantics, `contracts/enums.py:155-171`).

**Rejected alternative (carried from rev 2.1):** synthetic branches on the old coalesce
machinery — fatal because branch rosters were config-static, coalesce grouped by shared
`row_id` (`builder.py:1504-1517` rejects exactly that shape in the row_union walk), and
coalesce is N→1 merge, not N→M batch. Rev 3 inverts the direction: instead of pushing
dynamic groups into the static machinery, the static machinery becomes a case of the
general one.

## 4. Token model — the lineage path (Workstream 1, the rewrite)

### 4.1 The field

`TokenInfo` (`contracts/identity.py`):

```python
lineage_path: tuple[LineageFrame, ...] = ()   # outermost first

@dataclass(frozen=True, slots=True)
class LineageFrame:
    kind: FrameKind          # FrameKind.FORK | FrameKind.EXPAND (contracts enum)
    group_id: str            # non-empty; minted at the opening operation
    member_key: str          # FORK: branch name; EXPAND: member token_id
```

**The tri-field (`fork_group_id`, `join_group_id`, `expand_group_id`) and `branch_name`
are RETIRED from `TokenInfo` as stored fields.** The facts survive as derived accessors
reading the path — `branch_name` = innermost FORK frame's `member_key`, `fork_group_id` =
its `group_id`, `expand_group_id` = innermost EXPAND frame's `group_id` — so the wide
consumer surface (routing, executors, explain) keeps its call syntax while the
representation is single. Derived accessors are NOT dual representation — one source of
truth, zero shims, no fallback reads.

**`join_group_id` (resolved per review):** a merge is an event, not a membership, so it
does not derive from the path and leaves `TokenInfo`. Its consumers are enumerated with
a named replacement each — "read the audit row" alone was rejected as a hot-path
regression:

| Consumer | Replacement |
|---|---|
| `orchestrator/outcomes.py:257` (COALESCED accounting — hot path) | the join context rides the in-memory carrier already in hand — `TokenWorkItem` or a `RowResult` field; the site consumes a `RowResult`, and an in-process leader merge may not hold a work item for the freshly minted merged token, so the concrete carrier is settled when the plan touches the file. The pinned commitment: never a DB query on the accounting path. Same for `sink.py:760` — SinkExecutor buffers TokenInfos across WorkItem boundaries (`identity.py:26-28`), so if no work item is in hand the join context rides the buffered entry |
| `engine/processor.py:2856-2942` resume-start dispatch | reclassifies on `(lineage_path shape, work-item barrier fields)` — the tri-field combination patterns are retired with the fields |
| `orchestrator/resume.py:356` discriminator | same reclassification |
| `executors/sink.py:760` sink finalization | merged token's work item |
| `coalesce_effects.result_join_group_id` UNIQUE + composite FK onto `tokens.join_group_id` (`schema.py:1206/:1218/:1229-1230`) | the `tokens.join_group_id` COLUMN stays (audit row for the merged token); the FK constraint is why — it constrains and is kept |
| `TokenWorkItem` | keeps `join_group_id` for merged tokens (an event attribute of that work item, not lineage) |

### 4.1a Accessor equivalence contract (rev 3.1 — the WS1 delta enumeration)

Today's tri-field semantics are **destructive** — verified: `fork_token` drops
`expand_group_id`, `expand_token` drops `fork_group_id` (while inheriting `branch_name`
in memory only — the durable tokens row for an expand child carries neither,
`data_flow/tokens.py:1376-1384`), and `coalesce_tokens` drops all three
(`engine/tokens.py`). The derived accessors are **preservative**. Under ruling 26 the
accessors are **corrected from day one** — no today-equivalent shim, no
preservative-later phase. The intended WS1 deltas, exhaustively:

| Topology | Accessor vs today (in-memory / durable-row) |
|---|---|
| plain; fork-only child; expand-only child (no enclosing fork) | identical to both truths |
| expand child inside a fork branch | `branch_name` = today's in-memory value (durable row said None — the two truths already disagreed; the accessor adopts the in-memory one); `fork_group_id` regains the branch's group (both truths said None) |
| fork child inside an expand | `expand_group_id` regains the outer group (today None) |
| merged (post-coalesce) token under outer frames | outer FORK/EXPAND membership visible (today all None). Required for whole-roster settlement at the outer closer |
| merged token, top-level | all None — identical |
| row_union released token | `branch_name`/`fork_group_id` become None (frame popped at release, ruling 27; today deliberately retained, `processor.py:3043-3048`). A WS1 delta — the pop is model semantics, so row_union fixtures change at the WS1 checkpoint, not later |

Every consumer that discriminates on the exact field *combination* is rewritten
path-aware inside WS1, with its decision pinned: the resume-start dispatcher
(`processor.py:2881/:2906` — raises `OrchestrationInvariantError` on unknown patterns),
branch→sink routing on `branch_name is not None` (`token_traversal.py:782/:849`),
`create_token`'s mutual-exclusivity/`branch_name ⇒ fork_group_id` checks
(`data_flow/tokens.py:363-372` — retired with the columns), `resume.py:356`, and
`sink_effect_identity.py:55` (sink-effect identity uses `expand_group_id` — its identity
inputs must be pinned pre/post). The frozen invariant is **decisions, not field values**:
routing targets, dispatch arms chosen, dispositions, and sink-effect identities must not
change on any topology that remains buildable under §7 and is outside the deltas tabled
above (rulings 22/23/25/27 deliberately reject or change some today-expressible
topologies — those are migrations, not checkpoint failures); the field values feeding
the decisions change exactly as tabled.

### 4.2 Minting rules (exhaustive — every primitive)

| Primitive | Rule |
|---|---|
| source row → root token | `lineage_path = ()` |
| `fork_token` | child for branch *b* gets `parent.lineage_path + (LineageFrame(FORK, fork_group_id, member_key=b),)` |
| `expand_token` (EVERY expansion, declared opener or not — aggregation flush emission included) | child *i* gets `parent.lineage_path + (LineageFrame(EXPAND, expand_group_id, member_key=child_i.token_id),)` |
| any non-minting transform | `with_updated_data()` preserves the path (`dataclasses.replace`, `identity.py:110` pattern) |
| `coalesce_tokens` / row_union release | **strict pop (ruling 24 as amended by 28):** pops the innermost frame, which MUST be the closer's own FORK frame. A TRUE invariant now — §7 rule 5 forces every opener inside the region to be bound and close in-region, so at release nothing can sit below the closer's frame; violation is legitimately `OrchestrationInvariantError` (engine/validation bug, unreachable from config). Parents must share their full remaining path |
| row_union release, specifically | pops like coalesce, on EACH of the N released tokens (ruling 27). Released tokens no longer carry branch identity in the path — a deliberate break from today's documented retain-identity behaviour (`processor.py:3043-3048`), accepted under ruling 22. Downstream consumers needing the pre-union branch read audit rows. This also retires the `_row_union_group_released` staleness hazard structurally: a popped frame can no longer authenticate a loss under the §6.2 guard |
| collector release | strict pop of the collector's own EXPAND frame — same invariant, same reasoning |
| resume reconstruction | codec-pure from the journal row (§4.3), cross-checked against the frames table |

**Uniform push means uniform truth:** there is no declared/undeclared distinction in the
token model. Declaration lives entirely in the binding registry. An unbound frame is
inert — no losses staged for it, no roster watched (best-effort by construction; ADR-020
posture, now structural at the model level). **Inside a bound region there are no inert
expands (ruling 28):** an undeclared multi-row transform inside a coalesce branch —
legal today with N=1 via the binding-survives-expansion posture
(`token_traversal.py:254-262`) — becomes a `GraphValidationError`: wrap it in a scope
whose collector closes before the region's closer. An accepted break (ruling 22),
migrated with the other ruling casualties; it converts a data-dependent runtime
ambiguity (N decided per-input at runtime) into a build error, and is what makes the
strict pop rule and one-token-per-member statically true everywhere.

Opener/frame logic lives INSIDE `TokenManager.fork_token`/`expand_token` — never at call
sites (rev 2.1 ruling; `expand_token` has two callers at the TokenManager layer,
`token_traversal.py:241` and `processor.py:1623`, and per-site wiring is the
accreting-callers failure class).

### 4.3 Persistence

- **`token_lineage_frames`** — replaces the tri-columns on EVERY table that carries
  them (see the retirement matrix below): `(token_id, run_id, depth, kind, group_id,
  member_key)`, PK `(token_id, run_id, depth)`, INDEX `(run_id, group_id, member_key)`.
  Written in the token-INSERT transaction (`core/landscape/data_flow/tokens.py` owns it).
- **`group_records`** — minted for **EVERY group-opening operation, bound or not**
  (rev 3.1/3.2 — resolves the WS1/WS2 ordering collision: WS1 lands the table complete
  without knowing bindings): `(run_id, group_id, kind, opener_token_id, member_count,
  created_at)`, PK `(run_id, group_id)`. EXPAND records mint in the opener's expansion
  transaction (one row per aggregation flush is accepted audit enrichment; the mint is
  gated on `creates_tokens=True` — a plain filter returning zero rows is not an
  expansion); FORK records mint in the fork transaction as uniform audit enrichment —
  the FORK roster AUTHORITY remains config (declared branches), the record is the
  durable cross-check. The three new tables enter the portable export at the WS1 flip
  (manifest rotation adjudicated once, there). **`member_count=0` is legal and
  REQUIRED at any multi-row transform whose expansion is empty** — the zero-row path
  (`token_traversal.py:198-226`, `success_empty()`, never calls `expand_token` —
  verified) is changed to mint the empty record unconditionally, giving the
  `require_all` empty-group failure its referent (rev 2 fix, generalized). This makes
  the empty-expansion mint testable inside WS1, before `scopes:` exists (quality F8).
- **`group_losses`** — ONE ledger, replacing `coalesce_branch_losses`:
  `(loss_id PK, run_id FK, closer_name, group_id, member_key, token_id, reason,
  recorded_by, recorded_at, adopted_epoch)`, UNIQUE `(run_id, closer_name, group_id,
  member_key)`. The rev-2 key-collision hazard is structurally gone: the key is
  group-scoped and sibling members' inner FORK groups have distinct `group_id`s.
  `token_id` is recorded for lineage-corruption detection (same-key different-token
  raises Tier-1, as the old ledger documents). Loss `reason` values stay within the
  categorical branch-loss vocabulary — bare shared tokens, never prose.
- **Column retirement matrix (rev 3.1 — the tri-columns live on FOUR tables, not one;
  verified in `core/landscape/schema.py`):**

  | Table.columns | Disposition |
  |---|---|
  | `tokens` tri-columns + `branch_name` (`:611-614`) | DELETED — replaced by `token_lineage_frames`; `tokens.join_group_id` alone stays (merged-token audit row; anchors the `coalesce_effects` FK) |
  | `token_outcomes` tri-columns (`:663-665`) | DELETED — outcome consumers (`mcp/analyzers/reports.py:706-720` fork/join counts) re-derive from the frames table |
  | `token_work_items` `fork_group_id`/`expand_group_id` + `branch_name` (`:725-728`) | DELETED — `lineage_path` rides the work item instead. `token_work_items.join_group_id` is KEPT (ruling 20: the work item is the merged token's join-context carrier; the `schema.py:851` COALESCED predicate stays on it) — allowlisted in the retirement guard |
  | `coalesce_branch_losses.branch_name` (`:1046`) | table replaced wholesale by `group_losses` |

  `TokenWorkItem`'s barrier BINDING fields (`coalesce_node_id`, `coalesce_name`,
  `row_union_name`, `barrier_key`) are the closer's address, not lineage — they stay.
  Its lineage fields (`branch_name`, tri-field, `contracts/scheduler.py:138-141`) are
  retired with the model; keeping them beside `lineage_path` would be dual
  representation by the back door (systems M2). `join_group_id` stays on the work item
  for merged tokens (§4.1).
- **Journal/codec purity (rev 2.1, carried):** `lineage_path` serializes onto
  `TokenWorkItem` (`contracts/scheduler.py:107-141`); `token_from_journal_item`
  (`payload_codec.py:74-103`, documented "no engine access") reconstructs purely;
  restore integrity-checks codec-vs-table **bidirectionally**.
- Pre-release posture: schema lands, dev databases wiped (`auth.db` never). No
  migrations, no compat columns. The retired columns are DELETED, not deprecated.

### 4.4 Replay predicates (Tier-1 — the single biggest risk surface)

All barrier/mint replay reconciliation is rewritten against the path:
`_reconcile_fork_replay` (`data_flow/tokens.py:546-587`) asserts each replayed child's
persisted frames equal parent's + its own FORK frame (the old `expand_group_id is None`
assertion at `:575` is retired WITH the column); expand replay asserts parent's + own
EXPAND frame and reuses the existing idempotency against `batches.expansion_group_id`
(`data_flow/tokens.py:1254-1330`) extended to `group_records` — a re-drive can never
mint a second group; coalesce/row_union/collector replay assert the **strict** pop rule
(innermost frame = the closer's own; §4.2).
Divergence remains `AuditIntegrityError`. Every replay fixture in the tree is rebuilt
under the §11 frozen-oracle protocol. This is where the schedule risk concentrates; §11
names the checkpoint.

## 5. Roster accounting at closers (Workstream 4)

Uniform across closer kinds; only the roster authority differs.

- **minted:** FORK — the fork's full declared branch list, which §7 requires to EQUAL
  the closer's declared branches (whole-roster, ruling 23); EXPAND —
  `group_records.member_count` cross-checked against `DISTINCT member_key` in
  `token_lineage_frames` at the group. Mismatch = integrity error.
- **settled:** arrived (a consumed token whose innermost-relevant frame names the
  member) or lost (adopted `group_losses` row naming the `member_key`).
- **Closure = minted == settled as identity sets.** Duplicate arrival of the SAME token
  for a settled member is a CAS-fenced idempotent skip (`barrier_adopted_epoch` fence —
  lease-expiry redelivery is by design; rev 2 ruling, carried). Two *distinct* tokens
  for one member is build-time impossible EVERYWHERE (§7 rule 5: every opener inside a
  bound region closes in-region, so each member presents exactly one token by
  construction); its occurrence at runtime is legitimately an integrity error — an
  engine or validation bug, not a config shape.

**Collector executor** (unchanged from rev 2.1): own executor, `executors/aggregation.py`
untouched; buffers keyed per group; flush order = opener expansion ordinal — resolved
via the member's EXPAND frame `member_key` → the OPENER's `token_parents.ordinal` row,
never the arriving token's own parent chain (a member whose subtree forked-and-coalesced
arrives as a merged token with a fresh token_id; arch review, minor 3) — never arrival
order, which is unrecoverable after takeover; group-relative
`validated_quarantined_indices` (`aggregation_result.py:14-51`) reused; passthrough
prohibition (`:83-84`) unchanged; all-quarantined guard (`aggregation.py:546-550`) and
no-output guard (`:527-532`) replicated with the same semantics.

**Coalesce/row_union executors:** merge/union logic, timeouts, and plugin-visible
behaviour untouched. What changes is underneath: their arrival/loss bookkeeping reads
the unified roster, and their failure paths stop writing terminals directly (§6).
**Pending-state re-keying (rev 3.1, arch M1 — real scope, not bookkeeping):** the
coalesce executor keys pending state by `(coalesce_name, row_id)` — the in-memory
pending dict (`coalesce_executor.py:511-512`), the checkpoint scalars
(`CoalescePendingScalars`, `:577`), the landscape completion check (`:803/:811`), and
completed-key dedup (`:1470/:1516`). §7 legalizes forks inside bound EXPAND regions, and
expand siblings share `row_id`, so sibling members forking into the same coalesce NODE
are distinct concurrent FORK groups colliding under the current key. All four surfaces
re-key to `(coalesce_name, fork_group_id)` — durable-state shapes, priced into WS4.

**Flush condition:** collector = `end_of_group` only, no trigger config (count/timeout
inexpressible — a timeout on a closer converts a liveness bug into a silently short
group). Coalesce keeps its shipping policy/timeout semantics. The drain loop's
`has_blocked_barrier_work` (`engine/orchestrator/leader_drain.py:511`) counts
collector-buffered members or the fixpoint exits early (rev 2, carried). The EOF
non-empty-buffer abort (`leader_drain.py:481-491`) fires post-exhaustion — B3's
dishonest shape — so the real guarantees are settlement completeness plus the §8 gate,
not the drain abort.

## 6. Settlement, losses, escalation (Workstream 3 — ONE channel)

### 6.1 The single seam, made true

One routine — settle-member — is called by every terminal-disposition path, replacing
the three-arm `_notify_barrier_of_lost_branch` plus its bypasses. It walks the failing
token's `lineage_path` from the innermost frame to the first BOUND frame and stages one
`GroupLossSpec` for that frame's member. The known bypass sites are all retired into it,
each verified in rev 1/2 review:

1. The three raw `record_token_outcome` calls inside `coalesce_executor.py` (`:841,
   :1054, :1254`).
2. The non-empty-flush `QUARANTINED_AT_SOURCE` asymmetry (`processor.py:1643-1646` vs
   `:1441-1446`).
3. **Row_union's unconditional in-line group failure** (`processor.py:3028-3103`) — with
   a bound enclosing frame it defers (stages the enclosing loss at intake via
   escalation); with none, v1 fail-closed behaviour is preserved verbatim.
4. The coalesce-failure `FAILURE`/`UNROUTED` path (`processor.py:3265-3290`) — same
   treatment: enclosing bound frame ⇒ deferred verdict; none ⇒ today's behaviour.
5. `(TRANSIENT, BATCH_CONSUMED)` aggregator-flush terminals (`processor.py:1621-1652`)
   are retired by construction — **aggregators are banned inside ALL bound regions**
   (ruling 25, superseding rev 2's collector-region-only ban; precedent
   `builder.py:1504-1517` reasoning). This closes the loss-blindness gap (a lost batch
   member invisible to a coalesce roster) instead of preserving it. Outside bound
   regions no roster is watching — unchanged.

**Scope of the behaviour delta (rev 3.1, systems M4 — stated honestly):** under
unification every fork→coalesce region IS a bound group, so items 3–4 change the failure
behaviour of every existing nested-barrier pipeline with zero YAML change (today the
inner failure bypasses the seam and the outer barrier waits or times out; now the
verdict defers and settles the enclosing member). This is the intended improvement,
accepted under ruling 22 — but "unbound pipelines untouched" in §9 means pipelines with
NO bound group anywhere, a set that excludes every fork pipeline. Release-note it that
way.

The false docstring (`processor.py:3121-3155`) and the "at most one arm yields results"
claim (`:3139-3141`) are deleted with the arms they describe. `WorkItem` carries no
binding (unchanged); the trap comment lands there.

### 6.2 Loss staging — one spec type, one guard

`GroupLossSpec(closer_name, group_id, member_key, token_id, reason)` replaces
`BranchLossSpec` (`contracts/scheduler.py:70-86`, including its overloaded
`coalesce_name`). Per-claim staging: **at most one loss per bound frame per claim**,
riding the claim's disposition transaction (the singular `branch_loss` parameter through
`dispositions.py:162-229` / `scheduler_repository.py:492-620` becomes a per-frame
collection — that plumbing is in WS3's blast radius, rev 2 finding).

**One guard, frame-authenticated (rev 2.1's fix, now the only rule):** a staged spec is
legal iff the claimed token's own `lineage_path` contains a frame matching
`(group_id, member_key)` — self-authenticating because frames are minted by openers,
never asserted by failing code. The old token-equality guard
(`take_claim_branch_loss`, `scheduler_drain.py:996-1006`) is retired with
`BranchLossSpec`; for a FORK frame the new guard is exactly as strong (the branch token
carries its own branch frame), and for EXPAND frames it is the correct generalization
the old guard crashed on. Row_union's release-time pop (§4.2) closes the one case where
"exactly as strong" failed in rev 3: a post-release terminal can no longer name a
popped frame.

**Adoption-context staging (rev 3.1, arch minor 2 — the guard where no claim exists):**
escalation losses are staged in the adoption transaction (§6.3), where there is no
claimed token to authenticate against. There the spec authenticates against the durable
roster authority instead: the `(group_id, member_key)` must match the enclosing group's
own frames — a `token_lineage_frames` row at that group (EXPAND) or the declared branch
list (FORK). Same self-authentication property, different witness.

Staging is record-then-notify, unconditional, before any in-memory notify
(`processor.py:3191-3210` discipline, carried). Followers stage the innermost bound
loss only (`processor.py:3216-3218` shape, carried). **Takeover restore reads the FULL
table** regardless of `adopted_epoch` (the §E.4 pattern verified in
`scheduler/branch_losses.py:193`) — a stated requirement with its own test, not an
inheritance by analogy.

### 6.3 Escalation (intake-only, fixpoint, wait-for-settlement — carried intact)

1. Leader-only, computed at barrier intake, staged in the **adoption transaction**
   (guard per §6.2's adoption-context rule).
2. On a closer's FAIL verdict: stage one loss against the enclosing bound frame, notify
   the enclosing executor, iterate to fixpoint (the drain loop is bounded and raises on
   non-convergence — `leader_drain.py:417, :514-518`, verified). Mid-run intake is
   one-pass-per-drain-cycle: a two-level failure settles its outer level one drain cycle
   after its evidence — acceptable latency, the EOF fixpoint guarantees completion
   (systems M7, stated for the record; the companion proposal carried this sentence and
   rev 3 dropped it).
   **The acceptance scenario (maintainer, 2026-08-21):** nesting is depth-unbounded in
   the MODEL — a single token failing in the thousandth layer unwraps level by level
   (each `require_all` verdict escalating one frame outward, survivors terminating
   `scope_group_failed`) until the outermost group's declared terminal handling fires
   and the parent source row is flagged for quarantine. Correctness is
   depth-independent by design. **Operational support is not (maintainer, same
   session):** per-transaction audit churn scales with depth — every token INSERT
   writes one `token_lineage_frames` row per layer, and settlement/escalation walk the
   stack — so deep nesting is explicitly an unsupported configuration, not a promised
   one. **The supported guarantee is 5 layers of bound-region nesting (maintainer
   ruling, 2026-08-21):** the builder enforces the boundary fail-closed — bound
   nesting deeper than 5 is a `GraphValidationError`, config-overridable for whoever
   knowingly accepts the churn (deeper-than-cap remains model-correct, merely
   unsupported). Five covers the realistic ceiling (batch→fork→batch→fork→batch, e.g.
   document explode → per-page fork → per-page section explode, is depth 4) at
   trivial per-insert cost. The guarantee is symmetric: the test matrix MUST exercise
   depth-5 settlement, escalation-to-quarantine, and resume (plan item). The
   fixpoint's non-convergence bound is derived at build from the actual depth (+
   margin), never a constant — today's `MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000`
   would collide with an override-deep unwind. A `best_effort` level still absorbs
   deliberately (item 4) — the full unwrap is the all-`require_all` case.
3. **Verdicts wait for settlement** — rendered only when the roster closes; escalated
   rows are materialized derivations, idempotent on the natural key, re-derivable at
   takeover, never authoritative.
4. Per-level policy: a `best_effort` closer absorbs (no escalation); this now applies
   uniformly — including a `best_effort` coalesce absorbing inside a `require_all`
   collector group.

### 6.4 Policy semantics (carried from rev 2.1, now uniform)

- Settlement propagation is policy-independent for every bound closer.
- All-members-lost: the roster settles with zero arrivals; the engine closes the group
  WITHOUT invoking the plugin (`all_members_lost`; not a failure under `best_effort`).
- `require_all` group failure: engine-performed disposition; plugin never invoked.
- Survivors: run to completion (no cancellation v1 — standing ruling), terminated as
  **`scope_group_failed`** at BOTH emission sites (`barrier_coordination.py:447-479`
  live release AND `:1438` restore reconcile).
- Empty expansion at a bound opener: `require_all` ⇒ group failure `empty_expansion`;
  `best_effort` ⇒ closes silently, parent keeps `SUCCESS / FILTER_DROPPED`. No
  single-member fast path. (The empty group record itself is minted at EVERY empty
  expansion, bound or not — §4.3.)

## 7. Build-time validation (Workstream 2)

Enforced for BOUND groups; pipelines with no bound group anywhere are untouched. New
rejections of previously-buildable shapes are accepted breaks (ruling 22):

1. Binding exclusivity: each frame binds at most one closer; collector requires a scope;
   a `collectors:` node with no scope is a build error; openers are multi-row
   transforms (aggregators cannot open).
2. **Whole-roster fork closure (ruling 23):** a fork is either fully bound — EVERY
   declared branch flows to the fork's single closer, and the closer's `branches:`
   equals the fork's branch list — or fully unbound (pure fan-out; no branch reaches
   any closer). A mixed fork (some branches to a coalesce, others direct to sinks —
   buildable today via the branch-keyed maps at `processor.py:483-499`, with only a
   subset-direction check at `builder.py:743`) is a `GraphValidationError`. Subset
   closure can be added additively later; the reverse narrowing never could be.
3. Well-nestedness: bound regions fully contain or are disjoint — partial overlap
   rejected. (Fork regions participate: a fork inside a collector-bound group is a
   nested region and must close inside it.)
4. **SESE walk, both directions** (rev 2.1, carried; applies to fully-bound fork
   regions too, which whole-roster makes consistent): forward — every path from the
   opener reaches the closer before any sink/terminal (sinks inside a bound region
   rejected flat; this walk is new mechanism, no precedent claimed); backward — every
   path into an in-region node originates at the opener (a coalesce declaring one
   branch from outside the region is a `GraphValidationError` at build, per the
   row_union precedent `builder.py:1462-1527`, not a runtime crash).
5. **Every token-creating operation inside a bound region is itself bound and closes
   before the region's closer** (ruling 28 — "a shape change inside a group must
   itself be a group"): forks close at an in-region coalesce/row_union (as before);
   multi-row transforms must be declared scope openers with an in-region collector; an
   undeclared expand inside any bound region is a `GraphValidationError`. Each member
   therefore presents exactly one token, statically. The unscoped-row_union
   nested-fork prohibitions (`builder.py:1462-1527`) remain for unbound topologies;
   inert expands stay legal outside bound regions (the batch posture).
6. **Aggregators rejected inside ALL bound regions** (ruling 25 — both output modes,
   every closer kind's region, fork→coalesce included). Migrate any example/test using
   aggregation inside a coalesce branch.
7. **Roster authority rule** (standing ruling): `require_all` legal exactly where a
   roster authority exists — declared branches or a bound EXPAND group. Aggregator:
   none, stays policy-free. (Observed, not committed: `end_of_source` is the degenerate
   run-wide roster; a source-opened group would give run-level `require_all` later.)
8. `escalate` at an outermost bound group = build error (standing ruling); outermost
   closers declare terminal handling.
9. `on_error` targets: the parse-time `validate_on_error` field validators
   (`core/config.py:834`, `:1364`) and the builder's error-edge wiring
   (`builder.py:1144-1184` — the transform block at `:1144` and the gate block at
   `:1164`; rev 3's `:1132-1142` citation pointed at the source-quarantine wiring) both
   learn that a bound region's closer is a legal `on_error` target from inside the
   region; omitted `on_error` inside a region derives from structure.
10. Composer parity in the same commit: `web/composer/state.py:6655-6699` lifted to the
    builder's validation; `tools/generation.py:489` explanation table gains the new
    codes. **Every new rejection site this section adds needs an adjudicated
    disposition in `config/cicd/runtime_rejection_parity.yaml` plus its Stage-1
    composer mirror** (the 2026-08-17 runtime-rejection-parity gate — roughly a dozen
    new sites across rules 1–9; quality F3). The new `collectors:`/`scopes:` config
    surface also fires the composer three-pin: the `pipeline_capabilities.md`
    canonical-field-inventory diff, the redaction-policy snapshot, and the frontend
    `guidedDecoder.ts` `exactRecord` lists — the last is invisible to every backend
    suite and must be an explicit checklist item.

## 8. Resumability (Workstream 5)

Both layers, both surfaces (rev 2, carried) — now covering ALL bound groups, so
coalesce/row_union gain B3 protection they never had:

1. Wedges cannot form: §6.1's single seam + the in-region aggregator ban.
2. Fail-closed satisfiability gate, shared implementation consumed by BOTH the advisory
   `can_resume` (`recovery.py:403`) and `ResumeCoordinator.resume()`'s enforcing chain
   (which never calls the advisory surface — verified; the source-lifecycle gate is the
   two-surface precedent): every minted member of every open bound group must be
   non-terminal, arrived, or named in `group_losses`; otherwise refuse with scope,
   group, member named. ADR-038 amended; the aggregation-violation pair in
   `tests/integration/audit/test_contract_violation_token_outcomes.py`
   (`test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens` /
   `test_aggregation_count_flush_violation_abandons_tokens_at_finalization`) gains a
   third sibling for the group-satisfiability refusal.

## 9. Cost & complexity ledger

Sizes: S ≈ a focused session, M ≈ 1–3 sessions, L ≈ a multi-session campaign, XL ≈ L
plus a full audit-fixture rebuild.

| # | Workstream | Size | Tier-1? | Principal files | Dominant risk |
|---|---|---|---|---|---|
| 0 | Docstring/invariant corrections | S | no | `processor.py`, docs | none — standalone value |
| 1 | **Lineage rewrite**: `LineageFrame`/`lineage_path`, tri-field retirement per the §4.3 four-table matrix + derived accessors + path-aware consumer rewrites (§4.1a), `token_lineage_frames` + universal `group_records`, ALL replay predicates, journal codec + `TokenWorkItem` lineage-field retirement, empty-expansion mint | **XL** | **YES** | `contracts/identity.py`, `contracts/scheduler.py`, `engine/tokens.py`, `core/landscape/data_flow/tokens.py`, `payload_codec.py`, `token_traversal.py`, `schema.py` (incl. `:851` predicate), checkpoint/journal restore (`queue.py`, `work_items.py`, `restore_read_model.py`, `scheduler_work_codec.py`), and the full consumer roster: `web/execution/{schemas,diagnostics,accounting}.py`, `landscape/exporter.py`, `contracts/audit.py`, `contracts/export_records.py`, `execution/sink_effect_identity.py` + `_finalization.py`, `mcp/analyzers/*`, `tui/widgets/lineage_tree.py`, `lineage.py`, `model_loaders.py`, `query_repository.py`, `web/frontend/src/types/index.ts` (+ its two test files) | THE risk concentration: ~623 src tri-field refs across ~45 files (+~800 in tests; `branch_name` counts include config-level uses that are NOT retired — the guard must distinguish); a missed direct-field read is a silent wrong answer; §4.1a semantic drift under nesting is the failure the suite alone cannot catch |
| 2 | Config + unified binding registry, bidirectional SESE, whole-roster/nesting/fork-close/aggregator rules, canonical conditional key, canonical-hash pin corpus, composer parity + rejection-parity adjudications + three-pin | L | hash-adjacent | `core/config.py`, `core/dag/builder.py`, `schema_validation.py`, `graph.py`, `core/canonical.py`, `web/composer/state.py`, `tools/generation.py`, `config/cicd/runtime_rejection_parity.yaml`, `guidedDecoder.ts` | Composer/runtime drift; hash stability must be pinned BEFORE the workstream, not after |
| 3 | Single settlement channel: settle-member seam, `GroupLossSpec` + frame guard (claim + adoption contexts), `group_losses` ledger + full-table restore, escalation fixpoint, coalesce/row_union failure paths rerouted | L | **YES** | `processor.py` (incl. `:3028-3103`, `:3265-3290`), `scheduler_drain.py`, `barrier_coordination.py`, `coalesce_executor.py`, `dispositions.py`, `scheduler_repository.py`, `scheduler/branch_losses.py` (replaced), `contracts/scheduler.py`, `schema.py` | Multi-worker concurrency; the §6.1 behaviour delta on ALL fork pipelines; mutation-test the frame guard with enumerated mutants (plan item) |
| 4 | Collector executor (rosters, CAS arrivals, frame-resolved ordinal flush, empty-group close) + coalesce/row_union bookkeeping onto unified rosters + **pending-state re-keying `(coalesce_name, row_id)` → `(coalesce_name, fork_group_id)`** (§5 — checkpoint scalars, completion check, dedup keys: durable state) | L | partially | NEW `executors/collector.py`, `CollectorSettings`/`ScopeSettings`; `coalesce_executor.py` (`:511-512`, `:577`, `:803/:811`, `:1470/:1516`) / row_union internals; `executors/aggregation.py` + `engine/triggers.py` **untouched** | Group-relative quarantine seam; re-keying touches checkpoint shapes; regression risk to shipping coalesce merge behaviour (its tests are the guard) |
| 5 | Satisfiability gate both surfaces; `has_blocked_barrier_work` counts collectors; ADR-038 | M | yes | `recovery.py`, `engine/orchestrator/resume.py`, `engine/orchestrator/leader_drain.py`, tests | False-refuse; prefer exact-reason refusal |
| 6 | Disposition vocabulary (`scope_group_failed` both sites, `empty_expansion`, `all_members_lost`), landscape/MCP query surface for `group_records`/`group_losses`, explain surfaces re-pointed (`mcp/analyzers/queries.py:203-207`, `reports.py:706-720`), ADR + docs | M | no | `barrier_coordination.py`, `contracts/enums.py`, `mcp/analyzers/*`, docs | Acceptance criterion: given a failed nested group, an operator reconstructs from audit rows via the landscape MCP tools alone — which member failed, with what reason, through which escalation chain, why each survivor terminated |

**Aggregate:** roughly one workstream-equivalent over rev 2.1 — WS1 is XL (four-table
retirement + full consumer roster + fixture rebuild) while WS3 shrank (one channel,
three special cases deleted) and the two-ledger/two-spec/two-guard surfaces never get
built; WS4 gained the re-keying. Three new tables replace what would have been three new
plus one widened. The trade the maintainer accepted: more risk and cost NOW, in exchange
for an audit model with one lineage truth before production locks it in.

**What this does NOT cost:** pipelines with no bound group anywhere — config untouched,
canonical hash pinned byte-identical by the §3 corpus test. Coalesce/row_union
plugin-visible merge behaviour unchanged. The §6.1 failure-path delta applies to every
fork pipeline and is release-noted as such (not as "unbound untouched").

## 10. Decisions

**Standing maintainer rulings (2026-08-21, in-session; this spec is the record):**
1. No cancellation v1 — survivors bill in full, terminate as `scope_group_failed`.
2. `escalate` at an outermost bound group = build error.
3. Completeness requires a roster authority.
4. Aggregator/collector split — aggregator is a window, never a closer, untouched.
5. **UNIFY NOW (rev 3):** one lineage/settlement system across fork/coalesce/row_union
   and scopes; tri-field retired; accepted as high-schedule-risk to avoid a
   two-representation audit model reaching production.

**Taken by this spec (rev 2→3, from review findings — flag any for reversal):**
6. `LineageFrame(kind, group_id, member_key)`; member identity in the frame.
7. `group_records` is the EXPAND roster authority; empty groups minted durably.
8. Aggregators banned inside bound regions; forks close before the region's closer.
9. Escalation intake-only in the adoption transaction; verdicts wait for settlement.
10. Duplicate arrivals are CAS-fenced idempotent skips.
11. Collector flush order = opener expansion ordinal.
12. Resume gate = shared implementation on both surfaces.
13. Empty/all-lost groups close without plugin invocation.
14. No DIVERT edge for bindings; no new `RoutingMode`.
15. Collector policy v1 = `require_all|best_effort`; coalesce keeps its four; row_union
    stays `require_all`.
16. One frame-authenticated claim guard; token-equality guard retired with
    `BranchLossSpec`.
17. `lineage_path` rides `TokenWorkItem`; codec stays pure; frames table cross-checked
    bidirectionally.
18. Frame logic internal to `TokenManager` primitives; openers are multi-row transforms.
19. Bound coalesce/row_union failures defer to the enclosing closer; unbound keep
    today's behaviour verbatim.
20. `join_group_id` leaves `TokenInfo`; consumers per the §4.1 replacement table (work
    item for hot paths, audit rows otherwise); the `tokens` column stays for the
    `coalesce_effects` FK.
21. Derived accessors (`branch_name` etc.) are the ONLY read path for legacy names; any
    direct column/field resurrection is a defect.

**Maintainer rulings, 2026-08-21 post-review (rev 3.1):**
22. **Pre-1.0 break posture:** no shipping pipelines exist (the 0.3-era line was forked
    as CLI-only); backward compatibility does not constrain this design; breaks that
    narrow or correct the model are taken now because they are never available again.
23. **Whole-roster fork closure:** a fork closes entirely at one closer or not at all;
    mixed branch→closer/branch→sink topologies are build errors. Subset closure stays
    available as a future additive extension.
24. **Strict pop (as amended by ruling 28):** a closer pops exactly its own frame —
    the innermost, which §7 rule 5 guarantees statically; violation is a genuine
    engine invariant. Inert expands remain legal outside bound regions (the batch
    posture); the replay predicates assert the same rule. (The rev 3.1 generalized-pop
    form is superseded — it existed only to tolerate inert frames inside bound
    regions, which ruling 28 removes.)
25. **Aggregators banned in ALL bound regions** (fork→coalesce regions included),
    closing the `BATCH_CONSUMED` loss-blindness gap; offending examples/tests migrate.
26. **Accessors corrected from day one:** no today-equivalent shim; the WS1 checkpoint
    is the §4.1a enumerated-delta contract against §11's frozen invariants.
27. **Row_union pops frames on release** (all N released tokens); the documented
    retain-branch-identity behaviour is retired; downstream reads audit rows.
28. **A shape change inside a group must itself be a group** (maintainer, 2026-08-21:
    "if you're declaring inside another span you have to be a span yourself — you
    can't change the shape on only one line of effort within a span/group"): every
    token-creating operation inside a bound region must be bound and close in-region —
    forks coalesce before the enclosing closer, expands are declared scopes whose
    collector closes before it, and vice versa across the nesting (fork-in-batch and
    batch-in-fork-line are both legal, each closing inside the other). An undeclared
    expand inside a bound region is a `GraphValidationError`. Statically eliminates
    the duplicate-distinct-arrival ambiguity: one token per member, everywhere, by
    construction.

**Still open:** none — §2 vocabulary signed off 2026-08-21; the spec is complete.

## 11. Delivery sequencing and risk posture

WS0 → WS1 → WS2 → (WS3 ∥ WS4 skeletons) → WS3+WS4 integration (own line item, own
multi-worker tests) → WS5 → WS6.

**Risk posture (the ruling's own terms — high risk, taken deliberately):**
- **Why now, on the record:** the tolerance window for this class of change closes at
  launch and never reopens. Post-launch, users will tolerate catastrophic data loss for
  a few weeks at most; after that, a lineage-model rewrite that invalidates recorded
  audit history becomes permanently unshippable — the "audit hell" lock-in. The
  pre-release wipe posture is the entire reason rev 3 is cheap enough to do at all:
  no migrations, no dual-read compatibility, retired columns deleted outright. None of
  those options exist after launch.
- **The checkpoint is end of WS1 (rev 3.1 — restated per ruling 26):** full suite green
  with observable deltas ONLY in the §4.1a-enumerated lineage-metadata surfaces and the
  new audit rows (`token_lineage_frames`, universal `group_records`). The FROZEN
  invariants, pinned by oracle before WS1 starts: plugin-visible outputs, routing
  decisions, dispatch arms, terminal dispositions, sink-effect identities, canonical
  hashes (§3 corpus), and the `dag_scenario_corpus` group-id-normalized stable
  projections (`tests/fixtures/dag_scenario_corpus/schema.py` —
  `StableTokenProjection`, `SinkOutputProjection`, `StableTerminalDisposition`,
  `StableExpansionProjection`), which are byte-stable across the rewrite BY CONSTRUCTION
  and are diffed pre/post as the delta oracle. Every fixture surface is classified
  **frozen** (behaviour-bearing: projections, dispositions, sink bytes, ordinals,
  branch names, the 56 golden JSONs each individually adjudicated) or **regenerated**
  (representation-bearing: raw token/journal rows) BEFORE WS1 starts — a rebuilt
  fixture is never the oracle for its own rebuild. **The oracle set is versioned per
  workstream:** fixtures whose topology is rejected by rulings 23/25 (e.g. the
  `parallel-coalesces` corpus fixture — two coalesces over one fork, which rule 2
  rejects; `fork-multiple-terminals-partial-failure` is PURE fan-out, fully unbound,
  and stays legal/frozen — the rev 3.1 text misnamed it) stay frozen
  through the WS1 diff, then leave the frozen set at WS2 with an adjudicated migration
  recorded — so migrating a ruling casualty is distinguishable from tampering with the
  oracle. If WS1 cannot reach green-with-only-enumerated-deltas, STOP and surface to
  the maintainer — do not press into WS3 on a red foundation.
- **WS1 landing shape (rev 3.1 — the abort criterion made real):** behaviour-neutral
  prep slices first (consumers migrated to accessor call syntax while still backed by
  stored fields), then ONE atomic representation flip (schema, codec, replay
  predicates, fixture regeneration in a single reviewable change), then cleanup. The
  abort criterion — any landed state leaves the tree consistent, single representation
  per surface, no dual reads ever — is evaluated at those slice boundaries; schema
  columns are never half-deleted. Abandonment AFTER the flip leaves a rewritten Tier-1
  audit model whose payoff feature never shipped — accepted residue (the model is
  strictly more consistent than today's, whose in-memory and durable truths disagree).
- The tri-field consumer sweep is grep-exhaustive (`git grep` for each retired name,
  adjudicated per-table-column and per-surface, not per-name — `branch_name` has
  legitimate config-level uses that stay) and pinned by a lint/AST guard scoped to what
  a guard CAN enforce: no stored field with a retired name on `TokenInfo`/
  `TokenWorkItem`, no new columns with retired names outside the §4.1 allowlist
  (`tokens.join_group_id`), `token_lineage_frames` as the sole lineage write path. What
  the guard structurally cannot catch — accessor semantic drift under nesting — is
  covered instead by differential equivalence tests over the nested corpus fixtures
  (`sequential-nested-fork-coalesce`, `parallel-coalesces`) asserting the §4.1a table
  case by case.
- **Whole-tree gate obligations (rev 3.1):** WS1/WS3 rewrite Tier-1-dense files — the
  trust-tier corpus is diffed before/after each slice (add nothing; the gate's
  fail-closed corpus is the baseline, not zero). The campaign's churn invalidates any
  staged judge-signature bundle (bundles are exact-source-bound), so 0.7.2 allowlist
  signing sequences AFTER this campaign settles — do not stage across it.
- No calendar commitment is made or implied; the release-branch discipline holds; each
  workstream lands with tests. The implementation plan additionally owes: the
  edge-case→harness matrix (empty expansion bound/unbound, all-members-lost, duplicate
  arrival via lease-expiry, escalation fixpoint with nesting, resume-mid-group refusal
  AND happy path incl. collector-buffer takeover with ordinal flush, nested
  fork-in-collector runtime settlement — each bound to the specific existing harness it
  extends: `tests/e2e/recovery/` process-death/timing/chaos suites, the Postgres
  testcontainer lock-order suites, the CAS/adoption unit seams, the property suites)
  and the enumerated frame-guard mutants (guard on `group_id` alone / `member_key`
  alone; walk stopping at innermost instead of first-bound; walking outermost-first;
  escalation against the failing rather than enclosing frame; CAS fence removed;
  restore filtered by `adopted_epoch`), run with `-n 0`.
