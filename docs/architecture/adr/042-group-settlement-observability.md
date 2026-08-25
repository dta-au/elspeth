# ADR-042: Group Settlement Vocabulary and Observability — One Closed Reason Set for Coalesce/Scope Settlement, One Lineage Read Authority

**Date:** 2026-08-25
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** contracts, audit, lineage, coalesce, collectors, resume, amends-adr-019, amends-adr-038

## Context

The unified-lineage campaign (spec `docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md`,
rev 3.2) replaced the three per-token columns (`branch_name` / `fork_group_id`
/ `expand_group_id`) with ONE lineage truth: `TokenInfo.lineage_path`, a stack
of `LineageFrame(kind, group_id, member_key)` minted at every opening
operation and durably recorded in `token_lineage_frames`. Every bound group
— a fork closed by a coalesce, an expansion closed by a scope's collector —
settles its roster through one `group_losses` ledger and one settlement
channel.

That left an observability question with no single answer: **how does an
operator reconstruct a nested group failure from audit rows alone?** Three
things stood in the way.

1. **Settlement reasons were prose-adjacent strings written at emission
   sites.** The coalesce executor wrote `"late_arrival_after_merge"` as a
   literal, the collector executor wrote `"empty_expansion"` and
   `"all_members_lost"` as literals, and the intake coordinator carried a
   default of the same literal for a missing reason. Nothing pinned the set,
   and a new emitter could invent a spelling.
2. **One reason covered two different facts.** A member arriving at a
   coalesce after its group had already closed was always recorded as
   `late_arrival_after_merge` — whether the group had MERGED (a benign
   straggler behind a successful release) or had FAILED closed (a survivor
   of a group whose verdict was already failure). An operator reading the
   release context could not tell a healthy pipeline's straggler from a
   failed group's member. Spec §2 names the second case `scope_group_failed`
   and requires that it never be reported as a late arrival.
3. **The discriminator is not the obvious column.** A group that closed by
   FAILURE writes `node_states` rows at the closer with `completed_at` set,
   exactly as a merge does. "Completed" therefore cannot separate the two;
   only `status == COMPLETED` (a successful release) can. The row_union
   executor had already learned this (its holdless restore reconcile reads a
   released-only set), and the coalesce path had not.

## Decision

### 1. `GroupSettlementReason` is THE vocabulary for coalesce / scope-failure settlement

`contracts/enums.py` gains a `StrEnum` with exactly four members:

| Member | Wire value | Emitted when |
| --- | --- | --- |
| `LATE_ARRIVAL_AFTER_MERGE` | `late_arrival_after_merge` | a member arrives at a coalesce whose group already **released** (merged) |
| `SCOPE_GROUP_FAILED` | `scope_group_failed` | a member arrives at a coalesce whose group already **failed closed** |
| `EMPTY_EXPANSION` | `empty_expansion` | a collector closes a zero-member expansion without running the plugin |
| `ALL_MEMBERS_LOST` | `all_members_lost` | a best-effort collector closes a group every member of which was lost |

Emission sites reference members (`GroupSettlementReason.X.value`), never
the string. `tests/unit/engine/test_group_settlement_reasons.py` pins the
member set and AST-scans `src/` for any hand-written occurrence of a
vocabulary value outside the enum's own definition, so a literal cannot
land green.

**Scope of the "closed" claim — deliberately narrow (META-9.3).** The enum
closes the vocabulary for coalesce settlement and scope/collector closure.
It does NOT absorb row_union's own closed reasons, which stay where they are
in `engine/row_union_executor.py`: `row_union_branch_lost`,
`late_arrival_after_release`, and `row_union_group_failed`. Those are a
sibling vocabulary for a different closer with different release semantics
(N→N branch release, not a merge). Folding them would have made "closed"
mean "one enum for everything", which nothing needs and which would force
every row_union reader to learn coalesce reasons it can never see.

