# ADR-038: Non-Terminal ABANDONED Path — Run-Death Fate Decision for Undecided Tokens

**Date:** 2026-08-06
**Status:** Accepted
**Deciders:** ELSPETH maintainers
**Tags:** contracts, audit, row-outcomes, run-lifecycle, amends-adr-019

## Context

ADR-019's two-axis terminal model gives a failed token exactly one fate,
`(FAILURE, UNROUTED)`, and exactly one non-terminal path, `BUFFERED`
("hasn't decided yet"). Filigree `elspeth-b4254f9a01` (child of
`elspeth-82d4c5146c`) documents the gap between those two: a token whose
fate **nothing will ever decide**, on a run that will never resume, has no
honest representation.

The reproduced symptom (pinned by
`tests/integration/audit/test_contract_violation_token_outcomes.py::test_aggregation_count_flush_violation_strands_tokens_permanently`):
a count-triggered aggregation flush raises `PluginContractViolation` from
input-schema validation (`engine/executors/aggregation.py::_validate_batch_inputs`)
while the source is still `loading`. The run crashes and finalizes FAILED;
the buffered tokens keep only their non-terminal `(NULL, BUFFERED)`
acceptance rows; run accounting reports `pending=N, failed=0,
closure='open'` on a finished run — and a resume attempt is refused with
`IncompleteSourceResumeError` because the source never reached a complete
lifecycle state. The tokens are pending forever, and the audit trail
contains a contradiction: a finished run that claims its work is still in
flight.

Two constraints make the obvious fixes wrong, both verified against source:

1. **The executor must not terminalize.** Writing `(FAILURE, UNROUTED)` at
   the flush raise site is exactly the pair the ADR-030 §E.3a restore
   reconcile matches (`engine/barrier_coordination.py::restore_from_journal`,
   via `find_failed_unrouted_terminal_token_ids`) — it RELEASES the token's
   BLOCKED journal rows at restore. For the END_OF_SOURCE sibling case
   (`test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens`),
   which IS resumable, that release destroys the buffered work a resume
   depends on. The raise site cannot discriminate the two cases: both are
   "a raised flush" at the same site; the difference (source lifecycle,
   whether anyone can resume) is decided elsewhere and later.
2. **No terminal outcome value is honest.** `SUCCESS` and `FAILURE` claim a
   lifecycle answer the token never received; `TRANSIENT` claims the answer
   lives on another token/record, which is false. The token is genuinely
   undecided — what is missing is a way to record that the undecidedness is
   *permanent*.

The parent ticket's history also records the seam lesson: per-executor
outcome recording was implemented twice in one wave, diverged three ways
immediately, and let a third executor silently keep the defect. The fix
must therefore live at a single seam that already sees every run death,
not be choreographed per raise site.

## Decision

### 1. New non-terminal path: `TerminalPath.ABANDONED`

`(outcome=NULL, path=ABANDONED, completed=False)` — "abandoned, undecided."
The token never received a lifecycle answer, and the run that owned it
terminated in a state from which no resume can ever deliver one. Added to
`_NON_TERMINAL_PATHS` in `contracts/enums.py`; the ADR-019 mapping table
gains one non-terminal row:

| circumstance | `completed` | `outcome` | `path` | Predicate counter(s) | Resume re-derive? |
| --- | --- | --- | --- | --- | --- |
| Run died non-resumably with token undecided | `False` | `NULL` | `ABANDONED` | none (not a predicate input; no structural counter) | Never — a resume encountering it is an audit-integrity crash |

Field constraints (`contracts/audit.py::_TERMINAL_PAIR_FIELD_CONSTRAINTS`):
all discriminator columns forbidden. The abandonment reason travels in
`context_json` (run status stamped, the non-resumability arm that fired,
and the incomplete source states), not in discriminator columns — an
abandonment is not attributable to a sink, batch, or error site; tying it
to the flush error that happened to precede it would be inference the
producer cannot honestly declare.

`ABANDONED` is **not** a legal `RowResult`/`PendingOutcome` value
(`contracts/results.py`, `contracts/engine.py` keep their
`outcome=None ⇒ path=BUFFERED` guards): it is not a row-processing result
and never flows through the engine's result pipeline. Its counter-effect
entry (`engine/orchestrator/counter_classification.py`) has empty
increments and `forbidden_in_processing_results=True`, so an ABANDONED
record reaching the accumulator or the resume re-derivation crashes
loudly rather than counting as anything.

### 2. The producer is run finalization, inside the fenced terminal transaction

The single writer of `(NULL, ABANDONED)` is `complete_run`
(`core/landscape/run_lifecycle_repository.py`) — the §D run-finalization
verb every failure path already funnels through
(`engine/orchestrator/ceremony.py::emit_failed_ceremony` /
`emit_interrupted_ceremony` → `finalize_run` → `complete_run`). Within the
existing single write transaction, **after** the epoch fence and **after**
the terminal conditional UPDATE succeeds, when:

- the stamped status is `FAILED` or `INTERRUPTED` (never any success
  status — a success finalize with undecided tokens remains a quiescence /
  closure violation, not something to sweep under a rug), AND