**Naming adjacency, stated so nobody "fixes" it.** `row_union_group_failed`
(the row_union executor's prior-failure closure constant) is NOT the
escalation ledger's `group_failed` (the `group_failed=` flag the settlement
channel passes when a group's own failure escalates its consumed members).
They are adjacent spellings for different purposes on different surfaces;
renaming one to reduce the adjacency is out of scope and was ruled against.

### 2. The merged-vs-failed discriminator is release status, carried in memory when known and read durably otherwise

`scope_group_failed` must never double as `late_arrival_after_merge`. The
coalesce executor now records the closure **flavor** alongside each
completed key (`_completed_keys: OrderedDict[key, bool | None]` — `True`
after a merge, `False` after a failed closure or a merge that raised,
`None` when the key was seeded by restore or backfilled from the
plain-completion Landscape lookup). The late-arrival arm uses the in-memory
flavor when it is known and otherwise asks the durable discriminator
`BarrierRestoreReadModel.has_released_group_for_node(run_id, node_id,
group_id)` — a status-COMPLETED `node_states` row at the closer for a token
carrying the group's frame — caching the answer. Restore seeding never
guesses "merged": the completed-key enumeration cannot tell the two apart,
so it seeds `None`.

The restore-time §E.3a reconcile in `engine/barrier_coordination.py`
(adopted-but-unreleased straggler against a Landscape-completed key) reads
the released-only set (`get_released_group_ids_for_nodes`) beside the
completed set and picks the reason by membership — the same released-only
read the row_union holdless reconcile already used.

The intake coordinator no longer defaults a missing reason: a late-arrival
`CoalesceOutcome` with `failure_reason is None` is an executor contract
violation (`OrchestrationInvariantError`) before any release write, because
the two flavors are not interchangeable and a default would silently pick
one.

### 3. `token_lineage_frames` is the sole lineage read authority; legacy names are derived

The three retired columns do not come back as read paths. Every consumer
that needs a branch name or a group id reads it from the token's lineage
path — `TokenInfo`'s derived accessors in memory, `token_lineage_frames`
durably. Wire projections that still carry the legacy names
(`branch_name` / `fork_group_id` / `expand_group_id` on the MCP `list_tokens`
row) derive them from the path via `contracts.identity.path_branch_name` /
`path_fork_group_id` / `path_expand_group_id` (ruling 21: the wire names
stay, the mechanism underneath changes). `tokens.join_group_id` is the one
surviving group column (the `coalesce_effects` FK anchor).

The operator surface for reconstructing a group failure is the Landscape
MCP analyzer over the three group tables — `group_records` (every
expansion, including empty ones), `group_losses` (the settlement ledger,
read whole, never filtered by `adopted_epoch`), and `token_lineage_frames`
(membership at every depth). The acceptance criterion is a depth-3 nested
failure reconstructed from those tools alone; the WS6 Task 7/8 slice lands
the dedicated `list_group_records` / `list_group_losses` /
`get_token_lineage` tools and the acceptance test that pins it
(`tests/integration/mcp/test_group_failure_forensics.py`). This ADR fixes
what those tools may read (the three tables and the path-derived
projections) and what they must never do (re-derive a binding from
`group_records`, which is binding-blind by spec §4.3).

### 3a. Collector release-group frames and merging closers (META-38, 2026-08-25)

A collector release carries its own release-group EXPAND frame innermost
for the rest of its life — a release group has no closer, so nothing ever
pops that frame. Every MERGING closer downstream of a collector (a
coalesce, an outer collector, the settle seam's consumed-token pop)
therefore closes its own frame by **guarded truncation**
(`contracts.identity.truncate_at_closer_frame`): it finds its frame by the
single guarded walk (`innermost_own_frame` — innermost → outward, skipping
only frames the written fact `group_records.closes_group_id` verifies as
collector release groups) and continues with the path *below* it. A frame
above the closer's own that is not a release group is an unclosed scope
inside a closing region and is refused (spec §7 rule 5; the build refuses
the buildable such shape, an unbound fork inside a bound region reaching
the enclosing closer, at validation). Every "this token's own group"
question — the collector arrival keying on the traversal, at intake
adoption, in `CollectorExecutor.accept` and in the journal restore, and the
coalesce anchor — uses that same walk; `lineage_path[-1]` is never the
answer.

**Deliberate, visible audit-shape change.** The merged continuation's
exported `lineage_path` OMITS the release frame(s) a parent carried: a
release token `(EXPAND outer, FORK a, EXPAND release)` merged at the fork's
coalesce yields a continuation with path `(EXPAND outer,)`, not
`(EXPAND outer, EXPAND release)`. Nothing is lost from the audit trail —
the release frame stays on the parent's own `token_lineage_frames` rows,
the continuation's `token_parents` rows name that parent, and the parent's
release group's `group_records.closes_group_id` names the group it closed
— but a reader reconstructing the continuation's full provenance must
follow `token_parents` to the parent's frames rather than read the
continuation's path alone. Pass-through closers (row_union, `pop_fork_frame`)
are unchanged: they release the ORIGINAL tokens and preserve every other
frame, release frames included.

**Survivor-hold payload shape differs by closer kind (META-40/41).** A
collector survivor's hold nests the group-failure cause under the
`ExecutionError` context (`error.context` carries `failure_reason`,
`lost_members`, `member_disposition`), while a coalesce survivor's hold
carries `member_disposition` top-level in the `CoalesceFailureReason`
payload beside `failure_reason` — a legitimate DTO difference between the
two executors' hold records, not drift to reconcile. Readers must not fail
open on a missing top-level `error["member_disposition"]` for collector
survivors; read each hold via the documented shape for its closer kind.

### 4. Resume protection (cross-reference)

The same three tables feed the fail-closed group-satisfiability resume gate
(`core/checkpoint/recovery.py::check_group_satisfiability_resumable`,
shared by the advisory `can_resume` and the enforcing `resume()` guard):
every minted member of every bound group must be lost (a `group_losses` row,
adopted or not), live (a frame-bearing token with no completed terminal), or
arrived (a journal row addressed to the closer). ADR-038 §3a carries the
sweep-asymmetry ruling that gate depends on.

### 5. Vocabulary frozen (2026-08-25, WS6 lane 1 — the declaration ruling 7878's lift trigger fires on)

The settlement vocabulary is FROZEN as of this declaration. Two things,
together:

1. **The closed `GroupSettlementReason` membership** (§1's table, exactly
   four members): `late_arrival_after_merge`, `scope_group_failed`,
   `empty_expansion`, `all_members_lost`. Scope is coalesce/scope-failure
   only (META-9.3): row_union's closed reasons (`row_union_branch_lost`,
   `late_arrival_after_release`, `row_union_group_failed`) are a sibling
   vocabulary and stay outside the enum; `row_union_group_failed` remains
   deliberately distinct from the escalation ledger's bare `group_failed`
   category token — naming adjacency, documented here, never renamed.
2. **The two survivor-hold payload shapes §3a documents**: collector
   survivors carry the group-failure cause nested under the
   `ExecutionError` context; coalesce survivors carry `member_disposition`
   top-level in the `CoalesceFailureReason` payload. The shape difference
   is part of the frozen contract — readers read per closer kind and never
   fail open on it.

**Enforcement tripwires** (all pre-existing; this declaration names them
as the freeze's witnesses):

- the AST literal canary
  (`tests/unit/engine/test_group_settlement_reasons.py`): pins the member
  set and scans all of `src/` for hand-written vocabulary literals
  (`ast.Constant` only — the concatenation/f-string limitation is
  documented and reviewed);
- the two META-41 re-frozen oracle snapshots
  (`fork-coalesce-policies` `require-all-lost-c` and
  `quorum-impossible-lost-c`, survivor `error_hash 36a9c100f8cd10fb`) —
  MD1-verified as the corpus's ONLY pin on the settle seam's terminal
  vocabulary; protect them accordingly;
- the WS6 acceptance test
  (`tests/integration/mcp/test_group_failure_forensics.py`): drives the
  operator-visible surface end to end at depth 3, §4 reading the
  structured `member_disposition` hold payload.

**The bar for any change after this freeze:** an amendment to this ADR
**plus** a META-class corpus re-freeze ruling (the META-39/META-41
precedent), with mutation evidence that the re-frozen snapshots fail
under the old vocabulary, and an export test selection that includes
`tests/integration/core/dag`. A member addition, removal, or rename, a
wire-value change, or a hold-payload shape change without both is a
freeze violation, not a refactor.

**Terminal-pair note (META-32):** collector survivors reuse the existing
`(SUCCESS, COALESCED)` terminal pair; a proposed collector-specific
terminal pair was REFUSED as a vocabulary change. That refusal's
rationale is covered by this freeze — a new terminal pair for group
settlement is a vocabulary change and takes the bar above.

## Consequences

- **Positive:** a release context's `reason` now answers the operator's
  first question — did this group succeed or fail? — without a join.
  `scope_group_failed` rows are the survivors of failed groups; every
  `late_arrival_after_merge` row sits behind a genuine release.
- **Positive:** the vocabulary cannot drift silently. A new emitter either
  imports the enum or fails the whole-`src` literal scan.
- **Behaviour change (operator-visible):** stragglers of FAILED coalesce
  groups that were previously recorded as `late_arrival_after_merge` are now
  recorded as `scope_group_failed`, in the live intake path, the takeover
  cache-miss path, and the restore-time reconcile. Run-status projection
  (`run_status_projection.py`) still excludes merge stragglers from the
  failed-barrier count by the enum's merge member; a `scope_group_failed`
  straggler counts toward a barrier that had already failed, which is the
  honest reading.
- **Cost:** one extra `has_released_group_for_node` point lookup on the
  first late arrival against a key whose flavor is unknown in memory
  (restore-seeded or FIFO-evicted). Subsequent arrivals hit the cache.
- **Not changed:** row_union's reasons and their readers; `CollectorOutcome`'s
  other failure reasons (`collector_missing_members`,
  `collector_transform_error`), which are collector-plugin outcomes rather
  than group-settlement dispositions and stay as plain strings.

## Alternatives Considered

- **One campaign-wide enum including row_union's reasons.** Rejected
  (META-9.3): different closer, different release semantics, no reader
  needs both; the fold would be churn for a claim nobody makes.
- **Discriminate on `completed_at`.** Wrong by construction: a failed
  closure sets `completed_at` on its closer states too. Only
  `status == COMPLETED` separates a release from a failure; that is why
  `has_released_group_for_node` / `get_released_group_ids_for_nodes` exist
  and must not be "simplified" onto the plain-completion reads.
- **Seed restored keys as merged.** Would report every post-resume
  straggler of a failed group as a benign late arrival — the exact defect
  this ADR removes, reintroduced on the path an operator is least likely to
  exercise.
- **Keep the coordinator's `or "late_arrival_after_merge"` default.** A
  default that is wrong for one of the two flavors is worse than a crash;
  the executor always emits a reason, so the fallback was dead until the
  day it would have been wrong.

## Related Decisions

- ADR-019 (two-axis terminal model; amended by WS1 for lineage frames).
- ADR-020 (batch posture — inert frames outside bound regions).
- ADR-029 / ADR-030 (the journal truths the satisfiability gate's arrived
  limb reads; §E.3a late-arrival release).
- ADR-038 §3a (the sweep-asymmetry ruling the resume gate depends on).
- The unified-lineage spec rev 3.2 (§2 vocabulary, §4.3 binding-blind
  `group_records`, §6.4 collector closure reasons, §8 resume gate).