- the run is **non-resumable**, determined on the same connection,

then every token of the run with no `completed=1` outcome row and no
existing live `(NULL, ABANDONED)` row receives one `(NULL, ABANDONED)`
record (idempotent by that WHERE clause; a takeover-refusal re-finalize
re-runs the sweep as a no-op).

**Why inside the transaction, after the stamp:** abandonment becomes true
at the instant the run is irrevocably dead. Written before the stamp, a
deposed leader's fence refusal (or a multi-worker takeover of a
RUNNING-stale run) leaves false "nothing will decide me" records on tokens
another leader is still driving. Written after, the run is
immutable-terminal (`complete_run`'s already-terminal backstop) and the
non-resumability facts are frozen post-mortem, so the record is
permanently true. A sweep failure rolls back the whole finalize — the run
stays RUNNING-stale (an understood, liveness-recoverable state) rather
than stamping a terminal status over an audit trail the recorder could not
make consistent, consistent with the existing quiescence-refusal
precedent.

### 3. The non-resumability predicate mirrors the resume gates, arm for arm

The sweep fires iff a hypothetical future resume would be refused. Three
arms, each the mirror of an existing structural refusal in
`engine/orchestrator/resume.py`:

| Sweep arm | Mirrored refusal |
| --- | --- |
| No checkpoint rows exist for the run | `get_resume_point` → no resume point / `EmptyResumeStateError` |
| No `run_sources` records exist | `EmptyResumeStateError` (resume.py:734) |
| Any `run_sources.lifecycle_state` outside the source-complete set | `IncompleteSourceResumeError` (resume.py:742) |

The source-complete set (`EXHAUSTED`, `LOADED`) — currently the private
`_SOURCE_COMPLETE_LIFECYCLE_STATES` in `resume.py` — moves to
`contracts` beside `RunSourceLifecycleState` as the shared
`SOURCE_COMPLETE_LIFECYCLE_STATES`, imported by both consumers. Both
consumers point the same safety direction: the resume gate refuses when a
source is incomplete, the sweep abandons when the same refusal would fire;
a drift between them is the exact bug class the shared constant prevents.
(`NonResumableRunError` — the status-based refusal — needs no mirror: the
sweep runs only while stamping `FAILED`/`INTERRUPTED`, which are the
resumable statuses.)

When the run **is** resumable (sources complete, checkpoints exist), the
sweep does not fire and undecided tokens stay `BUFFERED` with
`closure='open'` — honest: recoverable-but-unrecovered. A resumable run
that is simply never resumed keeps that state forever, and that is the
truth.

### 4. Accounting: `abandoned` tokens and `closure='abandoned'`

`web/execution/accounting.py` splits the undecided set:

- **decided** — has a `completed=1` outcome row (unchanged);
- **abandoned** — undecided, with a `(NULL, ABANDONED)` row; reported as
  `tokens.abandoned`, excluded from `tokens.pending` and from
  `integrity.missing_terminal_outcomes`;
- **pending** — undecided and unexplained (unchanged semantics, now
  net of abandoned).

`integrity.closure` gains the value `'abandoned'`: every emitted token is
either decided or explicitly abandoned, none unexplained. `'closed'`
continues to require zero abandoned; `'open'` means unexplained undecided
tokens remain. A token that is both decided AND abandoned is an audit
contradiction: accounting raises, alongside the existing
duplicate-terminal-outcomes raise.

### 5. Non-goals

- **No executor-site writes.** The aggregation flush raise sites remain
  recording-free; the scope boundary in
  `test_contract_violation_token_outcomes.py` stands.
- **No journal mutation.** BLOCKED `token_work_items` rows of an abandoned
  run are left intact (mirroring "FAILED/INTERRUPTED leave the journal for
  resume"); no resume can ever adopt them because the resume gates refuse
  the run first. The §E.3a reconcile is untouched — it continues to match
  only `(FAILURE, UNROUTED)`.
- **The seam-wide "explicit fate decision at every contract-violation
  raise site" refactor** (the architecture reviewer's wider point on
  `elspeth-b4254f9a01`) is not attempted here. This ADR closes the
  contract gap and installs the one fate decider that requires no per-site
  choreography; requiring every raise site to declare
  terminal / abandoned / resumable is a separate, larger decision.

## Consequences

### Positive

- The pinned contradiction is gone: a finished, non-resumable run explains
  every token. `closure='open'` now always means "a resume could still
  decide these" or "audit is genuinely incomplete" — never "stranded
  forever but pretending otherwise."
- One producer site, zero new choreography: every existing failure path
  (run crash, resume crash, leader-drain failure, interruption) inherits
  the fate decision through `complete_run`.
- The abandonment class is closed generically, not per-executor: any
  future stranding shape (e.g. the unexecuted-expand sibling class noted
  on the parent ticket) that leaves undecided tokens on a non-resumable
  dead run is swept by the same decision.
- Fail-closed belts on every "impossible" path: resume re-derivation and
  the accumulator crash on ABANDONED records; accounting crashes on
  decided∧abandoned.

### Negative

- `complete_run`'s terminal transaction grows a SELECT and up to N
  INSERTs on the FAILED/INTERRUPTED arm. Bounded by undecided-token count,
  runs once per run death; acceptable for an audit-tier verb.
- A hard crash (kill -9) still leaves `closure='open'` with no
  abandonment records — no finalize, no sweep. That is the pre-existing
  crashed-process story (RUNNING-stale, liveness), not changed here.
- Consumers of the accounting wire shape see two additions
  (`tokens.abandoned`, `closure='abandoned'`). Additive; frontend types
  updated in lockstep.

### Neutral

- The DB schema is unchanged (new enum *value* in an existing `String`
  column; no migration, no DB wipe — pre-ADR-038 rows never carry the
  value).
- `rows_buffered` and all public counters are untouched; ABANDONED has no
  counter. Operator visibility comes from accounting, which derives from
  `token_outcomes` directly.

## Alternatives Considered

### 1. Terminalize at the flush raise site (the retracted fix)

Rejected — see Context. `(FAILURE, UNROUTED)` is the §E.3a release
trigger; the raise site cannot discriminate resumable from stranded.
Recorded here because it was recommended once and retracted on
`elspeth-b4254f9a01`; do not resurrect it.

### 2. Executor-site ABANDONED write, discriminated by source lifecycle

The flush site could read `run_sources` and write ABANDONED only when a
source is incomplete. Rejected: resurrects the per-executor recording seam
the parent ticket's history indicts (three divergences in one wave), fixes
only aggregation (not the wider stranding class), and burdens an executor
with a run-level judgment ("will anyone ever resume?") it has no business
making mid-crash.

### 3. Derive abandonment lazily in accounting (no audit record)

`accounting` could report `closure='abandoned'` by joining run status ×
source lifecycle × pending tokens at read time. Rejected: the audit trail
itself would keep the contradiction, and the Auditability Standard is
explicit — the record, not an inference over it, is the evidence. The
producer (run finalization) *knows* the fate at a well-defined instant; it
must declare it.

### 4. A terminal `(FAILURE, ABANDONED)` pair instead of a non-terminal path

Rejected: `completed=True` asserts a lifecycle answer exists. It does not.
It would also make abandonment a predicate input (`rows_failed`),
silently flipping run-status semantics for runs that already carry their
own failure indicator, and would collide with the §E.3a-style "terminal
FAILURE means journal rows are releasable" assumption the restore paths
encode.

## Related Decisions

- **Amends:** ADR-019 (Two-Axis Terminal Model) — adds one non-terminal
  row to the mapping table; all other rows unchanged.
- ADR-030 (Multi-Worker Deployment Shape) — §D finalization transaction is
  the sweep's home; §E.3a reconcile semantics are deliberately untouched.
- ADR-029 (Journal is Barrier-Buffer Truth) — journal rows of abandoned
  runs are left intact.
- Filigree `elspeth-b4254f9a01` (this gap), `elspeth-82d4c5146c` (parent;
  row-level half fixed as `3cb883229`).

## Implementation Notes

Surface, in dependency order (parity-swept against every consumer of
`(None, BUFFERED)`):

1. `contracts/enums.py` — `TerminalPath.ABANDONED`; `_NON_TERMINAL_PATHS`
   gains it (import-time partition assertions self-verify).
2. `contracts/audit.py` — `TokenOutcomeRecord.__post_init__` and
   `validate_token_outcome_persisted_fields`: `completed=False` admits
   `{BUFFERED, ABANDONED}`; `_TERMINAL_PAIR_FIELD_CONSTRAINTS` gains
   `(None, ABANDONED)` with all discriminators forbidden.
3. `contracts/lifecycle.py` (or wherever `RunSourceLifecycleState` lives) —
   `SOURCE_COMPLETE_LIFECYCLE_STATES` moves in from `resume.py`.
4. `engine/orchestrator/counter_classification.py` — `(None, ABANDONED)`
   entry: `increments=()`, `forbidden_in_processing_results=True`;
   non-terminal lockstep assertion updated.
5. `engine/orchestrator/run_status.py` — resume re-derivation raises
   `AuditIntegrityError` on `(None, ABANDONED)` (a resume is running over
   a run the audit says can never resume).
6. `core/landscape/run_lifecycle_repository.py` — the sweep inside
   `_complete_run_in`, gated on FAILED/INTERRUPTED + the three-arm
   predicate; writes go through the same persisted-field validation as
   `record_token_outcome` (identical Tier-1 checks, same connection).
7. `web/execution/accounting.py` — abandoned split, closure value,
   decided∧abandoned raise; frontend type union updated in lockstep.
8. Tests — contracts pair tests; repository sweep tests (fires /
   resumable-no-op / idempotent / SUCCESS-never); the two pinned
   aggregation tests updated (count case now abandoned + `closure='abandoned'`,
   EOF case byte-identical assertions); module docstring scope boundary
   updated to name this ADR.
