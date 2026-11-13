# Executor hot-path remediation — plan

Status: **plan only — nothing here has landed.** It covers every finding in
[hot-path-recon.md](hot-path-recon.md) that lies outside `src/elspeth/web/`:
the engine, the Landscape, canonical hashing, the payload store,
checkpointing, the sink phase, plugins and the performance test harness.
Mechanisms were re-verified, and most were measured, at `1a7ff0e6`
(measurement runs on docs-only commits on top of it), SQLite, one process.
This is a working document: update the status column as items land, and
delete it when it stops being useful. Line numbers are valid for that commit
only.

Web-tier findings (web-run LLM governance, the run-progress websocket poll,
per-request auth, the web validation trunk) are out of scope here, as is the
composer, which has its own plan in
[composer-performance-remediation.md](composer-performance-remediation.md).

## How to read this

The summary sections are the working plan: the fix list, the confirmations
and rulings it needs, and the order. Appendices L1–L6 hold the full design of
each item in one format (finding, verified mechanism, evidence, change, what
must not change, risk, ruling, verification, expected saving). Each appendix
is one investigator's report, reproduced with only heading levels changed,
and each ends with its own list of what was not verified.

**Where the summary and an appendix disagree, the summary governs.** The
summary was revised after an independent review (see "Review" below); the
appendices were not. The section "Where this summary overrides an appendix"
lists each difference.

The appendices cite scripts, traces and logs under `.claude/lanes/`. That
directory is gitignored and does not survive the session that produced it;
treat those citations as a description of how a number was obtained, not as
files you can open. Committing the instruments is items H-1 and H-2.

| Appendix | Scope |
|---|---|
| L1 | Landscape SQL trunk: statement construction, fences, read-backs, transactions per transition |
| L2 | Canonical JSON, hashing and row-object churn |
| L3 | Payload store, checkpointing and the end-of-run sink phase |
| L4 | Engine control flow, startup, cross-row concurrency and the performance test harness |
| L5 | Plugins and the external-call path |
| L6 | Findings the first five lanes left uncovered: external-call recording, sink member re-derivation, remaining row copies, fork-path verbs |

## Relationship to the earlier executor review

An earlier, independent executor performance review exists on the unmerged
branch `claude/upbeat-planck-33a7pd` at `5d9726f6`
(`docs/development/executor-performance-review.md`, measured at `231cc44e`,
with a harness directory and a reverted prototype). It is not on this
branch. This plan was written with that review in hand:

- Where a finding is the same item, this plan **adopts that review's ID**
  (`E-*` ready fixes, `R-*` rulings, `M-*` measurements, `H-*`
  housekeeping) and re-verifies the mechanism at this commit.
- Items that review does not have carry a lane ID (`L1-n` … `L6-n`).
- Where the two disagree, the disagreement is listed in the next section
  with the evidence.

Neither document is the plan of record yet. They should be folded into one
first; which branch and release that belongs to is the maintainer's
decision and has not been made here.

## Corrections to the earlier documents

Verification changed several claims. The recon has been corrected in place
where it was wrong; the earlier review is on another branch and is not
edited from here.

| Claim | Source | What verification found |
|---|---|---|
| Fork/coalesce is super-linear (102 → 136 ms/row) | Recon § 1, Appendix F | **Not reproduced.** Marginal CPU is flat at 109–110 ms/row across 100, 200, 400 and 800 rows, with constant statements and commits per row. The recon figure came from three single wall-clock runs (L4) |
| "Nothing measured here is super-linear" | Earlier review | Wrong in two places: the sink reservation lock loop is quadratic per sink group (L3-1), and the run-end orphan sweep is quadratic in fork rows (E-11) |
| E-1a and E-1b are ready with no ruling | Earlier review | The clock-authority source gate rejects a prebuilt leader fence, leader renewal, both clock helpers, and any prebuilt statement executed with a time value in its bind mapping. Only the membership fence and by-key selects pass as-is (L1, L1-4) |
| Prebuilding "changes no SQL text" | Earlier review | True on SQLite; on PostgreSQL the placeholder names change (L1) |
| R-13 (node-state timestamps on the process clock) may be a defect | Earlier review | Not a defect: ADR-047 explicitly leaves `node_states` timestamps on the process clock (L1) |
| E-11 may need an index | Earlier review | The index exists; SQLite picks a bad join order and a query rewrite fixes it with no schema change (L4) |
| The external-call path adds nothing above the measured floor | Earlier review, Limits | An Azure LLM row adds 16 statements, 3 commits and 2 payload files; an OpenRouter row adds 31, 6 and 4, because HTTP-transport providers record two calls per query (L5, L6) |
| Canonical JSON and hashing is 11.5–16.2% of per-row time | Recon § 1 | A cProfile bucket that over-weights small Python calls. Removing about half of those calls changed CPU by −4.6% (gate) and −0.3% (transform) on narrow rows; it reaches −17% at 100 columns (L2) |
| Three `to_dict()` thaws per token × transform | Recon Appendix A | Seven in engine code plus one or two in the plugin (L2) |
| Five canonical serialisations per token at the sink | Recon Appendix B | At least twelve; `SinkEffectMember` is built seven times per row and re-derives each time (L2, L6) |
| Three read transactions per sink member | Recon Appendix B | Four; 16 statements and 4 commits per token measured (L3, E-4) |
| Ingest is 28 statements with 9 leader-fence and 5 clock reads | Recon Appendix B | 29 statements plus `BEGIN IMMEDIATE`, with 7 leader-fence updates and 7 clock reads (L1) |
| The gate profile includes the default checkpoint cost | Recon (implicit) | The shipped gate example sets `every_n: 100`. At the default `every_row` a checkpoint costs 9 statements and 1 commit per sink-written token (L3-4) |
| CSV sink re-hashes the output once per effect, effects per run unknown | Recon § 3 item 7 | One effect per sink per run in the common case, so the item is downgraded. With many distinct error hashes on one sink it becomes a failure (L3-8) |
| A 30-thread pool holds one row at a time | Recon § 3 item 3 | The default of 30 is capped by `max_workers` and overridden by `pool_size` (default 1); one worker thread ran in the measured runs. The conclusion — no two rows' calls overlap — is confirmed by measurement (L4-5, L5) |
| Retry wrapper and per-node traversal loop are hot | Recon Appendix A top-10 | 11.65 µs per token × node and under 0.4% of profiled time; not worth fixing (L4-2, L4-3) |
| The stale benchmark is `test_db_write.py` | Recon § 1 | Wider: six changed repository verbs, and 23 of 30 tests in the `stress` tier fail on the same drift (L4-6) |

## Two things that are defects, not costs

These surfaced during verification and should not wait for a performance
argument. The independent review reproduced the first and confirmed the
second from the code.

- **L2-6 — a `set` inside a row is hashed non-deterministically.**
  `stable_hash` of a `PipelineRow` holding a `set` gave three different
  hashes under three `PYTHONHASHSEED` values, so the recorded hash cannot be
  reproduced. The same value in a plain dict is refused. It needs a
  transform that emits a set; no source can. Shipped transforms were not
  surveyed for one. Needs a ruling on where to refuse it.
- **L3-8 — a run fails when one sink receives more than 257 distinct error
  hashes.** The sink phase mints one sink effect per distinct
  (outcome, path, error hash) group, each rebuilt from all its predecessors;
  the cap is at `engine/executors/sink_effects.py:1179-1181` and the 258th
  effect raises `LandscapeRecordError` after every transform has already
  run. The error hash is the hash of the transform's reason text, and
  shipped plugins put row-specific values in that text deterministically:
  `plugins/transforms/pdf_rasterize.py:608-616` (blob reference and body
  size), `plugins/transforms/llm/image_inputs.py:116-120`, and
  `plugins/transforms/aws/textract_inline_analysis.py:529,599`. So a PDF or
  image pipeline with an `on_error` sink and more than 257 failing inputs of
  differing size reaches the cap with no provider text involved. That
  reachability is from reading the code; the failure itself was reproduced
  only with a harness that forces distinct reasons. Needs a ruling on the
  grouping.

## Governing rule

The rule from the composer plan and the earlier review applies unchanged:
**verify when taking ownership, retain the verified immutable
representation, and recompute only facts whose inputs changed.** In the
executor the retained thing is one of: a compiled SQL statement, a fence
already taken on this connection in this transaction, a row the transaction
itself just wrote, or the canonical hash of a frozen row.

Three constraints bound every item:

- **Audit integrity outranks speed.** A change that alters a recorded value,
  a hash, a fence, a clock sample, how often an integrity check runs, or
  what a crash leaves behind is a confirmation or a ruling, never a silent
  optimisation.
- **Ownership is proved by who constructed the object, not by its container
  type.** A memo keyed on a mutable mapping is a correctness bug, and a
  boolean parameter saying "the caller already checked" is a trust
  statement, not a proof. Pass the verified object as the witness instead.
  Memos live on frozen instances; a module-level `lru_cache` on contract
  hashing is additionally refused by the runtime VAL manifest at run start.
- **Do not feed both sides of a divergence check from one retained value.**
  That turns the check into a tautology. A comparison between two
  independently derived values stays.

## Master fix list

Status is `open` for every row. Savings are per-item estimates on SQLite and
overlap heavily — removing a statement also removes its construction cost —
so they do not add up. Unless a row says "measured end to end", a CPU
percentage is a per-row count multiplied by a micro-benchmark delta taken on
a loaded shared box. "TC" marks items that owe the PostgreSQL testcontainer
suite as well as the default one.

### A. Instruments first

Every later fix is proved by a pinned number moving, so these land before
any of them.

| ID | Item | Risk | Appendix |
|---|---|---|---|
| L4-8 | Pin exact per-row counts (SQL statements, commits, canonical-JSON and hash calls) on a small linear and a small fork pipeline through the production entry, plus a linearity oracle that names any function whose call count grows faster than rows. Prototyped: 68 statements / 8 commits per row linear, 381 / 51 fork; a mutant goes red. Build the PostgreSQL-parametrised variant at the same time | Low | L4 |
| L1-2 | Statement-identity, per-row statement-budget and bind-equivalence guards for the Landscape verbs. Overlaps L4-8's statement pins; build as one instrument | Low | L1 |
| M-1 | Take the PostgreSQL baseline (and one with a follower worker) **before** any SQL item lands. Needs a Docker host; none of the lanes had one | Low | L1, L4 |
| L4-6 | Rewrite the stale benchmarks and stress fixtures against the current repository API | Low | L4 |
| H-1, H-2 | Commit the measurement harness, the workload generator and the scripts each ready item names as its before/after instrument. Use `scripts/perf/`; the earlier review's `notes/` destination is gitignored | Low | L4 |
| — | Run both source gates in full against a private copy carrying E-3a, E-3b, E-10 and L1-1 before starting any of them. The lanes probed only the prebuilt-statement variants; whether the mutation-fencing gate accepts `RETURNING` on fenced DML, a new connection method or a read-then-insert arm is unknown | Low | L1 |

### B. Ready to fix — no ruling needed

| ID | Fix | Risk | Expected saving | TC | Appendix |
|---|---|---|---|---|---|
| L3-1 | Stop rebuilding the locked-id tuple on every lock in sink reservation; stop evaluating the backend pid per iteration | Low | Removes a quadratic term: about 1.3 s per 2,000-member sink, projected minutes at 100k | TC | L3 |
| E-11 | Rewrite the run-end orphan sweep to drive from `token_parents`. The review checked the rewrite is logically identical, including NULL handling | Low | 225 → 2 ms at 800 fork rows; removes a rows² term | TC | L4 |
| E-6a | Memoise `SchemaContract.version_hash()` on the frozen instance. Editing this code changes the runtime VAL manifest, so a run interrupted before the upgrade is refused on resume after it — the existing refuse contract, stated here so it is not a surprise | Low | Linear in field count: 0.3–0.6 ms/row narrow, 5–12 ms/row at 100 columns | — | L2 |
| E-6b | Share the version hash across equal contracts on the engine side (value interning in the engine; no plugin edit). This is Appendix L2's design. The plugin-side interning in Appendix L5 is tracked separately as L5-11 | Low | Remaining contract hashes per transform row → 0 | — | L2 |
| E-1a part 1 | Prebuild the membership fence statement | Low | ≈1.6% gate, ≈2.5% transform | TC | L1 |
| L1-1 | Stop recompiling the two dialect-specific inserts on every row. After the change the SQLite arm no longer exercises `ON CONFLICT`, so add a testcontainer replay test for the conflict path | Low to Low–Med | ≈0.5–1% | TC | L1 |
| E-3a | Return after-images with `RETURNING` instead of selecting them back — six of the seven read-backs. The seventh, the post-insert routing-group ownership assertion, is split out as E-3a-own in section C1 | Low–Med | 6 statements per gate row | TC | L1 |
| E-3b | Skip re-validation and repeated work-item selects when the same transaction wrote the rows. **Amended design:** pass the `(Row, Token)` records returned by the insert verb as the witness and compare `row_id`, `token_id` and `ingest_sequence` in memory; do not add "written by this transaction" booleans. Foreign keys back only the run-ownership check; the other equalities become by-construction and the item must say which | Low–Med | 6 statements per source row | TC | L1 |
| E-1b part 1 | Prebuild the by-key selects, including `pending_sink_bundle_clause()` if L6 places it here | Low | ≈1.5% after E-3 | TC | L1 |
| E-4 | Batch the sink-flush lineage loads on the enforced read-only connection | Low–Med | 16 statements and 4 transactions per sink token | TC | L3 |
| E-9a | Compute the topology hash once per run. The design adds a new `OrchestrationInvariantError` on the interrupt path if the graph hash changes mid-run; today that case writes the new hash silently | Low | 1.1–1.5% at the default checkpoint cadence | — | L3 |
| L4-1 | Do not probe the Landscape for a coalesce group the leader holds open in memory. **Amended invariant:** any removal from `_completed_keys` — FIFO eviction, restore replacement, or the `flush_pending` clear at `engine/coalesce_executor.py:1594-1599` — falsifies exactness. Add a test for an arrival after `flush_pending`. Read `row_union_executor.py` before extending the shape to it | Low | 2 probes per fork row | — | L4 |
| L2-3 | Drop the gate executor's duplicate input hash (the Fix 9 change, applied to gates) | Low | One canonicalisation per token × gate | — | L2 |
| L2-2 | Hash the source row once for its source node state | Low | One per source row | TC | L2 |
| L2-1 | Serialise once where one object is both hashed and stored (ingest, calls, operations) | Low | One per source row, two per external call | TC | L2 |
| L2-10 | Serialise the source contract once per contract | Low | ~60 µs/row after E-6a | — | L2 |
| L2-5 | Canonicalise a row from its frozen data without thawing. Waits for the L2-6 answer, because it must reproduce the frozenset arm that L2-6 asks to remove | Low | 8–9% of each row hash | — | L2 |
| L2-7 | Cut engine thaws per token × transform. **Amended:** the value handed to the declared-output-type check keeps its thawed form (or frozen container types are mapped back to their thawed names before recording), because a violation records the container's type name and frozen data would record `tuple` or `mappingproxy` where `list` or `dict` is recorded today. Add a test pinning `actual_type` for a container under a declared scalar type. The `emitted.keys()` change is safe as designed | Low | Up to five deep copies per token × transform; fewer with the retained thaw | — | L2 |
| L2-8 | Make the second normalisation walk non-copying and hash bytes directly. Keep the policy in `contracts/hashing.py` | Low–Med | 10–35% of each remaining canonicalisation | — | L2 |
| L1-3 | Halve the cost of a scheduler event's identity, through the contracts-layer hashing primitive only — no second direct `rfc8785` call site. Its saving is the same one L2-8 counts for event dicts | Low | ≈0.3% | — | L1 |
| E-8a | Compute the model-catalog digest without importing litellm. The digest is an audit column and the shortcut reads a file whose shape litellm owns, so the equality test must run in the default suite against the locked litellm version | Low–Med | About 2.2 s CPU per non-LLM CLI run | — | L4 |
| L4-4 | Keep heavy SDK imports out of plugin module import | Low–Med | About 1.0–1.1 s CPU per CLI run | — | L4 |
| L5-2 | Build the pooled HTTP client only when a pooled request is made. Must not change: Power Automate stays refused by the separate check at `plugins/infrastructure/clients/http.py:1463-1464` | Low | 7 ms CPU per `web_scrape` / `blob_fetch` row | — | L5 |
| L5-4 | Take the telemetry hashes from the recorded call — LLM-client success path only. The image-rejection branch and the HTTP client would change an emitted telemetry value and are not included | Low | 0.08–0.25 ms per call | — | L5 |
| L5-5 | Memoise the template's contract hash. With engine-side E-6b alone the memo hits only when the transform is fed the source contract object; it hits on every row only after L5-11. Open question: two computed hashes have no reader | Low | 55–355 µs per query when it hits | — | L5 |
| L5-7 | Build the output-declaration stamp table once per transform | Low–Med | ~40 µs per row per transform; 32 call sites in 24 modules | — | L5 |
| E-9b | Derive `ValueTransform` created fields and expression kinds once | Low | 36 µs per row beyond L5-7. The earlier review's 1.7% was not reproduced | — | L5 |
| L5-11 | Intern equal emitted contracts in `BaseTransform` (Appendix L5's "E-6b, plugin side"). After L5-7, which edits the same function; owes the full default suite | Low | Enables the identity-keyed memos downstream of field-adding transforms | — | L5 |
| L6-1 | Allocate the call index inside the record transaction at the four sites where no external effect separates the two (the logical call of HTTP-transport providers, operation calls). The pre-effect allocation in the audited clients stays: its fence is the only item-lease check before the external call | Low | 4 statements, 1 commit, ~1.3 ms CPU per OpenRouter or Gateway query; none for Azure | TC | L6 |
| L6-4 | `SinkEffectMember`: verify content once, then rebind ordinals without re-deriving. Leaves the real cross-checks in place | Low–Med | 12 of 14 canonicalisations per sink row: 250 µs at 5 columns, 1.9 ms at 100 | — | L6 |
| L6-6 | Build `pending_sink_bundle_clause()` once. Independent of E-1b and of the clock-authority gate | Low | ~0.3–0.4 ms per row, about 1% | TC | L6 |
| L6-7 | Carry `ingest_sequence` from the claim instead of reading it per continuation | Low | 16 statements and 4 read transactions per fork row | TC | L6 |
| L6-8 | Resolve token ownership once, inside the fork, coalesce or expand transaction | Low–Med | 22 statements and 6 read transactions per fork row (10 statements after E-10) | TC | L6 |
| L6-5 | Remove the plugin double copy (`copy.deepcopy(row.to_dict())`) in `value_transform`, `type_coerce`, `truncate`, `passthrough`, `reference_join` and `batch_replicate` — only as a ride-along with E-9b and L5-7, never on its own (see the plugin-hash rule below) | Low | 2.5–6 µs per row per transform | — | L6 |

### C1. Needs a short maintainer confirmation

Each of these is a small, well-understood change that nevertheless narrows a
check or changes what a failure leaves behind. They need a yes, not a design
debate.

| ID | Change | What to confirm | Saving | TC | Appendix |
|---|---|---|---|---|---|
| E-5 | Record a transform's output contract once per distinct contract. **One design:** a memo on the transform executor (run-scoped by construction), keyed on node and the contract's version hash, which E-6a and E-6b make free; the entry is added only after the fold transaction has committed. Depends on E-6a and E-6b | The stored-hash versus recomputed-hash check on the `nodes` row drops from once per row to once per distinct contract | One write transaction per transform per row; −11% CPU on two A/B pairs with a value-keyed variant | TC | L1, L2 |
| E-3a-own | Drop the post-insert routing-group ownership assertion (`core/landscape/execution/node_states.py:1520-1525`) | It is a Tier-1 guard against a mixed routing decision; the argument for removing it rests on the routing authority lock and is clean only on SQLite | 1 statement per gate row | TC | L1 |
| E-10 | Plain read transaction for engine-internal reads. Refuse any `Select` carrying `FOR UPDATE` on the new path | It removes `PRAGMA query_only` on SQLite and `READ ONLY` on PostgreSQL from engine reads, narrowing defence in depth; a `Select` is not proof of a read. E-4's batched loads stay on the enforced read-only connection either way | 2 statements per gate row after E-4; more on fork rows | TC | L1 |
| L3-6 | Write the source-row blob before the ingest transaction opens. **Amended design:** the data-flow layer builds a prepared-record object that the verb accepts, rather than the caller passing a loose hash and ref | A deposed leader can then leave one unreferenced blob it could not leave before, and a store error now precedes the leadership-lost error. Edits the same verb as L2-1 and R-2a | Two fsyncs per row leave the write-lock hold | TC | L3 |
| L5-1 | Share one HTTP transport per provider instead of one per row | Cookies are no longer carried between attempts of one state; and a stale kept-alive connection can surface as a retryable network error, adding an error `calls` row a fresh connection would not have produced | 6–7 ms CPU per row plus a TLS handshake per row on real endpoints | — | L5 |
| L4-7 | Type-check the performance tests' repository calls and smoke-run them at tiny N | Adds a CI step and a 20–40 s mypy-in-test; keep it only if it meets the "materially improves reliability" bar | Prevents the rot L4-6 repairs | — | L4 |
| L6-2 | Stop re-selecting the parent state's immutable columns when allocating a call index and when updating payload refs | The re-select is a defensive read of columns that cannot change | 1–2 statements, 0.2–0.4 ms per call | TC | L6 |
| L6-9 | Memoise the node-cursor existence check | A foreign key already backs it; the in-process check becomes once per node | 9 statements per fork row | TC | L6 |

### C2. Needs a ruling first

Each row gives the short form of the question; the exact wording, the ADR
text it touches and the options are in the appendix. The independent review
did not open the ADRs, so the ADR citations are the lane investigators'
readings.

| ID | Decision | Touches | Why it is a decision | Saving if approved | Appendix |
|---|---|---|---|---|---|
| L2-6 | Refuse a `set` in row data, and at which seam | Canonical-JSON policy | Defect: recorded hash is not reproducible | None | L2 |
| L3-8 | Group sink writes by (outcome, path) and carry the error hash per member | ADR-019 | Changes the number and ids of effects for runs with several error hashes per sink; today such a run can fail | Run completes; removes an O(K²) rebuild | L3 |
| R-2a | Let a leader `_on` verb discharge its fence by proving the outer transaction already took it | ADR-047, ADR-030 D4 | ADR-047 gives raw-connection callers an explicit renewal obligation | 6 statements per source row (prototyped: ingest 30 → 24) | L1 |
| L1-4 | Extend the clock-authority gate to prove provenance through a prebuilt statement's bind mapping | ADR-047 | The gate pins the source shape of the fence and clock statements | Unlocks the two rows below | L1 |
| E-1a part 2 | Prebuild the leader fence, leader renewal and clock helpers | via L1-4 | Blocked by the gate today | ≈5–6% | L1 |
| E-1b part 2 | Prebuild every write that binds a timestamp | via L1-4 | Blocked by the gate today | Unknown until measured; the lane's estimate is 8–12% | L1 |
| R-2b | Issue the leader deadline once, at the completion boundary | ADR-047 | The entry renewal is a deliberate part of the leader context | 2 statements per leader transaction | L1 |
| R-7 | Fold the transactions of one transition (options A–D) | ADR-030 D4, ADR-026 | Changes what a crash leaves behind | A: 3 commits per gate decision; D: 9 → 2 write transactions per transform row | L1 |
| R-12 | Lock form of the fences (`SELECT … FOR UPDATE` versus a no-op `UPDATE`) | ADR-030 D7, ADR-047 | D7 chose the verify-UPDATE form | One WAL frame per member transaction on SQLite; PostgreSQL unmeasured | L1 |
| L3-4 | One progress checkpoint per durable sink write group instead of one per token | ADR-029, ADR-030 | Changes the documented behaviour of `every_row`; checkpoint rows hold no token position | 9 statements and 1 commit per sink token; 4.8 s of a 60.6 s gate run at the default cadence | L3 |
| L3-5 | Validate the payload-store directory once per operation, or bind to a directory fd | Payload-store threat model | The earlier review left this alone as deliberate hardening | `lstat` per row 129–245 → about 45 | L3 |
| L3-3 | Whether sinks stay deferred to the end of the run | ADR-030 D2, ADR-029, ADR-026 | Changes effect count, ids and output row order | Memory: about 7 kB peak RSS per narrow row today, projected ~1 GB at 100k rows | L3 |
| L2-4 | Carry the canonical hash on the frozen row and hand rows to the node-state verbs | ADR-032 | Changes which object the Landscape hashes; two tests pin "Landscape receives a dict" | 2 canonicalisations and 2 thaws per token × node | L2 |
| L4-5 | Whether rows may overlap at an external call | ADR-001, ADR-030 D1 | ADR-001 forbids row parallelism in the orchestrator | Up to W× wall time on external-call pipelines | L4 |
| E-9c | Skip barrier intake reads when the leader produced nothing to adopt | ADR-029 § E.2 | The ADR specifies per-iteration intake | About 2.5% of fork CPU | L4 |
| L5-3 | Share one TLS context across the per-request SSRF clients | SSRF guard | Touches the SSRF path | 6 ms CPU per SSRF request | L5 |
| L5-6 | Whether HTTP-transport providers need two call records per query | Call-recording contract | Changes the audit shape behind replay and verify | Up to 15 statements and 3 commits per query. Listed for completeness, not recommended as a performance fix | L5 |
| L6-3 | Whether call payload refs need their own transaction (options A–C) | Call-recording order; no ADR records the row → file → ref sequence | Carrying the ref in the `calls` insert makes a crash leftover read `PURGED` instead of `HASH_ONLY` (`core/landscape/row_data.py:17-19`); writing the files first lets a deposed worker write blobs before the fence | A or B: 5 statements, 1 commit, ~1.4 ms CPU per call | L6 |

### D. Measurement owed

| ID | Item | Status |
|---|---|---|
| M-1 | PostgreSQL baseline and a follower-worker run. Scheduled in section A so there is a before-number; repeated after the SQL items | open — no Docker on the box used |
| M-2 | Re-measure after section B and replace the estimates with measured figures | open |
| M-3 | Profile one external-call pipeline | **done** (L5): ChaosLLM, zero latency, 300 rows, both providers |

### E. Examined and not recommended

This table is a selection of the larger declines. Each appendix ends with its
own complete list.

| Item | Reason | Appendix |
|---|---|---|
| R-13 node-state timestamps on the process clock | ADR-047 leaves them there on purpose | L1 |
| R-8 record the catalog digest only for LLM pipelines | Moot once E-8a makes the digest cheap | L4 |
| E-8b cache the runtime VAL manifest | At most 0.5 s; a sound cache key would be the manifest itself | L4 |
| L2-9 hybrid C-`json` canonical serialiser | 4× faster and byte-identical on 36,000 cases, but a second serialiser behind a version pin is high risk for the gain | L2 |
| L3-9 serialise each sink member row once | It would feed both sides of the divergence refusal at `engine/executors/sink_effects.py:1360-1364` from one retained byte string, for about 0.1% | L3 |
| L4-2 retry wrapper, L4-3 per-node traversal loop | 11.65 µs per token × node; under 0.4% | L4 |
| Input and output schema validation per transform attempt | Two distinct contracts, not a duplicate | L2 |
| The executor's output hash | It converts NaN and Infinity into a routed violation (composer Fix 9 ruling) | L2 |
| L3-7 payload-store dedup-hit re-read | An integrity check at the moment a new audit reference is created | L3 |
| CSV / local-file sink effect rebuild | One effect per sink per run in the common case; the repeated snapshots are crash-reconciliation evidence. Returns if L3-8 is refused | L3 |
| L5-8 rate-limiter sleep-poll | About 5 ms on a wait of a second; the default 60 requests/minute is what dominates run time | L5 |
| L5-9 CSV source per-row validation | 46 µs per 10-column row at the trust boundary; nothing rebuilt per row | L5 |
| L5-10 batching mixin thread handoffs | 55 µs per row; the worker thread is what makes timeout and eviction possible | L5 |
| `BEGIN IMMEDIATE`, WAL, `synchronous=NORMAL` | Documented ADR-030 D5 choices | L1 |
| Removing `allocate_call_index` before the external call | Its fence is the only item-lease check before the external effect; removing it would need ADR-048 reclassified | L6 |
| Ingest thaw-then-freeze, fork-child deep copy, declaration-contract dispatch, `check_timeouts` scan, telemetry projection before the disabled check | Measured at 7 µs per row, 4.3 µs per child, ~10 µs of dispatcher per call across three distinct contract surfaces, 41 ns per pending group, and 1.3 µs with no manager when telemetry is off | L6 |
| Sink-path row thaw (`engine/executors/sink.py:1557`) | Four consumers; its hash is the recorded `input_hash`, so it is decided by the L2-4 ruling | L6 |
| Follower `live_leader` read per claim | A deliberate forced check | L6 |

## Where this summary overrides an appendix

| Item | Appendix says | Summary says |
|---|---|---|
| E-5 | L1: a repository memo keyed on run, node and hash. L2: an executor table keyed on the contract value | One design: executor memo keyed on node and version hash, entry added after commit; a confirmation, with TC |
| E-6b | L2: engine only. L5: intern in `BaseTransform` | Two items: E-6b is L2's design; L5's is L5-11, after L5-7 |
| E-3a | Seven read-backs including the routing-group ownership assertion | Six in section B; the assertion is E-3a-own in C1 |
| E-3b | Two "written by this transaction" booleans | Witness records compared in memory |
| E-10 | Ready, flagged for the maintainer in the text only | Confirmation in C1; refuse `FOR UPDATE`; saving restated net of E-4 |
| L2-7 | Read frozen data in the declared-output-type check | Keep the thawed value there; pin the recorded type name |
| L3-6 | Ready; pass hash and ref into the verb | Confirmation in C1; pass a prepared record |
| L3-9 | Ready | Declined |
| L4-1 | Exactness lost on eviction and on "journal restore (`:717`, `:1598`)" | Line 1598 is the `flush_pending` clear; any removal from `_completed_keys` falsifies exactness |
| L5-1 | No ruling; confirm cookie policy | Confirmation in C1, naming both behaviour changes |
| L5-4 | Both clients, all branches | LLM-client success path only |
| L3-2 | Chunk size 900; test (c) expects the "disappeared" message | On SQLite the message becomes "member tokens are missing"; the lock-order argument assumes Python sort order equals the database's for the id format, which holds for hex UUIDs and should be tested; finalisation chunks at 500. Scheduled after the PostgreSQL baseline |
| L3-8 | Raise fires "when an effect has 256 or more predecessors"; batch quarantine as an example | 256 predecessors pass and 257 fail; batch quarantine hashes are recorded as outcomes, not sink groups, and do not by themselves reach the cap |
| L3-4 | 7.8% and 7.9% for the same figure | The sound number is 4.8 s of a 60.6 s run; single runs on a box that moved about ±15% |
| E-4 | About 6% of gate CPU | A wrapped-region share the lane itself calls slightly high; quote the statement count |
| L2 § 0 | A module-level `lru_cache` "cannot be used in this code" | Narrower: the runtime VAL manifest refuses one on contract hashing |

## Order

1. **Instruments and baselines** — L4-8 with L1-2 (including the PostgreSQL
   variant), M-1, L4-6, H-1 and H-2, and the full source-gate run against a
   private copy of the E-3 / E-10 / L1-1 changes.
2. **Raise the two defect rulings** — L2-6 and L3-8 — so they are not queued
   behind performance work. Raise the C1 confirmations at the same time;
   they are cheap to answer and several ready items wait on them.
3. **Super-linear terms** — L3-1, then E-11. Both owe the testcontainer
   suite.
4. **Contract hashing** — E-6a, E-6b, then E-5 once confirmed.
5. **Local changes** — L2-3, L2-10, E-9a, L4-1; L2-2 and L2-1 (TC, because
   they sit on a path that takes a row lock); the plugin group L5-2, L5-4,
   L5-7, E-9b, L5-11, L5-5, and L5-1 once confirmed; the startup pair E-8a
   and L4-4.
6. **Landscape SQL, one testcontainer batch per group** — E-1a part 1 and
   L1-1; then E-3a, E-3b; then E-4, and E-10 once confirmed; then E-1b
   part 1, after E-3 so removed statements are not converted first; then
   L3-6 once confirmed.
7. **Row hashing** — L2-5 after the L2-6 answer, L2-7, then L2-8 last
   because it touches the hash of everything and its gain shrinks as the
   call count falls. L1-3 rides with L2-8.
8. **Measure** — M-1 again and M-2.
9. **L3-2** — after step 8, because its saving and its deadlock risk exist
   only on PostgreSQL.
10. **Remaining rulings, raised together with the measured numbers** — R-2a
    first (smallest, prototyped, shrinks E-1a part 2); L1-4, then E-1b
    part 2 and E-1a part 2 behind it; R-2b; L3-4; L2-4; R-7 option A, then
    B–D; L3-5; L3-3 after the sink fixes are re-measured; E-9c after L4-1;
    L4-5; R-12; L5-3; L5-6.

Appendix L6's items slot in as follows: L6-6 and L6-4 with step 5; L6-1 with
step 5 (TC); L6-7 and L6-8 with step 6, after E-10's confirmation because
L6-8's saving depends on it; L6-2 and L6-9 are raised with the other C1
confirmations in step 2; L6-5 rides with E-9b and L5-7; L6-3 is raised with
the step 10 rulings. Three edits to existing items come from the same lane:
add `_complete_node_states_many` to E-3a's site list; note that
`pending_sink_bundle_clause()`'s host statement moves from E-1b part 1 to
part 2 once E-3a folds it into an `UPDATE … RETURNING`; and note under R-7
that none of options A–D covers a fork, coalesce or barrier transaction.

If L1-4 is ruled in early, E-1b part 2 should follow E-3 directly and be
measured then.

## Working rules for every fix

- One fix per commit series. Run `scripts/branch-safety-check.sh` before
  each commit and push.
- Read CONTRIBUTING.md § Whole-tree gates before writing code. These items
  move a pinned inventory and must re-pin it deliberately, saying so in the
  commit: E-1 and R-2a (function identity in the clock-authority and
  mutation-fencing inventories); E-3b, E-10 and R-7 (they add to a closed
  inventory); L3-6 (a signature in the frozen mutation-API inventory); L2-4
  (the soft-mapping census).
- No aliases, padding or reordering to reduce trust-tier signature churn.
  Several items edit files likely to carry signed suppressions
  (`clients/http.py`, `infrastructure/base.py`, the Landscape verbs); the
  churn is an honest obligation for the operator to re-sign.
- Smoke gate for every engine or Landscape change, as the composer plan
  used: same exit code, row counts, sink output digests, and
  `node_states` `input_hash` / `output_hash` values before and after on
  `transform_pipeline` and `error_routing`. The smoke gate contains no
  violations, so it cannot see a change to a recorded violation value;
  items that touch a violation path (L2-7) need their own pinned test.
- Items marked TC owe `pytest tests/ -m testcontainer -n 0` on a Docker
  host. Changes to shared runtime behaviour owe the full suite once before
  merge via `scripts/full-suite-gate.sh --execute --detach`.
- Measure before claiming. Record counts from the L4-8 pins and CPU
  (user + sys), interleaving baseline and candidate; wall-clock on a shared
  box is not evidence.
- Plugin source hashes. Editing a transform's module changes its
  `source_file_hash`. For `value_transform` that hash is pinned three
  times in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, and
  a changed hash makes a run interrupted before the upgrade refuse to
  resume (`PLUGIN_IMPLEMENTATION_CHANGED`). So batch the plugin edits
  (E-9b, L5-7, L6-5) into one change per plugin, and never edit a plugin
  for a microsecond saving alone.
- Compatibility. L3-4 changes the documented behaviour of a config value;
  L3-3 and L3-8 change effect counts and ids for some runs; each needs its
  compatibility position stated when ruled. Resume refuses a run whose
  runtime VAL manifest has drifted (`engine/orchestrator/resume.py:639-648`),
  and contract hashing is inside what the manifest covers, so E-6a (and any
  other item editing manifest-covered code) makes runs interrupted before
  the upgrade non-resumable after it. Which files the manifest covers was
  not enumerated.

## Review

An independent reviewer read the first draft of this plan in full against
the code and returned 31 findings: one blocker (L2-7 would have changed a
recorded violation value), twelve major and the rest minor. All are applied
in the summary above; the appendices are as the investigators wrote them,
with the differences listed in "Where this summary overrides an appendix".
Appendix L6 was added afterwards to close the coverage gap the review found;
it and the summary rows drawn from it have not been independently reviewed,
and none of its items was prototyped.

The review also confirmed, by reading or experiment: the E-11 rewrite's
equivalence; the L3-1 mechanism and its test-seam consumers; the L1-1
caching claim; that the E-5 fold is a monotone join, so a skipped re-fold is
sound; the L2-1 hash equivalence; the L2-6 defect; the L3-8 cap; and that no
design needs a suppression or a new dynamic-attribute site.

The review did not reach: any ADR text; verification of the ruling items'
mechanisms against code; any pytest or gate run; E-4, E-8a, L4-4, L4-6,
L4-7, L4-8, L5-7 and E-9b against code; whether existing tests pin behaviour
that E-3a, E-3b or L2-8 would break; anything on PostgreSQL.

## Limits

- SQLite, one process, CLI entry. No PostgreSQL run, no follower, no resume
  case, nothing above 4,000 rows. Projections to 100k and 1M rows are
  extrapolations.
- The measurement lanes ran concurrently on a shared 4-core box. Counts are
  exact; CPU figures are indicative and A/B results rest on two to six
  pairs.
- The end-to-end measurements at this commit are: the L2 prototype (−4.6%
  gate, −0.3% transform, −17% at 100 columns), an E-5 variant (−11%
  transform, two pairs), and R-2a (ingest 30 → 24 statements). The earlier
  review's five-site prototype measured −12% gate and −16 to −18% transform
  at a different commit and SQLAlchemy version; the per-item estimates here
  account for roughly half of that transform figure, the difference is not
  reconciled, and that review itself flagged the figure's drift.
- No fix was run against the full default suite or the whole-tree gates.
  Prototypes lived in private copies and are not retained.
- Each appendix lists what its investigator did not verify.

## Appendix L1 — Landscape SQL trunk: remediation plan

Lane L1 of the executor remediation effort. Scope: `src/elspeth/core/landscape/`
(and `scheduler/`) — statement construction, fences and clock reads, read-back
after write, transactions per transition, the output-contract re-record, the
scheduler event identity hash, and what changes on PostgreSQL.

Plan only. Nothing in the tracked tree was edited; `git status --short` was
empty when this was written. All measurements are at HEAD `bf8adf06` (docs-only
on `1a7ff0e6`), file-backed SQLite, one process, SQLAlchemy **2.0.45** (the
version `uv.lock` pins; the prior review measured on 2.1.8). The box was shared
with other lanes, so counts are exact and timings are indicative.

### Summary

- **The prior review's "ready to fix, no ruling" E-1a is not ready at this
  commit.** Its prototype's changes, applied one site at a time to a private
  copy of the tree, turn four production tests of the ADR-047 clock-authority
  gate red (`tests/unit/core/landscape/test_database_clock_authority.py`). The gate pins
  the exact source shape of the leader fence and of both clock helpers, and it
  flags any deadline column bound at module scope. Four of the five prototype
  sites are gate-bound; only the membership fence is free. See E-1a and § Gate
  probe.
- **E-1b is behind the same gate for every statement that carries a
  timestamp.** `conn.execute(<module-level statement>, {…})` passes the gate
  when the mapping holds only identifiers, and is classified as "dynamic raw
  SQL" as soon as the mapping holds a time value, process clock or database
  clock alike. That covers the `node_states`, `rows`, `tokens`,
  `token_work_items` and `scheduler_events` writes, which is most of the
  statement-construction saving. One gate capability (L1-4, new, needs an
  ADR-047 ruling) unlocks all of them; without it the gate-free part of E-1 is
  the membership fence and the by-key selects.
- **Prebuilding does not fix every statement.** SQLAlchemy 2.0's
  dialect-specific `Insert` (`sqlite_insert`, `postgresql_insert`) is declared
  uncacheable, so two hot statements are fully recompiled on every row whether
  or not they are prebuilt (L1-1, new).
- **My trace agrees with the prior review's per-row table and corrects the
  recon by one statement.** Ingest is 29 statements plus `BEGIN IMMEDIATE`, not
  28; 7 are `UPDATE run_coordination` and 7 are clock reads (the recon says 9
  and 5). The gate row's main loop is 58 cursor statements and 6 commits, which
  is the prior review's 64.
- **Every current caller of the three leader `_on` verbs is already inside
  `fenced_leader_transaction` on the same connection with the same token**, the
  standalone wrappers included. The nested fences are redundant by construction
  today, and the transaction-scoped deadline registry already holds the proof.
  A four-line prototype took ingest from 30 to 24 statements per row with
  identical sink output (R-2a).
- **The entry renewal of a leader-fenced transaction is dead on every
  successful path**: the exit renewal overwrites it before commit. Issuing the
  deadline once, at completion, removes two statements per leader transaction
  without any process clock (R-2b, a different proposal from the prior
  review's elapsed-time test).
- **On SQLite the no-op verify-UPDATE fence costs one extra WAL frame per
  transaction** (measured: 2.11 frames against 1.11). The prior review had
  R-12 as inferred only.

### Measurement

Harness in `.claude/lanes/exec-remediation/L1/`: `sqlsite.py` (statements by SQL
text and call site, transactions by opener, ordered trace window), `marg.py` and
`inventory.py` (marginal per-row counts between two run sizes), `compiles.py`
(compiled-cache misses by call site), `bench2.py`, `bench_insert.py`,
`bench_event.py`, `bench_r12.py` (micro-benchmarks), `gateprobe.py` with
`variants.py` / `variants2.py` (whole-tree gates run against a patched private
copy of `src/`).

Command, per workload and size:

    .venv/bin/python .claude/lanes/exec-remediation/L1/sqlsite.py OUT.json TRACE.txt 9000 9500 \
        -- run --settings .claude/lanes/exec-remediation/L1/work/<workload>_<n>/settings.yaml --execute

Per-row figures are `(count at big N − count at 20 rows) / (big N − 20)`; gate
and transform use N = 320, batch and fork N = 120. All eight runs exited 0 with
the expected row counts.

#### Totals per source row

| Workload | Cursor statements | Commits | Rollbacks | Write txns (`BEGIN IMMEDIATE`) | Read txns (plain `BEGIN`) | `PRAGMA` | Clock reads | Leader CAS | Leader renew | Member fence |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gate | 81.45 | 10.04 | 0.02 | 5.04 | 5.02 | 10.00 | 8.17 | 3.04 | 4.08 | 4.00 |
| transform (2 transforms) | 90.36 | 13.03 | 0.02 | 9.03 | 4.02 | 8.00 | 11.14 | 4.03 | 6.06 | 7.00 |
| batch (count 5) | 99.37 | 9.47 | 2.21 | 5.79 | 5.89 | 7.36 | 17.40 | 5.39 | 8.78 | 2.40 |
| fork/coalesce | 393.31 | 53.03 | 8.01 | 27.03 | 34.01 | 52.00 | 45.11 | 9.02 | 16.05 | 20.01 |

"Cursor statements" counts everything that reaches `before_cursor_execute`,
which includes `BEGIN`/`BEGIN IMMEDIATE` and the pragmas. The gate and transform
totals match the recon's cProfile counts (81.46 and 90.39 `Cursor.execute` per
row, Appendix F § 6) and its commit counts (10.05 and 13.04).

By phase, gate row: ingest 30.0, drain 28.2, sink flush 23.2. Transform row:
ingest 30.0, drain 28.2, sink flush 32.1.

#### One gate row, main loop (ordered trace, `gate_320.trace.compact.txt` lines 25–94)

| Transaction | Opener | Statements after `BEGIN` | What they are |
|---|---|---:|---|
| ingest | `fenced_leader_transaction` ← `scheduler/queue.py:412` | 28 (+1 commit-guard clock read) | 3 entry fence; 3 re-fence + `INSERT rows` + `INSERT tokens`; 3 re-fence + `INSERT node_states` + read-back; txn clock, 3 reference selects, `INSERT token_work_items`, select, `INSERT scheduler_events`, select; select `FOR UPDATE`, clock, `UPDATE` claim, `INSERT scheduler_events`, select; 2 exit renewal |
| `begin_node_state` | `fenced_member_transaction` ← `execution/node_states.py:169` | 2 | member fence, `INSERT node_states` |
| routing pre-read | `_assert_routing_targets_recordable` ← `node_states.py:1243` | 3 | `PRAGMA query_only = ON`, select, `PRAGMA query_only = OFF` |
| `record_routing_event` | `fenced_member_transaction` ← `node_states.py:1015` | 8 | member fence, 4 pre-write selects, `INSERT routing_events`, 2 read-backs |
| `complete_node_state` | `fenced_member_transaction` ← `node_states.py:554` | 3 | member fence, `UPDATE node_states`, read-back |
| `mark_pending_sink` | `fenced_member_transaction` ← `scheduler/dispositions.py:717` | 7 | member fence, txn clock, before-image, `UPDATE`, after-image, bundle predicate, `INSERT scheduler_events` |

That is 52 statements, 6 `BEGIN`s and 6 commits: 58 cursor statements and 6
commits, the prior review's 64.

The transform row's drain is `begin_node_state` (2), `update_node_output_contract`
(2: fence, `SELECT … FOR UPDATE`), `complete_node_state` (3), the same three
again for the second transform, then `mark_pending_sink` (7): 7 write
transactions for two transforms.

#### Reconciling the recon (Appendix B) with the prior review

| Claim | Source | Trace at this commit | Verdict |
|---|---|---|---|
| Ingest is 28 statements, 1 commit | recon B § 1 | 28 inside the transaction + 1 commit-guard clock read = 29, plus `BEGIN IMMEDIATE` | Recon is one short: its enqueue step lists 7 and the trace has 8. It counted one of the two work-item selects (`work_items.py:319` and `queue.py:339`), not both |
| "9 are leader-fence/renew statements against the same `run_coordination` row" | recon B § 1 | 7 `UPDATE run_coordination` (3 CAS + 4 renew) | The recon's 9 is three 3-statement entry fences, which includes 3 clock reads; the exit renewal's 2 are listed separately |
| "5 are clock reads" | recon B § 1 | 7: 4 inside renewals, 1 transaction time (`queue.py:288`), 1 claim decision time (`leases.py:225`), 1 commit guard (`lease_deadlines.py:119`) | Recon is two short |
| Leader fence 11 per gate row | prior review, per-row table | 3 CAS + 4 renew `UPDATE` + 4 renewal clock reads = 11 | Confirmed |
| Other clock reads 4; 8 clock queries per row | prior review | 3 at ingest outside renewals + 1 in `_transition_on` (`dispositions.py:870`) = 4; 8.17 per row measured | Confirmed |
| 13 read-backs, 6 pre-write lookups, 12 audit writes, 4 membership fences, 14 transaction-control | prior review | 13, 6, 12, 4, 14 (6 `BEGIN` + 6 `COMMIT` + 2 `PRAGMA`) | Confirmed |
| `begin_node_state` 2 statements, `complete_node_state` 3, `mark_pending_sink` 7, contract no-change path 2 | recon B § 1 | 2, 3, 7, 2 | Confirmed |
| "SA `SQLCompiler.__init__` 4.00 per row" (gate), 2.00 (transform) | recon F § 6 | 2.0 and 1.0 compiled-cache misses per row, at two named sites | Halved; see L1-1 |

Leader fence plus clock reads are 14 of the 29 ingest statements either way.

#### Statement inventory, ordered by statements per row

Full table (153 call-site × statement rows at ≥ 0.2 per row in some workload):
`.claude/lanes/exec-remediation/L1/inventory.md`. The rows that matter for
conversion, with the constraint each one carries:

| Per row: gate / transform / batch / fork | Call site (file under `core/landscape/`) | Statement | Shape | Constraint |
|---|---|---|---|---|
| 6.16 / 9.13 / 14.19 / 33.10 | `read_landscape_decision_time` `database_clock.py:39` | `SELECT strftime(?, ?)` | fixed, per dialect | clock gate pins the helper body |
| 4.08 / 6.06 / 8.78 / 16.04 | `_renew_leader_deadline_on` `run_coordination_repository.py:408` | `UPDATE run_coordination SET leader_heartbeat_expires_at, updated_at` | fixed | clock gate: deadline column, `unproven-leader-deadline-renewal` |
| 4.00 / 7.00 / 2.40 / 20.01 | `verify_membership_fence` `run_coordination_repository.py:541` | `UPDATE run_workers SET status` | fixed | none (V5 passes both gates) |
| 3.04 / 4.03 / 5.39 / 9.02 | `verify_and_extend_leader_fence` `run_coordination_repository.py:449` | `UPDATE run_coordination SET leader_epoch` | fixed | clock gate: `first-effect-not-direct-execute` |
| 3.00 / 3.00 / 3.00 / 9.00 | `SchedulerEventStore.record` `scheduler/events.py:174` | `INSERT scheduler_events … RETURNING seq` | fixed | clock gate: `recorded_at` in the mapping (V10) |
| 2.01 / 2.01 / 3.21 / 12.01 | `read_landscape_transaction_time` `database_clock.py:61` | `SELECT CURRENT_TIMESTAMP` | fixed | clock gate pins the helper body |
| 1.00 / 1.00 / 1.97 / 9.00 | `resolve_token_ownership` `data_flow/ownership.py:96` | token/row join | fixed | read path; also E-10 |
| 1.00 / 1.00 / 1.00 / 9.00 | `validate_node_cursor` `scheduler/work_items.py:175` | `SELECT nodes.node_id` | fixed | E-3b removes it at ingest |
| 1.00 / 1.00 / 1.57 / 5.00 each | `validate_work_item_references` `work_items.py:195`, `:208` | token and row selects | fixed | E-3b |
| 1.00 / 2.00 / 0.20 / 5.00 | `begin_node_state` `execution/node_states.py:170` | `INSERT node_states` | fixed column set | clock gate: `started_at` in the mapping (as V8) |
| 1.00 / 2.00 / 0.20 / 3.00 | `complete_node_state_on` `node_states.py:630`, `:660` | `UPDATE node_states`; read-back | fixed | clock gate on the `UPDATE` (V8); the select is free (V9); E-3a merges the pair |
| 1.00 / 1.00 / 1.00 / 4.00 each | `claim_ready_row` `scheduler/leases.py:214`, `:249`, `:326` | select `FOR UPDATE`; claim `UPDATE`; select | `UPDATE` has 3 where-shapes | clock gate: `available_at`, `lease_expires_at` |
| 1.00 / 1.00 / 1.00 / 4.00 each | `_transition_on` `scheduler/dispositions.py:886`, `:941`, `:989`, `:991` | before, `UPDATE`, after, bundle | `UPDATE` has 4 image shapes × owner predicate | clock gate on the `UPDATE`: `updated_at` in the mapping, and `barrier_blocked_at` on the blocked shape; the selects are free |
| 1.00 / 1.00 / 1.00 / 3.00 each | `insert_work_items_idempotent` `work_items.py:288`, `:319` | `INSERT … ON CONFLICT DO NOTHING RETURNING`; read-back | per dialect | **uncacheable** (L1-1) |
| 1.00 / – / – / 2.00 | `_insert_routing_events_on` `node_states.py:1316` | `INSERT routing_events` | per dialect | **uncacheable** (L1-1) |
| 1.00 / 1.00 / 1.00 / 2.00 | `insert_row_with_token_on` `data_flow/tokens.py:479`, `:482` | `INSERT rows`; `INSERT tokens` | fixed column set | clock gate: `created_at` in the mapping (as V8) |
| 1.00 / 1.00 / 1.00 / 2.00 | `record_completed_node_state_on` `node_states.py:278`, `:299` | `INSERT node_states`; read-back | fixed | clock gate on the `INSERT` (as V8); E-3a merges the pair |
| – / 2.00 / – / 1.00 | `update_node_output_contract` `data_flow/graph.py:435` | `SELECT … FOR UPDATE` on `nodes` | fixed | E-5 removes the call |
| 5.00 / 4.00 / 3.68 / 26.00 of each | every `ReadOnlyDatabaseOps` read | `PRAGMA query_only = ON` / `OFF` built with `text()` per call | fixed | E-10 |

Files by statements per gate row: `run_coordination_repository.py` 16.2,
`query_repository.py` 16.0 (sink-flush lineage, another lane),
`execution/node_states.py` 16.0, `database_clock.py` 8.2,
`scheduler/work_items.py` 5.0, `scheduler/dispositions.py` 4.0,
`scheduler/events.py` 4.0, `scheduler/leases.py` 3.0, `data_flow/tokens.py` 2.0.

#### Cost of one statement, built per call against prebuilt

`bench2.py`, in-memory SQLite, CPU per call, best of three × 5,000:

| Statement | Built per call | Prebuilt with `bindparam()` |
|---|---:|---:|
| Leader CAS | 118.5 µs | 13.0 µs |
| Membership fence | 120.8 µs | 13.9 µs |
| Decision clock | 86.8 µs | 19.1 µs |
| Select one work item (38 columns) | 88.8 µs | 15.7 µs |
| Claim `UPDATE` | 214.9 µs | 22.1 µs |
| `node_states` completion `UPDATE` | 285.3 µs | 41.8 µs |

The ratio agrees with the prior review's 94 → 12 µs on 2.1.8. Absolute values
were taken under load.

#### Gate probe

Method: copy `src/` to `L1/proto/`, apply one change, and run the zero-argument
production tests of a whole-tree gate with its `_ROOT` pointed at the copy
(`gateprobe.py`; the lane directory is gitignored, so the helper's gitignore
subtraction is disabled for the copy). Control: the unmodified copy passes all
86 zero-argument tests of the clock-authority gate, as does the real tree.

| Variant | Change | Clock-authority gate result |
|---|---|---|
| V0 | none | 86 pass |
| V1 | clock helpers only (`database_clock.py`) | 3 fail: `test_every_clock_sensitive_decision…` and `test_landscape_authority_has_no_process_or_caller_clock_ingress` (every consumer of the helper becomes `unresolved-clock-provenance`, e.g. `sink_effect_finalization.py:505`), `test_production_leader_deadline_issuance_is_source_proven` |
| V3 | leader CAS only | `first-effect-not-direct-execute`; issuance provenance drifted |
| V4 | leader renewal only | `unproven-leader-deadline-renewal`; issuance provenance drifted |
| any variant with a module-level `leader_heartbeat_expires_at=bindparam(…)` | — | `run_coordination_repository.py:<module>: missing-database-time (leader_heartbeat_expires_at)` |

A second round ran the production-tree tests of both source gates (11 in the
clock-authority gate, 7 in
`tests/unit/architecture/test_web_landscape_mutation_fencing.py`; the unmodified
copy passes all 18). Each variant was also run as a pipeline first where it
changes behaviour, and exited 0.

| Variant | Change | Clock-authority gate (11) | Mutation-fencing gate (7) |
|---|---|---|---|
| V0 | none | 11 pass | 7 pass |
| V5 | membership fence executes a prebuilt statement; mapping holds `run_id`, `worker_id` | 11 pass | 7 pass |
| V9 | the three `node_states` read-backs execute a prebuilt `SELECT … WHERE state_id = :b`; mapping holds the id | 11 pass | 7 pass |
| V7 | module-level `node_states` and `scheduler_events` statements added, functions untouched | 11 pass | 7 pass |
| V6b | module-level `UPDATE token_work_items … lease_expires_at=None` added | 11 pass | 7 pass |
| V6a | module-level claim `UPDATE` added, binding `available_at <= :b` and `lease_expires_at = :b` | 1 fail: `leases.py:<module>: missing-database-time` for both columns | 7 pass |
| V8 | `complete_node_state_on` executes a prebuilt `UPDATE`; mapping holds `completed_at` (process clock) | 3 fail: `node_states.py:648 … dynamic-authority-raw-sql (_COMPLETE_NODE_STATE)`, with `missing-database-time`, `caller-clock-authority`, `forensic-clock-authority`, `process-clock-authority (dynamic raw SQL)`; the function also enters the authority inventory as `unexpected` | 7 pass |
| V10 | `SchedulerEventStore.record` executes a prebuilt `INSERT … RETURNING seq`; mapping holds `recorded_at` (database clock at every caller) | 2 fail: `events.py:177 … dynamic-authority-raw-sql (_INSERT_EVENT_RETURNING_SEQ)`, `missing-database-time`, `caller-clock-authority`, `forensic-clock-authority` | 7 pass |
| R-2a | `ensure_leader_fence_on` added and used by the three `_on` verbs | 1 fail: `test_clock_authority_definition_inventory_is_closed_and_stable`, `unexpected=[… 'ensure_leader_fence_on']` — the closed inventory, re-pinned on purpose | 7 pass |

What the probe establishes:

1. A prebuilt statement executed with a mapping of identifiers passes both
   gates (V5, V9).
2. The same pattern with a time value in the mapping fails the clock gate,
   whether the value is a process-clock forensic timestamp (V8) or a database
   sample (V10), and whether or not the statement touches a deadline column.
   The gate cannot see which column the value lands in, so it treats the call
   as raw SQL.
3. A deadline column written or compared from a module-level statement is a
   finding at `<module>` (V6a, and the `leader_heartbeat_expires_at` case
   above). `lease_expires_at=None` is not (V6b).
4. The mutation-fencing gate's seven production tests did not object to any
   variant, including a fence statement and a DML statement that moved out of
   their functions.

Limits: these are the gates' zero-argument production tests, run through a
filter (names containing `test_landscape_`, `test_every_`, `test_production_`,
`test_first_leader_fence_statement`, `test_clock_inventory`,
`test_required_authority`, `test_clock_authority_definition`), not the whole
files; the other tests exercise the scanners on synthetic sources. Logs:
`L1/gate-variants.log`, `gate-variants2.log`, `gate-variant-V6b.log`, `-V8.log`,
`-V9.log`, `-V10.log`, `-R2a.log`. The private trees were deleted afterwards;
`variants.py` and `variants2.py` rebuild V1–V7, and `L1/patches/` holds the
diffs for V9, V10 and R-2a. V8 is the `complete_node_state_on` `UPDATE` moved to
a module-level statement with typed binds, executed with a nine-key mapping.

The mutation-fencing gate
(`tests/unit/architecture/test_web_landscape_mutation_fencing.py`, 27,941 lines)
freezes the production DML identity and write set
(`test_landscape_dml_identity_and_write_set_are_frozen`), the caller set, and
the rule that every Landscape DML transaction's first database effect is the
full-token fence
(`test_every_landscape_dml_transaction_is_full_token_fenced_first`). It did not
object to a moved statement, but any item that adds a verb or a parameter
(R-7, E-3b, E-10) adds to an inventory it closes.

---

### Items

#### E-1a — Prebuild the fence and clock statements, in two parts

**Finding.** Prior review finding 1 / E-1a; recon Appendix F § 5 (statement
build and cache-key generation). The five fence and clock sites issue 21.3 of
the 81.5 statements per gate row and 31.2 of 90.4 per transform row, each built
from scratch.

**Mechanism (verified at 1a7ff0e6).** `read_landscape_decision_time` builds
`func.strftime(…)` or `func.clock_timestamp()` per call
(`core/landscape/database_clock.py:35`, `:39`); `read_landscape_transaction_time`
builds `func.current_timestamp()` (`:56`, `:61`). `_renew_leader_deadline_on`
builds its `update(run_coordination_table)` inline
(`run_coordination_repository.py:408-416`), as do `verify_and_extend_leader_fence`
(`:449-457`) and `verify_membership_fence` (`:541-549`).

The prior review lists this as low risk with no ruling. **REFUTED for four of
the five sites.** The clock-authority gate recognises these functions by exact
source shape: the leader fence's first effect must be a direct
`conn.execute(update(run_coordination_table)…)`
(`tests/unit/core/landscape/test_database_clock_authority.py:3881` onwards), the
decision helper's body is matched literally
(`test_fresh_clock_provenance_refuses_changed_implementation`, `:5131`), and a
deadline column written from a module-level statement is reported as
`missing-database-time` at `<module>`. `database_clock.py:11` says the same:
"The authority gate binds their exact owned implementations".

**Evidence.** § Gate probe (V1, V3 and V4 fail against a V0 control of 86
passes; V5 passes). Micro-benchmark above: 118.5 → 13.0 µs (leader CAS),
120.8 → 13.9 µs (membership fence), 86.8 → 19.1 µs (decision clock).

**Change.**

*Part 1, membership fence.* One module-level statement in
`run_coordination_repository.py`:

```python
_MEMBER_FENCE_CAS = (
    update(run_workers_table)
    .where(
        run_workers_table.c.run_id == bindparam("b_run_id"),
        run_workers_table.c.worker_id == bindparam("b_worker_id"),
        run_workers_table.c.status == "active",
    )
    .values(status="active")
)
```

and `verify_membership_fence` executes it with
`{"b_run_id": …, "b_worker_id": …}` (add `type_=` to each bind as E-1b
requires; omitted here for width). The rowcount check and the
unregistered/evicted diagnosis below it are unchanged.

*Part 2, leader CAS, leader renewal and the two clock helpers.* Same pattern,
but it lands only with the gate change in L1-4: the gate must follow a bind
name from a module-level statement to the execute site to prove the bound
value derives from `read_landscape_decision_time`, and the helper-body matcher
must accept `conn.scalar(_SQLITE_DECISION_TIME)` with a control that rebinding
the module constant is refused. R-2a and R-2b remove most of these statements
first (11 → 4 per ingest), so Part 2 is worth doing only if M-2 still shows
them after that.

Rejected: `lambda_stmt` (keeps the expression inline, but the fence's first
effect would still not be a direct `conn.execute(update(…))` — not probed — and
it adds closure analysis per call). Rejected: caching the
statement in a function attribute or `lru_cache` keyed on the token (same gate
failure, plus hidden state).

**Must not change.** The SQL text on SQLite (verified identical for the
membership fence); the rowcount-1 refusal and both refusal exception types; the
order "CAS, then clock sample, then renewal" (ADR-047); the fence being the
first statement of its transaction; the `fence_refusal` event on a fresh
connection after rollback.

**Risk.** Part 1 Low: one statement, no deadline column, same text; the probe's
V5 passes both source gates. Part 2 Medium: mechanical in `src/`, but it rests
on L1-4.

**Ruling needed.** Part 1: No. Part 2: Yes, through L1-4 (ADR-047).

**Verification.** `tests/unit/core/landscape/test_database_clock_authority.py`,
`test_database_clock.py`, `test_coordination_fence_constructs.py`,
`test_leader_fence_stale_token.py`, `test_scheduler_fencing.py`,
`test_heartbeat_transaction_fencing.py`,
`tests/unit/architecture/test_web_landscape_mutation_fencing.py`. New: the
equivalence test in L1-2 (compiled text and bind types of each prebuilt
statement equal the inline form on both dialects; negative control: drop a
predicate from the prebuilt statement and the test goes red). Testcontainer
suite owed (`tests/testcontainer/core/test_enqueue_membership_fence_postgres.py`,
`test_database_clock_postgres.py`, `test_heartbeat_takeover_postgres.py`).
Measurement: `sqlsite.py` statement counts unchanged; interleaved A/B CPU on
gate and transform at 1,000 rows, four runs each.

**Expected saving.** Part 1: 4.0 fences per gate row × ~107 µs ≈ 0.43 ms/row,
about 1.6% of the 26.6 ms marginal row; 7.0 × 107 µs ≈ 0.75 ms, about 2.5%, on
the transform workload. Part 2 today: ≈ 1.3 ms/row gate (≈ 5%), ≈ 1.8 ms/row
transform (≈ 6%); after R-2a and R-2b roughly a third of that. Basis: per-row
counts × micro-benchmark deltas. The prior review measured −12% and −16–18%
for all five sites plus the contract-hash memo.

---

#### E-1b — Prebuild the per-row scheduler, node-state and token statements

**Finding.** Prior review finding 1 / E-1b. After E-1a about 40 statements per
gate row are still built inline (81.5 less 20 transaction-control and pragma
statements, less the 21.3 of E-1a).

**Mechanism (verified at 1a7ff0e6).** Inline construction at every site in the
inventory above: `scheduler/events.py:174-176`
(`insert().values(**values).returning(seq)`), `scheduler/work_items.py:175`,
`:195`, `:208`, `:319`, `scheduler/queue.py:339`, `scheduler/leases.py:214-219`,
`:249-257`, `:326-331`, `scheduler/dispositions.py:886`, `:896-957`, `:989`,
`:991`, `execution/node_states.py:170-183`, `:278-296`, `:299`, `:630-643`,
`:660`, `:1221`, `:1377`, `:1443`, `data_flow/tokens.py:479-491`,
`data_flow/graph.py:435-439`.

The prior review lists this as mechanical with no ruling. **REFUTED for the
writes.** Every hot `INSERT` and `UPDATE` in this list binds a timestamp
(`started_at`, `completed_at`, `created_at`, `recorded_at`, `updated_at`,
`available_at`, `lease_expires_at`), and the clock-authority gate classifies a
prebuilt statement executed with a time value in its parameter mapping as
dynamic raw SQL (§ Gate probe, V8 and V10). The by-key selects are free (V9).

**Evidence.** Inventory table; micro-benchmark (88.8 → 15.7 µs for a work-item
select, 214.9 → 22.1 µs for the claim `UPDATE`, 285.3 → 41.8 µs for the
node-state completion `UPDATE`); gate probe V5, V8, V9, V10, V6a.

**Change.** Conversion pattern:

1. *Fixed-shape statements* become module-level constants next to the verb that
   uses them, named `_<VERB>_<WHAT>`, with every caller value a
   `bindparam("b_<name>", type_=<column>.type)`. The `b_` prefix avoids the
   collision SQLAlchemy raises when a bind is named after a column in the same
   `INSERT`/`UPDATE`. The explicit `type_` is not optional: a bind with no type
   loses the column's bind processor, and on SQLite a `DateTime` stored in the
   wrong text format compares wrongly as text (the hazard ADR-047 item 4
   describes).
2. *Inserts* use the bare `table.insert()` (plus `.returning(…)` where present)
   and pass the row as the parameter dict. The column list is then fixed by the
   dict's key set, so each verb passes the same keys every call, `None`
   included, as the code already does.
3. *Branch-shaped statements* get one constant per shape, built at import by a
   small builder, selected by an explicit `if`/`elif` or a dict keyed on the
   branch: `_transition_on` has four image shapes (`BlockedImage`,
   `TerminalImage`, `FailedImage`, `PendingSinkImage`) × owner predicate present
   or absent; `claim_ready_row` has three where-shapes (plain, strict
   membership, claim-verb membership). An absent predicate is a different
   statement, never an `OR :b IS NULL` arm.
4. *Variable-length `IN`* (`expected_statuses`, chunked id lists) uses
   `bindparam("b_…", expanding=True)`; the compiled form stays cached and the
   list is expanded at execution on both dialects.
5. *Dialect-neutral by construction.* The constants are Core expressions with
   no dialect baked in; the engine's compiled cache keys on the dialect, so one
   constant serves SQLite and PostgreSQL. `with_for_update(of=…)` compiles to
   nothing on SQLite. Where the code already branches on `conn.dialect.name`
   (`_lock_routing_state`, the idempotent inserts) keep one constant per branch.
6. *Registry.* Each constant is created through one helper,
   `prebuilt(name, stmt)`, which appends to a module-level tuple; L1-2's tests
   iterate it.

It lands in two parts:

- *Part 1, no time value in the mapping* — the by-key selects: work item by id,
  node state by id, the routing lookups (`node_states.py:1221`, `:1377`,
  `:1443`, `:1484`), the disposition before-image, the reference validation,
  the node-contract select. No gate objection (V9). Do it after E-3a, E-3b and
  E-5, which delete most of these statements; what remains per gate row is
  about five.
- *Part 2, everything that binds a timestamp* — every hot write. It waits for
  L1-4.

Rejected: a generic statement cache keyed on call-site string (hides the
statement from the reader and from the gates). Rejected: `text()` SQL (loses
type processing and dialect neutrality, and is what the gate already calls raw
SQL). Rejected: prebuilding only the `WHERE` and adding `.values(…)` inline per
call (still a new statement object and a new cache key per call).

**Must not change.** SQL text on SQLite (identical); on PostgreSQL only the
placeholder names change (`%(run_id_1)s` → `%(b_run_id)s`, verified by
compiling both forms), which also changes the parameter keys in JSONL journal
records on PostgreSQL. Every bound value, every bind type, every rowcount and
integrity check, every `LandscapeRecordError` wrapper.

**Risk.** Part 1 Low. Part 2 Medium: each conversion is mechanical, but the set
is wide (about nine files), a missing bind type is silent, and it rests on a
gate change.

**Ruling needed.** Part 1: No. Part 2: Yes, through L1-4 (ADR-047).

**Verification.** `tests/unit/core/landscape/test_scheduler_events.py`,
`test_scheduler_ingest.py`, `test_scheduler_queue_contract.py`,
`test_node_state_recording.py`, `test_token_recording.py`,
`test_token_recording_integrity.py`, `test_routing_group_authority.py`,
`test_scheduler_pending_sink_claim.py`, `test_state_engine_forbidden_paths.py`;
both source gates in full; L1-2's tests. Testcontainer suite owed in full.
Smoke gate from the prior review: same exit code, row counts, sink output
digests and `node_states.input_hash`/`output_hash` on `transform_pipeline` and
`error_routing`. Measurement: `compiles.py` shows no new cache misses;
interleaved A/B CPU.

**Expected saving.** Part 1: about five statements per gate row after E-3,
≈ 0.4 ms/row (≈ 1.5%). Part 2: unknown until measured; estimate 8–12% of
per-row CPU on the gate workload (about 25 writes × 70–240 µs of construction
and cache-key work against a 26.6 ms row). Both overlap E-3a/E-3b one for one
on the statements those remove.

---

#### L1-4 — Teach the clock-authority gate to resolve a prebuilt statement and its bind mapping

**Finding.** New; it is the precondition the prior review's E-1a and E-1b did
not know they had. Recon § 1 ("Where the time goes") puts roughly 50–60% of
per-row time in SQLAlchemy statement construction and compilation; this is what
stands between that finding and most of its fix.

**Mechanism (verified at 1a7ff0e6).** The gate proves clock provenance by
reading inline SQLAlchemy expressions: it sees `values(completed_at=timestamp)`
and can tell that a process-clock value lands in a forensic column, or that a
deadline column receives a database sample. `conn.execute(_STATEMENT, {…})`
hides the column from it, so it reports the call as
`dynamic-authority-raw-sql` and every time value in the mapping as unproven
(probe V8: `node_states.py:648`; V10: `events.py:177`). A deadline column in a
module-level statement has no enclosing function to prove its value and is
reported at `<module>` (V6a). The leader fence and the two clock helpers are
matched by literal source shape
(`tests/unit/core/landscape/test_database_clock_authority.py:3881`, `:5131`).

**Evidence.** § Gate probe.

**Change.** One capability in the gate's scanner, with its controls:

- When the first argument of `conn.execute`/`conn.scalar` is a `Name` bound
  exactly once at module scope to a SQLAlchemy construct, parse that
  construct, map each `bindparam("b_x")` to the column it is compared with or
  assigned to, and then treat the call's mapping entry `"b_x": value` exactly
  as the gate treats `column == value` or `values(column=value)` today.
- Fail closed when the name is rebound, imported, built conditionally, or the
  mapping is not a literal dict with literal keys; when a bind in the statement
  has no entry or the mapping has an entry with no bind; and when the same bind
  name feeds a deadline column and a forensic column.
- Extend the first-fence contract and the decision-clock matcher to accept the
  module-constant form of those exact statements, each with a rebinding
  control.
- Adversarial controls, in the gate's existing style: a mutation for each
  refusal above, and one showing that a process-clock value bound to
  `lease_expires_at` through a prebuilt statement is still caught.

No change under `src/` belongs to this item.

Rejected: exempting module-level statements from the gate (it would reopen
exactly the fail-open the gate closes). Rejected: allowlisting each converted
statement by name (about forty entries to keep in step by hand).

**Must not change.** Every finding the gate produces today on the unconverted
tree; the closed inventories and their digests except where a converted
function's identity is deliberately re-pinned; "no `xfail`, no `--deselect`,
no assertion loosening" (ADR-047, original context).

**Risk.** Medium–High. The gate is 8,326 lines and is the enforcement of an
accepted ADR; an error in the new resolver fails open.

**Ruling needed.** Yes — ADR-047 ("Static gates must prove clock provenance and
operation ordering, and retain their adversarial controls"; the first-fence
contract in the original context). Question: *may Landscape authority code
execute module-level prebuilt statements, with the clock-authority gate
extended to prove provenance through a literal bind mapping rather than an
inline expression — and does that extension also cover the leader fence, the
leader renewal and the two clock helpers, whose source shape the gate pins
exactly?*

**Verification.** The gate's own suite
(`tests/unit/core/landscape/test_database_clock_authority.py`,
`test_database_clock.py`), run in full at `-n 0` before and after with the
finding set diffed, as ADR-047's threading plan requires. Positive controls:
the V5 and V9 forms still pass; the V8 and V10 forms now pass. Negative
controls: the mutations listed above. No testcontainer run owed for the gate
change itself.

**Expected saving.** None directly. It unlocks E-1a part 2 and E-1b part 2:
≈ 5–6% plus an estimated 8–12% of per-row CPU on the gate workload today.

---

#### L1-1 — Stop recompiling the two dialect-specific inserts on every row

**Finding.** New. Recon Appendix F § 6 reports "stmt compiles = cache misses"
at 4.00 per gate row and 2.00 per transform row without naming them.

**Mechanism (verified at 1a7ff0e6).** `sqlalchemy.dialects.sqlite.dml.Insert`
and `sqlalchemy.dialects.postgresql.dml.Insert` set `inherit_cache = False`
(`.venv/lib/python3.13/site-packages/sqlalchemy/dialects/sqlite/dml.py:76`,
`postgresql/dml.py:73`), so a statement built from `sqlite_insert(…)` or
`postgresql_insert(…)` is compiled on every execution. Two such statements run
per row: `insert_work_items_idempotent` (`scheduler/work_items.py:288-309`,
`ON CONFLICT DO NOTHING … RETURNING`) on every enqueue, and
`_insert_routing_events_on` (`execution/node_states.py:1305-1316`) on every
gate decision. The SQLite arm of the second is a plain
`sqlite_insert(routing_events_table)` with no conflict clause at all.
Prebuilding does not help: a prebuilt dialect insert still compiles once per
call.

**Evidence.** `compiles.py`: 2.007 compiles per gate row (1.00 at
`work_items.py:288`, 1.00 at `node_states.py:1316`), 1.007 per transform row
(`work_items.py:288`). `bench_insert.py`, CPU per call:
`sqlite_insert(…).on_conflict_do_nothing(…).returning(…)` 176.6 µs inline and
136.7 µs prebuilt, 1.00 compile per call either way; generic prebuilt
`insert().returning(…)` 30.3 µs, 0 compiles.

**Change.**

- `node_states.py:1316`: on SQLite execute the generic
  `routing_events_table.insert()` (prebuilt). Same SQL text; the existing
  comment already states that `BEGIN IMMEDIATE` makes a plain insert correct
  there. PostgreSQL arm unchanged.
- `work_items.py:288`: on SQLite, read first, then insert. Select the existing
  rows for the batch ids with a prebuilt `IN` (expanding) statement, validate
  replays with `_validate_replay` as today, and insert the remainder with the
  generic `token_work_items_table.insert().returning(token_work_items_table)`.
  Under `BEGIN IMMEDIATE` no other writer can interleave between the read and
  the insert. This also yields the inserted rows, which E-3b needs. PostgreSQL
  keeps `ON CONFLICT DO NOTHING`, where one compile is small beside a round
  trip.

Rejected: `insert().prefix_with("OR IGNORE", dialect="sqlite")` — cacheable,
but it ignores every constraint violation, not only the `work_item_id`
conflict. Rejected: waiting for a SQLAlchemy release that caches these
constructs (not verified that one does; `pyproject.toml` allows `<3`, the lock
pins 2.0.45).

**Must not change.** Idempotent replay semantics: an existing row with a
different image still raises `LandscapeRecordError`; an identical one is a
no-op and records no second `ENQUEUE` event. The set returned as "inserted".

**Risk.** Low for routing (identical SQL). Low–Medium for work items: the
SQLite and PostgreSQL arms diverge in shape, which the
`contract_invariants.portable_sqlite_insert` lint rule
(`tests/unit/elspeth_lints/test_portable_sqlite_insert_rule.py`) may have an
opinion on.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_scheduler_ingest.py`,
`test_scheduler_queue_contract.py`, `test_routing_group_authority.py`,
`tests/unit/elspeth_lints/test_portable_sqlite_insert_rule.py`; testcontainer
`test_routing_reason_postgres.py`. New test: `compiles.py`'s hook as a unit
test asserting zero compiles per row after warm-up on a 30-row in-memory run;
negative control: restore `sqlite_insert` at one site and the count returns to
1 per row.

**Expected saving.** ≈ 0.11–0.15 ms per call (176.6 → 30–60 µs): about 0.5% of
a transform row, 1% of a gate row. Small, and invisible to E-1b.

---

#### L1-2 — Guard tests: statement identity, statement budget, bind equivalence

**Finding.** New; the task asks for a guard that stops inline construction
returning. Recon Appendix B § 10: nothing in the tree measures statements or
commits per row, and the four DB-write micro-benchmarks do not run.

**Mechanism (verified at 1a7ff0e6).** SQLAlchemy memoises the cache key on the
statement object, so "prebuilt" is observable at run time as *the same
statement object executed again*. `before_execute` receives the
`ClauseElement`; `before_cursor_execute` receives the text.

**Evidence.** `sqlsite.py` and `compiles.py` already produce the three
measurements below from an unmodified run.

**Change.** Three tests in a new
`tests/unit/core/landscape/test_statement_discipline.py`, run in the default
selection on an in-memory Landscape with a fixed gate pipeline and a fixed
two-transform pipeline:

1. *Identity.* Listen on `before_execute`, keep a strong reference to every
   statement object executed while a frame from the hot-verb list is on the
   stack, and run 10 rows then 30. Assert the number of distinct statement
   objects did not grow between row 10 and row 30. A statement built inline
   yields a new object per row, so growth is exactly the regression. Exemptions
   are an explicit set keyed by call site, so the test can land first and
   ratchet down; the set is empty only once E-1b part 2 has landed, and until
   L1-4 is ruled it names every timestamp-carrying write.
2. *Budget.* Assert exact statements, commits and compiles per row for each
   pipeline, pinned from the fixture's own measurement (the CLI workloads give
   81 / 10 / 2 and 90 / 13 / 1; an in-memory fixture will differ). Any change
   to a count fails and must be re-pinned in the same commit, with the reason.
3. *Equivalence.* For every statement in the `prebuilt` registry, on the SQLite
   and PostgreSQL dialects: no bind has `NullType`; each bind's type is the
   type of the column it is compared with or assigned to; and for the
   statements converted from an inline form, the compiled text equals a
   reference inline construction kept in the test (modulo placeholder names on
   PostgreSQL).

Negative controls, one per test: add one inline `select()` to a hot verb in a
monkeypatched copy (identity grows by 20); add one statement to a verb (budget
off by one); change one `bindparam` to carry no `type_` on a `DateTime` column
(equivalence red).

Rejected as the primary guard: an AST rule forbidding `select(`/`update(` in
named functions. It is cheap to evade by moving construction into a helper and
it adds a third source-shape gate to two that are already very large.

**Must not change.** Nothing in `src/` beyond the registry helper.

**Risk.** Low. The budget test will be touched by every lane that removes a
statement; that is its purpose.

**Ruling needed.** No.

**Verification.** The three negative controls above. No testcontainer run owed
for the tests themselves.

**Expected saving.** None directly; it holds the savings of E-1 and E-3.

---

#### R-2a — Stop re-taking the leader fence inside a transaction that already holds it

**Finding.** Prior review finding 2 / R-2a; recon Appendix B § 0 ("the `*_on`
helpers re-run the 3-statement entry fence when composed").

**Mechanism (verified at 1a7ff0e6).** `fenced_leader_transaction` fences as its
first statement (`run_coordination_repository.py:489-490`).
`insert_row_with_token_on` (`data_flow/tokens.py:457-459`),
`record_completed_node_state_on` (`execution/node_states.py:257-262`) and
`record_failed_source_quarantine_state_on` (`node_states.py:334-339`) each call
`verify_and_extend_leader_fence` again on the caller's connection. All of their
callers are inside `fenced_leader_transaction` with the same token: the ingest
verb (`scheduler/queue.py:412-434`), the quarantine ingest (`queue.py:525-556`),
and the two standalone wrappers (`tokens.py:413-418`, `node_states.py:213-219`),
which therefore fence twice on their own.

The second CAS cannot fail: the first one holds the seat row lock (PostgreSQL)
or the database write lock (SQLite) until commit, so no other transaction can
change `leader_epoch`. Each renewal's stored value is overwritten by the next
and finally by the exit renewal (`:492-493`); only the last is ever committed.
`has_issued_deadline(conn, key=_leader_deadline_key(token))`
(`lease_deadlines.py:183-186`) is already true exactly when this outer
transaction renewed this exact `(run_id, worker_id, leader_epoch)`, is bound to
the transaction object (`:75`), and is cleared on commit, rollback and
intentional release (`run_coordination_repository.py:1173`).

**Evidence.** Trace lines 9026–9028 and 9031–9033: two nested fences, 6
statements, per ingest; 3.04 leader CAS and 4.08 renewals per gate row.

Prototype, in a private copy of `src/` on `PYTHONPATH` (`elspeth.__file__`
verified; diff kept as `L1/patches/R-2a-ensure-leader-fence.patch`): the helper
below plus the three call-site changes, gate workload at 320 rows. Exit 0; ingest fell from 9,600 to 7,680
statements (30.0 → 24.0 per row); drain (9,020) and sink flush (8,135) were
unchanged; both sink output files have the same SHA-256 as a baseline run made
immediately afterwards. CPU was 18.84 s against 18.55 s for that baseline —
single runs with other jobs on the box, so the saving is below what this
measurement resolves.

**Change.** One helper beside `verify_and_extend_leader_fence`:

```python
def ensure_leader_fence_on(conn, *, token, window_seconds, verb):
    if has_issued_deadline(conn, key=_leader_deadline_key(token)):
        return
    verify_and_extend_leader_fence(conn, token=token, window_seconds=window_seconds, verb=verb)
```

and the three `_on` verbs call it instead of `verify_and_extend_leader_fence`.
A raw-connection caller, or a caller presenting a different token (a stale
epoch has a different key), takes today's path unchanged, so every stale-token
test keeps its refusal.

Rejected: a witness type yielded by `fenced_leader_transaction` (the prior
review's preference). "Only it can construct" is not enforceable in Python
without another source gate, it changes three public signatures frozen by
`test_landscape_mutation_api_inventory_is_literal_complete_and_cardinality_one`,
and the registry already is a runtime fact tied to the transaction. Rejected:
failing closed when no fence is held (it would turn the stale-token tests'
`RunLeadershipLostError` into an integrity error).

**Must not change.** The committed `leader_heartbeat_expires_at` and
`updated_at` (still the exit renewal's sample); the fence as first statement of
the transaction; refusal with `RunLeadershipLostError` and the `fence_refusal`
event for a stale token on any path; the commit guard's check.

**Risk.** Medium. The logic is four lines on the leader authority path. In the
probe the prototype passed the mutation-fencing gate's seven production tests,
`test_every_landscape_dml_transaction_is_full_token_fenced_first` included, and
ten of the clock gate's eleven; the one failure is the closed authority
inventory reporting `ensure_leader_fence_on` as a new definition, which is the
re-pin the gate exists to force.

**Ruling needed.** Yes — ADR-047, current decision, "Connection-accepting
helpers register obligations on the caller's outer transaction" and the
verifier docstring's "Raw Connection callers retain an explicit renewal
obligation"; ADR-030 D4, "Every run-scoped write verb … opens with a
verify-and-extend CAS". Question: *may a connection-accepting leader verb
discharge its fence obligation by proving, from the transaction-scoped
issued-deadline registry, that this outer transaction already performed the
full-token CAS and renewal for this exact token, instead of repeating both?*

**Verification.** `tests/unit/core/landscape/test_leader_fence_stale_token.py`,
`test_coordination_fence_constructs.py`, `test_coordination_deadline_finalization.py`,
`test_lease_deadline_guard.py`, `test_scheduler_ingest.py`,
`test_state_engine_forbidden_paths.py`, both source gates. New tests: (a) inside
`fenced_leader_transaction` the three verbs issue zero `run_coordination`
statements; (b) negative control — the same verb on a bare `begin_write`
connection still issues the CAS and renewal, and with a stale-epoch token inside
a transaction fenced under the live token it still raises
`RunLeadershipLostError`; (c) after `forget_issued_deadline` the verb fences
again. Testcontainer suite owed
(`test_coordination_deadline_finalization_postgres.py`,
`test_checkpoint_fence_serialisation_postgres.py`,
`test_quarantine_resume_crash_matrix_postgres.py`). Measurement: ingest falls
from 29 to 23 statements in `sqlsite.py`.

**Expected saving.** 6 statements per source row (4 `UPDATE`, 2 clock reads):
about 0.65 ms/row, ≈ 2.5% of a gate row, while they are built inline; under
0.1 ms after E-1a Part 2. Six round trips per row on PostgreSQL.

---

#### R-2b — Issue the leader deadline once, at completion

**Finding.** Prior review R-2b proposes making the post-body renewal
conditional on elapsed time. Recon Appendix B § 0: "6 overhead statements per
leader-fenced transaction".

**Mechanism (verified at 1a7ff0e6).** A leader-fenced transaction runs: CAS
(`:449`), clock read and renewal `UPDATE` at entry (`:406-416` via `:465`), the
body, clock read and renewal `UPDATE` at exit (`:492-493`), then one more clock
read in the commit listener because a deadline was issued
(`lease_deadlines.py:117-119`). The entry renewal's row values are replaced by
the exit renewal before anything commits, and on any failure the transaction
rolls back. So on every successful path the entry clock read and entry
`UPDATE` have no durable effect. Their only live effect is registering the
deadline key, which is what makes the exit renewal run (`:492`).

The prior review's elapsed-time test needs a time source: a database clock
read costs the statement it is trying to save, and a process clock in the
fence is what ADR-047 exists to prevent.

**Evidence.** Trace lines 9024–9025 (entry) and 9049–9050 (exit), then 9052
(guard): three clock samples and two identical-shape `UPDATE`s around a body
that, at ingest, takes a few milliseconds. Leader-fenced transactions per row:
1.04 gate, 2.03 transform (ingest and the per-row checkpoint), 3.39 batch, 7.02
fork.

**Change.** In `fenced_leader_transaction` only: entry performs the identity
CAS and registers "seat lock held for this token" without a clock read or
renewal; exit performs the single clock read, the renewal and
`record_issued_deadline`, unless the body released the seat. The commit guard
is unchanged and still samples after journal precommit work.
`verify_and_extend_leader_fence` keeps its CAS-then-renew body for
raw-connection callers, which have no exit. A leader-fenced transaction goes
from 6 overhead statements to 4.

Before implementing, read every body that reads `leader_heartbeat_expires_at`
inside a leader-fenced transaction: such a body would now see the pre-entry
value. By grep the only readers are verbs in `run_coordination_repository.py`
itself (`live_leader`, `worker_heartbeat`, `admit_follower`, the acquisition
CAS at `:798-857`, `:1013-1064`, `:1228-1238`, `:1353-1362`, `:1446-1484`); I
did not read each one end to end.

Optional third step, separately ruled: when no journal is attached and the
exit renewal was the transaction's last statement, let the commit guard reuse
the exit sample instead of reading the clock again (4 → 3).

Rejected: the elapsed-time conditional (above). Rejected: dropping the exit
renewal and keeping the entry one (the committed deadline would then be issued
before the body, the staleness ADR-047's amendment removed).

**Must not change.** The committed deadline is `sample taken after the body +
window`, exactly as today. The seat lock is taken by the first statement and
held to commit. Intentional release still cancels the renewal. The guard still
refuses a consumed reserve.

**Risk.** Medium–High. It changes the leader context's protocol and the gate's
"split issuance" contract (`_first_fence_contract_violations`,
`_leader_deadline_sources_are_proven`).

**Ruling needed.** Yes — ADR-047, current decision: "An identity-preserving
UPDATE establishes the lock before the fresh deadline renewal" and "A
successful leader-fenced body renews its still-owned seat at its normal
completion boundary". Question: *inside `fenced_leader_transaction`, is the
renewal at the completion boundary sufficient on its own, so that the entry
step is the identity CAS alone? And separately: may the commit guard reuse the
exit renewal's sample when nothing ran between them?*

**Verification.** `tests/unit/core/landscape/test_coordination_deadline_finalization.py`,
`test_lease_deadline_guard.py`, `test_run_coordination_repository.py`,
`test_run_coordination_liveness.py`, `test_database_clock_authority.py`;
testcontainer `test_coordination_deadline_finalization_postgres.py`,
`test_lease_deadline_guard_postgres.py`, `test_heartbeat_takeover_postgres.py`,
`test_run_coordination_release_postgres.py`. New tests: committed expiry is
within the window of a sample taken after a deliberately slow body (negative
control: issue at entry only and the assertion fails by the body's duration);
a body that raises leaves the seat row untouched; `release_seat` leaves the
seat vacated.

**Expected saving.** 2 statements per leader-fenced transaction: 2 per gate
row, 4 per transform row, 7 per batch row, 14 per fork row. About 0.2 ms per
transaction while built inline.

---

#### R-12 — Decide the lock form of the fences

**Finding.** Prior review R-12, "inferred from the SQL, not measured". Recon
Appendix B § 0 describes the member fence as one no-op `UPDATE`.

**Mechanism (verified at 1a7ff0e6).** `verify_membership_fence` writes
`status='active'` where it is already `'active'`
(`run_coordination_repository.py:541-549`); the leader CAS writes
`leader_epoch = leader_epoch` (`:449-457`); `fenced_item_transaction` writes
`status='LEASED'` where it is `'LEASED'` (`item_fencing.py:43-56`). SQLite does
not elide a no-op update: the page is rewritten into the WAL. The JSONL journal
records every one of them, because it keeps any statement that starts with
`UPDATE` (`journal.py:774-776`).

**Evidence.** `bench_r12.py`, file-backed WAL, `synchronous=NORMAL`, one
transaction = `BEGIN IMMEDIATE`, fence, one payload insert, `COMMIT`, 4,000
transactions, run twice:

| Fence | WAL per transaction | CPU per transaction (raw `sqlite3`) |
|---|---:|---:|
| none | 4,564 bytes (1.11 frames) | 13.6 µs |
| `SELECT` | 4,564 bytes (1.11 frames) | 10.4, 11.2 µs |
| no-op `UPDATE` | 8,684 bytes (2.11 frames) | 21.1, 15.0 µs |

One extra 4 KiB frame per member-fenced transaction: 4 per gate row, 7 per
transform row, 20 per fork row. PostgreSQL (dead tuples, WAL records, possible
non-HOT updates if `status` is indexed) was not measured: no Docker on this
box.

**Change.** Options for the ruling, not a recommendation to implement now:

- *A.* Leave as is.
- *B.* Membership and item fences become locking reads: `SELECT … FOR UPDATE`
  with the same predicate and a cardinality-one check. On PostgreSQL this takes
  the row lock and re-evaluates the predicate after a lock wait, as the
  `UPDATE` does. On SQLite `FOR UPDATE` compiles to nothing and `BEGIN
  IMMEDIATE` already holds the write lock, so the fence becomes a plain
  existence read.
- *C.* B, and the leader identity CAS as well. This contradicts ADR-047's text
  directly.

Under B the fences disappear from the JSONL journal; whether the journal should
show them is part of the decision.

**Must not change.** The lock is held to commit; rowcount-0 becomes
"no row returned" with the same exception and the same unregistered-versus-
evicted diagnosis; the fence stays the first statement.

**Risk.** High. It changes the locking primitive of every write transaction on
both dialects.

**Ruling needed.** Yes — ADR-030 D7 ("Membership fences port as
verify-UPDATEs"; "an UPDATE row-lock survives Postgres READ COMMITTED where a
bare EXISTS subquery's snapshot does not") and, for option C, ADR-047 ("a
direct full-token conditional UPDATE and immediate cardinality-one refusal").
Question: *does a `SELECT … FOR UPDATE` with the full fence predicate and a
cardinality-one check satisfy D7 for the membership and item fences; on SQLite,
is a plain read under `BEGIN IMMEDIATE` sufficient; and should fences remain
visible in the JSONL journal?*

**Verification.** Owed before any ruling: M-1's PostgreSQL measurement of
`n_dead_tup` and WAL bytes per row on `run_workers` and `run_coordination`.
If ruled in: `tests/unit/core/landscape/test_scheduler_fencing.py`,
`test_heartbeat_transaction_fencing.py`,
`test_scheduler_membership_eviction_serialization.py`, the mutation-fencing
gate, and the whole testcontainer suite (the contention proofs are what this
changes: `test_enqueue_membership_fence_postgres.py`,
`test_item_generation_fence_postgres.py`,
`test_scheduler_lease_eviction_postgres.py`,
`test_finalizer_membership_postgres.py`).

**Expected saving.** SQLite: 4 KiB less WAL per member-fenced transaction and a
few microseconds of engine time; no measurable CPU change once the statement is
prebuilt. PostgreSQL: unknown until measured.

---

#### E-3a — Return after-images with `RETURNING` instead of selecting them back

**Finding.** Prior review finding 3 / E-3a; recon Appendix B § 8 (in-transaction
reads).

**Mechanism (verified at 1a7ff0e6).** Each site re-read at this commit:

| Site | Write, then read-back | Per gate row |
|---|---|---:|
| `record_completed_node_state_on` | insert `execution/node_states.py:278-294`, select `:299` | 1 |
| `complete_node_state_on` | update `:630-643`, select `:660` | 1 per token × node |
| `claim_ready_row` | update `scheduler/leases.py:249-257`, select `:326-331` | 1 |
| `_transition_on` | update `scheduler/dispositions.py:896-957`, select `:989`, bundle predicate `:991` | 2 |
| `_insert_or_load_routing_decision` | insert `:1510`, select `:1519`, group-ownership select `:1520` | 2 |

No production caller uses the value `complete_node_state` returns: every call
site under `src/elspeth` is a bare expression statement (grep for
`= .*complete_node_state` and `return .*complete_node_state` finds only the two
delegating `return`s inside the repository). The
read-back therefore only feeds the loader's integrity check.

`RETURNING` is already used on both dialects in this package, including whole
rows (`node_states_table.insert().returning(node_states_table)`,
`node_states.py:467`) and `UPDATE … RETURNING`
(`scheduler/dispositions.py:400`, `scheduler/leases.py:757`). SQLite here is
3.53.1. Neither `node_states`, `token_work_items` nor `routing_events` has a
trigger (the only triggers in `schema.py` are on the audit-export tables,
`:2105-2276`).

**Evidence.** Trace: the seven statements above appear once each per gate row
(`node_states.py:299`, `:660`, `:1519`, `:1443` second call, `leases.py:326`,
`dispositions.py:989`, `:991`).

**Change.**

- `record_completed_node_state_on`: `insert().returning(node_states_table)`,
  take `.all()`, require exactly one row, pass it to the loader.
- `complete_node_state_on`: add `.returning(node_states_table)` to the update.
  Zero rows returned takes the existing "not found or already terminal"
  diagnosis select (`:646-659`) unchanged; one row goes to the loader.
- `claim_ready_row`: `.returning(token_work_items_table)` on the claim update.
  Zero rows takes the existing membership re-probe; more than one is the
  existing integrity error; the returned row replaces the select at `:326`,
  with the claimant check done on its `status` and `lease_owner`.
- `_transition_on`: `.returning(token_work_items_table,
  pending_sink_bundle_clause().label("bundle_complete"))` on each of the four
  update shapes. The before-image select (`:886`) stays: neither dialect can
  return old values, and the event's `from_*` fields must come from the
  database.
- `_insert_or_load_routing_decision`: on SQLite
  `insert().returning(routing_events_table)`; if the number of returned rows
  equals the number of events, feed them to `_load_matching_routing_decision`
  and skip both post-insert selects; otherwise (PostgreSQL conflict path) keep
  today's re-select.

Cardinality is checked on the returned rows (`.all()` then `len`), the pattern
at `dispositions.py:392-405`, not on `rowcount`.

Integrity checks that remain: every loader validation; the "terminal after
completion" check (`node_states.py:673`); the already-terminal and foreign-run
diagnoses; the bundle-complete refusal; the before-image presence and
`attempt` type checks; the pre-insert group-ownership check with
`expected_count=0` (`:1504`); content equality of durable and expected routing
events. The one check that goes is the post-insert group-ownership re-read
(`:1520`), whose result is implied by the pre-insert check under the routing
authority lock plus the returned rows all carrying this `state_id` — which the
content comparison verifies.

Rejected: dropping the loader pass because callers ignore the result (it is
the only place a just-written row is proven readable). Rejected: returning old
values through a CTE (not portable to SQLite).

**Must not change.** Recorded values (none change); the exception type on each
failure path; the journal's record of the write (its statement text gains
`RETURNING …`, which matters only if restore tooling replays text — it stores
statement and parameters, `journal.py:254-258`).

**Risk.** Low–Medium. Seven statements, four files, an existing house pattern;
the care is in the zero-row arms.

**Ruling needed.** No. The dropped post-insert ownership re-read is the one
place a maintainer may prefer to keep a statement.

**Verification.** `tests/unit/core/landscape/test_node_state_recording.py`,
`test_scheduler_pending_sink_claim.py`, `test_scheduler_ingest.py`,
`test_routing_group_authority.py`, `test_scheduler_lease_recovery_races.py`,
`test_state_engine_forbidden_paths.py`. New tests with negative controls:
completing an already-terminal state still raises the "already terminal"
`LandscapeRecordError` (control: remove the status predicate and it does not);
a row the loader rejects still raises `LandscapePostCommitError`; a transition
to `PENDING_SINK` with an incomplete bundle still raises and rolls back.
Testcontainer suite owed (`test_routing_reason_postgres.py`,
`test_scheduler_lease_eviction_postgres.py`,
`test_item_generation_fence_postgres.py`). Measurement: `sqlsite.py` shows 7
fewer statements per gate row, and L1-2's budget pin moves by exactly that.

**Expected saving.** 7 statements per gate row, 1 per token × node on transform
pipelines: about 0.6–0.9 ms/row while built inline (≈ 3%). Seven round trips
per row on PostgreSQL.

---

#### E-3b — Skip re-validation and repeated work-item selects when the transaction just wrote the rows

**Finding.** Prior review finding 3 / E-3b; recon Appendix B § 8 ("reference
validation at ingest … re-reading rows the same transaction inserted").

**Mechanism (verified at 1a7ff0e6).** In the ingest transaction
`enqueue_ready_claimed_on` calls `validate_work_item_references`
(`scheduler/queue.py:313`), which selects the token (`work_items.py:195-197`),
the row (`:208-210`) and the node (`:175-177`). The token and row were inserted
a few statements earlier by `insert_row_with_token_on` from the same
`SourceIngestSpec`, and `queue.py:430-433` already checks the returned
identities against it. The run-ownership half of the validation is also
enforced by the database: `token_work_items` has composite foreign keys
`(token_id, run_id)`, `(row_id, run_id)`, `(node_id, run_id)` and
`(coalesce_node_id, run_id)` (`schema.py:976-979`).

The work item is then inserted (`work_items.py:288`), selected for replay
validation (`:319`), selected again (`queue.py:339`), selected `FOR UPDATE`
(`leases.py:214-219`), updated, and selected a fourth time (`leases.py:326`).

**Evidence.** Trace lines 9037–9048: 3 reference selects and 4 work-item
selects per ingest.

**Change.**

- `enqueue_ready_claimed_on` gains a keyword-only
  `references_written_by_this_transaction: bool` (default `False`). The ingest
  verbs pass `True` and the three selects are skipped; standalone callers are
  unchanged. For those, fold the three selects into one join
  (`tokens` ⋈ `rows`, left join `nodes`) so they cost one statement.
- `insert_work_items_idempotent` returns the inserted rows (L1-1's
  `RETURNING token_work_items_table`), and reads back only ids that were not
  inserted. That removes `work_items.py:319` and `queue.py:339` on the
  fresh-insert path.
- `claim_ready_row` gains `row_inserted_by_this_transaction: bool`; when true
  it skips the `FOR UPDATE` select at `:214`, since a row this transaction
  inserted is invisible to every other transaction until commit. With E-3a's
  `RETURNING` on the claim update, `:326` goes too.

Fresh-insert ingest then issues no work-item select at all. A replayed enqueue
(the row already exists) keeps every read it has today.

Rejected: inserting the work item directly as `LEASED`. It removes one more
statement but collapses a READY → LEASED compare-and-swap into the insert and
moves where the claim's clock sample is taken; that is an ADR-047 question of
its own.

**Must not change.** The `ENQUEUE` and `CLAIM_READY` events and their field
values; the claim's fresh decision-time sample and item deadline registration;
replay validation for an existing row; reference validation for every caller
that did not write the references itself.

**Risk.** Low–Medium. Two new boolean parameters on internal verbs that the
mutation-fencing gate inventories; the flags are a trust statement between two
functions in one package and should be keyword-only and defaulted to the safe
value.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_scheduler_ingest.py`,
`test_scheduler_queue_contract.py`, `test_token_ownership_run_scope.py`,
`test_scheduler_events.py`; testcontainer
`test_run_scope_foreign_keys_postgres.py`,
`test_enqueue_membership_fence_postgres.py`. New tests: ingest issues zero
`SELECT … FROM token_work_items`; negative controls — a standalone enqueue with
a token from another run still raises `AuditIntegrityError`, and with the flag
forced true the foreign key raises instead (proving the database backstop); a
replayed enqueue with a different payload still raises `LandscapeRecordError`.
Measurement: 6 fewer statements per source row (7 with E-3a's claim change).

**Expected saving.** 6 statements per source row plus E-3a's one: ingest falls
from 29 to 22 (16 with R-2a). About 0.5–0.7 ms/row while built inline (≈ 2.5%).

---

#### R-7 — Fold the transactions of one transition

**Finding.** Prior review finding 7 / R-7; recon § 1 ("Each node costs two
commits") and Appendix B § 2 (2T + 3 write commits per token).

**Mechanism (verified at 1a7ff0e6).** `NodeStateGuard.__enter__` calls
`begin_node_state` (`engine/executors/state_guard.py:219`), which opens its own
member-fenced transaction (`execution/node_states.py:169`); `guard.complete`
calls `complete_node_state` (`state_guard.py:430`), which opens another
(`node_states.py:554`). The config gate, between them, calls
`record_routing_event` (`engine/executors/gate.py:495`), which runs a read
transaction (`node_states.py:1047` → `:1243`) and then a write transaction
(`:1015`). The drain then calls `mark_pending_sink`
(`engine/scheduler_drain.py:786` → `scheduler/dispositions.py:717`). A gate
decision is four write transactions and one read transaction after ingest; a
transform is two (three with the contract write).

The tree already has the folded form for one case: the source-quarantine ingest
records the row, token, FAILED state, DIVERT routing event and PENDING_SINK
handoff in one transaction, using `prepare_routing_event_for_new_state` and
`record_routing_event_on` (`scheduler/queue.py:525-571`,
`node_states.py:1059`, `:1117`).

**Evidence.** Trace: 5 `BEGIN IMMEDIATE` and 1 `BEGIN` per gate row in the main
loop; 9 `BEGIN IMMEDIATE` per transform row. Each member-fenced transaction
costs `BEGIN IMMEDIATE`, a fence `UPDATE`, a commit and (R-12) one extra WAL
frame.

**Change.** Options, smallest first. They compose.

- *A. Atomic gate decision.* Evaluate the gate in memory, store the reason
  blob, then one member-fenced transaction inserts the node state already
  terminal (COMPLETED, or FAILED on a handled evaluation error) together with
  its routing events. `started_at` is still sampled before evaluation and
  `completed_at` after. Gate: 3 write + 1 read transaction → 1 write.
- *B. Complete and dispose together.* For the last node of a claim, run
  `complete_node_state_on` and `_transition_on` on one connection (both `_on`
  verbs exist). It needs the executor to hand the completion to the drain
  instead of committing it inside the guard.
- *C. No durable OPEN for effect-free nodes.* A node whose plugin declares
  `DETERMINISTIC` or `SEEDED` (`contracts/enums.py:114-126`) inserts its state
  terminal after it has run. Nodes declaring `IO_READ`, `IO_WRITE`,
  `EXTERNAL_CALL` or `NON_DETERMINISTIC` keep the durable OPEN before
  execution.
- *D. One transaction per claim.* Buffer a claim's node-state and routing
  writes in memory and write them with the disposition, flushing early before
  any node that is not effect-free. This is B and C generalised.

What a crash leaves behind for one token (gate pipeline; the transform case is
the same without the routing rows):

| Crash point | Today | A | B | C / D |
|---|---|---|---|---|
| after ingest commit | row, token, source state, work item LEASED, 2 events | same | same | same |
| after `begin_node_state` | + OPEN state for the attempt | no gate rows yet | as today | no rows for an effect-free node |
| after the reason blob is stored | + an unreferenced blob | + an unreferenced blob | as today | as A |
| after `record_routing_event` | + routing event(s) attached to an OPEN state | not reachable | as today | not reachable |
| after `complete_node_state` | + COMPLETED state; item still LEASED | COMPLETED state and routing events; item LEASED | not reachable for the last node | not reachable under D |
| after disposition | PENDING_SINK | same | same | same |

In every pre-disposition case recovery is the same today and under each option:
the lease expires, the item is rotated, and the token is re-driven from the
work item's cursor under an attempt above the recorded maximum
(`engine/scheduler_drain.py:900-938`). The options differ in what the dead
attempt leaves in the audit trail. Today it can leave an OPEN state that is
never closed and a routing event whose state never completed; A and C leave
nothing for an effect-free node that did not finish; B and D close the window
in which a node is COMPLETED but its token is re-executed from the start of
the claim. For an `EXTERNAL_CALL` transform that last window is a duplicated
external call, so B narrows at-least-once exposure rather than widening it.

Whether recovery closes an abandoned OPEN state I did not verify.

**Must not change.** A node that can have an external effect has a durable
OPEN state before it runs. Final rows for a completed token are identical
(same hashes, same routing events, same scheduler events) except that a folded
state is written by one `INSERT` instead of `INSERT` + `UPDATE`. Each folded
transaction is fenced once, first. Attempt numbering stays above the recorded
maximum.

**Risk.** A: Medium (one executor, an existing pattern). C: Medium–High (it
makes a plugin declaration load-bearing for crash evidence). B and D: High
(they move a commit boundary across the executor/drain seam).

**Ruling needed.** Yes — ADR-030 D4 (which verbs are fenced, each in its own
transaction) and ADR-026 (durable token scheduler; I did not read it in full,
so the exact precondition is UNVERIFIED). Questions: *(1) Is an OPEN node state
for a pure in-memory config gate audit evidence the product must keep, or may
the gate's state and routing events be written atomically in terminal form?
(2) May a node whose plugin declares `DETERMINISTIC` or `SEEDED` skip the
durable OPEN state? (3) May the last node's completion and the token's
disposition commit together?*

**Verification.** `tests/unit/core/landscape/test_node_state_recording.py`,
`test_routing_group_authority.py`, `test_state_engine_forbidden_paths.py`,
`tests/unit/engine/test_gate_executor.py`, `test_node_state_guard.py`,
`tests/e2e/recovery/` (crash and resume), the mutation-fencing gate (new verbs
join a frozen inventory). `tests/unit/engine/test_gate_claim_recovery.py` pins
today's image for the "after `complete_node_state`" row directly: a gate state COMPLETED at
attempt 0 survives a lost lease and the token is re-driven at attempt 1. It
holds unchanged under A and C and would be rewritten under B and D.
Testcontainer suite owed in full
(`test_quarantine_resume_crash_matrix_postgres.py`,
`test_follower_retry_reclaim_postgres.py` in particular). New tests, per
option: a crash-matrix test that kills between each pair of statements and
asserts the table above (negative control: with folding disabled the OPEN row
is present); a test that an `EXTERNAL_CALL` node still has a committed OPEN
state before its plugin runs.

**Expected saving.** A: 3 commits and about 12 statements per gate decision
(10.0 → 7.0 commits per gate row). C: 1 commit and 2 statements per token ×
effect-free node. B: 1 commit and 2 statements per token. D on the transform
workload: 9 write transactions per row → 2. CPU unknown until measured; each
transaction removed is `BEGIN IMMEDIATE` + fence + commit, roughly 0.3–0.4 ms
today.

---

#### E-10 — Give engine-internal reads a plain read transaction

**Finding.** Prior review E-10; recon Appendix B § 2 ("A single-row lookup is
therefore BEGIN + PRAGMA + SELECT + PRAGMA + COMMIT on SQLite").

**Mechanism (verified at 1a7ff0e6).** `ReadOnlyDatabaseOps.execute_fetchone`,
`execute_fetchall` and `execute_fetchall_many` open
`self._db.read_only_connection()` per call (`_database_ops.py:39`, `:53`,
`:62`). `read_only_connection` opens a transaction, executes
`text("PRAGMA query_only = ON")`, yields, and executes
`text("PRAGMA query_only = OFF")` (`database.py:2243-2254`); both `text()`
objects are built per call. On PostgreSQL it executes
`SET TRANSACTION READ ONLY` (`:2247`). Its docstring names its purpose:
"Defense-in-depth for untrusted SQL execution (e.g., MCP query tool)".
`LandscapeDB.connection()` is an alias for it (`:2203`). Read-only handles
(`from_url(read_only=True)`) arm `query_only` once at connect
(`database.py:1141`) and never disarm it.

**Evidence.** Pragmas per source row: 10.0 gate, 8.0 transform, 7.4 batch,
52.0 fork. Read transactions per row: 5.0, 4.0, 5.9, 34.0. In this lane's part
of the gate row that is the routing pre-read (`node_states.py:1243`); the rest
are the sink-flush lineage reads the prior review's E-4 batches.

**Change.** Add `LandscapeDB.internal_read_connection()`: a plain deferred
transaction with no pragma and no `SET TRANSACTION`. `ReadOnlyDatabaseOps`
uses it when the handle is writable and the statement is a SQLAlchemy `Select`
or `CompoundSelect` (an `isinstance` check on a type the engine builds, ADR-032),
and raises otherwise. `read_only_connection()` and `connection()` keep the
wrapper for the MCP query tool, the web read surfaces and any caller passing
text. On a read-only handle nothing changes: `query_only` is armed at connect.

Separately and trivially, hoist the two `text()` pragmas to module constants.

Rejected: autocommit reads with no `BEGIN`/`COMMIT` at all (a larger change to
the begin listener, and `execute_fetchall_many` needs one snapshot). Rejected:
removing the wrapper from `connection()` (pinned by
`tests/unit/core/landscape/test_write_intent_begin.py`: "`connection()`
additionally rejects DML").

**Must not change.** Untrusted or text SQL never runs without the write
barrier. Engine reads stay inside one transaction per call (snapshot
consistency for `execute_fetchall_many`). A deferred `BEGIN` on SQLite takes no
write lock (ADR-030 D5).

**Risk.** Low–Medium. The barrier being removed is defence in depth for
statements the engine itself compiles; the replacement is a type check at the
one chokepoint.

**Ruling needed.** No. Flag for the maintainer: this narrows a defence-in-depth
measure for engine-built `Select`s on writable handles.

**Verification.** `tests/unit/core/landscape/test_database_ops.py`,
`test_write_intent_begin.py`, `test_database_pragma_probe.py`,
`test_database_engine_lifecycle.py`, `test_ci_coverage_database.py`. New tests: an engine read issues exactly
`BEGIN`, the select, `COMMIT`; negative controls — passing an `Update` or a
`text()` to `execute_fetchone` raises before any SQL is sent, and
`read_only_connection()` still rejects an `INSERT` with the database's own
error. Testcontainer suite owed (`SET TRANSACTION READ ONLY` path). Measurement:
pragma count per row falls to zero.

**Expected saving.** 2 statements per engine read on SQLite, 1 round trip of 3
per read on PostgreSQL: 10 statements per gate row, 52 per fork row. On the
gate row the E-4 lane removes most of those reads outright, leaving 2 here.
About 40–50 µs per pragma today (statement build plus execute).

---

#### E-5 — Record a transform's output contract once per distinct contract

**Finding.** Prior review finding 5 / E-5; recon Appendix B § 1 ("3 commits when
the contract object changes").

**Mechanism (verified at 1a7ff0e6).** `TransformExecutor` calls
`update_node_output_contract` whenever `output_contract is not
token.row_data.contract` (`engine/executors/transform.py:800-807`), an identity
test. The repository computes `contract.version_hash()`, opens a member-fenced
transaction, selects the node `FOR UPDATE`, parses the stored JSON into a
`SchemaContract`, hashes it, compares the stored hash, and returns when the
candidate hash equals the current one (`data_flow/graph.py:433-458`).

**Evidence.** Trace: both transforms of the `transform_pipeline` workload
(`type_coerce`, `value_transform`) take this path on every row — 2.00
`SELECT nodes.output_contract_json …` per row, 2 of the 9 write transactions.

**Change.** A per-process memo in `DataFlowGraphRepository`, keyed
`(run_id, node_id, candidate_hash)`, added after a call returns normally
(either the equal-hash return or a successful compare-and-swap). A later call
with a memoised key returns before opening a transaction. Putting it in the
repository rather than the executor covers the other caller
(`plugin_audit_writer.py:235`) and keeps the rule next to the fold it relies
on. The executor's identity test stays as the cheap first filter. The memo is
bounded by distinct contracts per node per run.

The memo is sound because the record is a join: once hash H has been folded
into node N, folding it again changes nothing, whatever other contracts were
folded in between (`merge_for_node_evolution`, `contracts/schema_contract.py:571-607`:
union of fields, `nullable` is OR, a type conflict raises). I read the merge's
contract, not its implementation; the property test below is the proof.

It still costs one `version_hash()` per row until the prior review's E-6a/E-6b
land (another lane).

Rejected: caching the parsed stored contract and keeping the transaction
(saves the parse, keeps the commit). Rejected: a memo keyed on contract object
identity (transforms build a new equal object per row).

**Must not change.** `nodes.output_contract_json` and `output_contract_hash`
after a run are byte-identical. A genuinely new contract still takes the full
locked path. A type conflict still raises `FrameworkBugError`. One thing does
change and should be accepted knowingly: the stored-hash-versus-recomputed-hash
check on the `nodes` row (`graph.py:452-456`) runs once per distinct contract
per process instead of once per row.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_graph_recording.py`,
`test_data_flow_fencing.py`, `tests/unit/engine/test_executors.py`,
`tests/unit/engine/test_processor_transform_execution.py`,
`tests/testcontainer/core/test_output_contract_concurrency_postgres.py`
(testcontainer owed: the path takes a row lock). New tests: second fold of the
same contract issues zero statements; negative control — a contract adding a
field still writes, and the stored JSON then contains it. Property test:
`fold(fold(S, C), C) == fold(S, C)` and `fold(fold(fold(S, C), K), C) ==
fold(fold(S, C), K)` on generated contracts; negative control with a
type-conflicting `C` raises. Smoke: `nodes` rows identical before and after on
`transform_pipeline`.

**Expected saving.** One write transaction per transform per row: 3 statements,
1 commit, 1 WAL frame, one JSON parse and two contract hashes. Transform
workload: 90.4 → 84.4 statements and 13.0 → 11.0 commits per row. The prior
review sampled 6.2% of CPU.

---

#### L1-3 — Halve the cost of a scheduler event's identity

**Finding.** Recon Appendix B § 9, top-10 item 7: "2 canonical_json + sha256
per event to derive `event_id`".

**Mechanism (verified at 1a7ff0e6).** `_event_values`
(`scheduler/events.py:83-106`) calls `canonical_json` on the event's context
(`:84`) and again on a 15-key identity dict that embeds that string (`:86-104`),
then SHA-256s the result. The first value is stored in `context_json`; the
second exists only to produce `event_id`. `canonical_json` here is
`elspeth.core.canonical.canonical_json` (`:33`): a normalising walk for
numpy/pandas types, a second walk for frozen containers, then `rfc8785`. Every
value in the identity dict is already `str`, `int` or `None`. The context is
`None` on all four hot-path events (enqueue, claim, disposition, terminalise).

**Evidence.** `bench_event.py`: 2 `canonical_json` calls per event (counted);
47.1 µs per `_event_values` call; 4 events per gate row (3 `record`, 1
`record_many`), which is 8 of the recon's 16 `canonical_json` calls per gate
row. A version that uses a constant `"{}"` for an empty context and calls
`rfc8785.dumps` directly on the identity dict takes 24.7 µs and produced the
same `event_id` and `context_json` on three shapes (no lease, lease, nested
context).

**Change.** In `_event_values`: `context_json = _EMPTY_CONTEXT_JSON` when the
context is `None` or empty, else as today; build the identity bytes with the
contracts-layer `canonical_json` (`contracts/hashing.py`, no numpy walk) or
`rfc8785.dumps` directly. Pin the result with a golden test.

**Must not change.** `event_id` and `context_json` bytes for every event. They
are persisted content identities; a change is a persisted-data compatibility
break.

**Risk.** Low, given the golden test.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_scheduler_events.py`. New
golden test: fixed events of every `SchedulerEventType`, with and without
context and lease fields, assert literal `event_id` values recorded from the
current implementation (negative control: change one identity key's name and
every digest moves). No testcontainer run owed (pure Python).

**Expected saving.** ≈ 22 µs per event, ≈ 0.09 ms per gate row (≈ 0.3%). Do it
only alongside other work in the file.

---

#### M-1 — Measure the same workloads on PostgreSQL

**Finding.** Prior review M-1; recon Appendix B § 2 ("PostgreSQL: no pragmas …
Every statement counted in section 1 is a network round trip and every commit
is a server-side WAL flush at default settings (UNVERIFIED)").

**Mechanism (verified at 1a7ff0e6).** The engine is created with no pool or
PostgreSQL arguments on the run path (`database.py:2046`, per the recon; I did
not re-read that call). The statement counts above carry over to PostgreSQL
with these differences, all from code I read: the routing write adds one
`pg_advisory_xact_lock` statement (`execution/node_states.py:1364-1368`);
reads issue `SET TRANSACTION READ ONLY` instead of two pragmas
(`database.py:2247`); `BEGIN` is not a separate cursor statement; the decision
clock is `SELECT clock_timestamp()` (`database_clock.py:35`).

**Evidence.** None on PostgreSQL: this box has no Docker and no server.

**Change.** How each item's weight shifts, to be confirmed by the measurement:

| Item | On PostgreSQL | Why |
|---|---|---|
| E-1a, E-1b, L1-1, L1-4 | Matter less in relative terms | They remove client CPU (≈ 0.1 ms per statement). A round trip costs that or more even on loopback, and far more across a network |
| R-2a, R-2b | Matter more | 6 + 2 round trips per source row on the leader path |
| E-3a, E-3b | Matter more | 13 round trips per gate row |
| R-7 | Matters most | Each commit is a WAL flush at default `synchronous_commit`; SQLite at `synchronous=NORMAL` does not fsync per commit |
| R-12 | Matters more, direction unmeasured | Every fence is a new tuple version on one hot row; 4–20 per source row |
| E-10 | Matters, less than on SQLite | 1 statement per read instead of 2 |
| E-5 | Matters more | A row lock on `nodes` and a commit per transform per row; with followers it also serialises them on that row |
| L1-3 | Unchanged | Pure Python |

Measurement owed: the four workloads at 300 and 1,000 rows against PostgreSQL
16 in the testcontainer, loopback and with 1 ms of injected latency
(`tc netem` or a proxy), recording statements, commits and round trips per row
(the `sqlsite.py` hooks work unchanged), CPU, wall time, server
`pg_stat_statements` totals, WAL bytes per row (`pg_current_wal_lsn()` delta)
and `n_dead_tup` on `run_workers`, `run_coordination` and `token_work_items`.
Repeat with one follower for the fork workload.

**Must not change.** Nothing; measurement only.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** The measurement is the deliverable. Re-run after each landed
item (the prior review's M-2).

**Expected saving.** None directly. It decides the order of R-2, R-7 and R-12.

---

### Tables

#### 1. All items

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| E-1a part 1 | Prebuild the membership fence | Low | No | E-1a | ≈ 1.6% gate, ≈ 2.5% transform |
| E-1a part 2 | Prebuild leader CAS, leader renewal, clock helpers | Medium | **Yes**, via L1-4 (ADR-047) | E-1a | ≈ 5–6% today; a third of that after R-2a/R-2b |
| E-1b part 1 | Prebuild the by-key selects | Low | No | E-1b | ≈ 1.5% after E-3 |
| E-1b part 2 | Prebuild every write that binds a timestamp | Medium | **Yes**, via L1-4 (ADR-047) | E-1b | Unknown until measured; estimate 8–12% |
| L1-4 | Teach the clock-authority gate to resolve a prebuilt statement and its bind mapping | Medium–High | **Yes** (ADR-047) | new | Unlocks E-1a part 2 and E-1b part 2 |
| L1-1 | Stop recompiling the two dialect-specific inserts | Low / Low–Medium | No | new | ≈ 0.5–1% |
| L1-2 | Guard tests: statement identity, budget, bind equivalence | Low | No | new | Holds E-1 and E-3 |
| R-2a | Stop re-taking the leader fence inside a fenced transaction | Medium | **Yes** (ADR-047, ADR-030 D4) | R-2a | 6 statements per source row (prototyped: 30 → 24 at ingest) |
| R-2b | Issue the leader deadline once, at completion | Medium–High | **Yes** (ADR-047) | R-2b (different design) | 2 statements per leader transaction |
| R-12 | Decide the lock form of the fences | High | **Yes** (ADR-030 D7, ADR-047) | R-12 | 1 WAL frame per member transaction on SQLite; PostgreSQL unknown |
| E-3a | `RETURNING` instead of read-back | Low–Medium | No | E-3a | 7 statements per gate row, ≈ 3% |
| E-3b | Skip re-validation and repeated selects at ingest | Low–Medium | No | E-3b | 6 statements per source row, ≈ 2.5% |
| R-7 | Fold the transactions of one transition (options A–D) | Medium to High | **Yes** (ADR-030 D4, ADR-026) | R-7 | A: 3 commits per gate decision; D: 9 → 2 write transactions per transform row |
| E-10 | Plain read transaction for engine reads | Low–Medium | No | E-10 | 10 statements per gate row, 52 per fork row |
| E-5 | Record the output contract once per distinct contract | Low | No | E-5 | 1 transaction per transform per row; prior 6.2% |
| L1-3 | Halve the cost of a scheduler event's identity | Low | No | new | ≈ 0.3% |
| M-1 | Measure on PostgreSQL | Low | No | M-1 | Decides ordering |

#### 2. Recommended order

1. **L1-2** (identity and budget tests, with the exemption set full) — first, so
   every later item moves a pinned number.
2. **E-5**, **E-1a part 1**, **L1-1 (routing arm)** — independent, no ruling,
   no dependency on each other.
3. **E-3a**, then **E-3b** with **L1-1 (work-item arm)** — E-3b needs the
   inserted rows that L1-1's change returns, and the claim change in E-3b
   needs E-3a's `RETURNING`.
4. **E-10** — independent; coordinate with the lane that owns the sink-flush
   lineage reads so the same reads are not changed twice.
5. **E-1b part 1** — after step 3, so removed statements are not converted
   first.
6. **M-1** and a re-measure (prior M-2).
7. Rulings, raised together with the M-1 numbers: **R-2a** first (smallest,
   already prototyped, and it shrinks E-1a part 2); then **L1-4**, because it
   gates the largest remaining CPU saving; then **E-1b part 2** and **E-1a
   part 2** behind it; then **R-2b**; then **R-7** option A; then R-7 B–D and
   **R-12**.
8. **L1-3** whenever `scheduler/events.py` is next open.

The order changes if the maintainer rules L1-4 in early: E-1b part 2 is then
the largest single item and should follow E-3 directly.

Every step from 2 onward that touches SQL owes `pytest tests/ -m testcontainer
-n 0`, and a full run of both source gates
(`tests/unit/core/landscape/test_database_clock_authority.py`,
`tests/unit/architecture/test_web_landscape_mutation_fencing.py`) with their
inventories re-pinned deliberately where a function's identity moves.

#### 3. Examined and recommended not to fix

- **`node_states.started_at` / `completed_at` on the process clock (prior
  R-13).** Not a defect. ADR-047, "What this ADR does not do": "It does not
  touch forensic timestamps on `runs`, `nodes`, `tokens`, `node_states`, … —
  those remain process-clock `created_at`/`recorded_at` facts."
- **The commit-guard clock read** (`lease_deadlines.py:119`), on its own. It is
  the one sample ADR-047 places after journal precommit work. Only the
  optional third step of R-2b touches it, under a ruling.
- **The before-image select in `_transition_on`** (`dispositions.py:886`). The
  event's `from_*` fields must come from the database and neither dialect can
  return old values from an `UPDATE`.
- **The routing pre-write lookups** (`_lock_routing_state`,
  `_routing_event_run_id`, the existing-decision read, the pre-insert group
  check). They are the idempotent-retry and cross-run checks; R-7 option A
  makes two of them unnecessary by construction, which is the right way to
  remove them.
- **Inserting the work item directly as LEASED at ingest.** See E-3b.
- **`BEGIN IMMEDIATE`, WAL, `synchronous=NORMAL`.** Agree with the prior
  review; documented ADR-030 D5 choices.
- **`pg_advisory_xact_lock` on the routing group.** PostgreSQL only, one
  statement per decision, and it is what serialises a routing group.

#### 4. Not verified

- PostgreSQL: nothing was run. All PostgreSQL statements above are from
  reading; SQL text differences were checked by compiling with the dialect.
- Whether a SQLAlchemy release newer than 2.0.45 caches dialect-specific
  inserts.
- Whether recovery ever closes an abandoned OPEN node state (R-7's table
  assumes it does not).
- ADR-026 was not read in full; R-7 names it without a section.
- `merge_for_node_evolution`'s implementation (`merge_union_contracts`) was not
  read; E-5 relies on its documented semantics plus a property test to add.
- The bodies of the `run_coordination_repository.py` verbs that read
  `leader_heartbeat_expires_at` were not all read end to end (R-2b's
  precondition).
- `tests/unit/architecture/test_web_landscape_mutation_fencing.py` was probed,
  not read; how it identifies a DML site is unknown to me. Its seven production
  tests passed on every variant, which may mean the moved statements are
  outside what it inventories rather than accepted by it.
- Both source gates were probed through their zero-argument production tests
  only (11 and 7), on private copies, not run in full. The second probe batch
  was stopped by the background time limit after V6a; V6b was run separately.
- The gate-free rule ("identifiers only in the mapping") is inferred from six
  variants, not from reading the scanner. A by-key select inside a function the
  gate treats differently could still be flagged.
- The trust-tier allowlist: any function this plan edits that carries a signed
  suppression will show signature drift for the operator to re-sign. I did not
  run the keyless scan to count them.
- CPU percentages are counts × micro-benchmark deltas taken on a loaded shared
  box, against the recon's unprofiled 26.6 and 29.8 ms per row. Only the prior
  review's five-site prototype is a measured end-to-end saving, and that was at
  a different commit and SQLAlchemy version.
- Fork and batch workloads were traced for counts only; no item was designed
  against their specific verbs (`fork_token`, `coalesce_tokens`,
  `complete_barrier`, `resolve_token_ownership` at 9 statements and 6 read
  transactions per fork row).

## Appendix L2 — Canonical JSON, hashing and row-object churn on the per-row path

Lane report for the executor remediation plan. Tree: HEAD `bf8adf06` (docs-only
on `1a7ff0e6`); every `path:line` below was opened at that commit. Nothing in
the tracked tree was changed. Scripts, raw outputs and a private prototype are
under `.claude/lanes/exec-remediation/L2/` (gitignored).

### 0. What this lane found, in one page

1. **`SchemaContract.version_hash()` is the largest hashing cost on the row
   path, and it grows with the number of columns.** It costs about 11 µs per
   field per call (112 µs at 10 fields, 1,078 µs at 100) and runs 5 times per
   source row on a gate pipeline and 11 times on a two-transform pipeline. It
   is more than half of all canonical-JSON time in both profiled workloads.
   On a 100-column, two-transform run the hashing items in this lane cut
   whole-run CPU by 17% (21.9 s to 18.2 s, 310 rows).
2. **On narrow rows the same changes are small.** Gate, 1,000 five-column
   rows: −4.6% CPU (three interleaved pairs, same sign each time). Transform,
   1,000 four-to-seven-column rows: −0.3% over six pairs, which is inside the
   run-to-run noise. The recon's "canonical JSON + hashing 11–16%" is a
   cProfile bucket and overstates the real share for narrow rows; the prior
   review's 3–5% is the right order.
3. **The same row content is canonically serialised about 20 times per source
   row on a gate pipeline**, not the 12 the recon's per-phase counts add up to.
   Seven of those are in the node-state and ingest path this lane owns; the
   rest are at the sink (handed to the sink lane, § 5).
4. **Recorded values do not move.** A prototype of the lane's changes produced
   byte-identical `rows.source_data_hash`, `source_data_ref`,
   `source_contract_json`, all 1,240 `node_states.input_hash`/`output_hash`
   pairs, `nodes.output_contract_json`/`hash`, sink member `payload_hash` and
   sink file digests on three workloads.
5. **Two things need a ruling**: handing the frozen row (rather than a thawed
   dict) to the node-state verbs so one hash can be carried (L2-4), and a
   latent defect found on the way — a `set` inside a `PipelineRow` hashes to a
   different value in every process (L2-6).
6. **A module-level `functools.lru_cache` cannot be used in this code.**
   Runtime-VAL refuses it at run start (`FrameworkBugError: Runtime-VAL cannot
   deterministically normalize global dependency … of type
   functools:_lru_cache_wrapper`). Every memo below is instance state.

Where this report contradicts the recon or the prior review is listed in § 6.

### 1. Baseline: calls per source row, by caller

Method: `cProfile` on 10-row and 310-row runs of the recon's gate and
transform workloads; per-row = (calls at 310 − calls at 10) / 300, with pstats
caller attribution. Commands: `L2/run.sh <workload>_<n> prof`, then
`L2/callers.py gate 10 310` and `L2/callers.py transform 10 310` (outputs
`L2/callers-gate.txt`, `L2/callers-transform.txt`). The µs column is cProfile
cumulative time, which inflates Python-heavy code roughly 2–3×; use it for
ranking only. Class: (a) recorded audit value, (b) integrity cross-check,
(c) control-flow guard, (d) incidental duplicate of a computation already done
on the same content.

Totals: `rfc8785.dumps` **53.05 / row** (gate) and **76.00 / row**
(transform), matching recon Appendix F § 6.

#### Gate workload (csv → config gate → csv sink), per source row

| Caller (`path:line` of the call) | Calls | What is serialised | Class | cProfile µs |
|---|---:|---|---|---:|
| `contracts/schema_contract.py:446` and `:849` via `scheduler/payload_codec.py:37-38` | 4 | contract fields (`version_hash`) | (d) — one distinct value all run | 1,891 |
| `contracts/schema_contract.py:446` via `engine/processor.py:2990` | 1 | contract fields (`version_hash`) | (d) | 473 |
| `core/landscape/scheduler/events.py:84`, `:86` | 8 | event context and identity (4 events) | (a) | 1,189 |
| `data_flow/tokens.py:349` | 1 | source row → `source_data_hash` | (a) | 156 |
| `data_flow/tokens.py:362` | 1 | same row → payload bytes | (a) value, (d) computation | 120 |
| `execution/node_states.py:270`, `:271` | 2 | same row → source state input and output hash | (a) values, (d) computation | 282 |
| `engine/executors/gate.py:288` | 1 | same row → `result.input_hash` | (d) | 152 |
| `execution/node_states.py:154` | 1 | same row → gate state input hash | (a) value, (d) computation | 126 |
| `execution/node_states.py:610` | 1 | same row → gate state output hash | (a) value, (d) computation | 160 |
| `execution/node_states.py:615`/`:618`/`:621` | 1 | reason or context JSON | (a) | 80 |
| `execution/node_states.py:1099`, `:1263`, `:1268`, `:1284` | 4 | routing reason and ids | (a) | 299 |
| `execution/node_states.py:493` | 1 | same row → sink state input hash | (a) value, (d) computation | 91 |
| `execution/node_states.py:703` | 1 | `{"row": …}` → sink state output hash | (a) | 106 |
| `engine/executors/sink.py:810` | 1 | same row → canonical round trip | functional (sink lane) | 89 |
| `execution/sink_effect_identity.py:267`, `:271`, `:272` | 3 | lineage, row payload hash, pending identity | (a) | 247 |
| `engine/executors/sink_effects.py:1360` | 1 | same row → payload bytes | (b) compared at `:1363` | 76 |
| `contracts/sink_effects.py:566`, `:579` (`SinkEffectMember.__post_init__`) | 14 | lineage and row, on each of 7 constructions | (b) verify-on-construct | 536 |
| `sink_effect_identity.py:131`, `sink_effect_reservation.py:99` | 2 | effect identity over all members | (a) | 454 |
| `core/canonical.py:335` (`is_non_canonical_number`) | 3 | one scalar each | (c) | 66 |
| `scheduler/payload_codec.py:71`, `contracts/sink_effects.py:277` | 2 | scrub marker, evidence | (a) | 102 |

Row content alone: 20 canonicalisations per source row (tokens 2, source state
2, gate 3, sink state 1, sink.py 1, identity 1, payload 1, member
re-verification 7, plus the wrapped `{"row": …}`).

#### Transform workload (csv → type_coerce → value_transform → csv sink), per source row

| Caller | Calls | What | Class | cProfile µs |
|---|---:|---|---|---:|
| `data_flow/graph.py:433`, `:451` | 4 | emitted and stored contract (`version_hash`) | prior E-5 | 2,109 |
| `contracts/contract_records.py:300` via `graph.py:450` | 2 | stored contract re-verified | (b), prior E-5 | 1,141 |
| `schema_contract.py:446`/`:849` via `payload_codec.py:37-38` | 4 | contract (`version_hash`) | (d) | 2,088 |
| `schema_contract.py:446` via `processor.py:2990` | 1 | contract (`version_hash`) | (d) | 495 |
| `core/canonical.py:231` (`compute_full_topology_hash`) | 6 | node configs and topology, once per checkpoint | prior E-9a | 1,702 |
| `scheduler/events.py:84`, `:86` | 8 | event context and identity | (a) | 1,270 |
| `engine/executors/transform.py:737` | 2 | output row of each transform | (c) + feeds `result.output_hash` | 355 |
| `execution/node_states.py:610` | 2 | same output row | (a) value, (d) computation | 331 |
| `execution/node_states.py:154` | 2 | input row = previous node's output | (a) value, (d) computation | 261 |
| `execution/node_states.py:270`, `:271` | 2 | source row twice | (a) values, (d) computation | 247 |
| `data_flow/tokens.py:349`, `:362` | 2 | source row hash and payload | (a), second is (d) | 235 |
| `execution/node_states.py:615`/`:618`/`:621` | 2 | success reason JSON | (a) | 369 |
| `core/canonical.py:335` (`is_non_canonical_number`) | 14 | one scalar each | (c) | 273 |
| sink-side callers (as in the gate table) | 24 | | (a)/(b) | ~1,900 |

`version_hash` is 5,647 of the 11,163 cProfile µs under
`contracts.hashing.canonical_json` on this workload (2,280 of 5,694 on gate).

#### Row-object churn, same runs

From `L2/count_contracts.py` (in-process call-site counter; outputs
`L2/count-gate_310.err`, `L2/count-transform_310.err`), 310 rows:

| Measure | Gate | Transform (2 transforms) |
|---|---:|---:|
| `PipelineRow.to_dict()` calls | 930 (3.0 / row) | 5,890 (19.0 / row) |
| `PipelineRow` constructed | 310 (1.0 / row) | 1,860 (6.0 / row) |
| `SchemaContract` constructed | 309 instances, 2 values | 3,415 instances (11.0 / row), 18 values |
| `version_hash()` calls | 1,557 on 1 instance | 3,417 on 1,239 instances, 5 values |

`to_dict()` per token × transform, by call site (620 calls each = one per
token × transform): `transform.py:881`, `transform.py:696`,
`transform.py:781`, `core/canonical.py:176` (inside the `:737` hash),
`output_declaration.py:110`, `declared_output_types.py:132`,
`declared_output_types.py:135` — **seven engine thaws**, plus one or two in
the plugin (`type_coerce.py:419`; `value_transform.py:589`, `:593`).

### 2. Cost of one canonicalisation (micro-benchmark)

`L2/bench.py`, CPU µs per call (`time.process_time`, best of 5 batches, no
profiler; two runs agree within 5%, `L2/bench-1.out`, `L2/bench-2.out`).
Shapes: a 10-field flat row (192 canonical bytes), a 100-field flat row
(2,052 bytes), a nested order-like row (943 bytes).

| Measure | flat 10 | wide 100 | nested |
|---|---:|---:|---:|
| `core.canonical.stable_hash(dict)` today | 25.6 | 218.7 | 127.2 |
| `core.canonical.stable_hash(PipelineRow)` today | 27.8 | 240.6 | 139.5 |
| — `deep_thaw` of the row (`canonical.py:176`) | 2.2 | 20.9 | 11.1 |
| — walk 1 + copy, `_normalize_for_canonical` (`canonical.py:159`) | 6.3 | 60.7 | 31.9 |
| — walk 2 + copy, `_normalize_frozen_and_reject_non_finite` (`hashing.py:38`) | 3.3 | 29.9 | 19.6 |
| — walk 3, `rfc8785.dumps` (pure Python; shallow-copies each container, `_impl.py:215`, `:228`) | 13.6 | 123.4 | 66.6 |
| — `bytes.decode` + `str.encode` + SHA-256 (`hashing.py:87`, `:169`) | 0.6 | 2.4 | 1.3 |
| A: one normalisation walk, then `rfc8785` | 22.6 | 193.5 | 114.9 |
| B: A, and walk the row's frozen data without thawing (`PipelineRow` input) | 22.3 | 189.7 | 116.4 |
| F: no-copy check that the data is already plain JSON, then `rfc8785` | 17.3 | 136.7 | 79.6 |
| H: C `json` encoder on the subset where it provably equals RFC 8785 | 6.6 | 54.3 | 24.0 |
| Hash + payload bytes of one object today (`stable_hash` then `canonical_json`) | 51.9 | 441.8 | 258.9 |
| Same, serialised once and the bytes hashed | 24.8 | 195.3 | 109.4 |
| `PipelineRow(dict, contract)` (deep freeze) | 4.3 | 42.1 | 22.4 |
| `PipelineRow.to_dict()` (deep thaw) | 2.2 | 21.8 | 10.9 |
| `SchemaContract.version_hash()` for a contract of that many fields | 112.5 | 1,077.7 | 59.9 (5 fields) |
| `hash(contract)` / `contract == equal_copy` | 1.3 / 1.7 | 11.4 / 14.9 | 0.7 / 0.8 |

The recon's "three Python-level tree walks and two full structure copies" per
hash (Appendix B § 3) is **verified**, with one addition: `rfc8785` makes a
third, shallow, copy of every container as it goes. `rfc8785` itself is 53–56%
of a hash, walk 1 is 25–28%, walk 2 is 13–15%, and the bytes→str→bytes round
trip is 1–2%.

Byte identity. `L2/prop_test.py` (Hypothesis) compares each candidate with the
current `canonical_json(obj).encode()` and requires the same bytes or the same
exception class. 12,000 mixed values (5,914 serialisable, 6,086 raising),
12,000 `PipelineRow`s and 36,000 plain-JSON values (7,260 through H's fast
arm) all pass for A, B, F and H (`L2/prop-12000.out`). Negative control: using
`json.dumps` as the serialiser fails on the first float (`b'0.0' != b'0'`).
The first two versions of option A failed this test on exception class and on
error precedence; see L2-8.

### 3. Prototype evidence used by several items

A private copy of `src/elspeth` at `L2/proto_tree/` (diff against the checkout:
`L2/prototype.patch`, 9 files) implements E-6a, the E-6b interning, L2-1,
L2-2, L2-3, L2-4, L2-5, part of L2-7 and the merged walk of L2-8. A second
variant also carried prior-review E-5, to separate its effect. Provenance was
checked on every run (`elspeth.__file__` inside `proto_tree`).

Recorded values (`L2/audit_projection.py`: ids and clocks excluded;
`rows`, `node_states`, `nodes`, work items, outcomes, sink members, output file
SHA-256):

| Workload | Projection lines | Baseline vs prototype |
|---|---:|---|
| gate, 310 rows | 2,191 | identical |
| transform, 310 rows | 2,500 | identical |
| wide (100 columns, two transforms), 310 rows | 2,500 | identical |

One column, `sink_effect_members.evidence_hash`, is masked: two baseline runs
of the same input differ in it and in nothing else, which is also the control
that the comparison can go red.

CPU (user + sys, `bash time`, interleaved; `L2/ab-*.txt`, `L2/ab3-*.txt`,
`L2/ab2n-*.txt`). Other lanes were running on the box.

| Workload | Baseline | This lane's items | Plus prior E-5 |
|---|---|---|---|
| gate, 1,000 rows (3 pairs) | 33.04, 30.93, 30.71 | 31.80, 29.50, 29.07 (median −4.6%) | not applicable |
| transform, 1,000 rows (6 pairs base / lane; 2 with E-5) | median 33.4 | median 33.3 (−0.3%, inside noise) | 31.15, 30.23 (−11% against the paired lane-only runs) |
| wide, 310 rows (2 runs each) | 22.02, 21.87 | 18.43, 17.96 (−17%) | 16.60, 17.17 (−23%) |

With the recon's ~7 s fixed startup removed, the wide run goes from about
48 ms to 36 ms per row for this lane's items.

Call counts under the prototype (`L2/callers-pgate.txt`,
`L2/callers-ptransform.txt`): `rfc8785.dumps` 53.05 → 42.05 per row (gate) and
76 → 58 (transform); `rfc8785` cumulative time 4,241 → 2,241 and
8,343 → 3,415 µs per row; `version_hash` computations 5 → 0 and 11 → 0;
`to_dict()` 3 → 2 and 19 → 8.

Tests run against the prototype tree (`-o pythonpath=…` and an exported
`PYTHONPATH`, both needed; the first attempt without `-o` silently imported
the checkout):

- contracts, canonical and property suites: 408 passed, 2 failed
  (`L2/pytest-proto-contracts.log`). Both failures are
  `tests/unit/core/test_canonical.py::…::test_canonical_json_delegates_after_domain_normalization`
  and `::test_stable_hash_delegates_after_domain_normalization`, which pin
  that `core.canonical` delegates to the `contracts.hashing` primitive.
- four engine executor suites: 234 passed, 2 failed
  (`L2/pytest-proto-engine.log`):
  `tests/unit/engine/test_executors.py::TestTransformExecutor::test_extracts_dict_for_landscape_recording`
  and `::test_begin_node_state_called_with_correct_args`, which pin that the
  Landscape receives a plain dict.

Both pairs of failures are designed-in consequences, discussed under L2-8 and
L2-4. No full suite and no testcontainer suite was run.

### 4. Remediation items

#### E-6a — Memoise `SchemaContract.version_hash()` on the frozen instance

**Finding.** Prior review E-6a and finding 6; recon Appendix A top-10 item 4
and Appendix B top-10 item 9. New here: the cost is linear in field count and
this is the dominant hashing cost on the row path.

**Mechanism (verified at 1a7ff0e6).** `version_hash()`
(`contracts/schema_contract.py:394-433`) sorts the fields, builds a six-key
dict per field, runs `canonical_json` and SHA-256 on every call; nothing is
cached. `SchemaContract` is `@dataclass(frozen=True, slots=True)` (`:149`)
over a tuple of frozen `FieldContract` (`:68`). Per source row it is reached
from `to_checkpoint_format` at `:446` (via `engine/processor.py:2990` and
`scheduler/payload_codec.py:38`) and from `PipelineRow.to_checkpoint_format`
at `:849` (via `payload_codec.py:37`); `serialize_row_payload` runs at ingest
(`engine/scheduler_work_codec.py:106`) and at `mark_pending_sink`
(`engine/scheduler_drain.py:763`). That is 5 calls per row, as the recon says.
`update_node_output_contract` adds **3** per call, not the recon's 2
(`data_flow/graph.py:433`, `:451`, and `contracts/contract_records.py:300`
inside `to_schema_contract`).

**Evidence.** 5.00 calls per row on gate (1,557 calls, one instance, one
value); 11.00 per row on transform (3,417 calls, 1,239 instances, 5 values).
112.5 µs per call at 10 fields, 1,077.7 µs at 100. Prototype: computations
5 → 0 and 11 → 0 per row (with E-6b), recorded values identical.

**Change.** Add a non-compared, non-init slot and fill it on first use:

    _version_hash_memo: str | None = field(default=None, init=False, repr=False, compare=False, hash=False)

    def version_hash(self) -> str:
        memo = self._version_hash_memo
        if memo is None:
            memo = self._compute_version_hash()      # today's body, unchanged
            object.__setattr__(self, "_version_hash_memo", memo)
        return memo

`dataclasses.replace` re-initialises an `init=False` field, so `with_field`,
`with_locked` and every other derived contract start with an empty memo.
Soundness rests on `fields` being a tuple of frozen `FieldContract`; nothing
in `__post_init__` (`:172-195`) checks that. Add
`if type(self.fields) is not tuple: raise TypeError(...)` there and run the
contract suites; if a caller passes a list, fix the caller. Rejected: a
module-level `functools.lru_cache` keyed on the contract value — Runtime-VAL
refuses it at run start (measured, § 0 point 6); caching the
`to_checkpoint_format()` dict — it is mutable and callers own it.

**Must not change.** The 32-hex value; that it covers mode, locked and every
field attribute (docstring `:400-408`); `from_checkpoint`'s recompute-and-
compare on restore (`:461` onward) must still hash the restored object, which
it does because a fresh instance has no memo.

**Risk.** Low. One frozen class, value unchanged.

**Ruling needed.** No.

**Verification.** `tests/unit/contracts/test_schema_contract.py`,
`tests/unit/contracts/test_pipeline_row.py`,
`tests/unit/contracts/test_contract_records.py`,
`tests/unit/contracts/test_union_merge.py`, `tests/unit/core/checkpoint/`.
New test: 100 calls on one instance run `canonical_json` once (count through
`monkeypatch` on `elspeth.contracts.hashing.canonical_json`); negative control
— `with_field(...)` on a memoised contract returns a different hash and
computes again. New test that a contract built with a list for `fields`
raises. Testcontainer not owed. Before/after: `L2/callers.py` rows for
`version_hash` and `_compute_version_hash`.

**Expected saving.** About 11 µs × fields × calls per row: 0.3–0.6 ms per row
on the five-to-seven-column workloads, 5.4 ms (gate) to 11.9 ms (two
transforms) per row at 100 columns. Alone, without E-6b, transform pipelines
keep 4 computations per row (1,239 instances over 310 rows).

#### E-6b — Share the version hash across equal contracts rebuilt per row

**Finding.** Prior review E-6b: transforms build a fresh, equal
`SchemaContract` for every row, so an instance memo misses.

**Mechanism (verified at 1a7ff0e6).** Per row the transform workload
constructs 11 contracts: `SchemaContract.with_field`
(`schema_contract.py:312`) from `value_transform.py:631` three times,
`plugins/infrastructure/base.py:1214` (`_apply_declared_output_field_contracts`)
once per transform, `type_coerce.py:574`, `declared_output_types.py:149`
once per transform, and `contract_records.py:293` once per
`update_node_output_contract`. 3,415 instances carried 18 distinct values.
`SchemaContract` equality and hash cover `mode`, `fields`, `locked` — exactly
the inputs of `version_hash` — so equal contracts have equal version hashes.

**Evidence.** Counts above. `hash(contract)` plus one equality costs 3 µs at
10 fields and 26 µs at 100, against 112 µs and 1,078 µs for the hash.

**Change.** Do it in the engine, not in the plugins, so no plugin source hash
moves. `TransformExecutor` keeps a per-executor table of contracts it has
seen, keyed by value, and a method on the contract copies the memo from an
equal twin after checking equality:

    # contracts/schema_contract.py
    def adopt_version_hash(self, twin: SchemaContract) -> None:
        if self._version_hash_memo is not None or twin is self:
            return
        if type(twin) is not SchemaContract or twin != self:
            raise ValueError("adopt_version_hash requires an equal SchemaContract")
        object.__setattr__(self, "_version_hash_memo", twin.version_hash())

    # engine/executors/transform.py, _prepare_success_completion, after output_contract is known
    canonical = self._contract_intern.setdefault(output_contract, output_contract)
    output_contract.adopt_version_hash(canonical)

Bound the table (clear it past a few hundred entries) so an observed-schema
source with many row shapes cannot grow it without limit. The same table gives
prior E-5 the right key: `(node_id, contract)` by value rather than
`(node_id, version_hash)`, so the skip does not itself hash a fresh contract
per row. Rejected: caching in `_apply_declared_output_field_contracts` or in
`with_field` — it edits plugin-facing code and each plugin file edit moves a
`source_file_hash` pin; rewrapping `result.row` with the interned contract —
the engine would be rewriting what the plugin returned. The optional
plugin-side clean-up (build the output contract once per input contract) is a
plugin-lane item; after this change it saves only construction, about 10 µs
per contract.

**Must not change.** `nodes.output_contract_json` and `output_contract_hash`
(identical in the prototype); the proof is equality, checked on every
adoption, never identity or container type.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/engine/test_executors.py`,
`tests/unit/engine/test_processor_transform_execution.py`,
`tests/unit/engine/test_declared_output_fields_contract.py`. New test: two
equal contracts built separately → second `version_hash()` does not call
`canonical_json`; negative control — a contract differing in one field's
`nullable` raises from `adopt_version_hash` and computes its own hash. Smoke:
`L2/audit_projection.py` identical before and after on the transform
workload. Testcontainer not owed.

**Expected saving.** With E-6a, the remaining 4 computations per row on a
two-transform pipeline go to 0; that is the difference between "partly helps"
and "removed" in prior finding 6. Overlaps E-5.

#### L2-1 — Serialise once where one object is both hashed and stored

**Finding.** Recon Appendix B § 3 ("per source row", items 1–2; "per external
call"). Not in the prior review.

**Mechanism (verified at 1a7ff0e6).** Three sites canonicalise an object for
its hash and again for its payload bytes, although `stable_hash` is by
definition SHA-256 of those bytes (`contracts/hashing.py:168-169`):
`data_flow/tokens.py:349` then `:362` (source row);
`execution/calls.py:334-335` then `:340`, `:342` (request and response);
`execution/operations.py:91` then `:93`, and `:169` then `:205`. Class (d).

**Evidence.** Gate and transform: `_prepare_source_row_record` makes one
`stable_hash` and one `canonical_json` call per row. Micro-benchmark: 51.9 →
24.8 µs (10 fields), 441.8 → 195.3 µs (100 fields). The calls and operations
sites are static only (no external-call workload was run).

**Change.** One helper in `core/canonical.py`:

    def canonical_bytes_and_hash(obj: Any) -> tuple[bytes, str]:
        payload = canonical_json(obj).encode("utf-8")
        return payload, hashlib.sha256(payload).hexdigest()

Use it at the three sites. In `tokens.py` only the non-quarantined branch
changes; the quarantined branch keeps `canonical_or_recorded_hash` and
`canonical_or_recorded_repr_payload` (`:346-361`), whose fallbacks differ. In
`calls.py` the bytes are computed even when no payload store is configured;
that costs nothing extra because the hash needs them. In `operations.py:169`
hold the bytes until the store call at `:205`. Rejected: returning the bytes
from `payload_store.store` — the store is another lane's.

**Must not change.** The recorded hash and the stored bytes (both are the same
function of the same object as today); the order "row inserted, then payload
stored after the status check" in `operations.py:200-205`.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_token_recording.py`,
`test_token_recording_integrity.py`, `test_row_data.py`,
`test_call_recording.py`, `tests/unit/core/test_canonical.py`. New property
test: for any canonicalisable object, `canonical_bytes_and_hash(x)` equals
`(canonical_json(x).encode(), stable_hash(x))`; negative control — a helper
that hashes the `str` instead of the bytes of a non-ASCII payload fails.
Testcontainer owed for `tokens.py`, `calls.py`, `operations.py`
(Landscape persistence), although no SQL changes.

**Expected saving.** One canonicalisation per source row (about 25 µs at 10
fields, 220 µs at 100) and two per external call; the response payload of an
LLM call is the largest object the system hashes, so that is where it matters.

#### L2-2 — Hash the source row once for its source node state

**Finding.** Recon Appendix B § 3 ("per source row", items 3–4). Not in the
prior review.

**Mechanism (verified at 1a7ff0e6).** `ingest_row_with_initial_claim` passes
`input_data=source.data, output_data=source.data` (`scheduler/queue.py:440-441`),
and `record_completed_node_state_on` hashes each
(`execution/node_states.py:270`, `:271`). Same object, two serialisations.
Class (d).

**Evidence.** 2.00 `stable_hash` calls per row from
`record_completed_node_state_on` on both workloads; 1.00 under the prototype.

**Change.** At `node_states.py:271`:

    output_hash = input_hash if (output_data is input_data and not quarantined) else stable_hash(output_data)

The `not quarantined` term is required: on the quarantined branch `input_hash`
may be a `repr_hash` fallback (`:264-268`) while line 271 today raises for the
same data, and the shortcut must not turn that raise into a recorded value.
This is a comparison of two arguments inside one call with nothing in between,
not a memo keyed on a mutable object.

**Must not change.** Both recorded values; the quarantined path's behaviour.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_node_state_recording.py`.
New tests: same object → one `stable_hash` call and equal hashes; negative
controls — an equal but distinct dict is hashed twice, and
`quarantined=True` with a NaN payload still raises. Testcontainer owed
(node-state persistence).

**Expected saving.** One canonicalisation per source row. Subsumed by L2-4 if
that lands with the row passed through ingest.

#### L2-3 — Drop the gate executor's own input hash

**Finding.** The duplicate that composer Fix 9 removed from the transform
executor is still in the gate executor; Fix 9 says "gates untouched". Recon
Appendix A § 3 (`gate.py:287-288`).

**Mechanism (verified at 1a7ff0e6).** `execute_config_gate` computes
`input_hash = stable_hash(input_dict)` (`engine/executors/gate.py:288`), then
opens the guard, whose `begin_node_state` hashes the same dict
(`node_states.py:154`). The executor's value is used only for
`result.input_hash` (`gate.py:376`, `:443`) and `result.output_hash`
(`:444`). Nothing compares the two, so it is class (d), not a cross-check.
It is not a control-flow guard either: a non-canonical row raises from
`begin_node_state` inside `NodeStateGuard.__enter__`
(`state_guard.py:219`), before any state exists, exactly as line 288 does.

**Evidence.** 1.00 `stable_hash` per row from `execute_config_gate`; 0 under
the prototype, with identical `node_states` hashes.

**Change.** Delete line 288 and read `guard.state.input_hash` at `:376`,
`:443`, `:444`, as `transform.py:1014` does.

**Must not change.** `node_states.input_hash`/`output_hash`; the values on
`GateResult`; that a non-canonical row raises before a state is opened.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/engine/test_gate_executor.py`,
`tests/unit/engine/test_node_state_guard.py`; update the stub in
`tests/fixtures/executors.py` only if a gate test relies on the executor's
hash. New test in the style of Fix 9: `stable_hash` calls per gate attempt
1 → 0 in the executor module (red against HEAD); negative control — a NaN
input still raises with no node state written. Testcontainer not owed.

**Expected saving.** One canonicalisation per token × gate.

#### L2-4 — Carry the row's hash on the frozen row and hand the row to the node-state verbs

**Finding.** Recon summary § 1 and Appendix B § 3: each row version is
serialised three times per token × node — twice as output, once as the next
node's input. **Verified**, and the same holds for a gate (three hashes of an
unchanged row).

**Mechanism (verified at 1a7ff0e6).** The executor thaws the row and gives
the dict to the guard (`transform.py:881`, `:910`); `begin_node_state` hashes
it (`node_states.py:154`). After the plugin returns, the executor hashes
`result.row` (`transform.py:737`) inside the `try` that turns a NaN, Infinity
or unserialisable value into a routed `PluginContractViolation`
(`:735-748`) — class (c), and it must stay. It then thaws the same row again
(`:781`) and `complete_node_state_on` hashes that copy (`node_states.py:610`).
The row becomes `updated_token.row_data` (`:1047`), and the next node's
`begin_node_state` hashes a third thaw of it. The leader keeps the in-memory
item between hops (`engine/scheduler_drain.py:616-617`), so it is the same
`PipelineRow` object throughout.

**Evidence.** Transform: `stable_hash` 2.00 per row each from
`_populate_result_audit_fields`, `complete_node_state_on` and
`begin_node_state`. Prototype: the two repository calls become memo reads
(about 1.5 µs each under cProfile, against 130–165), recorded hashes
identical on all three workloads.

**Change.** Three parts.

1. `PipelineRow` gets a third slot, `_stable_hash_memo`, set to `None` in
   `__init__` (`schema_contract.py:622`, `:647-648`; it must be assigned
   there because `__getattr__` at `:684` raises for unset underscore names),
   a read-only property and a set-once setter that raises
   `AuditIntegrityError` if asked to change a value.
2. `core.canonical.stable_hash` and `canonical_json`, for an argument that is
   exactly a `PipelineRow`: return the memo if present; otherwise compute and
   store it **only if the walk met no `numpy.ndarray`**. That is the complete
   rule: `PipelineRow.__init__` deep-freezes into containers it built itself
   (`contracts/freeze.py:55-87` always wraps a fresh dict), so the containers
   are owned and immutable by construction; of the leaf types the
   canonicaliser accepts (`canonical.py:47-156`) only an ndarray is mutable in
   place; every other mutable leaf makes `rfc8785` raise, so no hash exists to
   store. A row holding an array is simply re-hashed each time, as today.
3. The executors pass the row itself: `input_data=token.row_data` at
   `transform.py:910` and `gate.py:309`; `output_data=result.row` (or
   `list(result.rows)`) at `transform.py:781-785`; `output_data=token.row_data`
   at `gate.py:456`. The repository still calls `stable_hash(input_data)`
   itself; it now gets the memo. `transform.py:737` stays where it is and
   becomes the one place the output row is serialised.

The memo is never persisted: a row rebuilt from the journal
(`scheduler_work_codec.py:145`, `scheduler_drain.py:982`) or by
`__deepcopy__` (`schema_contract.py:818-833`) is a new object and is hashed
from its own data. Two threads storing the memo store the same value.
Rejected: passing the executor's hash string into the repository — a caller-
asserted hash is exactly what Fix 9 noted the repository is never given; a
dict keyed by `id(row)` or by the thawed dict — a memo on a mutable or
reusable key is a correctness bug; a field on `TokenInfo` — it would have to
be cleared by hand at every `with_updated_data`.

**Must not change.** Every recorded `input_hash` and `output_hash`; the
quarantined path's `repr_hash` fallback (`node_states.py:148-152`), which is
never memoised because it is not a canonical hash; the NaN/Infinity
conversion at `transform.py:735-748`; hashing from stored data on resume.

**Risk.** Medium. The value is unchanged, but the convention that "Landscape
stores raw dicts, not PipelineRow objects" (`transform.py:877-878`,
`gate.py:285-286`) is pinned by two tests and by the stub repository in
`tests/fixtures/executors.py`, and the soft-mapping census pins the
`Mapping[str, object]` annotations whose signatures widen
(`node_states.py:119`, `:193-194`, `:547`, `:573`;
`execution_repository.py:208`, `:234`; `state_guard.py`).

**Ruling needed.** Yes. *May the node-state verbs accept an engine-owned
frozen `PipelineRow` as `input_data`/`output_data`, so that the recorded hash
is the row's memoised canonical hash (computed once, from the same frozen
data, by the same function) rather than a second derivation from a thawed
copy?* No ADR states the dict convention; the closest are ADR-032 (nominal
typing of owned types) and the Fix 9 note in
`docs/development/composer-performance-remediation.md`. If the answer is no,
L2-2 and L2-3 still remove two of the duplicates and the per-node pair stays.

**Verification.** `tests/unit/engine/test_executors.py` (two tests change by
design, named in § 3), `test_gate_executor.py`, `test_node_state_guard.py`,
`test_processor_transform_execution.py`,
`tests/unit/core/landscape/test_node_state_recording.py`,
`tests/unit/contracts/test_pipeline_row.py`,
`tests/property/canonical/test_freeze_hash_equivalence.py`,
`tests/property/audit/test_nan_infinity_rejection.py`. New tests, each with
its control: (i) one transform attempt serialises the output row once (count
`rfc8785.dumps` on row-shaped input; red at HEAD with 2); (ii) a row holding
an ndarray is not memoised, and mutating the array between two hashes changes
the second hash — the control that the rule is load-bearing; (iii) NaN output
still routes as a Tier-2 violation and never stores a memo; (iv) a row
restored from `row_payload_json` has no memo; (v) property: for any row,
`stable_hash(row)` with and without a warm memo equals
`stable_hash(row.to_dict())`. Smoke gate: `L2/audit_projection.py` identical
on gate, transform and `error_routing`. Re-pin the soft-mapping census in the
same commit. Testcontainer owed (node-state verbs). Before/after:
`L2/callers.py`.

**Expected saving.** Two canonicalisations per token × node (about 50 µs at
10 fields, 440 µs at 100) and two thaws. Measured together with the other
items in § 3; not separable on narrow rows.

#### L2-5 — Canonicalise a `PipelineRow` from its frozen data, without thawing

**Finding.** Recon Appendix B § 3 ("a `PipelineRow` argument adds a
`deep_thaw` copy first") and § 6 item 4.

**Mechanism (verified at 1a7ff0e6).** `_normalize_for_canonical` calls
`data.to_dict()` on a row (`core/canonical.py:175-176`) and then walks and
copies the result. The walk already handles `Mapping` and `tuple`
(`:178-193`), which is what the frozen data is made of.

**Evidence.** 620 `to_dict()` calls from `canonical.py:176` in the transform
count; thaw cost 2.2 / 20.9 / 11.1 µs.

**Change.** Give `PipelineRow` a read-only `frozen_data` property returning
its `MappingProxyType` (already immutable, safe to hand out) and walk that.
One behaviour has to be reproduced exactly: `deep_thaw` turns a `frozenset`
into a list (`freeze.py:147-148`), so the frozen walk must do the same to stay
byte-identical — see L2-6 for why that behaviour is itself wrong.

**Must not change.** Bytes and exception classes; proven by the row arm of
`L2/prop_test.py` (12,000 rows).

**Risk.** Low.

**Ruling needed.** No (as specified, byte-identical including the frozenset
arm).

**Verification.** `tests/property/canonical/test_freeze_hash_equivalence.py`,
`tests/unit/core/test_canonical.py`. New property test: bytes from the frozen
walk equal bytes from `canonical_json(row.to_dict())`; negative control — a
walk that skips the tuple→list arm fails on any list-valued field.

**Expected saving.** One thaw per row hash: 8–9% of a `PipelineRow` hash.

#### L2-6 — A `set` inside a `PipelineRow` hashes differently in every process

**Finding.** New; found while proving L2-5 byte-identical. Not a performance
item.

**Mechanism (verified at 1a7ff0e6).** `deep_freeze` turns a `set` into a
`frozenset` (`contracts/freeze.py:74-75`); `to_dict()` thaws it into a list in
iteration order (`freeze.py:147-148`); `stable_hash(row)` then succeeds. The
same value in a plain dict is refused: `frozenset` raises `TypeError`
(`contracts/hashing.py:54-57`, "no canonical ordering") and `set` raises from
`rfc8785`.

**Evidence.** `L2/set_hash.py` under `PYTHONHASHSEED` 1, 2, 3: one row,
`{"tags": {"alpha", "beta", "gamma", "delta"}}`, three different hashes
(`b9d0b45a3ee562ca`, `5fe39b53ead8621b`, `47ad6922a10e3f35`). Whether a
shipped plugin can emit a set was not established; an observed-mode output
schema would not stop one.

**Change.** Refuse it where the dict path already does: the canonicaliser
raises the existing `TypeError` for a frozenset inside a row. At
`transform.py:742` that is already caught and routed as a Tier-2
non-canonical-output violation. Alternative: refuse a set in
`PipelineRow.__init__`.

**Must not change.** Rows without sets.

**Risk.** Medium: a pipeline that works today by accident would start routing
those rows to `on_error`.

**Ruling needed.** Yes. *A set in row data is recorded today with a hash that
cannot be reproduced. Should it be refused (routed as a non-canonical output,
as the same value in a plain dict already is), and at which seam — the
canonicaliser or `PipelineRow` construction?* Touches the canonical-JSON
policy in `contracts/hashing.py`; no ADR number found for it.

**Verification.** New test running the hash of a set-bearing row in two
subprocesses with different `PYTHONHASHSEED` (red at HEAD: hashes differ);
after the change both raise. `tests/property/canonical/test_hash_determinism.py`
is the natural home.

**Expected saving.** None.

#### L2-7 — Cut the engine's thaws per token × transform from seven to two

**Finding.** Recon Appendix A headline ("three `to_dict()` deep thaws") and
Appendix B § 6. The count is **seven**, not three.

**Mechanism (verified at 1a7ff0e6).** All seven are read-only uses by engine
code, of two contents (input row, output row):

| Site | Used for | Needs a `dict`? |
|---|---|---|
| `transform.py:881` | node-state input hash; preflight key sets (`:452`, `:478`); `input_schema.model_validate` (`:564`); error record (`:1093`) | only pydantic |
| `transform.py:696` | `output_schema.model_validate` | yes (pydantic strict) |
| `output_declaration.py:110` | iterating field names | no — `emitted.keys()` |
| `declared_output_types.py:132` | reading input values by normalised name | no |
| `declared_output_types.py:135` | reading emitted values | no |
| `core/canonical.py:176` | hashing | no (L2-5) |
| `transform.py:781` | node-state output hash | no (L2-4) |

None of them is trust-boundary protection. The protection is the deep freeze
in `PipelineRow.__init__` (`schema_contract.py:647`), which detaches the row
from whatever dict the plugin still holds; that copy stays. The plugin's own
`row.to_dict()` is its mutable working copy and also stays.

**Evidence.** 620 calls at each of the seven sites over 310 rows × 2
transforms. Prototype: 19 → 8 `to_dict()` per row.

**Change.** `output_declaration.py:110` iterates `emitted.keys()`.
`declared_output_types.py:132`, `:135` read `row.frozen_data` (L2-5): the
check only compares and type-tests fields whose declared type is concrete
(`:139` skips `object`), so containers never reach the comparison and frozen
and thawed values behave the same. The hashing uses go with L2-4 and L2-5.
What remains is one thaw of the input and one of the output, each made for
pydantic and for nothing else. Do **not** share those two dicts with the
integrity checks: a schema with a `mode="before"` validator receives the raw
dict and could change it, so the checks read the frozen row.

**Must not change.** What each check reads: normalised names only
(`declared_output_types.py:124-131` explains why `__getitem__` is not used).

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/engine/test_declared_output_fields_contract.py`,
`tests/unit/engine/test_executors.py`, the ADR-050 declared-type suites under
`tests/unit/engine/`. New test: `to_dict` calls per successful transform
attempt 7 → 2 in engine code (red at HEAD); negative control — a declared
`int` field emitted as `str` still raises `DeclaredOutputTypeViolation`, and
an emitted header-spelled `Name` is still not read as input field `name`.

**Expected saving.** Five thaws per token × transform: about 11 µs at 10
fields, 105 µs at 100.

#### L2-8 — Make the second normalisation walk non-copying, with a plain-data fast path

**Finding.** Recon Appendix B § 3: "three Python-level tree walks and two full
structure copies". Verified (§ 2).

**Mechanism (verified at 1a7ff0e6).** `core.canonical.stable_hash` runs
`_normalize_for_canonical` (`canonical.py:227`), then
`contracts.hashing.stable_hash` → `canonical_json` runs
`_normalize_frozen_and_reject_non_finite` over the result (`hashing.py:85`),
then `rfc8785.dumps` (`:86`), `.decode` (`:87`), `.encode` and SHA-256 (`:169`). For
the output of walk 1 the second walk changes nothing except to raise
`TypeError` if a `frozenset` survived.

**Evidence.** § 2: dropping walk 2 saves about 10% of a hash (option A); a
no-copy check that the input is already plain JSON, skipping both copies,
saves 33–37% (option F). The scheduler-event identity dict, hashed 4 times per
row (`scheduler/events.py:86`), is plain: 35.3 → 21.1 µs.

**Change.** Keep `rfc8785` as the only serialiser and keep the policy in
`contracts/hashing.py`:

1. `_normalize_frozen_and_reject_non_finite` returns its argument unchanged
   when it is an exact `dict`/`list` tree of exact `str`, `int`, `bool`,
   `None` and finite `float` with `str` keys (one validating pass, no
   allocation), and copies as today otherwise.
2. Add `canonical_bytes(obj) -> bytes` beside `canonical_json` and have
   `stable_hash` hash the bytes directly instead of decoding and re-encoding.

Do **not** merge the two walks into one in `core/canonical.py` without care.
The prototype's first merged walk changed an exception class
(`CanonicalizationError` instead of `TypeError` for a frozenset under the
tagged-mapping arm) and its second changed error precedence (`TypeError`
before the `ValueError` a `Decimal('NaN')` elsewhere in the object raises
today); both were caught only by the property test. A merged walk also breaks
the two tests that pin delegation to the contracts primitive (§ 3). The
non-copying second walk gets most of the gain and keeps both.

**Must not change.** Canonical bytes are RFC 8785 and identical to today's
for every input; exception class and precedence for every refused input.

**Risk.** Low–medium: small code, but it is the hash of everything.

**Ruling needed.** No.

**Verification.** `tests/unit/contracts/test_hashing.py`,
`tests/unit/core/test_canonical.py`, `test_canonical_mutation_gaps.py`,
`tests/property/canonical/`. New property test, promoted from
`L2/prop_test.py`: against a frozen copy of today's implementation kept in the
test file, same bytes or same exception class, over a strategy that includes
NaN, ±Infinity, −0.0, integers past 2**53, lone surrogates, astral and
U+E000–U+FFFF keys, non-`str` keys, tuples, frozensets, numpy scalars and
arrays, `Decimal`, `datetime`, `bytes` and the reserved type key. Negative
control in the same file: `json.dumps` as the serialiser must fail.

**Expected saving.** 10–35% of each remaining canonicalisation; after the
dedupe items that is 40–60 calls per row of mostly small objects. Small.

#### L2-9 — Hybrid C-`json` serialiser for the provably equivalent subset (not recommended now)

**Finding.** `rfc8785` 0.1.4 is pure Python and is 53–56% of each hash.

**Mechanism.** For exact `dict`/`list` trees with ASCII `str` keys, exact
`str`/`bool`/`None`, integers within ±(2**53−1) and floats whose `repr` has no
exponent and does not end in `.0`, CPython's C encoder with
`ensure_ascii=False, sort_keys=True, separators=(",", ":")` emits the RFC 8785
bytes: the escape table is the one `rfc8785` copied
(`rfc8785/_impl.py:30-41`), and ASCII keys sort the same by code point and by
UTF-16 code unit. Everything else falls back to `rfc8785`.

**Evidence.** § 2 option H: 4× faster per hash. Property test: 36,000 plain
values, 7,260 through the fast arm, byte-identical; 12,000 mixed values
byte-identical or same exception class.

**Change.** None proposed. It makes a second serialiser the authority for
part of the input space, and its equivalence rests on CPython's encoder
internals staying as they are.

**Must not change.** n/a.

**Risk.** High for the size of the gain once the duplicates are gone.

**Ruling needed.** Yes, if it is ever wanted: *may a second serialiser produce
canonical bytes for a subset of inputs, with byte identity enforced by a
property test and a version pin?* `CANONICAL_VERSION` (`hashing.py:29`) is
stored with every run.

**Verification.** The property test of L2-8, plus a pin on the Python minor
version.

**Expected saving.** Up to 75% of remaining canonical time; unknown in
whole-run terms until L2-1 to L2-8 land.

#### L2-10 — Serialise the source contract once per contract, not once per row

**Finding.** Recon Appendix A § 3 (`processor.py:2979`, now `:2990`): "the
contract is re-serialised for every row".

**Mechanism (verified at 1a7ff0e6).** `process_row` passes
`source_contract_json=checkpoint_dumps(source_row.contract.to_checkpoint_format())`
(`engine/processor.py:2990`) for every row; the string is stored in
`rows.source_contract_json` (class (a) value, class (d) computation). Both
workloads used one source contract object for the whole run.

**Evidence.** 1.00 `to_checkpoint_format` + 1.00 `checkpoint_dumps` per row
from `process_row`: 512 + 184 cProfile µs per row before E-6a, about 176
after (the prototype did not include this item).

**Change.** `RowProcessor` keeps the last `(contract, json)` pair per source
node and reuses the string when `source_row.contract is` the stored object.
Identity is sound here because the stored object is held (so its id cannot be
reused) and is frozen. An equal-but-distinct contract just recomputes.

**Must not change.** The stored string, byte for byte.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/engine/` processor suites,
`tests/unit/core/test_multi_source_foundation.py`. New test: 100 rows with one
contract → one `checkpoint_dumps` of a contract; negative control — a second
source with a different contract gets its own string.

**Expected saving.** About 60 µs per row after E-6a (real time, estimated from
the cProfile figure); more with wide contracts.

### 5. Tables

#### (1) All items

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| E-6a | Memoise `version_hash()` on the instance | Low | No | E-6a | ~11 µs × fields × 5–11 calls per row; 0.3–0.6 ms narrow, 5–12 ms at 100 columns |
| E-6b | Share the hash across equal contracts (engine-side value interning) | Low | No | E-6b | Remaining 4 computations per row on transform pipelines → 0 |
| L2-1 | Serialise once where an object is hashed and stored | Low | No | — | 1 canonicalisation per source row; 2 per external call |
| L2-2 | Hash the source row once for its source state | Low | No | — | 1 per source row |
| L2-3 | Drop the gate executor's input hash | Low | No | — (composer Fix 9 analogue) | 1 per token × gate |
| L2-4 | Hash memo on the frozen row; rows handed to node-state verbs | Medium | **Yes** | — | 2 canonicalisations + 2 thaws per token × node |
| L2-5 | Canonicalise a row from frozen data | Low | No | — | 8–9% of each row hash |
| L2-6 | A set in a row hashes non-deterministically (defect) | Medium | **Yes** | — | none |
| L2-7 | Engine thaws per token × transform 7 → 2 | Low | No | — | 5 thaws per token × transform |
| L2-8 | Non-copying second walk; hash bytes directly | Low–medium | No | — | 10–35% of each remaining canonicalisation |
| L2-9 | Hybrid C-`json` serialiser | High | **Yes** | — | not recommended now |
| L2-10 | Source contract JSON once per contract | Low | No | — (E-6 family) | ~60 µs per row after E-6a |

Measured together (§ 3): −4.6% CPU on 1,000 narrow gate rows, not resolvable
on 1,000 narrow transform rows, −17% on 310 rows of 100 columns.

#### (2) Recommended order

1. **E-6a**, then **E-6b**. Independent of everything else; most of the
   measurable gain. E-6b should land with or before prior **E-5** so E-5's
   skip can key on the contract value.
2. **L2-3**, **L2-2**, **L2-1**, **L2-10**. Independent, local, no ruling.
3. **L2-5**, then **L2-7** (L2-7's `frozen_data` reads need L2-5's property).
4. **L2-6** ruling, then **L2-4** ruling. L2-4 depends on L2-5 and should be
   decided after L2-6, because the memo rule and the frozenset arm meet in the
   same function. If L2-4 lands, L2-2 becomes redundant for the ingest path
   once the row itself is passed as `SourceIngestSpec.data`.
5. **L2-8** last: it touches the hash of everything and its gain shrinks as
   the call count falls.
6. **L2-9**: not scheduled.

Every step keeps the smoke gate: `L2/audit_projection.py` output identical
before and after on the gate, transform and `error_routing` workloads.

#### (3) Examined and recommended not to fix

- **Input and output pydantic validation per attempt** (`transform.py:564` in
  `_run_preflight`, `:696` in `_verify_success_emissions`; recon Appendix A
  top-10 item 5). They validate different schemas against different rows:
  `transform.input_schema` on the arriving row, `transform.output_schema` on
  each emitted row. Two distinct contracts, not a duplicate. Measured 4
  `model_validate` per row for two transforms, 547 cProfile µs per row
  together. Leave alone.
- **`serialize_row_payload` twice per row** (prior finding 6;
  `scheduler_work_codec.py:106`, `scheduler_drain.py:763`). In a transform
  pipeline the two calls serialise different row versions, so it is not a
  duplicate. For a token whose row is unchanged (gate-only) it is, but after
  E-6a each call falls from about 1,170 to 240 cProfile µs, and reusing the
  claimed item's stored JSON would tie the hand-off to object identity across
  the claim. Fix E-6a; leave the second call.
- **Splicing a cached contract JSON into each row payload**
  (`payload_codec.py:34-39`). The persisted string would then depend on
  matching `json.dumps` formatting by hand. Not worth it after E-6a.
- **`is_non_canonical_number`** (`core/canonical.py:335`; 3–14 calls per row
  from `plugins/infrastructure/schema_factory.py:42`, `:68`). 2.2 µs per call
  against 0.03 µs for `math.isfinite`, so about 30 µs per row. The docstring
  makes the canonicaliser the single authority on purpose (`:319-326`); a
  second copy of the rule could drift. Leave alone.
- **The output hash at `transform.py:737`**, and the same pattern at
  `aggregation.py:490-492` and `collector.py:1167`. Class (c). Stays, as
  Fix 9 ruled.
- **`execution_repository.py:1018`** (aggregation verifies a hash it is given)
  and `processor.py:4358` (receipt comparison). Class (b). Stay.
- **`TokenInfo.with_updated_data`** (`contracts/identity.py:310-332`,
  `transform.py:1047`): one `dataclasses.replace` per node, no row copy. Not
  worth touching.

#### Handed to other lanes (measured here, not designed here)

- **Prior E-5** (`transform.py:800-807`, `data_flow/graph.py:433-453`):
  −11% whole-run CPU on the 1,000-row transform workload in this lane's
  prototype, against the prior review's ~6% estimate. Key it on the contract
  value.
- **Prior E-9a**: `compute_full_topology_hash` makes 6 `stable_hash` calls per
  row at the default every-row checkpoint, 1,702 cProfile µs — the largest
  remaining canonical consumer on the transform workload once this lane's
  items land.
- **Sink lane**: `SinkEffectMember` is constructed 7 times per sink row
  (`sink_effect_reservation.py:112`, `:153`; `sink_effect_identity.py:292`,
  `:387`; `contracts/sink_effects.py:622`; `executors/sink_effects.py:1141`,
  `:1242`) and each construction re-canonicalises the row and the lineage
  (`contracts/sink_effects.py:566`, `:579`) — 14 canonicalisations per row.
  Also `sink.py:1557` thaws every row and `node_states.py:493` hashes the
  thawed copy; with L2-4 the sink can pass the row and get the memo.
- **Ingest lane**: `SourceIngestSpec.__post_init__` deep-freezes `data`
  (`contracts/scheduler.py:62`) immediately after `processor.py:2989` thawed
  it, a thaw-then-freeze pair per row the recon does not list.
- **Payload-store lane**: `payload_store.py:230` SHA-256s bytes whose hash the
  caller already holds (L2-1).
- **Plugin lane**: `value_transform.py:589` and `type_coerce.py:419` call
  `copy.deepcopy(row.to_dict())`, a deep copy of a fresh deep copy;
  `value_transform.py:593` thaws the row to read its keys; `:601` builds and
  freezes a `PipelineRow` per operation. Each plugin edit moves its
  `source_file_hash` pin.

#### (4) Not verified

- External-call payload hashing (`calls.py:334-342`) and operations
  (`operations.py:91-93`, `:169`, `:205`): read, not measured; no
  external-call workload was run.
- Fork, expand, coalesce and aggregation paths: not profiled in this lane.
  `__deepcopy__` per fork child and the hashes at `processor.py:4281-4358`
  were read only.
- PostgreSQL: nothing here changes SQL, but the testcontainer suite was not
  run against the prototype.
- The full default suite was not run against the prototype; only the 646
  tests in § 3.
- Whether any shipped plugin can put a `set` into a row (L2-6).
- Whether any caller constructs `SchemaContract` with a non-tuple `fields`
  (E-6a's precondition): a grep found no literal list, but
  `coalesce_executor.py:258`, `:392` and `union_merge.py:430`, `:508` pass
  computed values that were not traced.
- Whole-tree gates were not run against the prototype. Expected to move:
  soft-mapping census (L2-4 signatures), trust-tier corpus line numbers,
  and nothing under `src/elspeth/plugins/` (no plugin file is edited by any
  item above).
- The A/B timings are 2–6 pairs on a shared 4-core box; treat the narrow-row
  figures as "small", not as point estimates. The fixed-startup figure used
  for per-row arithmetic is the recon's, not re-measured.

### 6. Where this report contradicts the recon or the prior review

| Claim | Source | Finding here |
|---|---|---|
| Three `to_dict()` thaws per token × transform | Recon Appendix A headline | Seven in engine code plus one or two in the plugin (call-site count, § 1) |
| Five canonical serialisations of the row per token at the sink | Recon Appendix B § 3 | At least twelve of row content: `SinkEffectMember` is built 7 times per row and re-verifies each time |
| `version_hash()` twice per `update_node_output_contract` | Recon Appendix B § 6 | Three (`graph.py:433`, `:451`, `contract_records.py:300`) |
| Canonical JSON + hashing is 11.5–16.2% of per-row time | Recon § 1 table | A cProfile bucket. Removing about half of those calls changed CPU by −4.6% (gate) and −0.3% (transform) on narrow rows. It does reach −17% at 100 columns |
| `version_hash` is "5 calls per row, 1.6–2.8%" | Prior finding 6 | Right for narrow rows; the cost is linear in field count and is over half of all canonical-JSON time in both workloads |
| Memoising on the instance is the fix | Prior E-6a | Correct, but a module-level `lru_cache` variant is refused by Runtime-VAL at run start; equal contracts must share through instance state (E-6b design) |
| E-5 saves about 6% on transform pipelines | Prior finding 5 | −11% measured (2 pairs) with a value-keyed memo |
| Fix 9 removed the executor's duplicate input hash | Composer plan Fix 9 | Only for transforms; `gate.py:288` still has it (L2-3) |
| A row is always hashable deterministically | implicit | A `set` in a row gives a different hash per process (L2-6) |

## Appendix L3 — Payload store, checkpointing and the end-of-run sink phase

Lane L3 of the executor remediation plan. Read-only on the tracked tree; HEAD
`bf8adf06` (docs-only on `1a7ff0e6`), `git status --short` empty before and
after. Every `path:line` below was opened at this commit. Paths are under
`src/elspeth/` unless they start with `tests/` or `docs/`.

### How the numbers were produced

All scripts and raw results are in `.claude/lanes/exec-remediation/L3/`.

| File | What it does |
|---|---|
| `l3_measure.py` | Runs `elspeth run` in-process with SQLAlchemy cursor/commit listeners and wrappers that tag statements, commits and CPU (`time.process_time`) by region; counts `os.fsync`/`os.lstat`/`os.stat`, payload-store calls, checkpoint and topology-hash calls, sink effects and bytes hashed; samples RSS at first ingest, sink-flush entry and exit. Output `work/<workload>/m.json`. |
| `run.sh <workload>` | Wipes the workload's `runs/`, `output/`, `payloads/` and runs `l3_measure.py`. |
| `setup.py`, `setup_quar.py`, `setup_err.py` | Workload generators (the recon's gate workload re-rooted into this lane, a source-quarantine variant, an `on_error` variant). |
| `bench_quadratic.py` | Isolates the two tuple-rebuild lines with real SQLAlchemy `Row` objects, no SQL. |
| `pending_size.py` | Deep `sys.getsizeof` walk of `loop_ctx.pending_tokens` at sink-flush entry, then exits. |
| `topo_distinct.py` | Counts distinct `compute_full_topology_hash` results in one run. |
| `bench_sink_canon.py` | Per-row cost of each canonical serialisation on the sink path. |

Controls and caveats:

- `elspeth.__file__` recorded in every `m.json` is `<repo>/src/elspeth/__init__.py`.
- The gate workloads carry `payload_store.base_path` pointing into the lane
  directory so new-blob versus dedup-hit counts are not polluted by other
  lanes writing the same rows to the shared `.elspeth/payloads`. That path is
  13 components deep; the default is 5. `lstat` counts scale with that depth
  (L3-5), so my per-row `lstat` figure is higher than the recon's.
- The shipped gate example sets `checkpoint: every_n / 100`, so the gate
  workload does NOT exercise the default cadence. `gateER_2000` is the same
  workload with the `checkpoint:` block removed (default `every_row`).
- Other lanes were running on the 4-core box. Counts are exact; CPU figures are
  process CPU and moved about ±15% between runs; wall time is not used.
- The harness wrappers add overhead inside the regions they wrap (they do not
  change counts). Shares quoted from `l3_measure.py` are therefore slightly
  high for regions with many wrapped calls (payload store, hooks).
- The open-transaction counter used for "inside the write transaction" is
  process-wide and the heartbeat thread also begins transactions, so the
  figure is exact for the ingest region (it was tagged separately: 2,000 of
  2,000) and noisy by a handful elsewhere.

#### Gate workload, instrumented (one run each)

| Rows | CPU s | Statements | Commits | Sink-flush CPU s | `resolve_members` CPU s | Reservation lock loop CPU s | Tuple items built (each of 2 lines) | Peak RSS kB | RSS at flush entry kB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 200 | 9.2 | 17,485 | 2,116 | 0.99 | 0.31 | 0.059 | 10,100 | 400,948 | 399,904 |
| 1,000 | 28.6 | 82,651 | 10,151 | 4.79 | 1.57 | 0.446 | 250,501 | 408,124 | 400,012 |
| 2,000 | 55.6 | 164,140 | 20,197 | 10.55 | 3.67 | 1.707 | 1,001,001 | 413,572 | 400,796 |
| 4,000 | 111.9 | 327,119 | 40,290 | 24.03 | 7.44 | 5.599 | 4,003,156 | 429,640 | 401,252 |

Command: `.claude/lanes/exec-remediation/L3/run.sh gate_<n>`.

Sink-flush statements at 2,000 rows by region (`tag_sql`): `resolve_members`
32,000; `execute_effect` 8,571; reservation lock loop 4,012; scheduler
terminalisation 2,320; checkpoints 189 (21 at `every_n`); the rest 155. That
is 23.6 statements per row at `every_n`, 32.5 at the default `every_row`.

---

### Items

#### L3-1 — Stop rebuilding the locked-id tuple on every lock

**Finding.** Recon summary § 1 "Quadratic sink reservation" and Appendix F § 6. Not in the prior review, which states "nothing measured here is super-linear in run size"; that statement is wrong for the sink phase.

**Mechanism (verified at 1a7ff0e6).** `core/landscape/execution/sink_effect_reservation.py:351-358` locks member tokens one at a time and after each lock calls `self._after_token_lock(self._backend_pid(conn), tuple(item.token_id for item in locked_tokens))`; `:363-370` does the same for states with `_after_state_lock`. Each call rebuilds a tuple of everything locked so far, so one reservation of n members runs the generator body n(n+1)/2 times per line.

What the hooks are: `:994-1001`, three empty methods documented "Deterministic contention seam; production implementation is inert" / "Proof seam reached only after the complete ordered witness set". They are test seams for the PostgreSQL lock-order proofs, patched on the instance with `monkeypatch.setattr`. Consumers, all in `tests/testcontainer/core/test_sink_effect_lock_order_postgres.py`:

- `_after_token_lock` — one consumer, `:584-588` (`pause_after_first_token`), installed at `:600`. It uses only `len(token_ids) == 1` and the pid, to hold the reservation after its first token lock while an outcome writer is shown to block on it.
- `_after_state_lock` — no consumer anywhere in `tests/`, `src/` or `elspeth-lints/` (grep for the name returns the definition and the call only).
- `_after_witness_locks` — four consumers (`:179`, `:181`, `:490-491`, `:601`); called once per reservation at `:382` with tuples that already exist. Not part of the quadratic term.

Second cost on the same lines, missed by the recon: the first argument is `self._backend_pid(conn)` (`:988-992`), which on PostgreSQL executes `SELECT pg_backend_pid()`. It is evaluated on every iteration whether or not a seam is installed, so on PostgreSQL the loop costs 4n round trips (lock, pid, lock, pid), not 2n. Static only; SQLite returns 0 without a query.

**Evidence.** Three ways, all agree.

1. Exact iteration counts from the live run (table above, "Tuple items built"): 10,100 / 250,501 / 1,001,001 / 4,003,156 at 200 / 1,000 / 2,000 / 4,000 rows over two sinks, equal to the sum of n(n+1)/2 per sink (for example 999 and 1,001 members: 499,500 + 501,501 = 1,001,001).
2. Region CPU from the same runs: 0.059 / 0.446 / 1.707 / 5.599 s. Doubling rows from 2,000 to 4,000 multiplies it by 3.3. As a share of whole-process CPU: 0.6% / 1.6% / 3.1% / 5.0%; of sink-flush CPU at 4,000 rows, 23%.
3. Isolated, no SQL: `.venv/bin/python .claude/lanes/exec-remediation/L3/bench_quadratic.py 100 1000 2000 5000 10000 20000`

   | Members in one reservation | CPU for the two rebuild lines | ns per generator iteration | Linear control (hook called with `()`) |
   |---:|---:|---:|---:|
   | 1,000 | 0.317 s | 316 | 0.0001 s |
   | 2,000 | 1.315 s | 329 | 0.0002 s |
   | 5,000 | 8.411 s | 336 | 0.0004 s |
   | 10,000 | 35.4 s | 354 | 0.0007 s |
   | 20,000 | 156.4 s | 391 | 0.0015 s |

   Cost is about 3.3e-7 × n² seconds per reservation (rising slightly with n as the list leaves cache). The linear control is the negative control: the same loop without the rebuild is under 2 ms at 20,000.

Where it dominates. Per-row CPU for everything else on the gate workload is about 27 ms, of which the sink flush is about 5 ms. The quadratic term costs 3.3e-7 × n seconds per member, so in ONE reservation (one sink group):

- it exceeds the linear part of its own lock loop (about 0.3 ms per member) above roughly 900 members;
- it equals the rest of the sink flush at about 15,000 members;
- it equals the whole rest of the pipeline at about 80,000 members;
- at 100,000 members it is about 55 minutes of CPU, at 1,000,000 about 4 days.

The shipped `large_scale_test` (10,000 rows, about 5,000 per sink) already pays about 2 × 8.4 s = 17 s, roughly 7% of the prior review's 246 s baseline, inside what that review reported as "sink flush 16%".

**Change.** In `SinkEffectReservation._lock_and_validate_pipeline_witness`:

- Read the backend pid once before the loops (`pid = self._backend_pid(conn)`); it cannot change on one connection inside one transaction.
- Change the two per-item seams to carry a count and the id just locked, not the whole prefix:

  ```python
  def _after_token_lock(self, _pid: int, _locked_count: int, _token_id: str) -> None: ...
  def _after_state_lock(self, _pid: int, _locked_count: int, _state_id: str) -> None: ...
  ```

  and update the single consumer (`pause_after_first_token`: `len(token_ids) == 1` becomes `locked_count == 1`). `locked_tokens` is then unused and goes; `locked_states` stays (its rows are validated at `:376-381`).
- If L3-2 lands at the same time the per-item seams become per-chunk seams instead; see there. L3-1 alone is the smallest change that removes the quadratic term and can ship first.

Rejected: calling the hook only when one is installed (the tests patch the instance, so detection needs a `getattr`/`__func__` identity probe, which the house rules and the dynamic-attribute gate forbid); passing the growing list itself (hands a mutable internal to a seam; the count is all the consumer needs); deleting `_after_state_lock` outright (harmless, but it removes a seam the lock-order suite may want; keep it cheap instead).

**Must not change.** The lock order (tokens ascending by `token_id`, then states ascending by `state_id`, `:351`, `:363`); every `ValueError` raised on a missing row; the `_after_witness_locks` call and its arguments; nothing recorded changes.

**Risk.** Low. Two test-seam signatures and one test function change; production behaviour is identical.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_sink_effect_reservation.py`, `tests/unit/core/landscape/test_sink_effect_reservation_integrity.py`; `tests/testcontainer/core/test_sink_effect_lock_order_postgres.py` (owed: the seam is consumed only there, so `pytest tests/ -m testcontainer -n 0`). New test: reserve 3,000 members with a counting seam and assert total items handed to the seams is O(n) (exactly n calls, each constant-size); negative control: the same assertion run against the current code must fail (it reports 4,501,500). Add a PostgreSQL assertion that a reservation issues at most one `pg_backend_pid` statement. Before/after: `run.sh gate_4000`, region `reserve_lock_loop`.

**Expected saving.** Measured term, removed entirely: 3.3e-7 × n² s per reservation — about 1.3 s per 2,000-member sink, 17 s on the 10,000-row shipped example, hours above 100,000 members per sink. On PostgreSQL also 2n round trips per reservation (static).

---

#### L3-2 — Take the reservation witness locks in ordered chunks, not 2n point selects

**Finding.** Recon Appendix F § 6 last sentence ("one `SELECT ... FOR UPDATE` per member token and one per member state"). Not in the prior review.

**Mechanism (verified at 1a7ff0e6).** `sink_effect_reservation.py:353-354` and `:365-366` execute one `select(...).where(pk == id).with_for_update()` per token and per state inside the leader-fenced reservation transaction. On SQLite the dialect drops `FOR UPDATE` (the transaction is `BEGIN IMMEDIATE`), so the token loop is n point reads whose only effect is an existence check that `_token_rows_with_ingest_sequence` + `_validate_token_rows` (`:360-361`, `:467-475`) repeat immediately afterwards in 500-id chunks. The state loop's rows are needed (`:376-381`).

The repository already has the batched, ordered form of exactly this lock, used by finalization on the same tokens: `core/landscape/data_flow/outcomes.py:138-164` (`lock_token_outcome_dependencies`): `SELECT token_id … WHERE token_id IN (chunk) ORDER BY token_id FOR UPDATE OF tokens`, ascending chunks, skipped on non-PostgreSQL dialects. `core/landscape/bind_budget.py:1-37` states the rule for it: a read over a caller-supplied collection runs in ascending `bind_budget_chunks` (900 binds) "on the caller's one connection and transaction … ascending order keeps a locking read in the global primary-key lock order".

**Evidence.** `tag_sql["reserve_lock_loop"]` = 406 / 2,008 / 4,012 / 8,020 statements at 200 / 1,000 / 2,000 / 4,000 rows: 2.0 per row plus 3 per reservation. Linear part of the loop's CPU about 0.3 ms per member (fit of the table above after removing the L3-1 term). PostgreSQL round-trip cost not measured.

**Change.** Replace the two loops with chunked ordered locking reads:

```python
pid = self._backend_pid(conn)
token_ids = tuple(sorted(member.token_id for member in request.members))
if conn.dialect.name == "postgresql":
    for chunk in bind_budget_chunks(token_ids):
        locked = conn.execute(
            select(tokens_table.c.token_id)
            .where(tokens_table.c.token_id.in_(chunk))
            .order_by(tokens_table.c.token_id)
            .with_for_update(of=tokens_table)
        ).scalars().all()
        if len(locked) != len(chunk):
            missing = sorted(set(chunk) - set(locked))
            raise ValueError(f"sink effect member token {missing[0]!r} disappeared")
        self._after_token_lock_chunk(pid, tuple(chunk))
# existing chunked identity read + _validate_token_rows stays; it is the
# existence check on every dialect
```

and the same for states, selecting the five columns the validation at `:376-381` reads (`state_id, token_id, run_id, node_id, input_hash`), ordered by `state_id`, `FOR UPDATE OF node_states`, run on both dialects because the rows are consumed. The per-item seams become per-chunk seams; `pause_after_first_token` pauses after the first chunk, which still proves what the test proves (the reservation holds a token lock that an outcome writer then blocks on) because the test reserves fewer than 900 members.

Why the deterministic order is preserved: within a statement PostgreSQL locks rows as `LockRows` receives them from the sort, i.e. in `ORDER BY` order, and the keys are immutable primary keys; across statements every id in chunk k sorts before every id in chunk k+1. This is the same statement shape and the same global order (tokens by `token_id`, then node states by `state_id`) that `lock_token_outcome_dependencies` and `complete_node_states_completed_many` already take in finalization (`sink_effect_finalization.py:237-258`), so reservation and finalization keep one common acquisition order.

Rejected: reusing `lock_token_outcome_dependencies` directly (it returns nothing, so "disappeared" could not be reported; the reservation has no handle on the outcomes repository); one unchunked statement (breaks the bind budget, `tests/integration/pipeline/test_landscape_bind_budget.py` would refuse it); dropping the token locks on PostgreSQL because the states are locked (the token lock is what serialises against `record_token_outcome`, proven by the test at `:600`).

**Must not change.** Run row locked first (`:343-349`), then tokens ascending, then states ascending; the fresh re-read of current states under the locks (`:372-374`); each failure message class (`ValueError`); the leader fence as first statement (`:311-317`). Nothing recorded changes.

**Risk.** Medium. It is a PostgreSQL lock-acquisition change; the order argument is the one the codebase already relies on, but a mistake here is a deadlock that only PostgreSQL shows.

**Ruling needed.** No. The change applies the documented bind-budget rule and an existing statement shape; no ADR text changes.

**Verification.** Default suite files as L3-1 plus `tests/integration/pipeline/test_landscape_bind_budget.py`. Testcontainer owed: `tests/testcontainer/core/test_sink_effect_lock_order_postgres.py`, `tests/testcontainer/core/test_landscape_bind_budget_postgres.py`. New tests: (a) PostgreSQL, 2,000 members with the shared budget lowered to 100: record the statement sequence and assert the `FOR UPDATE` statements are 20 token statements in ascending id ranges followed by 20 state statements; negative control: reverse the chunk iteration in a test double and show the assertion fails. (b) two overlapping reservations plus a finalizer on interleaved member sets complete without `DeadlockDetected` (the existing `:490-491` test extended to more than one chunk). (c) a member token deleted between witness and lock still raises the "disappeared" error. Before/after: `tag_sql["reserve_lock_loop"]` falls from 2n+3 to 2·ceil(n/900)+3 on PostgreSQL and ceil(n/900)+3 on SQLite.

**Expected saving.** 2 statements per sink row on SQLite (4,012 → 9 at 2,000 rows), about 0.3 ms CPU per row (about 1% of gate CPU). On PostgreSQL 4 round trips per sink row become a handful per reservation; value unknown until M-1 measures it.

---

#### E-4 — Batch the sink-flush lineage loads

**Finding.** Prior review finding 4 / E-4; recon Appendix B § 8 ("3 read transactions per sink member").

**Mechanism (verified at 1a7ff0e6).** `core/landscape/execution/sink_effect_identity.py:236-306`. Per candidate the loop calls, through `_LineageQueryCache` (`:134-170`, memoises but never batches):

1. `query.get_token` (`:255`) → `core/landscape/query_repository.py:320-334`, which is TWO reads: the token select (`:328-329`) and `_load_lineage_paths([token_id])` (`:288-318`, `:332`);
2. `query.get_row` inside `_BoundedLineageResolver._walk` (`:198`) → `query_repository.py:158-171`;
3. `query.get_token_parents` (`:205`) → `query_repository.py:370-381`;
4. `get_tokens_by_ids(())` at `:223` issues nothing for a root token (`:149-150`); the second `get_row` at `:260` is a cache hit.

Each read goes through `ReadOnlyDatabaseOps` (`core/landscape/_database_ops.py:36-57`) and `LandscapeDB.read_only_connection` (`core/landscape/database.py:2229-2254`): its own transaction, `PRAGMA query_only = ON`, the select, `PRAGMA query_only = OFF`, commit.

The recon's "3 per member" is REFUTED: it is 4, because it did not count the lineage-frames read inside `get_token`. The prior review's "about 20 statements per sink row" is right for driver operations (16 cursor executes + 4 commits). Bulk loaders for three of the four reads already exist and are unused here: `get_tokens_by_ids` (`:345-368`, loads frames in bulk), `get_token_parents_for_tokens` (`:795-820`), and `ReadOnlyDatabaseOps.execute_fetchall_many` (`_database_ops.py:59-69`, several queries on one read snapshot). There is no bulk row loader by id.

**Evidence.** `tag_sql["resolve_members"]` / `tag_commit[...]`: 3,200 / 800 at 200 rows, 16,009 / 4,001 at 1,000, 32,000 / 8,000 at 2,000, 64,000 / 16,000 at 4,000 — 16.0 statements and 4.0 transactions per sink token, 20% of all statements in the run. CPU 3.67 s of 55.6 s at 2,000 rows (6.6% of the process, 35% of the sink flush). The run has two `resolve_members` calls (one per sink), so batching is per call, not per token.

**Change.** Prefetch, then run the existing per-candidate validation unchanged over the primed cache.

- `QueryRepository`: add `get_rows_by_ids(row_ids)` (chunked like `get_tokens_by_ids`) and `load_lineage_closure(token_ids, *, max_depth)` that, on ONE read-only connection, loads level by level: tokens (+ frames) for the frontier, their rows, their parent relations; the next frontier is the parent ids not yet loaded; stop when the frontier is empty or `max_depth` (`MAX_LINEAGE_DEPTH`) levels have been read. Each level is 4 statements per chunk.
- `_LineageQueryCache.prime(...)` stores the results, and stores `None` (tokens, rows) or `()` (parents) for every requested id that came back absent, so `_walk` and the loop raise exactly the errors they raise today (`:256-257`, `:199-200`, `:224-226`). Anything not primed falls through to the single-row reads, so correctness never depends on the prefetch being complete.
- `resolve_sink_effect_members` calls `prime` once with all candidate token ids. `_BoundedLineageResolver` and every bound and integrity check stay byte-for-byte.

Chunk size: use `bind_budget_chunks` (`BIND_BUDGET_PER_STATEMENT = 900`, `core/landscape/bind_budget.py:30`), one bind per id. That fits SQLite's historical `SQLITE_MAX_VARIABLE_NUMBER` of 999 (the venv's SQLite is 3.53.1, limit 32,766, but the bind-budget test lowers the ceiling deliberately) and PostgreSQL's 65,535 16-bit bind count. `QueryRepository._QUERY_CHUNK_SIZE = 500` (`:69`) is also safe; prefer the shared budget so the bind-budget suite covers the new reads. Memory: the prefetch holds one `Token`, one `Row` and the parent list per member, which the per-token path also ends up holding in the cache today.

Rejected: a recursive CTE for ancestors (two dialects, and it hides the depth/fan-in bounds inside SQL where the resolver's `_audit_error`s cannot name the offending token); loading with `get_all_tokens_for_run` (O(run), wrong for resumed partial flushes); removing the `PRAGMA query_only` wrapper here (that is E-10, separate).

**Must not change.** `lineage_json`, `lineage_hash`, `payload_hash`, `pending_identity_hash`, member ordinals and therefore `membership_or_manifest_hash` and `effect_id` (`:267-306`, `:339-400`) must be identical; all `MAX_LINEAGE_*` refusals; the cross-run and missing-row/missing-parent `AuditIntegrityError`s; reads stay on the enforced read-only connection.

**Risk.** Low–medium. Pure read-side; the hazard is a cache that answers "missing" where the database has the row, which the fall-through rule and the equality test below close.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_sink_effect_identity.py`, `tests/property/core/test_sink_effect_identity_properties.py`, `tests/unit/engine/test_sink_effect_executor.py`, `tests/integration/pipeline/test_landscape_bind_budget.py`. Testcontainer owed (new SQL on PostgreSQL): `tests/testcontainer/core/test_landscape_bind_budget_postgres.py`, `tests/testcontainer/core/test_sink_effect_lock_order_postgres.py` (it calls `resolve_sink_effect_members` at `:555`). New tests: (a) golden equality — for fork, expand, coalesce and root tokens, members resolved by the batched path equal members resolved with the prefetch disabled, field for field; negative control: corrupt one prefetched parent ordinal in the cache and show the equality test fails. (b) statement budget — 1,200 root members resolve in at most 8 statements and 1 transaction; negative control: the current code fails it at 19,200. (c) each integrity refusal (missing token, missing row, cross-run parent, depth, fan-in) still raises with a prefetch in place. Before/after: `tag_sql["resolve_members"]` and sink output digests on the gate and fork workloads.

**Expected saving.** 16 statements and 4 transactions per sink token (32,000 → about 12 statements at 2,000 rows); about 3.4 s of 55.6 s CPU on the gate workload (about 6%; the prior review estimated 4%). Overlaps E-1 and E-10.

---

#### L3-3 — Decide whether sinks stay deferred to the end of the run

**Finding.** Recon summary § 1 Spine, Appendix A Phase E and § 4, Appendix B § 0. Not in the prior review.

**Mechanism (verified at 1a7ff0e6).** Verified: `engine/orchestrator/leader_drain.py:300-313` calls `flush_and_write_sinks` once, after the source loop and the peer-lease wait (`:283-291`); the only other calls are the follower re-drain loop at `:349-361` (no-op with one worker). During the source loop every sink-bound result is appended to `loop_ctx.pending_tokens` (`engine/orchestrator/outcomes.py:98`, reached from `source_iteration.py:898`). `sink_flush.py:217-304` then issues one `SinkExecutor.write` per (sink, outcome, path, error_hash) group. Measured: 1 flush call per run, 2 writes on the gate workload (one per sink), each holding every token for that sink.

It is a deliberate design, not an accident:

- ADR-030 D2 (`docs/architecture/adr/030-multi-worker-deployment-shape.md:82-94`) lists "the sink-write phase" among five single-process roles owned by the leader and says "Distributing ingest, barriers, or sinks would each need its own ADR".
- The design note behind it (`docs/architecture/design-notes/option-c-multi-worker-coordination-design-2026-06-11.md` § D) makes "PENDING_SINK drained" step 4 of the leader's end-of-run sequence, after source EOF, upstream quiescence and the barrier flush loop.
- ADR-029 (`:75`, `:318`) removed the crash window in which flush outputs existed "only in `pending_tokens`": every queued token now has a durable `PENDING_SINK` journal row, so `pending_tokens` is a cache of the journal, not the truth.

Neither document argues that the sink phase must be a single pass; they establish who owns it and that it follows quiescence.

What an incremental flush runs into, all verified:

1. Effect identity is the whole member set. `effect_id` hashes the full `membership` list (`sink_effect_identity.py:360-376`). Flushing in chunks produces more effects with different ids, more `sink_effects`/`artifacts`/`operations` rows and a longer stream. The machinery supports it — reservation creates one effect per call for the unbound members and chains it to the stream tail (`sink_effect_reservation.py:505-553`) — but the recorded shape of a run changes.
2. Every extra effect has a large fixed cost. Measured with 41 effects on one 400-row run (`work/err_400_40/distinct.json`): about 30 commits and 330 statements per effect in `execute_effect`, 4 fsyncs, about 0.16 s CPU.
3. Replace-mode file sinks rebuild cumulatively. `plugins/sinks/_local_file_effects.py:795-824` (`continuation_emission`): a successor effect serialises every member of every predecessor again; the coordinator re-hydrates those members from the payload store (`engine/executors/sink_effects.py:1206-1240`). K effects over N rows cost O(K·N) hydrations and file bytes. Append mode re-streams and re-hashes the whole existing file per effect (`csv_sink.py:583-584`).
4. A stream is capped at 257 effects (L3-8): chunks would have to be at least N/257 rows.
5. Output row order. Within one effect members are ordered by (ingest_sequence, lineage, token_id) (`sink_effect_identity.py:290`); across effects the file is in effect order. Today that gives a file sorted by ingest sequence within each group. Incremental flushing gives flush order, which differs from today's whenever a later row finishes before an earlier one (retries, barriers, followers).
6. Checkpoint and resume semantics. Checkpoints are written only after sink durability (`checkpointing.py:139-146`); resume re-drives `PENDING_SINK` journal rows, never source rows (`core/checkpoint/recovery.py:728-735`). Incremental flushing is compatible with both, and would move checkpoints from an end-of-run burst to interleaved.

**Evidence.**

- Pending tokens: `pending_size.py` deep size at flush entry is 114,285 B for 200 tokens and 1,123,757 B for 2,000 — 562–571 B per token for this 5-field row, plus about 200 B for each row's `MappingProxyType` backing dict that `sys.getsizeof` does not see: about 0.8 kB per pending token. RSS at flush entry grows 0.36 kB per row between 200 and 4,000 rows (table above), consistent after allocator reuse.
- The memory that matters is the flush itself, not the queue. Peak RSS rises 28.8 MB across the flush at 4,000 rows (401,252 → 430,092 kB) and peak RSS overall grows 7.55 kB per row from 200 to 4,000 rows (5.4 kB per row between 1,000 and 2,000, 8.0 between 2,000 and 4,000). `SinkExecutor.write` materialises the whole group several times at once: `rows` (`sink.py:1557`), `canonical_rows` (`:808-813`), candidates (`:856-864`), members, finalization members with `{"row": dict(member.row)}` (`:891-903`), opened node states, durable member records.
- 10,000 rows was not run (4,000 took 190 s wall under contention; the brief caps runs at about 3 minutes).

Projection for this narrow row (about 75 bytes of data; real rows with LLM output are 10–100 times wider and the per-row figures scale with row width):

| Rows | `pending_tokens` | Peak RSS growth at 5.4–8.0 kB per row | Peak RSS (400 MB baseline) |
|---:|---:|---:|---:|
| 100,000 | about 80 MB | 0.54–0.80 GB | 0.9–1.2 GB |
| 1,000,000 | about 0.8 GB | 5.4–8.0 GB | 5.8–8.4 GB |

Time bites before memory does: at 100,000 rows in one sink group the L3-1 term alone is about 55 minutes, and a local-file effect is refused above 1 GiB or 10,000,000 rows (`_local_file_effects.py:42-43`).

**Change.** No code change is proposed without the ruling. Options to put to the maintainer:

- A. Keep the single end-of-run flush; fix what makes it expensive (L3-1, L3-2, E-4, L3-4, L3-8) and bound the transient memory by building `canonical_rows`/candidates/finalization members as one pass instead of five parallel lists. Recorded shape unchanged. This is my recommendation for now: after those fixes the flush is linear and its memory is the remaining issue, at about 1 GB per 100,000 narrow rows.
- B. Flush per sink when its pending count reaches a configured threshold, inside the source loop. Needs: an ADR under ADR-030 D2; a threshold floor of N/257 or removal of the chain cap; an incremental continuation for file sinks that does not re-serialise predecessors (which is its own audit question: today a replace-mode successor's content hash covers a full rebuild from durable members); a decision on output row order; a new public config key.
- C. Keep one effect per group but stream its members from the journal in chunks rather than holding `TokenInfo` objects (memory only; effect identity unchanged). Removes the 0.8 kB per token but not the flush-time materialisation.

**Must not change.** Under any option: a token's terminal outcome is recorded only after its sink effect is durable (`sink.py:1496-1500`); every sink-bound token has a `PENDING_SINK` journal row until then; an effect's membership is never rewritten to fit re-batching (`sink_effect_reservation.py:501-504`); a successor never overtakes an unfinalised predecessor; the leader alone writes sinks.

**Risk.** High for B (changes recorded effect/artifact shape, file row order, crash residue and a public config surface). Low for A.

**Ruling needed.** Yes. Question: "ADR-030 D2 makes the sink-write phase a leader role that follows end-of-input quiescence. Is one flush per run a requirement, or only the ownership? If a run may flush a sink before end of input: (1) is it acceptable that a run then records several sink effects, artifacts and `sink_write` operations per sink instead of one, with different `effect_id`s than today; (2) may the output file's row order become flush order rather than ingest order within the group; (3) what is the supported upper bound on rows per sink per run that option A must meet if the answer is no?" Touches ADR-030 (D2), ADR-029, ADR-026; a yes needs a new ADR.

**Verification.** For A: existing sink suites (`tests/unit/engine/test_sink_effect_executor.py`, `tests/unit/engine/orchestrator/test_pending_sink_grouping.py`, `tests/integration/pipeline/orchestrator/test_sink_diversion_counters.py`), plus a peak-RSS measurement at 2,000 and 4,000 rows before and after with the criterion "RSS growth across the flush per row falls". For B: not specified until the ruling.

**Expected saving.** A: unknown until measured; the target is the 7 kB per row of flush-time growth. B: bounds memory to the threshold, at a measured cost of about 0.16 s CPU and 30 commits per extra effect before any redesign of the file continuation.

---

#### L3-4 — Write one progress checkpoint per durable sink write, not one per token

**Finding.** Recon Appendix B § 1 "Per checkpoint" and § 5; Appendix A § 3 table. Not in the prior review (its E-9a is only the hash inside each checkpoint).

**Mechanism (verified at 1a7ff0e6).** Default cadence: `core/config.py:1939-1940` (`frequency: … = "every_row"`), mapped to `frequency=1` at `contracts/config/runtime.py:586` and `:610-611`. `SinkExecutor.write` calls `on_token_written(token)` for every accepted token after the effect is durable (`engine/executors/sink.py:1611-1616`); the checkpoint half of that callback (`engine/orchestrator/checkpointing.py:231-238`) calls `maybe_checkpoint`, which at `frequency == 1` and also at `frequency == 0` (`aggregation_only`, `:172-177`) writes a checkpoint every time (`:184-193`). `CheckpointManager.create_checkpoint` (`core/checkpoint/manager.py:101-192`) is one leader-fenced transaction.

What a checkpoint row holds: id, run id, sequence number, barrier scalars JSON, topology hash, format version (`manager.py:171-181`). No token, no row, no sink position. What resume reads: only the LATEST checkpoint (`core/checkpoint/recovery.py:711`, `engine/orchestrator/resume.py:874`), for (a) topology validation, (b) barrier scalars, (c) the sequence number to continue from (`resume.py:933`), (d) its id as the `resume_checkpoint_id` provenance marker on node states (`resume.py:983`). Which tokens still need a sink comes from the journal's `PENDING_SINK` rows and the sink-effect tables, not from checkpoints ("resume re-drives exactly this work (it never re-derives a source row)", `recovery.py:728-735`).

So within one end-of-run sink write the per-token checkpoints differ only in sequence number, id and timestamp: the topology hash is constant (measured, E-9a) and the barrier scalars cannot change while the leader is inside `SinkExecutor.write`. On success all of them are deleted (`run_lifecycle.py:653`, `manager.py:239-263`). `CheckpointSettings`' docstring ("every_n: … Lose up to N-1 rows on crash", `core/config.py:1930-1933`) and `docs/reference/configuration.md:3406-3408` describe a row-position model of checkpoints that the journal-first design (ADR-029) no longer has.

**Evidence.** `gateER_2000` (default cadence) against `gate_2000` (`every_n` 100), same input:

| | Checkpoints | Checkpoint statements | Checkpoint commits | `maybe_checkpoint` CPU | Run CPU | Sink-flush CPU |
|---|---:|---:|---:|---:|---:|---:|
| `every_n` 100 | 21 | 189 | 21 | 0.06 s | 55.6 s | 10.55 s |
| `every_row` (default) | 2,001 | 18,018 | 2,002 | 4.78 s | 60.6 s | 14.98 s |

That is 9.0 cursor statements (the recon's 8 plus the explicit `BEGIN`) and 1 commit per sink-written token, 2.39 ms CPU each; 7.9% of whole-run CPU and 32% of sink-flush CPU at the default cadence. Both recon claims are confirmed.

**Change.** Two parts.

1. (No ruling) E-9a, below.
2. (Ruling) Make the progress checkpoint a property of the durable sink write rather than of each token: `CheckpointProgressCallback.__call__` only advances the sequence counter; `flush()` (already called once per group at `sink_flush.py:319-320`, and currently a no-op, `checkpointing.py:240-243`) writes one checkpoint carrying the final sequence number and the current barrier scalars. `every_n` keeps its meaning as an upper bound on tokens between checkpoints inside one very large write. Sequence numbers still advance by one per token, so the duplicate-sequence guard and `rebase_sequence` are unaffected.

   Smaller alternative if the ruling is no: leave the cadence and change only the default to `every_n` with a default interval — but that changes a documented default for every existing settings file that omits the block, and still writes checkpoints that carry no extra information.

Rejected: dropping progress checkpoints entirely during the sink phase (resume needs a latest checkpoint whose barrier scalars reflect end-of-input; the run-start checkpoint has none); batching N checkpoint inserts into one transaction (still writes N rows that are deleted at success).

**Must not change.** A checkpoint is written only after sink durability; the run-start (sequence 0) and shutdown checkpoints (`checkpointing.py:99-131`, `:251-280`); the leader fence as the first statement of the checkpoint transaction (ADR-030 § C.4 row 5); monotonic, unique sequence numbers; the latest checkpoint always reflects the barrier scalars at the last durable sink write; `resume_checkpoint_id` still names an existing checkpoint at resume time.

**Risk.** Medium. The code change is small, but it changes what `every_row` means and how many `checkpoints` rows exist at any moment of a failed run (operators and `elspeth resume` preflight may display counts).

**Ruling needed.** Yes. Question: "A checkpoint row holds no token or row position; resume reads only the latest one for topology hash, barrier scalars, sequence and its id. Given that, may `every_row` (and `aggregation_only`) write one checkpoint per durable sink write group instead of one per token? Confirm that no consumer depends on the number of checkpoint rows or on a checkpoint per token (resume, `abandon`, the web run view, exports), and say whether the config values keep their names. Is this a 0.8.5+ config-compatibility matter in your reading (the accepted values and default are unchanged; the documented behaviour of each value changes)?" Touches ADR-029 (journal is resume truth) and ADR-030 § C.4 row 5; the docstring at `core/config.py:1930-1933` and `docs/reference/configuration.md:3394-3408` need correcting either way, because "lose up to N-1 rows on crash" is no longer what the cadence controls.

Resume consequence of changing the default cadence, stated plainly: none for which rows are re-driven or re-written (journal and effect tables decide that). The latest checkpoint's sequence number and id would be those of the last completed sink group rather than the last token; barrier scalars identical.

**Verification.** `tests/unit/core/checkpoint/test_manager.py`, `tests/unit/core/checkpoint/test_recovery.py`, `tests/integration/pipeline/orchestrator/test_orchestrator_checkpointing.py`, `tests/unit/engine/orchestrator/test_checkpoint_interrupted_edge.py`, `tests/unit/engine/orchestrator/test_checkpoint_leader_fence_guard.py`, `tests/property/core/test_checkpoint_properties.py`, the recovery e2e suites; testcontainer owed (`tests/testcontainer/core/test_checkpoint_fence_serialisation_postgres.py`, the resume crash matrices). New tests: (a) crash injected after the durable effect of group 1 and before group 2 — resume completes with identical sink digests and outcomes under the new cadence; negative control: delete the group-1 checkpoint before resume and show the resume point falls back to run-start scalars (the test must detect the difference in a barrier pipeline). (b) a barrier pipeline interrupted by shutdown restores the same latches as today. Before/after: `tag_sql["checkpoint"]` on `gateER_2000` (18,018 → about 27).

**Expected saving.** At the default cadence: 9 statements and 1 commit per sink-written token; 4.7 s of 60.6 s CPU (about 7.8%) on the gate workload at 2,000 rows. Zero for pipelines that already set `every_n`.

---

#### E-9a — Compute the topology hash once per run

**Finding.** Prior review finding 9 / E-9a; recon Appendix B § 5.

**Mechanism (verified at 1a7ff0e6).** `engine/orchestrator/checkpointing.py:75-89`: `_build_checkpoint_draft` calls `CheckpointCompatibilityValidator().compute_full_topology_hash(graph)` for every draft; that delegates (`core/checkpoint/compatibility.py:104-112`) to `core/canonical.py:231-…`, which hashes every node's config and the edge list. The graph is installed once per run (`leader_drain.py:137`, `resume.py:1252`) and cleared at the end (`:429`, `:1439`).

**Evidence.** `topology_hash` region: 2,001 calls, 0.68 s at 2,000 rows on a 4-node graph (0.34 ms per call, 1.1% of run CPU, 14% of checkpoint cost); the prior review measured 1.5% on the transform workload. `topo_distinct.py` on four workloads at default cadence (source-quarantine gate, fork/coalesce, transform, batch aggregation): 201 / 201 / 201 / 102 calls, 1 distinct hash and 1 graph object each.

**Change.** In `CheckpointCoordinator`: `set_active_graph` resets `self._active_topology_hash = None`; `_build_checkpoint_draft` computes it on first use and reuses it. To keep today's ability to notice a graph that changed under a run, `checkpoint_interrupted_progress` (once per run) recomputes and raises `OrchestrationInvariantError` if it differs from the memo. Rejected: memoising on the graph object (`ExecutionGraph` would need a cache slot and an invalidation story); `functools.lru_cache` keyed on the graph (identity-keyed caches on a mutable object, and it outlives the run).

**Must not change.** The value stored in `checkpoints.upstream_topology_hash`; that resume validates the current graph against it; the sequence-0 checkpoint is still computed from the graph actually being run.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `tests/unit/core/checkpoint/test_compatibility.py`, `tests/unit/core/checkpoint/test_manager.py`, `tests/integration/pipeline/orchestrator/test_orchestrator_checkpointing.py`. New test: a run of 50 rows at `every_row` calls `compute_full_topology_hash` exactly once on the run path and every checkpoint row carries the same hash; negative control: mutate a node config between two checkpoints through a test double and assert the shutdown-checkpoint guard raises. No testcontainer run owed (no SQL change).

**Expected saving.** 0.34 ms per checkpoint on a 4-node graph (more on larger graphs); 1.1–1.5% of CPU at the default cadence. Vanishes if L3-4 lands.

---

#### L3-5 — Validate the payload-store directory once per operation, or bind to a directory fd

**Finding.** Recon summary § 1 last bullet, Appendix B § 4, Appendix F § 6. The prior review looked at this and left it alone as "deliberate hardening of a filesystem trust boundary". I respect that: this item is a ruling request with the analysis attached, not a ready fix.

**Mechanism (verified at 1a7ff0e6).** `core/payload_store.py`. What each check defends against:

| Check | Where | Threat |
|---|---|---|
| Hash is 64 lowercase hex, `fullmatch` | `:202` | A caller-supplied or corrupted ref used as a path component (traversal, trailing newline) |
| `path.resolve()` and `base_path.resolve()` containment | `:210-214` | "Defense in depth" for the same traversal, through symlinks; two `realpath`s per operation |
| Base is not a symlink, is a directory, not group/world-writable; resolves under the pinned base | `_validate_store_directory`, `:69-85`, called for the base at `:114` | The base directory replaced by a symlink or loosened after construction; an ancestor turned into a symlink |
| Shard is not a symlink | `:115-118` | A planted `xx` symlink redirecting reads or writes outside the store |
| Shard validated, opened `O_DIRECTORY|O_NOFOLLOW`, then `lstat`/`fstat` device+inode compared and containment re-resolved | `_open_validated_directory`, `:87-110` | The shard swapped between validation and open (TOCTOU) |
| File opened/stat'ed/renamed/unlinked relative to the shard fd with `O_NOFOLLOW` | `:135`, `:150`, `:163`, `:262`, `:346` | The file being a symlink; operations escaping the validated directory |
| Temp file `O_EXCL`, fsync, `os.replace`, directory fsync | `:157-180`, `:261-264` | Partial blobs after a crash |
| Read back and re-hash on a dedup hit and on retrieve | `:239-248`, `:292-297` | Corrupt or tampered content |

The adversary is a local user who can write somewhere in the store tree, or a misconfigured mode. Ownership (`st_uid`) is not checked anywhere.

The redundancy: one dedup-hit `store()` performs five full `realpath`s (`:211`, `:212`, `:82` for the base, `:82` for the shard, `:103` after open), each `lstat`ing every path component, plus about six single `lstat`/`stat` calls. A new-blob `store()` runs `_open_payload_parent` twice (`:237`, `:256`), so eight. Cost is proportional to the DEPTH of `base_path`.

**Evidence.** Gate workload, 2,000 rows, base path 13 components deep: `_validate_store_directory` 19,753 calls (9.9 per row; recon 8); `os.lstat` 490,518 inside payload-store calls (245 per row; recon 129 at the 5-component default — the difference is path depth, which is the finding); `os.stat` 51,255. Payload-store CPU: `store` 2.73 s + `retrieve` 0.51 s of 55.6 s (5.8%, harness overhead included). Per row there are 3 `store` calls (source row; routing reason; sink member) and 1 `retrieve` (routing-reason read-back, `core/landscape/execution/node_states.py:1286-1289`).

**Change.** Two designs; both need the ruling.

- A (keeps path re-validation semantics; recommended). Validate once per operation, after the open, with one `realpath`:
  1. `lstat(base)` — not a symlink, is a directory, mode bits (as `:72-80`).
  2. `open(shard, O_RDONLY|O_DIRECTORY|O_NOFOLLOW)`; `fstat(fd)` for mode bits; `lstat(shard)` and compare device+inode with the fd (as `:97-102`).
  3. One `shard.resolve(strict=True)` and require it to equal `base_path / shard_name` exactly. Because `base_path` is stored resolved, equality holds only if no component — ancestor, base or shard — is a symlink, which subsumes the base containment check (`:82-84`), the shard containment checks (`:82-84`, `:103-105`) and both resolves in `_path_for_hash` (`:210-214`, whose file component is protected by `O_NOFOLLOW` at open).
  4. On a miss, `mkdir` then the same sequence once, instead of a second full `_open_payload_parent`.

  Every check category in the table survives; the pre-open validation is dropped because opening a directory read-only has no effect and the post-open validation is strictly later. About 1 `realpath` + 4 syscalls per operation instead of 5–8 `realpath`s.
- B (changes the threat model). Open the base once at construction and keep the fd; per operation `fstat(base_fd)` for mode bits, `openat(base_fd, shard, O_DIRECTORY|O_NOFOLLOW)`, `fstat(shard_fd)`, file operations as today. About 4 syscalls, independent of depth. Differences an operator would care about: the store follows the directory INODE opened at start, not the configured PATH. If the base path is renamed or replaced mid-run, today the next operation fails closed; B keeps writing into the original directory, which a later `resume` or export opening by path would not find. Adding one `lstat(base_path)` device+inode comparison per operation restores detection of that. B also gives the store an fd lifetime (no `close()` exists on the `PayloadStore` protocol), and it stops detecting an ancestor that became a symlink while still leading to the same inode (harmless).

Rejected: caching "validated at time T" for some interval (reintroduces exactly the TOCTOU the fd binding closes); skipping validation for hashes this process wrote (the directory can change independently of the blob).

**Must not change.** Fail-closed refusal of a symlinked or group/world-writable base or shard, of a shard swapped between validation and use, of traversal-shaped hashes; all file operations bound to the validated shard fd; atomic temp-file + fsync + rename + directory fsync; content verification on read and on dedup hit.

**Risk.** Medium for A (the tests pin the swap races and several patch individual steps; a reordering can pass tests while opening a window). High for B.

**Ruling needed.** Yes. Question: "The payload store re-validates the store path 5–8 times inside one operation. Is the guarantee you want (i) 'at the moment of each operation, the configured path resolves, without symlinks, to a directory we may trust, and the operation is bound to that directory' — which design A keeps with one resolution per operation — or (ii) the specific sequence of pre-open and post-open checks as written? And is binding to the directory inode opened at run start (design B) acceptable, given that a store path replaced mid-run would then be detected only by an added identity check rather than by re-resolution?" No ADR covers the payload-store threat model; the answer should be recorded where the tests in `tests/unit/core/test_payload_store.py` can cite it.

**Verification.** `tests/unit/core/test_payload_store.py` in full, in particular `test_store_rejects_shard_symlink_swap_after_validation` (`:611`), `test_store_rejects_shard_symlink_swap_before_idempotent_read` (`:684`), `test_store_binds_rename_to_validated_shard_directory` (`:713`), the `retrieve`/`exists`/`delete` swap tests (`:746-835`), `test_rejects_symlinked_base_directory` (`:641`), `test_rejects_group_or_world_writable_base_directory` (`:653`), `test_store_rejects_group_or_world_writable_shard_directory` (`:666`), `test_path_containment_after_resolution` (`:588`). New tests: an ANCESTOR of the base replaced by a symlink between two operations is refused (not covered today); a syscall budget — one dedup-hit `store` performs at most N `lstat` calls for a base path of depth d, with a negative control showing the current code exceeds it. Each swap test must be mutation-checked: remove the corresponding check from the new code and confirm the test goes red. Before/after: `lstat` per row and payload-store region CPU from `l3_measure.py` at two base-path depths.

**Expected saving.** A: `lstat` per row from 245 (depth 13) or about 129 (default depth) to about 45; payload-store CPU from 5.8% to roughly 1.5–2% of the gate run at depth 13 (less at the default depth; the prior review saw 1.4–3.3% in total). Unknown until measured.

---

#### L3-6 — Write the source-row blob before the ingest transaction opens

**Finding.** Recon Appendix B § 1 ("The payload-store write and four canonical serializations of the row happen inside this transaction, while the SQLite write lock is held") and § 4. Not in the prior review.

**Mechanism (verified at 1a7ff0e6).** `core/landscape/scheduler/queue.py:412-418` opens `fenced_leader_transaction` (`BEGIN IMMEDIATE` on SQLite) and calls `data_flow.insert_row_with_token_on`; that verifies the leader fence (`core/landscape/data_flow/tokens.py:456-458`) and calls `_prepare_source_row_record` (`:460-470`), which hashes the row (`:346-349`), serialises it again (`:359-362`) and calls `self._payload_store.store(payload_bytes)` (`:363`). A new blob is a temp-file write, `fsync(file)`, rename, `fsync(dir)` (`payload_store.py:171-174`, `:261-264`). The second ingest path at `queue.py:525-533` has the same shape. `expand_token` and `collect_tokens` also store inside their write transactions (recon Appendix B § 1; not re-read here — UNVERIFIED by me).

**Evidence.** `gate_2000`: 2,000 of 2,000 ingest-region `store` calls ran with a write transaction open (`ps_store_inside_open_tx@ingest`), all 2,000 wrote a new blob inside it, and 4,000 of the run's 4,014 fsyncs happened with a transaction open — 2 per source row. The lock is held: verified. How long each fsync holds it depends on the storage; on this box's filesystem it is too fast to say anything (the recon found no per-row fsync time either), so the lock-hold cost on a real disk is unmeasured.

What a crash leaves today: the blob is durable before the `rows` insert commits, so a crash between them leaves an unreferenced blob and no row. The audit DB never holds a `source_data_ref` to a blob that is not on disk.

If the blob write moves before the transaction: the same two states and no others. The blob is still durable before any row can reference it; a crash before commit still leaves an unreferenced blob. The one new case is a deposed leader: today the fence at `tokens.py:456` runs before the store, so a deposed leader writes nothing; moved earlier, it can write one content-addressed blob before the fence refuses the transaction. The multi-worker design note already classifies payload-store writes as unfenced by design — § C.4 row 11: "Payload-store writes | content addressing (existing) | sha256-addressed files; duplicate writes are byte-identical no-ops."

**Change.** Split `_prepare_source_row_record` into a pure preparation step (hash, payload bytes; no I/O, no clock) and the record build. In `ingest_row_with_initial_claim` (both call sites) run the preparation and `payload_store.store` BEFORE `fenced_leader_transaction`, and pass the resulting `data_hash` and `final_payload_ref` into `insert_row_with_token_on`. Keep `timestamp = now()` where it is, inside the transaction. The standalone `create_row_with_token` path keeps calling the combined helper. Rejected: making the blob write asynchronous (the "ref implies durable blob" invariant would then need a barrier before commit); dropping the directory fsync (changes crash durability).

**Must not change.** `rows.source_data_hash` and `source_data_ref` values; the quarantined-row repr fallback (`tokens.py:346-362`); "a committed row's ref names a durable, verified blob"; the leader fence as the first statement of the ingest transaction; the row timestamp is still sampled inside the transaction.

**Risk.** Low–medium: a mechanical move, on the ingest verb every row passes through.

**Ruling needed.** No, on the strength of design-note § C.4 row 11 — but say so in the change description, because a deposed leader can now leave one unreferenced blob it could not leave before. If the maintainer reads that as "what a crash leaves behind", treat it as a one-line confirmation.

**Verification.** `tests/unit/core/landscape/` ingest and token suites, `tests/integration/pipeline/test_source_replay_audit.py`, the resume crash matrices (`tests/testcontainer/core/test_quarantine_resume_crash_matrix_postgres.py`, `test_resume_mint_window_crash_postgres.py`) — testcontainer owed because the ingest transaction changes shape. New tests: (a) with a payload store whose `store` records whether a transaction is open on the Landscape engine, assert it is not during ingest; negative control: the current code fails it. (b) fault injection after the store and before commit leaves no `rows` row and a blob that a retry reuses. (c) a deposed leader's ingest is refused with zero DB mutation (existing fence test, extended to assert the DB only). Before/after: `fsync_inside_open_tx` from `l3_measure.py` (4,000 → 0 at 2,000 rows) and the write-transaction duration of the ingest verb on a real disk.

**Expected saving.** Removes 2 fsyncs and about 0.5 ms of payload-store CPU per source row from the time the SQLite write lock (or the PostgreSQL `run_coordination` row lock) is held. CPU total unchanged. Lock-hold reduction unknown until measured on real storage; it matters only when a second writer (heartbeat thread, followers) is waiting.

---

#### L3-8 — Stop minting one sink effect per distinct error hash

**Finding.** Recon Appendix E § B.2 could not say how many effects a sink receives per run ("engine lane; effect count per run UNVERIFIED") and Appendix A § 2 noted that "each ON_ERROR_ROUTED error hash is its own group". This item answers the question and reports a failure the recon did not find. Not in the prior review.

**Mechanism (verified at 1a7ff0e6).** Effects per sink per run = number of distinct `(outcome, path, error_hash)` groups routed to that sink, plus one per follower re-drain pass and per resumed partial flush:

- `engine/orchestrator/sink_flush.py:258-267` sorts and groups pending pairs by `(outcome, path, error_hash)`; each group is one `SinkExecutor.write` (`:304`).
- Each write reserves one new effect for its unbound members and chains it to the stream tail (`sink_effect_reservation.py:505-553`).
- For an `on_error`-routed token the error hash is `compute_error_hash(str(transform_result.reason))` (`engine/token_traversal.py:486-498`, `engine/scheduler_drain.py:222-228`). Many plugins keep reasons value-free (`plugins/transforms/type_coerce.py:65-78` says so explicitly), so normally a sink gets one or a few groups. The LLM transform does not: `plugins/transforms/llm/transform.py:390`, `:408`, `:639`, `:665`, `:749`, `:772`, `:877`, `:963` put `"error": str(e)` in the reason. Provider messages that carry a request id or a varying detail would give every failed row its own hash. Batch quarantine is distinct per row by construction: `engine/aggregation_result.py:67` hashes `f"quarantined_in_batch:{batch_id}:{index}"`.

Consequences of K groups on one sink, all verified in code:

1. Each successor effect rebuilds from all its predecessors: `engine/executors/sink_effects.py:1176-1240` walks the predecessor chain, loads every predecessor's members and re-hydrates each from the payload store (`_hydrate_member`, `:1366-1378`, a retrieve plus two canonical passes); replace-mode CSV then re-serialises the cumulative snapshot (`_local_file_effects.py:823-824`). Cost O(K²) members for one-row groups.
2. Hard failure: `sink_effects.py:1180-1181` raises `LandscapeRecordError("sink effect predecessor chain is cyclic or exceeds 256 effects")` when an effect has 256 or more predecessors (the same cap at `:463-464`, `:1944-1945`). The 258th effect of a stream cannot be executed. This happens in the end-of-run sink phase, after every transform — every LLM call — has already run.

**Evidence.**

- Common case, measured: gate workload 2 effects (one per sink) at every size; source-quarantine variant (`work/quar_200_20`, 20 invalid rows with distinct bad values) 3 effects — all 20 quarantined rows share one error hash; `on_error` variant with `type_coerce` (`work/err_200_20`) 2 effects.
- Row-specific reasons, SIMULATED: `L3_DISTINCT_ERRORS=1` makes the harness give every `TransformResult.error` a distinct `message` (it wraps the classmethod; the tree is untouched). This stands in for a plugin whose reason text varies per row; I did not run a real LLM failure, so whether real provider messages vary per row is UNVERIFIED (note `FailureInfo.message` passes through `scrub_text_for_audit`, `contracts/results.py:220`).
  - 400 rows, 40 failing (`work/err_400_40/distinct.json`): 41 sink writes and 41 effects; `_hydrate_member` 780 calls (= 40·39/2); sink-flush CPU 7.58 s against 1.14 s for the same shape with one error group; `execute_effect` 13,756 statements and 1,225 commits (about 330 and 30 per effect); 164 fsyncs in effects (4 each).
  - 600 rows, 300 failing (`work/err_600_300/distinct.log`): the run FAILS, exit code 4, with the `LandscapeRecordError` above raised from `_target_snapshot_members` after 258 effect executions; 32,896 hydrations (= 257·256/2) and 82.8 s of sink-flush CPU before the failure.

  Commands: `.venv/bin/python .claude/lanes/exec-remediation/L3/setup_err.py 600 300`, then `L3_DISTINCT_ERRORS=1 .venv/bin/python .claude/lanes/exec-remediation/L3/l3_measure.py <out.json> -- run --settings .claude/lanes/exec-remediation/L3/work/err_600_300/settings.yaml --execute`.

**Change.** Group by `(outcome, path)` only and carry the error hash per member. The per-member plumbing already exists: the member's `pending_identity` contains `error_hash` and is hashed into the membership (`sink.py:798-802`, `:860`; `sink_effect_identity.py:272`, `:325`), `SinkEffectFinalizationMember` has its own `error_hash` (`sink.py:900`), and `SinkEffectRoutingContext` is per member (`:803-807`). What is per-batch today is only the `pending_outcome` argument of `SinkExecutor.write` (`sink.py:1481`) and the grouping key. So:

- `sink_flush.py:258-263`: drop `error_hash` from `pending_sort_key`; pass a per-token `error_hash` map alongside `join_group_id_by_token` (which is exactly the precedent: the join group already "travels per token, not in the key").
- `SinkExecutor._write_primary_effect`: build `pending_identity`, routing context and the finalization member from the token's own error hash.
- `tests/unit/engine/orchestrator/test_pending_sink_grouping.py:178` (`test_quarantined_different_error_hashes_sort_separately`) pins the current key and has to change with it.

Rejected: an incremental content hash for the CSV rebuild (addresses a cost that disappears when there is one effect per group; and a SHA-256 midstate cannot be persisted from `hashlib` for resume); raising the 256 cap (keeps the O(K²) rebuild and the per-effect fixed cost); making plugins value-free by rule (worth doing for audit hygiene, but an engine failure mode should not depend on plugin message text).

**Must not change.** Each token's recorded `error_hash` on its outcome and member; that tokens with different outcome or path are never written under one pending outcome (the bug `test_pending_sink_grouping.py` exists for, elspeth-1569caa900); member ordering within an effect; one durable effect per write.

**Risk.** Medium. Engine sink path; changes how many effects, artifacts and `sink_write` operations a run with mixed error hashes records, and the row order of an error sink's file (today grouped by error hash, then ingest order; afterwards ingest order).

**Ruling needed.** Yes. Question: "A sink receiving tokens with K distinct error hashes gets K sink effects, each rebuilt from all predecessors, and the run fails with `LandscapeRecordError` at the 258th. May the sink phase group by (outcome, path) and carry `error_hash` per member, so such a run records one effect per sink per (outcome, path)? This changes the number and ids of effects/artifacts for runs with more than one error hash per sink, and the row order inside error-sink files. Was grouping by error hash chosen for a reason beyond 'write() takes one PendingOutcome'?" Touches ADR-019 (outcome/path model) and the sink-effect section of `docs/architecture/landscape.md`. If the answer is no, the fallback is to refuse at preflight or early in the flush with an actionable error when a sink has more than 257 groups, instead of failing mid-flush.

**Verification.** `tests/unit/engine/orchestrator/test_pending_sink_grouping.py`, `tests/unit/engine/test_sink_effect_executor.py`, `tests/unit/engine/test_sink_executor_diversion.py`, `tests/integration/pipeline/orchestrator/test_sink_diversion_counters.py`, `tests/integration/pipeline/test_csv_audit_context.py`, `tests/unit/engine/test_sink_effect_virtual_predecessor.py`; testcontainer owed (`tests/testcontainer/core/test_aggregation_error_route_postgres.py`, sink-effect suites). New tests: (a) a pipeline whose transform emits 300 distinct error reasons to one sink completes, with every token's `token_outcomes.error_hash` equal to its own reason hash; negative control: the current tree fails this test with the chain-cap error (reproduced above). (b) tokens with different outcomes still land in separate writes. Before/after: effect count and `hydrate_member` calls on `err_400_40` with `L3_DISTINCT_ERRORS=1` (41 effects / 780 hydrations → 2 / 0).

**Expected saving.** Common pipelines: none (already one effect per sink). Pipelines with K distinct error hashes on a sink: K−1 effects at about 0.16 s CPU, 30 commits, 330 statements and 4 fsyncs each, plus the O(K²) rebuild; and the run completes instead of failing above 257.

---

#### L3-9 — Serialise each sink member row once on the sink path

**Finding.** Recon Appendix A § 1 E6 and Appendix B § 3 "Per token at the sink — 5 canonical serializations of the row". Coordination: lane L2 owns row hashing in general; this item is only what is specific to `SinkExecutor`.

**Mechanism (verified at 1a7ff0e6).** Per sink-bound token the row is canonically serialised at:

1. `core/landscape/execution/node_states.py:493` — `stable_hash(input_data)` for the sink node state's `input_hash` (via `sink.py:634-645`);
2. `engine/executors/sink.py:810` — `canonical_json_loads(pipeline_canonical_json(row))`, the round trip the recon notes;
3. `sink_effect_identity.py:271` — `type_faithful_stable_hash(candidate.row)` → `payload_hash`;
4. `engine/executors/sink_effects.py:1360` — `canonical_json(deep_thaw(member.row)).encode()` for the payload store, checked against `payload_hash` at `:1363-1364`;
5. the finalization output hash over `{"row": …}` (built at `sink.py:894`).

The round trip at `:810` is not waste: it normalises pipeline values (numpy, pandas, Decimal, datetime) to the JSON-native form that `member.row` must have, because recovery requires `canonical_json(row).encode() == content` for the stored member payload (`sink_effects.py:1373-1378`). What is redundant is that (1), (3) and (4) all produce the bytes that (2) already produced: the reservation enforces `node_states.input_hash == member.payload_hash` (`sink_effect_reservation.py:380`, `:443-444`), and the payload store enforces `sha256(bytes) == payload_hash`.

**Evidence.** `bench_sink_canon.py`, 5-field gate row, CPU per call: (2) 19.7 µs, of which the encode is 14.6; (1) 15.8; (3) 15.6; (4) 12.1; (5) 18.3; one canonical encode plus SHA-256 is 11.4. Total about 81 µs per sink row, about 0.3% of the 27 ms per row. The script also asserts the equalities relied on below. Cost scales with row size, so it is larger for wide LLM rows; not measured.

**Change.** Keep the bytes from the encode at `sink.py:810`: carry `(canonical_row, canonical_bytes)` on `SinkEffectMemberCandidate`; `resolve_sink_effect_members` sets `payload_hash = sha256(canonical_bytes)`; `_persist_pipeline_member_payloads` stores the retained bytes instead of re-encoding. Leave (1) alone — an independently computed `input_hash` is what makes the reservation's state/member equality a real cross-check — and leave (5) to L2. Rejected: passing a precomputed hash into `begin_node_states_many` (turns the witness comparison into a tautology).

**Must not change.** `payload_hash`, stored payload bytes, `node_states.input_hash`, the finalization output hash; the `content_hash != member.payload_hash` refusal.

**Risk.** Low. Do it only together with whatever L2 decides about retaining canonical bytes on rows, so there is one mechanism.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_sink_effect_identity.py`, `tests/property/core/test_sink_effect_identity_properties.py`, `tests/unit/engine/test_sink_effect_executor.py`, `tests/unit/engine/test_sink_effect_spool_recovery.py`. New test: for rows containing numpy scalars, Decimal, datetime, large ints and integral floats, `payload_hash` and stored bytes are identical before and after; negative control: feed bytes from the non-normalising encoder and show the payload-store hash check refuses. No testcontainer run owed.

**Expected saving.** About 28 µs per sink row on the gate row (two of five serialisations), about 0.1% of CPU. Not worth doing on its own.

---

### 1. Item table

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| L3-1 | Stop rebuilding the locked-id tuple on every lock | Low | No | — | Removes 3.3e-7·n² s per reservation: 1.3 s per 2,000-member sink, about 17 s on the 10,000-row example, about 55 min at 100,000 members; 2n PostgreSQL round trips |
| L3-2 | Take the reservation witness locks in ordered chunks | Medium | No | — | 2 statements per sink row on SQLite, 4 round trips per sink row on PostgreSQL; about 1% CPU on SQLite |
| E-4 | Batch the sink-flush lineage loads | Low–medium | No | E-4 | 16 statements and 4 transactions per sink token; about 6% of gate CPU |
| L3-3 | Decide whether sinks stay deferred to end of run | High (if changed) | Yes | — | Memory: about 7 kB of peak RSS per narrow row today; saving unknown until measured |
| L3-4 | One progress checkpoint per durable sink write | Medium | Yes | — | 9 statements and 1 commit per sink token at the default cadence; about 7.8% of CPU |
| E-9a | Compute the topology hash once per run | Low | No | E-9a | 1.1–1.5% of CPU at the default cadence |
| L3-5 | Validate the payload-store directory once per operation | Medium (A) / High (B) | Yes | (prior "left alone") | `lstat` per row 129–245 → about 45; up to about 4% of CPU, unknown until measured |
| L3-6 | Write the source-row blob before the ingest transaction | Low–medium | No (state it) | — | 2 fsyncs per row leave the write-lock hold; CPU unchanged |
| L3-8 | Stop minting one sink effect per distinct error hash | Medium | Yes | — | Runs with K error hashes per sink: K−1 effects at about 0.16 s each and an O(K²) rebuild; run no longer fails above 257 |
| L3-9 | Serialise each sink member row once | Low | No | — | About 0.1% of CPU |

(L3-7 is in section 3; there is no L3-7 remediation.)

### 2. Recommended order and dependencies

1. **L3-1** — independent, smallest, removes the only super-linear term on the common path.
2. **E-4** — independent of everything in this lane; largest statement reduction in the sink phase.
3. **E-9a** — independent; skip it if L3-4 is approved first, since it then saves almost nothing.
4. **L3-2** — after L3-1 (same function; L3-2 replaces the per-item seams L3-1 retyped). Owes the testcontainer suite; batch it with E-4's testcontainer run.
5. **L3-6** — independent; coordinate with whichever lane owns the ingest transaction and the fences (R-2a touches the same verb).
6. Rulings, raised together because they interact: **L3-8** first (it is a failure, not only a cost, and its answer decides whether item 6 of the brief needs any further work), then **L3-4**, **L3-5**, **L3-3**. L3-3 should be decided after L3-1, L3-2, E-4, L3-4 and L3-8 are in and re-measured, because they remove most of what makes the single flush expensive.
7. **L3-9** — only alongside L2's row-hashing change.

Re-measure with `run.sh gate_2000`, `run.sh gate_4000` and `run.sh gateER_2000` after steps 1–4; the acceptance figures are `reserve_lock_loop` CPU linear in rows, `resolve_members` statements about 12 per call, and identical sink output digests.

### 3. Examined and recommended NOT fixing

- **L3-7 — dedup hit re-reads and re-hashes the existing blob** (`core/payload_store.py:237-248`). Verified: 3,998 of 6,001 `store` calls at 2,000 gate rows are dedup hits (every sink member payload equals its source-row payload in a pass-through pipeline; 1,998 routing reasons repeat two values), re-reading 293 kB in total. It is an integrity check and should stay: `store()` is the moment a new audit reference to that hash is created, and without the read a corrupt existing file would be accepted as the content just "stored" (`test_store_detects_corrupted_existing_file`, `tests/unit/core/test_payload_store.py:130`). For small blobs the read and hash are a few microseconds; the cost of a dedup hit is the directory validation (L3-5), not the re-read. A per-process "already verified" memo would stop detecting corruption that happens mid-run and saves little.
- **Routing-reason read-back** (`core/landscape/execution/node_states.py:1286-1289`): `store()` then `retrieve()` and a byte comparison, so a repeated reason is read and hashed twice. Deliberate ("Durably store and verify reason bytes before an event can refer to them"). Its cost also falls with L3-5. Leave.
- **CSV / local-file effect rebuild** (`plugins/sinks/csv_sink.py:499-652`, `plugins/sinks/_local_file_effects.py:465-530`, `:714-758`), recon rank 7 — DOWNGRADE. A sink gets one effect per run in the common case, so "O(K × final size)" is O(final size). Measured per effect: 7 `_snapshot` calls, the output hashed 5 times (4 snapshots + the staging digest: 346,220 snapshot bytes for 86,555 committed at 2,000 rows), 4 fsyncs, about 27 ms CPU for a 2,000-row file. The repeated snapshots are the compare-and-swap evidence around the atomic replace (`_local_file_effects.py:724-756`); trimming them changes crash-reconciliation evidence for a saving of milliseconds per run. The per-row `StringIO` + `DictWriter` (`csv_sink.py:605-606`) is part of that 27 ms. If L3-8 is refused, this item comes back, because K effects then cost K rebuilds.
- **`consume_group` list rebuild** (`engine/orchestrator/sink_flush.py:212-215`, `:321`): O(pending) per group, so O(K·N) only when a sink has many groups; that case is L3-8.
- **`_merge_batch_contract`** (`engine/executors/sink.py:596-600`): one merge per token, 0.047 s at 2,000 rows (0.08%). Leave, or let the contract-hashing lane short-circuit identical contract objects.
- **Batching the per-token `on_token_written` terminalisation**: already batched by 64 (`sink_flush.py:76-84`), 1.16 statements per row.

### 4. Not verified

- PostgreSQL behaviour of everything here. Lock-order reasoning for L3-2 and the 2n `pg_backend_pid` queries in L3-1 are static; no testcontainer run was made (Docker not attempted in this lane).
- 10,000 rows (brief asked "if it fits in about 3 minutes"; 4,000 took 190 s wall under contention). The 100k and 1M projections are extrapolations from 200–4,000 rows and from the isolated micro-benchmark up to 20,000 members.
- Whether real LLM provider error text varies per row after `scrub_text_for_audit`; L3-8's many-group runs use a harness that makes error reasons distinct. The chain-cap failure itself is real code reached by a real run.
- fsync latency and therefore the lock-hold time in L3-6 on production storage.
- `expand_token` / `collect_tokens` payload writes inside their transactions (recon Appendix B § 1) were not re-read.
- JSON, text, database and remote-object sinks: not read. The effect-count and cumulative-rebuild findings are engine-side and apply to any replacing-target sink; the per-effect cost figures are CSV only.
- The follower re-drain and resume paths create additional effects per sink; not measured (single process, no resume run constructed).
- Per-token peak memory for wide rows; only the 5-field gate row was measured.
- `l3_measure.py`'s "transaction open" counter is process-wide; it is exact for the ingest region and off by a few elsewhere.

### Where this report contradicts the recon or the prior review

| Source | Claim | What I found |
|---|---|---|
| Prior review, Baseline | "nothing measured here is super-linear in run size" | The reservation lock loop is quadratic per sink group (L3-1); on that review's own 10,000-row run it is about 17 s inside "sink flush". |
| Recon B § 8, B § 1 | 3 read transactions per sink member | 4: `get_token` also reads lineage frames (`query_repository.py:332`). 16 statements, 4 commits per token measured. |
| Recon summary, F § 6 | Quadratic term "about 1% of time at 2,000 rows" | The term alone is about 1.3 s per 2,000-member sink; the lock loop it sits in is 3.1% of CPU at 2,000 rows and 5.0% at 4,000 (two sinks). The recon also missed the per-iteration `pg_backend_pid` query on PostgreSQL. |
| Recon B § 1, § 5 | 8 statements per checkpoint | 9 cursor statements (explicit `BEGIN`) + 1 commit; otherwise confirmed, including that `aggregation_only` checkpoints every token. |
| Recon summary, F § 6 | 8 directory validations and about 129 `lstat` per row | 9.9 validations per row; `lstat` count is proportional to the depth of `base_path` (245 per row at depth 13). |
| Recon E § B.2 | Effects per sink per run UNVERIFIED; cost "O(K × final size)" | K = distinct (outcome, path, error_hash) groups; 1 in the common case. With many groups the cost is O(K²) members re-hydrated and the run fails at the 258th effect (L3-8). |
| Recon E § B.2 | "2 file fsyncs and at least 2 directory fsyncs" per effect | Confirmed: 4 fsyncs per effect. Additionally the output is SHA-256'd 5 times per effect. |
| Prior review E-9a | 1.5% on the transform workload | Confirmed (1.1% on gate at default cadence). But the checkpoint it sits in is 7.8% at the default cadence, and the rows it writes carry no per-token information (L3-4). |
| Recon A § 4 | "memory grows with total tokens" via `pending_tokens` | True but small (about 0.8 kB per token). The peak is the flush's own materialisation, about 7 kB per narrow row (L3-3). |

## Appendix L4 — Engine control flow, startup and the performance test harness

Lane L4 of the executor remediation plan. Tree: `bf8adf06` (docs-only on
`1a7ff0e6`), CPython 3.13, file-backed SQLite, one process unless stated.
Measured on a 4-core box shared with other lanes: **counts and CPU time are
the evidence; wall-clock figures are indicative.** Nothing in the tracked tree
was changed. Scratch, scripts, logs and prototypes are under
`.claude/lanes/exec-remediation/L4/` (paths below are relative to that
directory unless they start with `src/`, `tests/` or `docs/`).

Out of scope here (other lanes): Landscape statement prebuilding, fences,
read-backs, row hashing, payload store, checkpoints, sink flush, plugin
internals.

### Where this report contradicts the recon or the prior review

1. **"Fork/coalesce is super-linear" (recon § 1, Appendix F § 2) is not
   reproduced.** At four sizes, in two independent series, marginal CPU per
   row is flat (109.2 / 110.0 / 110.3 ms per row across 100→200→400→800).
   Every per-row SQL statement count is constant, and every Python function's
   call count is linear in rows except two generator expressions in the sink
   reservation (already reported by the recon, owned by the sink lane). The
   recon's 102 → 136 ms/row came from three single wall-clock runs.
2. **The prior review's "nothing measured here is super-linear" is also not
   quite right.** One statement is quadratic: the run-end ADR-019 I1a sweep
   (prior E-11). It is located and root-caused below. It is 0.2% of an 800-row
   fork run, so it explains none of item 1, but it grows as rows².
3. **E-11 is not a missing index.** `ix_token_parents_parent` exists. SQLite
   picks a join order that scans every outcome of the run for each fork
   parent. A query rewrite fixes it with no schema change.
4. **The stale benchmark is wider than `register_node(run_id=…)`.** mypy
   reports 16 errors in `test_db_write.py` against six changed verbs, and the
   whole `stress` tier is broken the same way (23 of 30 tests), which neither
   document recorded.
5. **Retry wrapper and per-node loop (recon Appendix A top-10 items 6 and 7)
   are not worth fixing.** Measured at 11.7 µs per token×node and under 0.4%
   of profiled time respectively.
6. **`git log -S` cannot name the commit that changed `register_node`:** this
   checkout is a shallow clone, and the drift predates its boundary.
7. **Prior review H-1 points the harness at `notes/`, which is gitignored**
   (`.gitignore:35`). Use `scripts/perf/`.

---

### Part 1 — Fork/coalesce scaling

#### Per-size table

Workload: the recon's `fork` workload (`examples/fork_coalesce` settings,
generated input; `setup.py`). Series 1 ran under a per-statement SQL listener
(`sqlcount.py`, log `grow_fork.txt`); series 2 under a lighter listener that
also times commits and GC (`phases.py`). All eight runs exited 0 with the
expected row counts.

| Rows | Statements / row | Commits / row | Rollbacks / row | CPU s (series 1) | CPU s (series 2) | GC s (series 2) | Peak RSS MB | Run-end I1a sweep, ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 402.5 | 53.80 | 8.22 | 16.2 | 14.6 | 0.80 | 391 | 2.8 |
| 200 | 398.0 | 53.42 | 8.12 | 27.9 | 25.5 | 1.22 | 394 | 16.8 |
| 400 | 395.7 | 53.23 | 8.07 | 53.3 | 47.5 | 1.49 | 396 | 49.6 |
| 800 | 394.6 | 53.14 | 8.04 | 95.1 | 91.6 | 1.85 | 402 | 224.7 |

| Segment | Marginal statements / row | Marginal commits / row | Marginal CPU ms / row, series 1 | Marginal CPU ms / row, series 2 |
|---|---:|---:|---:|---:|
| 100 → 200 | 393.53 | 53.05 | 116.5 | 109.2 |
| 200 → 400 | 393.42 | 53.04 | 127.3 | 110.0 |
| 400 → 800 | 393.43 | 53.04 | 104.4 | 110.3 |

Reading:

- Marginal statement and commit counts are constant to two decimals. No
  statement's count per row grows (`grow.py`: zero statements with count/row
  up more than 10% between 100 and 800 rows).
- Series 1 moves ±10% with no trend (its middle segment ran while another
  lane started a profiled run). Series 2 is flat to 1%.
- GC is 1–2 s per run and does not scale with rows (four gen-2 collections at
  every size; `gc.get_objects()` at exit is 536,5xx at every size). RSS grows
  15 KB per row. Heap growth is not a cause.
- Only one statement's time per call grows by 1.5× or more with meaningful
  total time: the I1a sweep (last column; roughly ×4 per doubling).
- Per-call time of ordinary statements drifts up by 10–30% between 100 and
  400 rows and back down at 800 (for example `INSERT INTO scheduler_events`
  68 → 78 → 90 → 86 µs). That is what the recon's cProfile segments also show
  (sqlite `execute` 15.3 → 17.4 ms/row with identical call counts).
- In-Python growth, from the recon's own cProfile dumps at 10/200/600 rows
  (`prof_grow.py`, output `prof_grow_fork.txt`): the only functions whose
  marginal calls per row grow are `sink_effect_reservation.py:358` and `:370`
  `<genexpr>` (106.5 → 401.5 calls/row). `coalesce_executor.py`,
  `barrier_coordination.py` and `row_union_executor.py` contribute none:
  `_pending` holds only in-flight groups, `_completed_keys` is an
  `OrderedDict` bounded at 10,000 with O(1) membership
  (`src/elspeth/engine/coalesce_executor.py:553`, `:790-796`), and the
  per-pass key sets in `run_intake_pass` are graph-sized.

#### E-11 — Rewrite the run-end I1a orphan sweep so it drives from `token_parents`

**Finding.** Prior review E-11: one run-end query took 0.58 s at 1,000 fork
rows and its site was not located. Recon § 1 reports fork/coalesce as
super-linear. This query is the only super-linear SQL in the workload.

**Mechanism (verified at 1a7ff0e6).**
`OutcomeRepository.find_orphaned_transient_parents`
(`src/elspeth/core/landscape/data_flow/outcomes.py:762-790`) is called by
`sweep_deferred_invariants_or_crash` (`:810-812`), reached once per run from
`src/elspeth/engine/orchestrator/leader_drain.py:383` (and from
`resume.py:1048`, `:1419`). The correlated `NOT EXISTS` joins `token_parents`
to an aliased `token_outcomes` with `child_outcomes.run_id == run_id` in the
`ON` clause. `EXPLAIN QUERY PLAN` on the produced DB:

```
SEARCH token_outcomes USING INDEX ix_token_outcomes_run_token (run_id=?)
CORRELATED SCALAR SUBQUERY 1
  SEARCH child_outcomes USING INDEX ix_token_outcomes_run_token (run_id=?)
  SEARCH token_parents USING INDEX sqlite_autoindex_token_parents_1 (token_id=? AND parent_token_id=?)
```

For each fork-parent outcome, SQLite walks **every** outcome of the run and
probes `token_parents` by primary key. With P parents and N outcomes that is
P × N probes. The index that would serve the intended order,
`ix_token_parents_parent (parent_token_id)`, exists and is not chosen: the
database has no `sqlite_stat1`, so the planner rates `run_id = ?` as selective,
and in a one-run audit DB it selects every row.

**Evidence.** `python sqlcount.py … -- run --settings work/fork_<n>/settings.yaml --execute`:
2.8 / 16.8 / 49.6 / 224.7 ms at 100 / 200 / 400 / 800 rows, about
0.35 µs × rows². `explain.py` prints the plan above at 100 and 400 rows.
`whatif_sweep.py` runs the as-issued and rewritten forms on an in-memory copy
of each produced DB:

| Fork rows | As issued | Rewritten | Result sets equal | Injected orphan reported by both |
|---:|---:|---:|---|---|
| 100 | 2.47 ms | 0.16 ms | yes | yes |
| 400 | 46.50 ms | 0.80 ms | yes | yes |
| 800 | 214.73 ms | 2.35 ms | yes | yes |

Rewritten plan: `SEARCH token_parents USING INDEX ix_token_parents_parent
(parent_token_id=?)`, then `SEARCH child_outcomes EXISTS USING COVERING INDEX
ix_token_outcomes_run_token (run_id=? AND token_id=?)`.

**Change.** In `find_orphaned_transient_parents` only, replace the join inside
the subquery with a nested `EXISTS`, which leaves the planner one table per
level:

```python
child_outcomes = token_outcomes_table.alias("child_outcomes")
child_has_outcome = (
    select(literal(1))
    .where(child_outcomes.c.token_id == token_parents_table.c.token_id)
    .where(child_outcomes.c.run_id == run_id)
    .exists()
)
child_witness = (
    select(literal(1))
    .select_from(token_parents_table)
    .where(token_parents_table.c.parent_token_id == token_outcomes_table.c.token_id)
    .where(token_parents_table.c.run_id == run_id)
    .where(child_has_outcome)
)
```

The outer query is unchanged. Rejected: a new index (none is missing, and it
would be a schema change: migration, testcontainer, compatibility); `ANALYZE`
at run end (writes planner tables into the audit DB and still depends on the
planner); `CROSS JOIN` / `INDEXED BY` (SQLite-only syntax); loading both tables
and joining in Python (moves an integrity predicate out of SQL).

**Must not change.** The predicate: a `fork_parent` / `expand_parent`
`TRANSIENT` outcome is an orphan iff no `token_parents` row for it in this run
has a child with any `token_outcomes` row in this run. Same rows, same
`AuditIntegrityError` text, still read-only, still run at the same point.

**Risk.** Low. One read-only query; logically equivalent form.

**Ruling needed.** No.

**Verification.** Existing: `tests/integration/test_adr_019_cross_table_invariants.py`,
`tests/integration/test_adr_019_sweep_durability.py`,
`tests/unit/core/landscape/test_data_flow_repository.py`,
`tests/integration/pipeline/test_unsettled_group_end_of_run_backstop.py`. New:
a scaling test that builds fork-shaped rows at two sizes and counts SQLite VM
steps for this query through `sqlite3.Connection.set_progress_handler`; the
ratio for 2× rows must stay under 2.5 (today it is about 4). Negative control:
the same test run against the current query text must fail. The PostgreSQL
testcontainer suite is owed (Landscape SQL shape changed, and the query also
runs there).

**Expected saving.** 225 ms → about 2 ms at 800 fork rows (measured on a
copy). Extrapolated from the quadratic fit, not measured: about 35 s at
10,000 fork rows and about an hour at 100,000. Zero for pipelines with no
fork or expand parents.

---

### Part 2 — Barrier intake

#### E-9c — Skip the intake reads when this leader has produced nothing to adopt

**Finding.** Recon Appendix A § 3 and top-10 item 10; prior review finding 9
and E-9c ("mechanism not yet read in full"). Once any barrier is configured,
every drain iteration polls the journal.

**Mechanism (verified at 1a7ff0e6).** `drain_claims` calls
`_run_barrier_intake_pass` at the top of every loop iteration
(`src/elspeth/engine/scheduler_drain.py:514`). `run_intake_pass`
(`src/elspeth/engine/barrier_coordination.py:419`) returns early only when no
aggregation, coalesce, row-union or collector exists (`:445-451`). Otherwise:

- `:457` `list_pending_blocked_barrier_items` — a full-row `SELECT` of
  `token_work_items` filtered to `barrier_adopted_epoch IS NULL`, on its own
  `engine.connect()` (`src/elspeth/core/landscape/scheduler/barrier.py:1063-1094`),
  so each call is `BEGIN`, `SELECT`, `ROLLBACK`.
- `:486` → `_replay_group_losses` → `:1225` `list_unadopted_group_losses`, when
  a coalesce, row-union or collector exists
  (`src/elspeth/core/landscape/scheduler/group_losses.py:249-270`); again its
  own connection.
- `:488` → `:508` `pending_empty_expansion_groups`, only with `require_all`
  collectors.

A blocked claim always forces another iteration (`scheduler_drain.py:748`,
`:760`). For one fork/coalesce source row the loop runs four times: source
claim, branch A (blocks), branch B (blocks), merged continuation. Passes 1
and 2 can find nothing at N=1; passes 3 and 4 adopt A and B. The group-loss
read finds nothing on any of the four.

**Evidence.** 800-row fork run (`work/fork_800/sql.json`): exactly 4.00
`list_pending_blocked_barrier_items` and 4.00 `list_unadopted_group_losses`
per source row, and 8.04 rollbacks per row (the count-gate prototype pins
8 rollbacks per row on its fork shape and 0 on the linear one). Together
4.2% of cursor time. Recon cProfile, fork: `run_intake_pass` 55.2 ms/row
cumulative, of which `_adopt_coalesce_row` 48.5 — so about 6.7 of 201 profiled
ms/row (3.3%) is the polling.

**Change.** Give `BarrierIntakeCoordinator` a "nothing changed" witness and
skip the reads when it holds.

- Two flags on the coordinator, both initially set: `_arrivals_dirty` and
  `_losses_dirty`.
- `_arrivals_dirty` is set by the drain whenever it marks a claim BLOCKED
  (`scheduler_drain.py:1019 _mark_claimed_scheduler_work_blocked`), by restore
  (`restore_from_journal`, including `reset_adoption_marker_to_pending`), and
  at coordinator construction.
- `_losses_dirty` is set whenever this process commits a group loss: any
  disposition or barrier verb called with a non-empty `group_losses=` (18 call
  sites under `src/elspeth/engine/`), `_stage_pending_escalations`, the
  empty-aggregation flush, and restore.
- A pass reads pending arrivals only if `_arrivals_dirty`, and clears the flag
  after a read that returned every row it then adopted. It reads unadopted
  losses only if `_losses_dirty`, likewise.
- Peers: a follower can block a token or record a loss without this process
  knowing. So the skip is allowed only in *solo mode*. Solo mode is
  re-established by the maintenance pass the leader already runs every
  `SCHEDULER_MAINTENANCE_INTERVAL` (64) drains (`scheduler_drain.py:349`,
  `:391 dead_non_leader_workers`): extend that read to report whether any
  non-leader worker is registered active. If one is, both flags stay set until
  a later maintenance pass sees none. Independently, force a full pass every
  64 iterations, on every `recover_pending_sinks` drain, and on every call
  from the end-of-input loop (`run_barrier_intake`, `processor.py:5186`).

Rejected: running both reads on one shared connection (halves the
`BEGIN`/`ROLLBACK` pairs but keeps every statement; worth doing only if the
ruling below is "no"); folding adoption back into the claim (reverses ADR-029
§ E.2); a database trigger or counter column (schema change for a poll).

**Must not change.** At N=1: the same rows are adopted in the same order
(`barrier_key, ingest_sequence, work_item_id`), with the same backdated
arrival instants, the same trigger evaluation after each adoption, hence the
same batch and merge composition and the same recorded rows. Journal-first
stays: adoption is still a fenced CAS on the durable row. Crash recovery is
untouched, because a skipped read writes nothing. With followers: a follower's
BLOCKED row or loss may be adopted up to 64 leader iterations later than
today, never lost (the end-of-input loop still runs until no BLOCKED row
remains).

**Risk.** Medium. The dirty-flag set must be complete; a missed setter is a
stalled group that only the forced pass or the EOF loop rescues.

**Ruling needed.** Yes — ADR-029 § E.2 specifies "per-iteration intake". Exact
question: *may the leader skip an intake read on an iteration where it has
itself blocked no token and committed no group loss since its last read, given
a forced full pass at least every 64 iterations and at end of input, and given
that with a follower attached the skip is disabled?*

**Verification.** Existing: `tests/unit/engine/` barrier and drain suites,
`tests/integration/pipeline/` aggregation, coalesce, row-union and collector
suites, `tests/e2e/recovery/` (resume with BLOCKED rows), the multi-worker
examples. New: (a) the count gate in Part 6 pins rollbacks per row on the fork
shape at 8 today; the fix must move the pin to the new exact value, and
reverting the skip must turn it red; (b) a unit test per setter that mutates it
out and shows a blocked arrival is not adopted until the forced pass; (c) a
follower test where a peer blocks a token mid-run and the leader adopts it
without waiting for EOF. Testcontainer suite: owed only if the maintenance
read's SQL changes.

**Expected saving.** Fork shape: 6 of 8 intake reads per row and their 6
`BEGIN`/`ROLLBACK` pairs; about 2.5% of fork CPU from the cProfile share
above. Aggregation shape: 1 of 2 reads per row; unknown until measured.

#### L4-1 — Do not probe the Landscape for a coalesce group this leader holds open

**Finding.** New. `CoalesceExecutor.accept` runs a completion probe against
the Landscape for every arriving branch token, including the second arrival of
a group it holds pending in memory.

**Mechanism (verified at 1a7ff0e6).**
`src/elspeth/engine/coalesce_executor.py:875`: `if key in self._completed_keys
or self._check_landscape_for_completion(…)`. On the normal path the key is
never in `_completed_keys` at arrival, so `_check_landscape_for_completion`
(`:746-774`) runs `has_completed_group_for_node`
(`src/elspeth/core/landscape/scheduler/restore_read_model.py:484`) through the
read-only wrapper, once per arriving branch. The probe exists to catch late
arrivals after FIFO eviction from `_completed_keys`. But every removal from
`_pending` is paired with `_mark_completed` on the next line (`:1154-1155`,
`:1324-1325`, `:1397-1398`), so a key present in `_pending` cannot be a closed
group in this process.

**Evidence.** 800-row fork run: 2.00 probes per source row, 97 µs each, plus
the wrapper's `BEGIN`, two `PRAGMA query_only` and `COMMIT` per probe.
Plan is index-driven and per-call time is flat with run size (80–101 µs).

**Change.** In `accept`, decide lateness in this order:

```python
if key in self._completed_keys:            late = True
elif key in self._pending:                 late = False   # open in this leader's memory
elif self._completed_keys_exact:           late = False   # nothing evicted, nothing restored
else:                                      late = self._check_landscape_for_completion(...)
```

`_completed_keys_exact` starts true, and becomes false permanently on the
first FIFO eviction in `_mark_completed` (`:794-796`) and on any journal
restore (`:717`, `:1598`). Apply the same shape to the two other probe sites
(`:1618`, `:1667`) and to `row_union_executor.py` if it has the equivalent
probe (not read). Rejected: raising `max_completed_keys` (memory for a cache);
dropping the probe (reopens the eviction window).

**Must not change.** Late-arrival detection after eviction and after resume;
the recorded failure reason (`late_arrival_after_merge` versus
`scope_group_failed`); no change to any written row.

**Risk.** Low. Two in-memory implications, each backed by a pairing visible in
the file.

**Ruling needed.** No.

**Verification.** Existing: `tests/unit/engine/` coalesce executor suite,
`tests/integration/pipeline/` coalesce and late-arrival tests. New: a test
that completes more than `max_completed_keys` groups, then sends a late
arrival for an evicted group and asserts it is still classified late (negative
control: with the exactness flag forced true it must be misclassified); a
resume test asserting the probe still runs. The Part 6 count gate moves by
exactly 2 probes per fork row.

**Expected saving.** 2 probes (10 round trips) of about 470 per fork row on a
fresh run of up to 10,000 groups; unknown CPU until measured, about 2% by
round-trip share. Beyond 10,000 groups the first-arrival probe returns.

---

### Part 3 — Control flow examined and left alone

#### L4-2 — Retry wrapper: no change recommended

**Finding.** Recon Appendix A § 3 and top-10 item 7: a tenacity `Retrying` is
built per token×node.

**Mechanism (verified at 1a7ff0e6).** `RowProcessor._execute_transform_with_retry`
(`src/elspeth/engine/processor.py:2435`) calls
`self._retry_manager.execute_with_retry` at `:2556` whenever a retry manager
exists, and the factory always builds one for a leader with settings
(`src/elspeth/engine/orchestrator/processor_factory.py:227`).
`execute_with_retry` (`src/elspeth/engine/retry.py:72`) builds `Retrying(…)`
with fresh `stop`, `wait` and `retry` objects at `:127-139` and three closures
per call. Confirmed as described.

**Evidence.** `python bench_retry.py` (CPU, best of 5 × 200,000):

| Path | µs per call |
|---|---:|
| direct `operation()` | 0.05 |
| `RetryManager.execute_with_retry`, success on attempt 1 | 11.69 |
| first-attempt fast path | 0.07 |

So 11.65 µs per token×node, against 24–30 ms of CPU per row: 0.04–0.08%. In
the recon's transform profile the wrapper and all tenacity frames total
0.25 s of 132.5 s (`prof_engine.py` on `transform_2000/run.prof`).

**Change.** None now. If it is ever wanted, the safe form is: run attempt 1
directly; on a retryable failure enter the existing tenacity loop with an
operation that re-raises the captured exception on its first call instead of
calling the plugin again. Tenacity then counts the replayed failure as
attempt 1, so attempt numbering, the backoff sequence, the `RetryError` →
`MaxRetriesExceeded` chain and `on_retry` ordering stay as they are. The
pre-attempt shutdown check must run before the direct attempt and be skipped
on the replay iteration, or a shutdown landing between the failure and the
replay would surface as "before retry attempt" instead of "during retry
wait". Rejected: hand-rolling the loop (re-implements jittered backoff);
caching one `Retrying` per manager (it carries per-call state and the
per-call `is_retryable`).

**Must not change.** `node_states.attempt` values, the state attributed to a
shutdown DIVERT (`state_tracker`, `processor.py:2516-2540`), backoff timing.

**Risk.** Low to write, but it touches shutdown and retry attribution for no
measurable gain.

**Ruling needed.** No.

**Verification.** `tests/unit/engine/test_retry.py` and the processor retry
suites would cover it.

**Expected saving.** 11.65 µs per token×node; under 0.1% of CPU.

#### L4-3 — `process_single_token` per-node loop: no change recommended

**Finding.** Recon Appendix A top-10 item 6: heartbeat check, three barrier
probes and two navigator lookups per node.

**Mechanism (verified at 1a7ff0e6).** Loop at
`src/elspeth/engine/token_traversal.py:1146-1288`. Per node:
`_heartbeat_active_claim` (`:1157`; a clock read and compare,
`scheduler_drain.py:1129`), `_maybe_coalesce_token` (`:1158`),
`_maybe_row_union_token` (`:1168`), `_maybe_collector_token` (`:1177`),
`resolve_next_node` and `resolve_plugin_for_node` (`:1185-1186`). Each probe
is a handful of identity comparisons that return at the first line when the
token carries no barrier cursor (`processor.py:3049`, `:3098`, `:3147`); each
navigator call is one dict membership test and one lookup
(`src/elspeth/engine/dag_navigator.py:157-195`). The maps are already built
once at construction. Before the loop, `validate_barrier_ordering` runs three
times per claim (`:1135-1140`) and `max_inner_iterations` is recomputed from
`len(self._processor._node_to_next)` (`:1143`). Nothing here is recomputed
from the graph beyond that `len`.

**Evidence.** Recon transform profile (2,000 rows, 2 transforms, under
cProfile, which inflates small Python calls): `process_single_token` self
37.6 µs per call, `heartbeat_active_claim` 2.9 µs, each `_maybe_*` 0.3–1.2 µs,
each navigator lookup 0.8–1.3 µs. All engine control-flow frames together:
about 0.47 s of 132.5 s, 0.36%.

**Change.** None. A per-node plan tuple `(next_node_id, plugin, kind)` built
in `RowProcessor.__init__` would remove two calls and about 3 µs per node.

**Must not change.** Not applicable.

**Risk.** Not applicable.

**Ruling needed.** No.

**Verification.** Not applicable.

**Expected saving.** Under 0.1% of CPU if done.

---

### Part 4 — Startup

Re-measured at this commit on the recon's gate workload at 10 rows.

`time elspeth run --settings work/gate_10/settings.yaml --execute`, three runs:
user+sys 8.20 / 7.68 / 7.39 s; the pipeline's own reported time 0.6–0.9 s.
So fixed cost is about 6.7–7.3 s of CPU, as both documents say.

Isolated CPU costs (same box, same session):

| Step | CPU s | How measured |
|---|---:|---|
| `import elspeth.cli` | 1.26–1.60 | `python -c "import elspeth.cli"` |
| of which pandas + numpy + networkx | 0.55–0.60 | `python -c "import pandas, networkx"`; pulled by `elspeth.core.canonical` |
| Plugin discovery (`get_shared_plugin_manager()`) | 1.33 | timed after `import elspeth.cli` |
| OpenRouter catalog digest (`import litellm`) | 2.28 | `read_openrouter_catalog_snapshot_id()` |
| Runtime VAL manifest | 0.48–0.53 | `build_runtime_val_manifest()`; second cached call 52 µs |
| Remainder (settings load, graph, schema, preflight, the 10 rows, exit) | about 1.5–2.3 | by subtraction |

`python -X importtime -m elspeth.cli run …` (`importtime-run.log`, ranked by
`importrank.py`): 4,634 modules, 6.76 s of import wall time under contention.
By package self time: litellm 2.12 s (1,005 modules), openai 0.64 s (imported
by litellm), chromadb 0.51 s, pandas 0.34 s, numpy 0.23 s, rich 0.21 s,
`elspeth.plugins.transforms` 0.20 s, sqlalchemy 0.13 s, networkx 0.12 s,
fastapi 0.10 s (imported by litellm), azure 0.10 s. All of `elspeth.*` is
1.15 s (17%).

Largest top-level import events in order: `elspeth.contracts.errors` 0.49 s,
`elspeth.config_loading` 1.31 s (the core tree, including canonical →
pandas/numpy/networkx 0.74 s), plugin discovery 1.69 s
(`…clients.retrieval.chroma` 0.66 s, `plugins.sources.llm.source` 0.45 s of
which `azure.monitor.opentelemetry` 0.31 s via `providers.azure`,
`power_automate_nonlive` 0.21 s, `web_scrape_extraction` 0.08 s), then
`litellm` 2.96 s, which is the last import of the run.

#### E-8a — Compute the bundled catalog digest without importing litellm

**Finding.** Prior review finding 8 / E-8a; recon § 1 "about 70% import".

**Mechanism (verified at 1a7ff0e6).** `src/elspeth/cli.py:1993-2000` calls
`read_openrouter_catalog_snapshot_id()` before every `orchestrator.run`
(again at `:2139-2141`). With no live snapshot it returns
`_bundled_openrouter_slice_sha256()`
(`src/elspeth/plugins/llm/model_catalog.py:230-255`), which reaches
`read_litellm_model_list` and `import litellm` at `:125`. In the import log
litellm is the final top-level import and nothing else in a CSV-only run
imports it.

**Evidence.** 2.28 s CPU for the digest call (3.67 s in a second, more
contended measurement); 2.96 s import wall. Prototype
`catalog_digest_proto.py` (log `catalog_digest_proto.log`; litellm 1.102.2):
reading `model_prices_and_context_window_backup.json` beside the installed
package via `importlib.util.find_spec("litellm")`, taking keys with the
`openrouter/` prefix and applying the same canonical hash gives
`31086dcb…c392`, identical to the production value from
`litellm.model_list`, in 0.03–0.05 s with `"litellm" in sys.modules` false.
269 ids either way.

**Change.** In `model_catalog.py` add a bundled-slice reader that parses the
cost-map JSON directly and make `_bundled_openrouter_slice()` use it when,
and only when, `LITELLM_LOCAL_MODEL_COST_MAP` is true in the environment
(`configure_litellm_pricing()` sets that default; if an operator turns it
off, litellm builds `model_list` from a fetched map and the shortcut would
disagree, so that case keeps the import). If the JSON file is absent, fall
back to the import path. If it is present and malformed, raise as
`_parse_litellm_model_list` does today. `read_litellm_model_list()` itself is
unchanged for the composer's `list_models`. Rejected: caching the digest on
disk (a stale cache would write a wrong audit value); moving the call later
(still paid once per run).

**Must not change.** `runs.openrouter_catalog_sha256` and
`openrouter_catalog_source` byte-for-byte; validate-time catalog membership
(`_read_openrouter_catalog` returns the same set); the "installed but broken
litellm raises" behaviour.

**Risk.** Low to medium. It reads a file whose name and shape litellm owns.
The new parser is a Tier-3 boundary and needs `@trust_boundary` metadata, so
it touches the trust-tier allowlist and the operator signing step.

**Ruling needed.** No (same recorded value). See R-8 for skipping it.

**Verification.** Existing: `tests/unit/plugins/llm/test_model_catalog.py`.
New: a test asserting the JSON-derived set equals the
`litellm.model_list`-derived set on the locked litellm version (negative
control: drop one key from a copy of the JSON and the test fails), and a
subprocess test asserting `litellm` is not in `sys.modules` after
`read_openrouter_catalog_snapshot_id()`. No testcontainer.

**Expected saving.** About 2.2 s CPU per CLI run that has no LLM node (30% of
a 10-row run).

#### L4-4 — Keep heavy SDK imports out of plugin module import

**Finding.** Prior review finding 8 (plugin discovery 15%); recon Appendix E
§ C.1.

**Mechanism (verified at 1a7ff0e6).** Discovery imports every plugin module
(`src/elspeth/plugins/infrastructure/manager.py:105-107`,
`discovery.py:294`). Some import a third-party SDK at module top:
`plugins/infrastructure/clients/retrieval/chroma.py:27-28` and
`plugins/sinks/chroma_sink.py:16-18` (`chromadb`), and
`plugins/transforms/llm/providers/azure.py` (pulls
`azure.monitor.opentelemetry`, `azure.identity`, `requests`; reached from
`plugins/sources/llm/source.py`). `providers/bedrock.py:190` already imports
litellm lazily, which is the pattern to copy.

**Evidence.** Discovery 1.33 s CPU. In the import log chromadb is 0.65 s and
the Azure provider subtree 0.36–0.45 s of the 1.69 s discovery window.

**Change.** Move those SDK imports into the functions that construct the
client (annotations under `TYPE_CHECKING`). Discovery, registration and
plugin names are untouched. Pin it with a subprocess test: after
`get_shared_plugin_manager()`, `sys.modules` contains none of `chromadb`,
`openai`, `litellm`, `azure.monitor`, `boto3`. Rejected for now: a static
name→module index so only the pipeline's plugins are imported (larger change
to registration, which the house rules class as full-suite territory; revisit
only if the residual after this item is still material).

**Must not change.** The registered plugin set and each plugin's
`plugin_version`/hash inputs; error behaviour when an SDK is missing (a
missing SDK must still fail at instantiation or probe, not silently).

**Risk.** Low to medium. Lazy imports move an `ImportError` from discovery to
first use, so each moved import needs a test of the missing-extra message.
Whole-tree gates: do not introduce `getattr`-based lazy loaders.

**Ruling needed.** No.

**Verification.** Existing plugin unit suites and
`tests/unit/plugins/` discovery tests; the new `sys.modules` pin (negative
control: restore one top-level import, test goes red).

**Expected saving.** About 1.0–1.1 s CPU per CLI run.

#### E-8b — Runtime VAL manifest on every CLI start

**Finding.** Prior review E-8b ("design not settled").

**Mechanism (verified at 1a7ff0e6).** `begin_run` calls
`cached_frozen_runtime_val_manifest_json()`
(`src/elspeth/core/landscape/run_lifecycle_repository.py:365`), an
`lru_cache` keyed on the registry fingerprint
(`src/elspeth/contracts/runtime_val_manifest.py:1743-1766`). A CLI process
calls it once, so the cache never hits.

**Evidence.** 0.48–0.53 s CPU for the first call (8 declaration contracts, 18
Tier-1 classes, 6.7 KB of JSON), 52 µs for the second.

**Change.** Recommend leaving it. The manifest is a hash over live source and
bytecode of the running tree, recorded in the run header and compared on
resume (`resume.py:640` deliberately bypasses the cache). A cross-process
cache would need a key that changes whenever any hashed dependency changes,
which is the manifest itself. The alternative is making the build cheaper
(profile `_iter_relevant_method_dependency_hashes`; not done here).

**Must not change.** `runs.runtime_val_manifest_json` and the resume drift
check.

**Risk.** High for a persisted cache; low for a cheaper builder.

**Ruling needed.** Yes if a persisted cache is wanted: *may the run header's
VAL manifest be read from an on-disk cache keyed by anything weaker than the
hashed inputs?* Recommended answer: no.

**Verification.** `tests/unit/contracts/` manifest suites.

**Expected saving.** At most 0.5 s per run; unknown for a cheaper builder.

#### R-8 — Record the catalog digest only for pipelines with an LLM node

Unchanged from the prior review and still a ruling: the column is an audit
value and a CSV-only run is stamped `bundled` today. After E-8a the cost is
about 0.05 s, so the ruling stops mattering for performance. Recommend closing
R-8 as not needed once E-8a lands.

---

### Part 5 — Concurrency ceiling

#### L4-5 — Decide whether rows may overlap at an external call

**Finding.** Recon Appendix A § 0.1 and § 4, Appendix E § A.3 and top-10
item 3: one leader thread, one row at a time; real concurrency for an LLM
pipeline is `pool_size` queries inside one row.

**Mechanism (verified at 1a7ff0e6).**

- `concurrency.max_workers` reaches the engine only as
  `TransformExecutor._max_workers`
  (`src/elspeth/engine/orchestrator/processor_factory.py:130`, `:499`;
  `src/elspeth/engine/executors/transform.py:228`). Its one use is
  `max_pending = min(transform.batch_pool_size, self._max_workers)` at
  `transform.py:364-367`. Its only other reader is the replay/verify guard
  that requires it to be 1 (`engine/orchestrator/run_modes.py:59`).
- At the hand-off the leader registers a waiter, calls
  `batch_runtime.accept(…)` (`transform.py:604`) and blocks on
  `waiter.wait(…)` (`:624`). The comment at `:606-617` records sequential rows
  as a design decision.
- The plugin side can already hold many rows: `BatchTransformMixin` has a
  `RowReorderBuffer(max_pending)` with FIFO release and a
  `ThreadPoolExecutor(max_workers=max_pending)`
  (`src/elspeth/plugins/infrastructure/batching/mixin.py:178-187`, submit at
  `:233-242`). Because the engine submits one row and waits, it never holds
  more than one.
- Threads under `src/elspeth/engine/`: heartbeat, idle-timeout pump,
  sink-effect lease heartbeat. No row pool (grep for `ThreadPoolExecutor(` and
  `threading.Thread(`, which does match the mixin and the pooled executor).
- Followers claim only READY rows. The leader pre-claims each source row
  inside the ingest transaction and keeps draining its own children
  (`processor.py:5115-5150`), so a linear pipeline leaves nothing READY.

**Evidence.** ChaosLLM with a fixed 500 ms latency and no faults
(`chaos/run.sh`, config `chaos/chaos.yaml`, key-free apart from a random
per-process fingerprint key). Server control: one request 0.52 s, eight
parallel requests 0.56 s, so the server is concurrent. Pipeline: csv → one
single-query `llm` transform → jsonl.

| Configuration | Rows | Pipeline time | Sum of row-call latency | Call completions closer together than one call's latency |
|---|---:|---:|---:|---:|
| default | 10 | 11.80 s | — | — |
| default | 30 | 24.80 s | 16.9 s | 0 of 29 |
| `concurrency.max_workers: 16` | 30 | 23.34 s | 16.5 s | 0 of 29 |
| `pool_size: 8` on the transform | 30 | 20.76 s | 16.1 s | 0 of 29 |

From the audit DBs' `calls` table: in every configuration each row's call
starts after the previous row's call was recorded; median spacing 0.56–0.64 s
per row, which is the 0.5 s latency plus 60–140 ms of engine work. Wall time
is N × (latency + overhead), independent of both knobs.

**Change.** A design decision. The options, and what each must preserve:

| Option | What changes | Preserves | Costs and open points |
|---|---|---|---|
| A. Raise in-row `pool_size` | Nothing in the engine | Everything | Helps only multi-query rows. Already available. |
| B. Leader ingests ahead; followers claim source rows | Ingest leaves rows READY (bounded backlog) when a follower is attached, instead of pre-claiming every row | `ingest_sequence` (leader still assigns in ingest order); per-token audit order under lease and CAS; one writer at a time through `BEGIN IMMEDIATE`; retry (followers run the same `settings.retry`); leader-only barriers, sinks and checkpoints | One OS process per unit of concurrency (about 390 MB RSS and 7 s start each, measured here); operator must start followers; the source is read ahead of processing, which interacts with the source-lifecycle gate on resume (ADR-038) and with how many READY rows a crash leaves |
| C. Windowed pipelining on the leader at batch-transform boundaries | The leader drives up to W rows to the `accept` hand-off, parks each claim, and resumes them as the mixin releases results in FIFO order. All audit writes stay on the leader thread | Single-threaded audit writing; deterministic order (begin states in ingest order, completions in FIFO order, so downstream aggregation batches are composed as today); `ingest_sequence`; SQLite single writer | Traversal must become resumable (`process_single_token` is a synchronous loop); the drain's single active claim (`_active_claim_work_item_id`) becomes a set with a heartbeat per parked lease; `PluginContext` is one mutable object scoped per claim; retry and `batch_wait_timeout` per parked row; a crash leaves up to W OPEN states and LEASED claims (recovered as a dead follower's are today). `max_workers` becomes W |
| D. In-process worker threads each running a drain | — | — | Refused by ADR-030 D1 (the processor's claim and heartbeat state is single-threaded), and CPU-bound work would not parallelise under the GIL |

Note for any option: the default rate limit is 60 requests per minute per
service (recon Appendix E rank 9), which caps throughput near one row per
second regardless.

**Must not change.** Audit ordering guarantees per token
(`(token_id, node_id, attempt)` uniqueness and CAS dispositions),
`ingest_sequence` assignment by the leader, one SQLite writer at a time, retry
semantics and attempt numbering, replay/verify determinism (which already
demands `max_workers=1`).

**Risk.** B: Medium. C: High.

**Ruling needed.** Yes. ADR-001 decides this today: "Concurrency lives at the
plugin boundary, NOT at the orchestrator level", lists "cannot pipeline across
stages" as an accepted consequence, and says not to introduce row-level
parallelism in the orchestrator. ADR-026 amended it only on the
worker-execution axis (more than one worker process), and ADR-030 D1 refuses
in-process pools. So D is settled (no), A needs nothing, and B and C are
open. Exact question: *Should a pipeline with one external call per row be
able to have more than one source row's call in flight? If yes: (B) let the
leader ingest ahead so follower processes can claim source rows, within
ADR-030 D2; (C) amend ADR-001 principle 1 to allow leader-side pipelining at
batch-transform boundaries with FIFO completion and `concurrency.max_workers`
as the window; or both? If no: change the `max_workers` description
("Maximum parallel workers", `src/elspeth/core/config.py:1872-1876`) to say it
caps a batch transform's pending slots.*

**Verification.** For B: the multi-worker examples (`examples/multi_worker`,
`multi_worker_showcase`, `join_refused`), `tests/e2e/recovery/`, the
testcontainer suite (claim and lease SQL). For C: every executor, drain and
recovery suite, plus a new ChaosLLM test asserting overlapping calls and
unchanged per-token audit order. Measurement for either: `chaos/run.sh` with
the new configuration; the last column of the table above must become
non-zero and pipeline time must fall towards N × latency / W.

**Expected saving.** Up to W× on wall time of external-call pipelines, bounded
by rate limits. None on CPU.

---

### Part 6 — Performance test harness

#### (a) What fails today, and why

Commands (each logged to `perf/`, exit code read from the file):

- `python -m pytest tests/performance -m performance -n 0 -q -rfE` →
  `perf/performance-all.log`. 51 selected, 30 deselected (the `stress` tier).
- `python -m pytest tests/performance/stress -m stress -n 0 -q -rfE` →
  `perf/stress-all.log`, **exit 1: 19 failed, 7 passed, 4 errors in 21 s.**
- `mypy tests/performance` → `perf/mypy-tests-performance.log`, 78 errors in
  9 files.

`-m performance` result: **exit 1 — 9 failed, 42 passed, 30 deselected,
1 error in 812 s** (13.5 minutes serial; the 10,000-row test alone is 210 s).

| Test | Kind | Detail |
|---|---|---|
| `benchmarks/test_db_write.py::test_create_row_throughput` | stale API | `TypeError: DataFlowRepository.register_node() got an unexpected keyword argument 'run_id'` |
| `benchmarks/test_db_write.py::test_create_token_throughput` | stale API | same |
| `benchmarks/test_db_write.py::test_begin_node_state_throughput` | stale API | same |
| `benchmarks/test_db_write.py::test_record_outcome_throughput` | stale API | same |
| `benchmarks/test_db_write.py::test_begin_run_latency` | budget, this box | 63.6 ms per `begin_run`, budget 50 ms (the recon measured 43 ms on a quieter box) |
| `benchmarks/test_cross_check_overhead.py::test_batch_flush_cross_check_within_budget` | budget, this box | median 2,292 µs, budget 1,500 µs (recon: 2,133 µs, also failing) |
| `benchmarks/test_cross_check_overhead.py::test_dispatcher_overhead_scales_with_registered_and_applicable_contracts[1-1]` | budget, this box | tail 61.0 µs, budget 54.0 µs |
| `benchmarks/test_token_expansion.py::…::test_deepcopy_medium_rows_baseline` | budget, this box | 521.6 µs, budget 200 µs (recon: 266.8 µs, also failing) |
| `benchmarks/test_token_expansion.py::…::test_shallow_vs_deep_copy_comparison` | budget, this box | 561.9 µs, budget 500 µs |
| `scalability/test_wide_rows.py::…::test_wide_row_1000_fields` (teardown error) | environment | `tests/conftest.py::_refuse_in_repo_elspeth_writes` saw other lanes writing `.elspeth/` during the session; the test body passed |

So in the `performance` selection: 4 stale-API failures, 5 budget failures
taken on a loaded box (two of them also failed in the recon's quieter run, so
those two budgets are probably wrong for this class of machine rather than
noise), and 1 environment artefact. No file other than `test_db_write.py`
fails for staleness in this selection.

Stale-API failures in the `stress` tier (new; in neither prior document):

| Tests | Count | Cause |
|---|---:|---|
| `stress/test_llm_retry.py` (all classes) | 19 failed | Shared fixture `tests/performance/stress/conftest.py:609` calls `register_node(run_id=…)` → `TypeError` |
| `stress/test_call_index_concurrency.py` | 4 errors | `:61` `register_node(run_id=…)`; `:48` `allocate_call_index()` missing `member_token` and `work_item` |

`test_db_write.py` is stale against six verbs, not one. mypy on that file:

| Line | Call | Current signature |
|---|---|---|
| 40, 53 | `register_node(run_id=…)` | no `run_id`; keyword-only `coordination_token` required (`src/elspeth/core/landscape/data_flow_repository.py:536-551`) |
| 103, 133, 171, 215 | `data_flow.create_row(…)` | does not exist; `create_row_with_token(…, coordination_token=)` (`:200-213`) |
| 147, 179, 223 | `create_token(row_id=…)` | `coordination_token` required (`:258-266`) |
| 186 | `begin_node_state(run_id=…)` | no `run_id`; `member_token` required (`execution_repository.py:203-215`) |
| 230 | `record_token_outcome(ref, outcome, path, sink_name=)` | `member_token` and `work_item` required (`data_flow_repository.py:450-464`) |
| 31, 77 | `begin_run(config, canonical_version)` | `openrouter_catalog_sha256` and `_source` required; masked at run time by the wrapper in `tests/conftest.py:473-517` |

Fixing `register_node` alone moves the `TypeError` one line down.

Which commit: **UNVERIFIED.** `git rev-parse --is-shallow-repository` is
`true`. `git log -S` returns only `4bf3645a` (2026-09-30), a shallow boundary
at which the source already had `coordination_token` and the test already
passed `run_id=`. The test file's last commit is `a575b8c0` (2026-06-18).
ADR-048 (2026-09-06, "every mutation API takes one current token,
keyword-only") is the likely cause; confirm on a full clone.

#### L4-6 — Rewrite the stale benchmarks against the fenced API

**Finding.** Recon § 1 "the DB-write micro-benchmarks do not run"; widened
above.

**Mechanism (verified at 1a7ff0e6).** As tabulated.

**Evidence.** The three logs above.

**Change.** In `tests/performance/benchmarks/test_db_write.py`:

- Build the run with `tests.fixtures.landscape.make_recorder_with_run()`
  (`tests/fixtures/landscape.py:693`), which begins the run, registers the
  source node and returns the leader `CoordinationToken`. Register the
  transform node with `register_test_node` (`:776`).
- Row and token creation: time
  `setup.data_flow.create_row_with_token(setup.source_node_id, i, data,
  source_row_index=i, ingest_sequence=i, coordination_token=setup.coordination_token)`.
  That is the verb production uses; merge the row and token benchmarks into
  it, and keep a separate `create_token(row_id, coordination_token=…)` loop
  only if that verb still has a production caller.
- `begin_node_state(token_id, node_id, 0, input_data,
  member_token=leader_member_token(setup.factory, setup.run_id))`.
- `record_token_outcome(ref, outcome, path, member_token=…,
  work_item=claim_test_work_item(setup.factory, member_token=…, token_id=…,
  node_id=…), sink_name=…)`; do the claims outside the timed block.
- Pass the catalog arguments to `begin_run` explicitly in
  `test_begin_run_latency`.

In `tests/performance/stress/conftest.py:596-615` and
`stress/test_call_index_concurrency.py:36-75`, make the same substitutions
(`register_node(coordination_token=leader_coordination_token(factory, run_id), …)`,
`allocate_call_index(state_id, member_token=…, work_item=…)`,
`allocate_operation_call_index(…, coordination_token=…)`).

Keep the thresholds; they are smoke bounds. Rejected: deleting the file (it
is the only micro-benchmark of the dominant bucket); adding compatibility
shims to the repositories.

**Must not change.** Production code. Test-only change.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** `pytest tests/performance/benchmarks/test_db_write.py -m performance -n 0`
and `pytest tests/performance/stress -m stress -n 0` exit 0 on a quiet box;
`mypy tests/performance/benchmarks/test_db_write.py tests/performance/stress/test_call_index_concurrency.py`
reports no `call-arg` or `attr-defined` error. Negative control: reintroduce
`run_id=` and both instruments go red.

**Expected saving.** None; restores a measurement.

#### (b) Why nobody noticed, and the cheapest guard

Verified: `pyproject.toml:486` has `-m "not slow and not stress and not
performance and not testcontainer and not live_provider"` in `addopts`, and
`tests/conftest.py:136-147` marks everything under `tests/performance/`. In
`.github/workflows/`, a search for `performance` matches one comment
(`ci.yaml:783`) and no command; the same search does match `-m testcontainer`
at `ci.yaml:867`, so the instrument works. The Test and Integration jobs pass
no `-m` and inherit `addopts`. mypy gates `src/` and `elspeth-lints/src/`
only (`ci.yaml:124`, `scripts/full-suite-gate.sh:231`). So no automated run
ever imports-and-calls these tests.

#### L4-7 — Type-check the performance tests' repository calls and smoke-run them with tiny N

**Finding.** Above.

**Mechanism (verified at 1a7ff0e6).** Above.

**Evidence.** `mypy tests/performance` finds every stale call found by
running the tests, plus the masked `begin_run` ones: 34 `call-arg` and 12
`attr-defined` errors, 78 errors in all.

**Change.** Two guards, cheapest first.

1. A default-suite unit test, `tests/unit/cicd/test_performance_suite_call_shapes.py`,
   that runs mypy's API over `tests/performance` with only the `call-arg` and
   `attr-defined` codes enabled and asserts none are reported against
   `elspeth.*` callees. It needs the other 32 errors left alone or fixed
   (pytest-benchmark `FixtureRequest.stats` and annotations); filtering by
   error code and callee module avoids holding the tests to the strict bar.
   About 20–40 s warm; mark it so it runs in one CI shard.
2. A smoke mode for the benchmarks: a `--perf-smoke` option in
   `tests/performance/conftest.py` that sets iteration counts to about 10 and
   turns budget assertions into reports, plus one CI step
   `pytest tests/performance/benchmarks tests/performance/scalability tests/performance/stress/test_call_index_concurrency.py -m "performance or stress" --perf-smoke -n 0`.
   This catches run-time breakage a type check cannot (fixtures, removed
   behaviour) in well under a minute.

Rejected: `--collect-only` (the failures here are call-time `TypeError`s;
collection succeeds, as the 51-item collection above shows); running the
suite with real N in CI (the 10,000-row test alone is minutes, and budgets
are box-dependent).

**Must not change.** The default selection and its pin in `tests/unit/cicd`.

**Risk.** Low.

**Ruling needed.** No. (It adds a CI step; keep it only if the maintainer
agrees it meets the "materially improves reliability" bar.)

**Verification.** Negative control for each guard: reintroduce one stale
keyword in a benchmark; guard 1 fails naming the line, guard 2 fails with the
`TypeError`.

**Expected saving.** None; prevents silent rot of the harness.

#### (c) A deterministic regression gate for the hot path

#### L4-8 — Pin exact per-row counts and per-function linearity through the production entry

**Finding.** Recon § 1: "existing perf tests do not cover the dominant
costs"; Appendix B § 10: nothing measures statements or commits per row.

**Mechanism (verified at 1a7ff0e6).** Per-row counts are exact integers once
one-time and periodic effects are removed. Prototype in
`proto/test_hot_path_counts_proto2.py` and `proto3.py` (run from the repo
root with `-p tests.conftest`; logs `proto/run2.log`, `proto/run3.log`): a
linear pipeline (`ListSource → PassTransform → CollectSink`) and a
fork/coalesce pipeline (the shape at
`tests/integration/pipeline/orchestrator/test_orchestrator_core.py:716-777`)
through `build_production_graph` (which calls
`ExecutionGraph.from_plugin_instances`), the real `Orchestrator`, and
`LandscapeDB.from_url("sqlite:///<tmp>/audit.db")`.

**Evidence.** Measured at this commit, sizes 16 / 32 / 48 after a discarded
4-row warm-up, main thread only:

| Per source row | Linear | Fork/coalesce |
|---|---:|---:|
| SQL statements (`after_cursor_execute`) | 68 | 381 |
| Commits | 8 | 51 |
| Rollbacks | 0 | 8 |
| `rfc8785.dumps` (every canonical serialisation) | 46 | 108 |
| `core.canonical.canonical_json` | 12 | 42 |
| `core.canonical.stable_hash` | 9 | 28 |

All second differences are zero. Negative control: a mutant that issues one
extra `SELECT 1` inside `begin_node_state` moves statements per row from 68
to 70 (`proto/v2-negative-control.txt`). Linearity oracle: profiling the same
three sizes with `cProfile` and taking the second difference of every
`elspeth/` function's call count reports exactly two offenders on both
shapes, `sink_effect_reservation.py:358` and `:370` `<genexpr>` with
second difference 256 = 16² (`proto/v3-nonlinear-_linear.txt`,
`…-_fork.txt`) — the known quadratic.

Things the prototype had to handle, which the real test must keep:

- Sizes stay below 64 rows: scheduler maintenance and the sink-terminal batch
  fire every 64 drains (32 → 64 showed 68.375 statements per row).
- A discarded warm-up run first: per-process caches make the first run differ
  by one `rfc8785.dumps` and 189 `_dispatch_site_marker` calls.
- Count only the main thread: the heartbeat thread issued 9 statements per
  run on its own clock.
- `canonical_json` and `stable_hash` are imported by name everywhere, so
  patching `elspeth.core.canonical.canonical_json` counts nothing (the first
  prototype measured 0). Count at the chokepoints instead: `rfc8785.dumps`
  and `core.canonical._primitive_canonical_json` / `_primitive_stable_hash`.

**Change.** Add `tests/unit/engine/test_hot_path_row_cost.py` (default
suite; about 30 s serial for the pins, about 75 s more for the profiled
oracle, so put the oracle behind the `slow` marker or run it at two shapes ×
three small sizes only):

1. `test_per_row_counts_are_pinned[linear|fork]` — asserts the six integers
   above exactly, and that each second difference is zero.
2. `test_extra_statement_is_detected` — the negative control above, kept in
   the suite.
3. `test_no_engine_function_is_superlinear[linear|fork]` — the second
   difference oracle over `elspeth/` functions with an explicit allowlist that
   today holds the two reservation generator expressions; the sink lane's fix
   deletes the allowlist entries, and a stale entry must fail the test.
4. `test_i1a_sweep_is_linear` from E-11.

Pin policy, in the test's docstring as in
`tests/unit/web/composer/test_schema_contract_walk_cost.py`: pins are exact,
not ceilings. A fix that lowers a count edits the constant in the same commit
and names the removed statements in the commit message. A count that rises is
a regression unless the commit explains the new audit write. No tolerance
band: a band is where regressions hide.

Rejected: wall-time or rows-per-second thresholds (the existing tests have
them; they did not move when per-row statements doubled); total Python call
count as a pin (changes with SQLAlchemy and Python versions; the second
difference does not); in-memory SQLite (different pool and journal mode from
production).

**Must not change.** Production code. The test reads counts through public
SQLAlchemy events and `monkeypatch`; it must not add hooks to `src/`.

**Risk.** Low. Brittleness risk: any deliberate statement change edits a pin,
which is the point; SQLAlchemy upgrades do not change statement counts.

**Ruling needed.** No.

**Verification.** The prototype is the evidence that it is feasible and that
each oracle goes red (mutant +2 statements; known quadratic reported).
PostgreSQL: not owed for the test itself; a parametrised testcontainer
variant of test 1 would give M-1 its statement counts for free and is
recommended as a follow-up.

**Expected saving.** None directly. It is the before/after instrument for
every other lane's fix: each prebuilt statement, removed read-back or folded
commit shows as an exact pin change.

#### (d) Measurement procedure for the whole plan

#### H-1, H-2, M-1, M-2, M-3 — One reproducible procedure, committed

**Finding.** Prior review H-1 (harness lives as `.py.txt`), H-2 (workloads
not reproducible), M-1 (PostgreSQL and follower baseline), M-2 (re-measure
after fixes), M-3 (external-call pipeline).

**Mechanism (verified at 1a7ff0e6).** The recon's and prior review's scripts
live in gitignored lane directories. This lane re-created them and they ran
unmodified in purpose: `setup.py` (workload generator), `sqlcount.py`
(per-statement count, cursor time, first call site), `phases.py` (cursor,
commit, GC, RSS, CPU), `grow.py` (cross-size comparison), `prof_grow.py`
(cProfile cross-size call-count diff), `explain.py` / `whatif_sweep.py`,
`importrank.py`, `bench_retry.py`, `chaos/run.sh`.

**Evidence.** Every number in this report came from those scripts.

**Change.**

- H-1 / H-2: commit the generator and the three listeners as lint-clean
  files under `scripts/perf/` (tracked, and `scripts/**/*.py` already allows
  `print`, `pyproject.toml:354`). **Not `notes/`, which the prior review's
  H-1 names:** `git check-ignore -v notes/x.py` reports `.gitignore:35:notes/`,
  so anything placed there is untracked. The four workloads (gate, transform,
  batch, fork) are generated from the shipped example settings by path
  substitution, seeded (`random.seed(42)`), sizes as arguments. Do not commit
  generated inputs.
- Procedure per fix, recorded in the fix's PR:
  1. L4-8 pins before and after (exact; this is the primary evidence).
  2. `sqlcount` on gate, transform and fork at 200 and 800 rows: statements,
     commits and rollbacks per row (marginal between the two sizes, which
     removes startup), and the per-statement table for any statement the fix
     names.
  3. CPU: `phases` on the same runs, interleaved baseline/fix, four
     repetitions each, report min and median of marginal CPU ms per row. Use
     `time.process_time`, not wall.
  4. Smoke gate from the prior review: same exit code, row counts, sink
     output digests and `node_states.input_hash`/`output_hash` on
     `transform_pipeline` and `error_routing` before and after.
  5. For startup items: `python -X importtime` total and the 10-row run's
     user+sys, three runs.
- M-3: `chaos/run.sh` is the external-call baseline (0.5 s fixed latency, no
  faults). Result here: 60–140 ms of engine time per row on top of the call.
- M-1: run step 2 against PostgreSQL in the testcontainer job (counts carry
  over; record them) and once with one follower on `examples/multi_worker`.
  Not done in this lane (no Docker run attempted).
- M-2: after the Landscape lanes' fixes, replace every "expected saving" in
  the plan with the measured pin change and marginal CPU.

Rejected: py-spy as a required step (not in the lock file; keep it optional
for share attribution).

**Must not change.** Nothing in `src/`.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** The committed scripts reproduce the Part 1 tables within
the stated noise on a quiet box.

**Expected saving.** None; makes every other saving checkable.

---

### Summary

#### 1. Items

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| E-11 | Rewrite the run-end I1a sweep to drive from `token_parents` | Low | No | E-11 | 225 → 2 ms at 800 fork rows; quadratic term removed (about 35 s at 10k rows, extrapolated) |
| E-9c | Skip intake reads when the leader produced nothing to adopt | Medium | Yes (ADR-029 § E.2) | E-9c | About 2.5% of fork CPU; 6 of 8 reads per row |
| L4-1 | No Landscape probe for a coalesce group held open in memory | Low | No | — | 2 probes (10 round trips) per fork row; CPU unknown until measured |
| L4-2 | Retry wrapper | — | No | — | Under 0.1%; recommend not fixing |
| L4-3 | Per-node traversal loop | — | No | — | Under 0.1%; recommend not fixing |
| E-8a | Catalog digest without importing litellm | Low–Medium | No | E-8a | About 2.2 s CPU per non-LLM CLI run |
| L4-4 | Lazy SDK imports in plugin modules | Low–Medium | No | finding 8 | About 1.0–1.1 s CPU per CLI run |
| E-8b | VAL manifest per CLI start | High if cached | Yes if cached | E-8b | At most 0.5 s; recommend leaving |
| R-8 | Catalog digest only for LLM pipelines | — | Yes | R-8 | About 0.05 s after E-8a; recommend closing |
| L4-5 | Cross-row concurrency at external calls | B Medium, C High | Yes (ADR-001, ADR-030) | — | Up to W× wall on external-call pipelines |
| L4-6 | Rewrite stale benchmarks and stress fixtures | Low | No | — | Restores measurement |
| L4-7 | Call-shape type check and tiny-N smoke for perf tests | Low | No | — | Prevents rot |
| L4-8 | Exact per-row count pins and linearity oracle | Low | No | — | The instrument for all other fixes |
| H-1, H-2, M-1, M-2, M-3 | Committed, reproducible measurement procedure | Low | No | H-1, H-2, M-1, M-2, M-3 | Makes savings checkable |

#### 2. Recommended order

1. **L4-8** first. Every later fix is proved by a pin change, and the pins
   exist today (68 / 8 and 381 / 51 / 8).
2. **L4-6**, then **L4-7** (the guard is only meaningful once the tests pass).
3. **H-1 / H-2** alongside, so M-2 can be run by anyone.
4. **E-11** (independent; owes testcontainer).
5. **E-8a**, then **L4-4** (independent of each other; E-8a first because it
   is the larger and makes R-8 moot).
6. **L4-1** (independent; moves the fork pin by 2 probes).
7. **E-9c** after its ruling; it depends on L4-8 for its proof and should
   land after L4-1 so the two pin changes are separable.
8. **L4-5** is a decision, not a fix; raise it with the ChaosLLM table.
9. M-1 and M-2 after the Landscape lanes land.

#### 3. Examined and recommended not to fix

- **Fork/coalesce "super-linearity"** — not reproduced; nothing to fix beyond
  E-11 and the sink lane's reservation loop.
- **Retry wrapper (L4-2)** — 11.65 µs per token×node.
- **Per-node loop (L4-3)** — under 0.4% of profiled time in total.
- **VAL manifest cache (E-8b)** — the cache key would be the manifest.
- **R-8** — moot after E-8a.
- **pandas / numpy / networkx at `import elspeth.cli`** (0.55–0.60 s via
  `src/elspeth/core/canonical.py:28-30`) — deferring them means changing the
  `isinstance(obj, np.floating)` checks at the top of `_normalize_value`,
  which is the hashing lane's hot path; not worth it for half a second.
- **`has_blocked_barrier_work` hydrating rows to return a bool**
  (`processor.py:5233`; `count_blocked_barrier_items` exists at
  `scheduler/barrier.py:1052`) — once per end-of-input loop; negligible.
- **A static plugin index for by-name discovery** — larger change to
  registration; reconsider only if L4-4 leaves discovery material.

#### 4. Not verified

- The commit that changed `register_node` (shallow clone).
- The cause of the recon's 102 → 136 ms/row. This lane shows it does not
  recur at four sizes; it did not reproduce the recon's exact three runs.
- Why per-call SQLite time drifts 10–30% between 100 and 400 rows. Random
  UUID keys spreading inserts across more B-tree pages is a plausible cause;
  it was not tested.
- That the E-9c dirty-flag setter list is complete: the 18 `group_losses=`
  call sites and the restore paths were counted, not each read.
- `row_union_executor.py` and the collector executor were not read for an
  equivalent of the L4-1 probe.
- PostgreSQL behaviour of the E-11 rewrite (no testcontainer run here), and
  everything in M-1.
- The 0.05 s digest prototype was checked on litellm 1.102.2 only.
- Budget-assertion results in `perf/performance-all.log` were taken while
  other lanes were running; treat each as "failed on a loaded box", not as a
  regression.
- pytest runs in this lane show one teardown error from
  `tests/conftest.py::_refuse_in_repo_elspeth_writes`, because other lanes
  were writing `.elspeth/payloads` at the same time. It is an artefact of the
  shared checkout, not of the tests run.

## Appendix L5 — Plugins and the external-call trunk

Lane L5 of the executor remediation plan. Scope: `src/elspeth/plugins/` and
`src/elspeth/core/rate_limit/`. Verified at `1a7ff0e6` (HEAD `bf8adf06`, docs
only on top). Plan only; nothing in the tracked tree was changed.

### Summary

- **M-3 is done: the LLM path now has a measured profile.** Both an
  Azure-shaped and an OpenRouter-shaped pipeline ran key-free against a local
  ChaosLLM server with zero injected latency and zero faults.
- **The call path is not "just the floor".** One Azure-shaped LLM row adds 16
  SQL statements, 3 commits and 2 payload files, and about 17 ms on the worker
  thread, of which about 13 ms is our code. One OpenRouter-shaped row adds 31
  statements, 6 commits and 4 payload files, and about 47 ms.
- **The largest plugin-side cost is a TLS context built per row.** OpenRouter,
  Gateway, the Azure safety transforms and Document Intelligence construct an
  `httpx.Client` per row: 6 ms CPU each (12–16 ms when a proxy variable is
  set), plus a fresh TCP+TLS handshake per row against a real endpoint. Azure
  OpenAI and Bedrock already share one SDK client and do not pay this.
- **Most of the rest is Landscape call recording**, which lane L1 owns:
  10 ms of the 17 ms on Azure. The incidental hashes in plugin code are real
  but small (0.25–0.55 ms per row).
- **The default rate limit dominates wall-clock in a default run.** At the
  shipped 60 requests/minute the 300-row run took 314 s; with the limiter
  non-binding it took 33 s. The sleep-poll itself is immaterial.
- **Three recon items should be left alone**: the limiter's sleep-poll, CSV
  source row emission (46 µs per 10-column row) and the mixin's three thread
  handoffs (55 µs per row).

### Method and workloads

All files are under `.claude/lanes/exec-remediation/L5/`.

| File | What it is |
|---|---|
| `chaos.yaml` | ChaosLLM config: port 8231, `base_ms: 0`, `jitter_ms: 0`, no error injection, canned responses from `examples/chaosllm_sentiment/responses.jsonl` |
| `wl/input.csv` | 300 rows, the `chaosllm_sentiment` example's rows repeated |
| `wl/or/settings.yaml` | The shipped `chaosllm_sentiment` pipeline (OpenRouter provider), default rate limit |
| `wl/orfast/settings.yaml` | Same, with `rate_limit.default_requests_per_minute: 6000000` so the limiter runs but never blocks |
| `wl/az/settings.yaml` | Same as `orfast` with `provider: azure`, `endpoint: http://127.0.0.1:8231` (loopback HTTP is admitted by `validate_azure_endpoint`) |
| `count_calls.py` | SQL listeners plus a `sys.setprofile`/`threading.setprofile` hook counting selected functions on every thread, with caller attribution |
| `spy_l5.py` | py-spy raw-stack analysis by thread and marker frame |
| `bench_httpx.py`, `bench_csv.py`, `bench_misc.py` | Micro-benchmarks |

Commands (from the repo root, `ELSPETH_FINGERPRINT_KEY` set to a random hex
string because the fake `api_key` is still fingerprinted):

```bash
.venv/bin/chaosllm serve --config $L/chaos.yaml --port 8231 --workers 1 &
.venv/bin/elspeth run --settings $L/wl/or/settings.yaml --execute                       # baseline, default limiter
.venv/bin/python $L/count_calls.py $L/wl/<wl>/count.json -- run --settings $L/wl/<wl>/settings.yaml --execute
.venv/bin/py-spy record -r 200 -f raw --threads -o $L/wl/<wl>/pyspy.raw -- \
    .venv/bin/python -m elspeth.cli run --settings $L/wl/<wl>/settings.yaml --execute
```

Every run exited 0 with 300 rows succeeded and 300 lines in `output/results.json`.

Limits of the measurement:

- SQLite, one process, CLI entry, 4-core box shared with other lanes. Counts
  are exact; millisecond figures are py-spy samples at 200 Hz over 300 rows
  and are wall-clock on that thread, so they include blocking in native code
  (fsync, socket reads). Treat them as ±20%.
- py-spy counted threads parked in `Condition.wait`, `EpollSelector.select`
  and the pool's `_worker` loop as samples. The per-thread figures below
  exclude samples whose leaf frame is one of those waits.
- Template rendering runs in a spawned worker process
  (`plugins/infrastructure/templates.py:852`). py-spy was not run with
  `--subprocesses`, so the child's CPU is not in the profile; the parent-side
  round trip is.
- The sandbox sets `HTTPS_PROXY`, which makes `httpx.Client()` build a second
  TLS context for the proxy mount. Figures are given with and without it.

### M-3 — measured profile of the LLM call path

#### Counts per row (exact, 300 rows)

| | Azure provider | OpenRouter provider |
|---|---:|---:|
| SQL statements, whole run / row | 99.5 | 114.5 |
| Commits / row | 13.3 | 16.3 |
| Call records written per query | 1 (LLM) | 2 (HTTP transport + logical LLM) |
| `allocate_call_index` statements / commits | 5 / 1 | 9 / 2 |
| `record_call` statements / commits | 6 / 1 | 12 / 2 |
| `_record_call_payload_refs` statements / commits | 5 / 1 | 10 / 2 |
| **Call recording total, statements / commits** | **16 / 3** | **31 / 6** |
| Payload files written by call recording | 2 | 4 |
| `stable_hash` + `canonical_json` in `_prepare_call_payloads` | 2 + 2 | 4 + 4 |
| `stable_hash` for the telemetry event | 2 | 2 |
| `canonical_json` in `render_with_metadata` | 2 | 2 |
| `httpx.Client` constructed | 0 | 1 |
| Audited client wrapper constructed | 1 `AuditedLLMClient` | 1 `AuditedHTTPClient` |
| `RateLimiter.acquire` / `try_acquire` | 1 / 1 | 1 / 1 |
| `SchemaContract.__init__` | 6 | 6 |

The statement shapes behind the three call-recording transactions are the
same on both providers: each opens `BEGIN IMMEDIATE`, runs the two fence
updates (`run_workers`, `token_work_items`) and re-selects the parent
`node_states` row, then does its one piece of work.

#### Where the worker thread's time goes (py-spy, ms per row)

| | Azure | OpenRouter |
|---|---:|---:|
| All work on the mixin worker thread | 17.2 | 47.0 |
| `record_call`, including payload store and the ref update | 10.1 | 19.4 |
| — of which `FilesystemPayloadStore.store` | 4.4 | 7.5 |
| — of which `_record_call_payload_refs` (third transaction) | 2.8 | 4.8 |
| — of which `_prepare_call_payloads` (hash + serialise) | 0.28 | 0.70 |
| `allocate_call_index` | 1.05 | 4.5 |
| `httpx.Client.__init__` (TLS context) | 0.0 | 15.8 |
| Network send and receive to the local fake | 3.9 | 3.3 |
| `render_with_metadata` (render round trip + hashes) | 0.55 | 0.70 |
| Telemetry event (two hashes + callback) | 0.25 | 0.30 |
| Contract propagate, stamp and align | 0.15 | 0.15 |
| Rate limiter acquire (non-binding) | 0.05 | 0.05 |

Whole-run figures: the default-limiter OpenRouter run took 313.9 s wall and
30.5 s CPU; the non-binding run took 49 s wall and 35 s CPU under py-spy
(32.9 s reported by the run itself); the Azure run took 41 s wall and 30 s CPU
under py-spy.

#### Verdict on the recon's claims for this trunk

| Recon claim | Status at `1a7ff0e6` |
|---|---|
| Per attempt: limiter, network call, 2 Landscape writes, 2 `stable_hash` | **Verified, with a correction.** Two Landscape *calls* (`allocate_call_index`, `record_call`) are three transactions when a payload store is configured, and `record_call` holds four more canonical serialisations |
| Per query: template render plus 3 canonical-JSON hashes | **Partly wrong.** Two `canonical_json` calls and three SHA-256 digests (`templates.py:219`, `:224`, `:230`); the third digest is over the prompt string, not canonical JSON |
| Per external call: 3 commits, 4 canonical serialisations, 2 payload files | **Verified for Azure. Doubled for OpenRouter and Gateway**, which record an HTTP call and a logical LLM call per query (L5-6) |
| `AzureOpenAI` SDK client shared for the run | **Verified** (`providers/azure.py:328-342`); 0 `httpx.Client` constructions measured |
| A new `AuditedHTTPClient` with its own `httpx.Client` per row for OpenRouter | **Verified** (`providers/openrouter.py:657`, `clients/http.py:322`); 1.0 per row measured |
| 30-thread mixin pool | **Misleading.** See L5-10: at most 4 threads by default, 1 for a default LLM transform, and exactly 1 thread was ever started in the measured runs |

#### Classification of every hash and write on the path

| Site | What | Class |
|---|---|---|
| `execution/calls.py:334-335` `stable_hash(request)`, `stable_hash(response)` | `calls.request_hash`, `calls.response_hash` | **Recorded audit value** |
| `execution/calls.py:340,342` `canonical_json(...)` of the same two dicts | Payload bytes for the store | Recorded payload; the serialisation **duplicates** the one inside `stable_hash` (lane L1) |
| `core/payload_store.py:230` SHA-256 of those bytes | Content address | **Integrity check**; equal by construction to the hash above (`contracts/hashing.py:167-168`) (lane L1) |
| `execution/calls.py:613-628` insert | `calls` row | **Recorded audit value** |
| `execution/calls.py:645` second transaction | `calls.request_ref`, `response_ref` | **Recorded audit value**; a separate transaction by design (row, then file, then ref) (lane L1) |
| `execution/calls.py:139-146` `allocate_call_index` | In-memory counter under a fenced transaction | **Integrity check** (fence + parent ownership); writes nothing after the first call per state (lane L1) |
| `clients/llm.py:671-672`, `clients/http.py:847-848` | `ExternalCallCompleted.request_hash/response_hash` | **Incidental duplicate** of the two recorded hashes (L5-4) |
| `llm/templates.py:219` | `variables_hash` → `success_reason.metadata` | **Recorded audit value** |
| `llm/templates.py:224` | `rendered_hash` | **Computed and dropped**: no reader in `src/` (L5-5) |
| `llm/templates.py:229-243` | `contract_hash` | **Computed and dropped**: no reader in `src/`, and recomputed per row from an unchanged contract (L5-5) |
| `core/llm_pricing.py:501` `stable_hash({"pricing_model": ...})` | Argument to a DEBUG log | Evaluated only when the cost calculator raises (the fake model here); not on the path for a priced model |

The grep behind "no reader" was `\.contract_hash|\.rendered_hash|contract_hash=|rendered_hash=`
over `src/elspeth`, excluding `schema_contract_hash` and `output_contract_hash`.
It matched the three assignment sites in `templates.py` (positive control) and
nothing else.

---

### Remediation items

#### M-3 — Profile one external-call pipeline

**Finding.** Prior review M-3 and recon § 3 item 1: the LLM path had never been profiled.

**Mechanism (verified at 1a7ff0e6).** `examples/chaosllm_sentiment` runs key-free against `chaosllm serve`; an Azure-provider variant also runs key-free because `validate_azure_endpoint` (`plugins/llm/config_validation.py:110-112`) admits loopback HTTP.

**Evidence.** The tables above; raw data in `wl/*/count.json` and `wl/*/pyspy.raw`.

**Change.** None to product code. Keep `count_calls.py` and `spy_l5.py` with the prior review's harness (H-1). Two things to fix when it is kept:

- Guard the harness body with `if __name__ == "__main__":`. The template sandbox spawns a worker with the `spawn` start method, which re-imports the main module; the unguarded script re-ran in the child with the parent's `sys.argv` and wrote a stray JSON file named `run` in the repo root (removed).
- Add `--subprocesses` to the py-spy command so the template worker is sampled.

**Must not change.** Nothing changes.

**Risk.** Low.

**Ruling needed.** No.

**Verification.** Re-run the three workloads after any item below lands; the per-row counts are the regression signal.

**Expected saving.** None; it is the measurement the other items rest on.

#### L5-1 — Share one HTTP transport per provider instead of one per row

**Finding.** Recon Appendix E rank 8 and § A.5. No prior-review ID.

**Mechanism (verified at 1a7ff0e6).** Four sites build an `AuditedHTTPClient` per audit parent and close it when the row ends:

- `plugins/transforms/llm/providers/openrouter.py:648-672`, released at `:674-703`.
- `plugins/transforms/llm/providers/gateway.py:920-944`, released at `:946-975`.
- `plugins/transforms/azure/base.py:560-580`, closed at `:325-330`.
- `plugins/transforms/azure/document_intelligence.py:454-472`.

Each `AuditedHTTPClient.__init__` builds its own `httpx.Client(timeout=..., follow_redirects=False)` (`clients/http.py:318-323`), and `httpx.Client.__init__` builds a TLS context.

Why it is per row: the wrapper binds `state_id`, `token_id`, `member_token` and `work_item` at construction (`clients/base.py:92-101`), and every recorded call takes its parent from those fields. That binding is the audit attribution and must stay per audit parent. The `httpx.Client` underneath carries none of it: it is constructed with a timeout only, and credentials are in `self._default_headers`, merged per request. The per-row transport is therefore incidental, not deliberate. The docstring at `azure/base.py:552-554` ("cached to preserve call_index across retries") is stale: indices are allocated by the repository (`clients/base.py:137-153`).

The Azure OpenAI provider is the existing precedent for the split: one shared SDK client (`providers/azure.py:328-342`) under a per-parent `AuditedLLMClient` (`:344-364`). Bedrock does the same (`providers/bedrock.py:370-386`).

**Evidence.**

- Counts: 1.00 `httpx.Client.__init__` per row on OpenRouter, 0.00 on Azure.
- py-spy: 15.8 ms per row in `Client.__init__` → `create_ssl_context` → `ssl.create_default_context`, 28.9% of the OpenRouter worker thread. Half is the proxy mount (`_init_proxy_transport`), present only because the sandbox sets `HTTPS_PROXY`.
- `bench_httpx.py`: construct + close costs 11.8 ms CPU with the proxy variable set and 7.0 ms without; a bare `ssl.create_default_context()` costs 6.0 ms.
- Not measured: the TCP and TLS handshake a real endpoint pays per row because no connection survives the row. The fake server is plain HTTP on loopback.

**Change.**

1. Give `AuditedHTTPClient.__init__` an optional `transport_client: httpx.Client | None = None`. When supplied, `self._client` is that object and `close()` leaves it open; when absent, behaviour is unchanged.
2. Each of the four owners builds one `httpx.Client` lazily under its existing lock, passes it to every per-parent `AuditedHTTPClient`, and closes it in its own `close()`. The per-parent wrapper, its cache and its refcount stay exactly as they are.
3. Replay and verify keep today's rule: no live client in REPLAY, and in VERIFY none until the first admitted request (`clients/http.py:1506-1511`). The owner must not create the shared client in those modes before admission.
4. Set a reject-all cookie policy on the shared client's jar (`client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))`). Today a cookie set by the server lives for one row; on a shared client it would be replayed on every later row.

Rejected alternatives:

- One `AuditedHTTPClient` for the run with `update_call_context` per row (`clients/base.py:243`, used by `azure_search.py:445`): that method is documented as safe only because rows are serial, which lane L4 may change.
- A per-row `httpx.Client(transport=shared_transport)`: closing the per-row client closes the shared transport.
- A module-level global client: wrong lifetime, and it would cross provider instances with different timeouts.

**Must not change.**

- Every `calls` row keeps the same `state_id` or `operation_id`, `call_index`, hashes and payloads; the wrapper is still per audit parent.
- No header, and so no credential, is stored on the shared client.
- REPLAY never opens a live client; VERIFY opens one only after admission.
- The SSRF-safe path is untouched (it does not use `self._client`; see L5-2 and L5-3).

**Risk.** Medium. Two behaviour changes need tests rather than argument: cookies (step 4), and a kept-alive connection the server has closed surfacing as a retryable `NetworkError`, which would add an error call record that a fresh connection would not have produced. httpx's default `keepalive_expiry` is 5 s, which bounds the second.

**Ruling needed.** No recorded value, hash, fence or crash state changes, and the SSRF guard is not touched. One decision to confirm in review: cookies are dropped rather than carried between attempts of the same row (today attempts within one state share a jar).

**Verification.**

- Existing: `tests/unit/plugins/llm/test_provider_openrouter.py`, `test_openrouter.py`, `test_provider_gateway.py`, `test_provider_lifecycle.py`, `test_http_provider_transport_limits.py`; `tests/unit/plugins/clients/test_audited_http_client.py`, `test_http.py`.
- New: N rows through one provider produce one `httpx.Client` construction; negative control, without `transport_client` each wrapper still builds and closes its own.
- New: a server that sets a cookie on row 1 sees no `Cookie` header on row 2; negative control, remove the policy and the test fails.
- New: closing a per-parent wrapper leaves the shared client usable; closing the provider closes it.
- New: REPLAY constructs no client at all.
- Testcontainer suite not owed (no SQL, schema or lock change).
- Measurement: `count_calls.py` on `wl/orfast` should show `httpx.Client.__init__` fall from 1.00 per row to about 0.003, and the `Client.__init__` marker in `spy_l5.py` fall to zero.

**Expected saving.** 6–7 ms CPU per row without a proxy variable, 12–16 ms with one (measured), on OpenRouter, Gateway, Azure safety and Document Intelligence rows. Against a real endpoint, one TCP+TLS handshake per row as well; unknown until measured.

#### L5-2 — Build the pooled HTTP client only when a pooled request is made

**Finding.** New. Related to recon § A.5.

**Mechanism (verified at 1a7ff0e6).** `AuditedHTTPClient.__init__` builds the pooled `httpx.Client` eagerly for the GENERIC policy (`clients/http.py:318-323`). `self._client` is read in exactly one place, `_execute_request` (`:1506-1512`). `web_scrape.py:1849-1863` and `blob_fetch.py:679-691` construct an `AuditedHTTPClient` per row and call only `get_ssrf_safe` / `request_ssrf_safe` (`web_scrape.py:1878,1894,1905`, `blob_fetch.py:698`), which build their own ephemeral client (`:1913`). The eager client is built, never used, and closed (`web_scrape.py:1969`, `blob_fetch.py:741`). Power Automate already skips it (`:320`).

**Evidence.** Static for the call graph; `bench_httpx.py` for the cost: 7.0 ms CPU per unused construction, 11.8 ms with a proxy variable. Not run end to end: no web-scrape workload was profiled.

**Change.** Initialise `self._client = None` unless a `transport_client` is supplied (L5-1), and create it inside the existing double-checked block at `:1506-1511`. Narrow the guard there to what it protects: raise `AuditIntegrityError` when the mode is REPLAY, create otherwise. Rejected: a constructor flag `ssrf_only=True`, which every caller would have to remember.

**Must not change.** REPLAY never constructs a live client, and the offensive check that says so stays. VERIFY still constructs only after admission. `close()` stays safe when nothing was created.

**Risk.** Low.

**Ruling needed.** No. The SSRF request path itself is not edited.

**Verification.**

- Existing: `tests/unit/plugins/clients/test_audited_http_client.py`, `test_http.py`, `test_http_redirects.py`, `test_http_power_automate_policy.py`; `tests/unit/plugins/transforms/test_web_scrape*.py`, `test_blob_fetch.py`.
- New: a web-scrape row constructs exactly one `httpx.Client` (the ephemeral SSRF one); negative control, a `post()` on the same wrapper constructs the pooled one.
- New: REPLAY + `post()` still raises.
- Testcontainer suite not owed.

**Expected saving.** 7 ms CPU per `web_scrape` or `blob_fetch` row (12 ms with a proxy variable), from the micro-benchmark.

#### L5-3 — Reuse one TLS context across the ephemeral SSRF clients

**Finding.** Recon § A.5: the SSRF-safe path builds a client per request and per redirect hop by design.

**Mechanism (verified at 1a7ff0e6).** `clients/http.py:1913-1917` opens `httpx.Client(timeout=..., follow_redirects=False, trust_env=...)` per request. The comment at `:1908-1912` gives the reason: the connection URL is the resolved IP, so every hostname on that IP would share a pool key and a shared client could reuse a TLS connection negotiated for a different hostname. That reason is about the connection pool. The per-request client also rebuilds the TLS context, which holds trust-store configuration and no connection.

**Evidence.** `bench_httpx.py`: 6.0 ms CPU per `ssl.create_default_context()`. Static otherwise. UNVERIFIED: that httpcore never passes a TLS `session=` (which would let a shared context resume a session across hostnames); I did not read httpcore's source.

**Change.** Keep one client and one pool per request. Build one `ssl.SSLContext` per `AuditedHTTPClient` policy (or per process) and pass it as `verify=` to each ephemeral client. Nothing else on the path changes.

**Must not change.** One connection pool per request; SNI and certificate verification per connection; the redirect-hop validation; `trust_env` semantics for proxies.

**Risk.** Medium, because the code is the SSRF guard and the benefit is CPU only.

**Ruling needed.** **Yes.** Question for the maintainer: *may the SSRF-safe request path share one `ssl.SSLContext` (CA trust configuration only) across its per-request `httpx.Client`s, given that each request still gets its own connection pool, or must every request keep a context of its own?* A second point to settle with it: the context would read `SSL_CERT_FILE` / `SSL_CERT_DIR` once instead of per request. No ADR names this; the nearest is the comment at `clients/http.py:1908-1912`.

**Verification.**

- Existing: `tests/unit/plugins/clients/test_http_redirects.py`, `tests/unit/plugins/transforms/test_web_scrape_security.py`.
- New: two hostnames resolving to one IP each complete their own handshake with their own certificate; negative control, a deliberately shared *client* fails the same test.
- Testcontainer suite not owed.

**Expected saving.** 6 ms CPU per SSRF request and per redirect hop. Small against a web fetch; do it only if the ruling is an easy yes.

#### L5-4 — Take the telemetry hashes from the recorded call

**Finding.** Recon § A.1 hop 20 and § A.2: two `stable_hash` per attempt for the telemetry event.

**Mechanism (verified at 1a7ff0e6).** `clients/llm.py:1210-1221` records the call through `_record_image_call`, which returns a `Call` carrying `request_hash` and `response_hash` (`execution/calls.py:656-667`). Those were computed as `stable_hash(request_data.to_dict())` and `stable_hash(response_data.to_dict())` (`calls.py:331-335`). `clients/llm.py:1225-1233` then discards the returned `Call` and `_emit_telemetry_after_audit` hashes `request_dto.to_dict()` and `response_dto.to_dict()` again (`:671-672`). Same objects, same function, same digests. The HTTP client does the same at `clients/http.py:847-848`.

The hashing runs whether or not telemetry is enabled, because the event is built before the callback is tried (`llm.py:655-659`).

**Evidence.** 2.00 `stable_hash` per row at `llm.py:671-672` (Azure) and at `http.py:847-848` (OpenRouter). py-spy: 0.25–0.30 ms per row for the whole telemetry step. `bench_misc.py`: 22 µs + 56 µs for a 0.7 KB request and 0.6 KB response; 105 µs + 129 µs for a 16 KB prompt and 8 KB completion.

**Change.** Keep the `Call` returned by the recording step and pass `call.request_hash` and `call.response_hash` into `_emit_telemetry_after_audit` in place of the two dicts. Apply it to the success path first. On paths where the recorded payload is replaced before recording (the image-rejection branch at `llm.py:575-595`), the `Call` already holds the hashes of what was actually recorded, which is the more accurate value for the event. Rejected: skipping the hashes when telemetry is disabled, which would move a first-party failure inside the best-effort catch.

**Must not change.** Nothing in the Landscape. The event is still built before the `try`, so a failure building it still crashes. Event field values are unchanged on every path where the telemetry input equals the recorded payload.

**Risk.** Low for the LLM client. For the HTTP client, UNVERIFIED that the dict hashed at `http.py:847` is always equal to the payload recorded at `:478-483` (`request_data` and `request_payload` are separate variables there, and `:848` tests truthiness where the LLM client tests `is not None`). Prove equality with a test before changing that file.

**Ruling needed.** No.

**Verification.**

- Existing: `tests/unit/plugins/clients/test_llm_telemetry.py`, `test_http_telemetry.py`, `test_audited_llm_client.py`.
- New, first: assert `event.request_hash == call.request_hash` and the same for the response on success, error and replay paths at the current commit. Negative control: perturb the telemetry input and the test fails. This is the proof the change is value-preserving.
- Testcontainer suite not owed.
- Measurement: `stable_hash` callers at `llm.py:671-672` fall from 2.00 per row to 0.

**Expected saving.** 0.08–0.25 ms per call depending on prompt size. About 1.5% of the Azure worker-thread time; negligible against a real call. Worth doing because it is a three-line change, not because of the number.

#### L5-5 — Stop recomputing the template's contract hash per row, and decide what it is for

**Finding.** Recon § A.2: the contract hash at `templates.py:229-239` is recomputed for every row although the contract normally does not change.

**Mechanism (verified at 1a7ff0e6).** `render_with_metadata` (`llm/templates.py:181-254`) sorts the contract's fields, builds a dict, canonicalises it and hashes it on every call (`:227-243`). It also hashes the rendered prompt (`:224`). `RenderedPrompt.contract_hash` and `.rendered_hash` have no reader in `src/`: the transform passes only `template_hash`, `variables_hash`, the lookup hash and the sources to `build_llm_audit_metadata` (`llm/transform.py:468-477`). The recon's classification of this as an audit hash is therefore wrong at this commit: it is computed and dropped.

**Evidence.** 1.00 `canonical_json` per row at `templates.py:230`. `bench_misc.py`: 55 µs for a 5-field contract, 355 µs for a 40-field one.

**Change.** A single-slot memo on the `PromptTemplate`: keep the last contract object and its hash, and reuse the hash when the next call passes the same object (`is`). The source's contract object is the same for every row once locked, so this hits on every row after the first. Identity, not equality, so no field-by-field comparison is paid. Leave `rendered_hash` alone (one SHA-256 of a string). Rejected: deleting `contract_hash` outright, which changes a public dataclass that tests assert on, on a performance argument.

**Must not change.** `variables_hash`, `template_hash` and `lookup_hash`, which are recorded in `success_reason`. The value of `contract_hash` for any given contract.

**Risk.** Low.

**Ruling needed.** No for the memo. A question worth asking while here, not blocking: *`contract_hash` and `rendered_hash` are computed for every LLM query and recorded nowhere — should they be recorded in `success_reason.metadata`, or removed?* Recording them would be a change to recorded audit content.

**Verification.**

- Existing: `tests/unit/plugins/llm/test_templates.py`, `test_prompt_template_contract.py`, `test_contract_aware_template.py`; `tests/unit/plugins/infrastructure/test_templates.py`.
- New: two renders with the same contract object canonicalise once; negative control, a different contract object with different fields yields a different hash.
- Testcontainer suite not owed.

**Expected saving.** 55–355 µs per query, by contract width.

#### L5-6 — Decide whether HTTP-transport providers need two call records per query

**Finding.** New; contradicts the recon's per-call figures for OpenRouter and Gateway.

**Mechanism (verified at 1a7ff0e6).** `OpenRouterLLMProvider.execute_query` posts through `AuditedHTTPClient` (`providers/openrouter.py:428-434`), which records a `CallType.HTTP` call, and then records a second, `CallType.LLM` call for the same exchange in `_record_logical_llm_success` (`:545-584`) or `_record_logical_llm_error` (`:592-629`). Each record is the full three-transaction sequence with its own request and response payload files. The response body is therefore serialised, hashed and stored twice in two shapes. Gateway follows the same structure. The Azure and Bedrock providers record one LLM call and no HTTP call.

**Evidence.** Counts per row: 31 statements, 6 commits and 4 payload files for call recording on OpenRouter against 16, 3 and 2 on Azure. py-spy: `record_call` 19.4 ms against 10.1 ms.

**Change.** None proposed without a ruling. The options are: keep both (current); record only the logical LLM call and fold the transport facts the HTTP record holds (status, fingerprinted headers, URL) into it; or keep both rows but let the LLM record reference the HTTP record's response payload rather than store a second one.

**Must not change.** Until ruled: everything. Both rows are recorded audit values, replay and verify key on call type and index, and web-run governance settles on the LLM call id.

**Risk.** High if changed: it alters recorded audit rows, call indices and the replay inventory.

**Ruling needed.** **Yes.** Question: *for providers that reach the model over `AuditedHTTPClient`, is the HTTP-level call record required alongside the logical LLM call record, given that SDK-based providers (Azure, Bedrock) record only the LLM call?* It touches the call-recording contract behind replay/verify; no ADR was located that states the two-record rule, so the first step is to find or write that rationale.

**Verification.** Not applicable until ruled. Any change owes the replay/verify suites (`tests/unit/plugins/clients/test_replayer.py`, `test_verifier.py`, `examples/replay_verify/run.sh`) and the testcontainer suite.

**Expected saving.** Up to 15 statements, 3 commits and 2 payload files per query, about 9–13 ms per row measured, if the HTTP record were dropped. Listed for completeness, not recommended as a performance fix.

#### L5-7 — Build the output-declaration stamp table once per transform

**Finding.** New; the general case of prior-review E-9b. Recon Appendix E does not list it.

**Mechanism (verified at 1a7ff0e6).** `_apply_declared_output_field_contracts` (`plugins/infrastructure/base.py:1150-1218`) starts every call with `self._stamped_output_field_contracts()` (`:1183`), which calls `output_field_declarations()` (`:1030`). That function rebuilds the whole table from configuration on every call (`:1090-1148`): `create_contract_from_config(output_schema_config)` constructs a `SchemaContract`, then come two dict builds, `created_output_fields()`, `carried_output_fields()` and `get_effective_guaranteed_fields()`. None of its inputs is row data. It is reached per row from 32 call sites in 24 transform modules, all through this one function. The engine reaches the same table per row at `engine/executors/declared_output_types.py:123` (`created_output_fields()`) and `:124`.

**Evidence.** `bench_misc.py` on a three-operation `ValueTransform`: `output_field_declarations()` 41 µs; `_apply_declared_output_field_contracts` 62 µs, of which 41 µs is the table. py-spy on the LLM run: propagate + stamp + align 0.15 ms per row. Static for the other transforms.

**Change.** Memoise the table on the instance, keyed by the identity of the three attributes it is built from: `self._output_schema_config`, `self._schema_config` and `self.declared_output_fields`. A lookup compares three references with `is` and returns the stored table; a subclass that reassigns any of them (as `ValueTransform.bind_upstream_input_types` does at `value_transform.py:502`) invalidates it without knowing the cache exists.

One hazard must be handled in the same change: `_apply_declared_output_field_contracts` mutates the dict it gets back when `dynamic_created_fields` is passed (`base.py:1187-1198`). Today that is a fresh dict per call. The cached table must be read-only (`MappingProxyType`), with a copy taken only on the dynamic branch.

Rejected alternatives:

- Computing the table once in `__init__`: `output_field_declarations()` is called during construction and graph build, before subclasses have finished setting their config.
- `functools.cached_property`: no invalidation when a subclass rebinds its config.

**Must not change.** The stamped contract of every emitted row, and so `nodes.output_contract_json` and its hash. Precedence operator > plugin > `any` (ADR-050). `blob_csv_expand`'s per-emission dynamic fields.

**Risk.** Low–medium. The table is the one stamping authority, so a stale cache would record a wrong contract. The identity key and the read-only table are what make it safe; a subclass whose `created_output_fields()` depends on something other than those three attributes would be missed, which is what the parametrised test below is for.

**Ruling needed.** No. Note for the operator: `plugins/infrastructure/base.py` is likely on the signed tier-model allowlist, so this edit will cause binding churn to be re-signed.

**Verification.**

- Existing: `tests/unit/plugins/infrastructure/test_output_declaration_stamp.py`, `test_output_contract_admits_created_fields.py`, `test_build_output_schema_config.py`; `tests/unit/engine/test_declared_output_fields_contract.py`; `tests/unit/plugins/transforms/test_value_transform_contract_metadata.py`.
- New, parametrised over every registered transform: the table returned after N rows equals a table rebuilt from scratch. Negative control: a test subclass that changes `created_output_fields()` without rebinding config must make it fail.
- New: `blob_csv_expand` with dynamic fields on row 1 does not leak them into row 2's table.
- Full default suite owed (shared plugin base). Testcontainer suite not owed.

**Expected saving.** About 40 µs per row per field-declaring transform, and the same again where the engine's per-row check reaches it. Under 0.5% of a 24 ms row; the value is mostly as the base that E-9b and E-6b sit on.

#### E-9b — Derive `ValueTransform`'s created fields and expression kinds once

**Finding.** Prior review finding 9 / E-9b: declared output fields and expression kinds are re-derived per row. Re-verified at this commit.

**Mechanism (verified at 1a7ff0e6).** `created_output_fields()` (`value_transform.py:458-461`) calls `_derived_targets()` (`:447-456`), which calls `_derived_kinds()` (`:426-445`), which runs `expression_kinds` over every operation with a linear alias search per name. Per row it is reached twice: from the stamp at `:663` through `base.py:1183` → `:1112`, and from the engine at `declared_output_types.py:123`. Its inputs are the operations and `_input_types`, which change only in `bind_upstream_input_types` (`:471-505`), and that method refuses to run once processing has started (`:477`).

**Evidence.** `bench_misc.py`, three operations: `_derived_kinds()` 30 µs, `created_output_fields()` 36 µs, whole `process()` 170 µs. So 77 µs per row is configuration-only recomputation (41 µs via the stamp table, 36 µs via the engine call). The prior review's 1.7% of the transform workload is not reproduced by this arithmetic: 77 µs of a 24 ms row is 0.3%. I did not re-run the transform workload under py-spy, so the two figures were not reconciled.

**Change.** Compute the tuple in `_refresh_target_contracts()` (`:463-469`), which already runs at the end of `__init__` and at the end of `bind_upstream_input_types`, and have `created_output_fields()` return it. Order inside the refresh matters: derive first, because the refresh itself calls the stamp table, which calls `created_output_fields()`. With L5-7 in place the stamp-table half disappears on its own; this item removes the engine-call half.

**Must not change.** The derived types and nullability of every target; the bind-once rule; the `GraphValidationError` raised at bind time for a contradicted declaration.

**Risk.** Low.

**Ruling needed.** No.

**Verification.**

- Existing: `tests/unit/plugins/transforms/test_value_transform.py`, `test_value_transform_contract_metadata.py`.
- New: `created_output_fields()` returns the same tuple object across rows; negative control, it differs before and after `bind_upstream_input_types` supplies a type that changes a derived kind.
- Testcontainer suite not owed.

**Expected saving.** 36 µs per row on top of L5-7's 41 µs, about 45% of this plugin's own `process()` time and 0.3% of the row.

Other shipped transforms checked for the same pattern (configuration-only values recomputed in `process()`):

| Site | What is recomputed | Disposition |
|---|---|---|
| `plugins/infrastructure/base.py:1183` (reached from 32 sites in 24 modules) | The stamp table | L5-7 |
| `plugins/transforms/value_transform.py:458` | Derived kinds and created fields | E-9b |
| `plugins/transforms/llm/templates.py:229` | Contract hash | L5-5 |
| `plugins/transforms/web_scrape.py:1846`, `blob_fetch.py:678` | `get_limiter(...)` registry lookup under a lock | Leave: one dict lookup |
| `plugins/transforms/web_scrape.py:1866-1874` | Static header dict and fingerprint-name set (when no per-row headers) | Leave: microseconds |
| `plugins/transforms/value_transform.py:589,593` | `row.to_dict()` twice and a `deepcopy` | Row data, not configuration; not this pattern |

Every `*.from_dict(...)`, `create_schema_from_config(...)` and `re.compile(...)` under `plugins/transforms/` is in an `__init__` or at module level (grep controlled against the known per-row hit at `base.py:1183`). The AWS and RAG transform bodies were not read line by line.

#### E-6b — Return one contract object for equal emitted contracts (plugin side)

**Finding.** Prior review finding 6 / E-6b: transforms build a fresh, equal `SchemaContract` per row. Lane L2 owns the hashing side (E-6a); lane L1/L2 own the per-row contract write (E-5).

**Mechanism (verified at 1a7ff0e6).** Three constructions happen in plugin code for each emitted row of a field-adding transform:

- `contracts/contract_propagation.py:51-55`: input fields plus one inferred field per new key.
- `plugins/infrastructure/base.py:1214-1218`: the stamped contract.
- `plugins/infrastructure/base.py:942-946`: the aligned contract, when mode or lock differ from the declaration.

`SchemaContract` and `FieldContract` are frozen, slotted dataclasses (`contracts/schema_contract.py:68,149`), so equal contracts are interchangeable, but each row gets new objects. That defeats any per-instance memo (E-6a) and makes the engine's identity test at `engine/executors/transform.py:800` true on every row.

**Evidence.** Counts on the LLM run: 6.00 `SchemaContract.__init__` per row, of which three are the plugin-side sites above (`contract_propagation.py:51`, `base.py:1214`, `base.py:942`) and three are engine or Landscape sites. Construction itself is cheap: 2.5 µs for 8 fields.

**Change.** Intern the emitted contract in `BaseTransform`: a small per-instance dict from `(mode, fields, locked)` to the `SchemaContract` first built with that value, consulted at the two return points in `base.py` (`:942` and `:1214`) before constructing. Use `dict.setdefault` so two threads cannot publish two objects for one key.

What the cache key must be: the full value of the emitted contract — `mode`, the ordered `fields` tuple, and `locked`. Nothing narrower is correct:

- Not the input contract's identity alone: `propagate_contract` infers the type of an undeclared new field from this row's value (`contract_propagation.py:45`), so two rows with the same input contract can emit different contracts.
- Not the set of field names: `original_name`, `python_type`, `required`, `nullable` and `source` are all part of the recorded contract.
- Field order is part of the key because it is part of the checkpoint and node-contract serialisation.

Bound the table (for example 64 entries, then stop inserting): `blob_csv_expand`'s dynamic fields and value-inferred types can produce an open set.

Rejected: caching on `(input contract identity, added names)` with a fast path that skips propagation. It is faster but needs all 32 call sites changed and is only valid when every added field is in the stamp table.

**Must not change.** The value of every emitted contract; therefore `nodes.output_contract_json`, its hash, and checkpoint contents. Interning must never return a contract that differs in any field from the one that would have been built.

**Risk.** Low. The key is the whole value, so a hit is by definition equal.

**Ruling needed.** No.

**Verification.**

- Existing: `tests/unit/plugins/infrastructure/test_output_declaration_stamp.py`, `test_output_contract_admits_created_fields.py`; `tests/unit/engine/test_declared_output_fields_contract.py`; the ADR-050 declaration suites the prior review names.
- New: two rows with the same shape emit the same contract object; negative control, a row whose undeclared field has a different inferred type emits a different object with the right type.
- New: the table stops growing at its bound and results stay correct past it.
- Full default suite owed (shared plugin base). Testcontainer suite not owed for this item alone.
- Measurement: with E-6a applied, `SchemaContract.version_hash` computations per row on the transform workload should fall to zero after the first row.

**Expected saving.** Nothing by itself beyond a few microseconds. It is the enabler that lets E-6a's memo hit on transform rows (prior review: 1.6–2.8% of samples) and lets E-5 reduce to a cheap check. Depends on L2 for the number.

---

### Examined and recommended not to fix

#### L5-8 — Rate limiter sleep-poll: leave alone

**Finding.** Recon Appendix E rank 9: one lock per service and a 10 ms sleep-poll.

**Mechanism (verified at 1a7ff0e6).** `acquire` loops on `try_acquire` and sleeps 10 ms between attempts (`core/rate_limit/limiter.py:281-294`); `try_acquire` takes one `threading.Lock` per limiter (`:308-309`). The default is 60 requests per minute per service (`core/config.py:1893,1915`). One limiter per service name, shared by every worker (`core/rate_limit/registry.py:142-150`).

**Evidence.** `bench_misc.py`:

- `try_acquire` costs 5.0 µs granted and 5.1–5.2 µs denied, independent of bucket size (60 or 6,000 items).
- With a 200 ms window and one permit, successive `acquire()` calls returned after 202–209 ms: the poll adds 2–9 ms, about 5 ms on average, to a blocked acquire.
- Non-binding in the pipeline: 0.05 ms per row.

The arithmetic:

- CPU while waiting: 100 wake-ups per second × 5 µs = 0.5 ms per second, 0.05% of one core per waiting thread.
- Latency at the default rate: a blocked acquire waits about 1,000 ms for its permit; 5 ms of overshoot is 0.5%.
- Against a real LLM call of 500 ms or more, 5 ms is under 1%.
- It only becomes visible when the configured spacing approaches the poll interval (6,000 requests per minute is 10 ms spacing). A single sequential worker making network calls cannot reach that rate.

**Change.** None. A computed sleep (ask the bucket when the oldest item expires) would remove the overshoot, but pyrate-limiter's bucket does not expose that through the non-blocking call used here, and a condition variable has nothing to be notified by, since permits free by the passage of time.

**Risk / ruling.** Not applicable.

**What does matter, and is not a code defect.** The default limit itself. The shipped example at the default 60 per minute took 313.9 s for 300 rows; the same run with the limiter non-binding took 32.9 s. The sliding window also admits the first 60 calls at once and then stalls for most of a minute. Both are rate policy. They belong in operator documentation and in lane L4's concurrency design, since any cross-row parallelism is capped by this number first.

#### L5-9 — CSV source row emission: leave alone

**Finding.** Recon Appendix E rank 10 and § B.1: pydantic `model_validate` plus contract validation per row.

**Mechanism (verified at 1a7ff0e6).** Per record (`plugins/sources/csv_source.py:589-633`):

- `dict(zip(headers, values))` (`:590`).
- `self._schema_class.model_validate(row)` (`:594`): the Tier-3 → Tier-2 admission. It coerces typed fields and then runs the after-validator `_validate_canonical_numbers` (`plugins/infrastructure/schema_factory.py:101-104`), which walks every value and asks `is_non_canonical_number` (`core/canonical.py:316-340`), which calls `canonical_json(value)` for each number.
- `validated.to_row()` (`:595`), a `model_dump`.
- `contract.validate(validated_row)` (`:611`): catches type drift on inferred (extra) fields that pydantic's `extra="allow"` accepts.
- `SourceRow.valid(...)` (`:629`).

Nothing is rebuilt per row: the schema class is built once in the constructor, headers are normalised once per file, and the contract is locked after the first valid row (`:598-604`).

**Evidence.** `bench_csv.py`, 20,000 rows of 10 columns:

| Schema | CPU per row |
|---|---:|
| Fixed, 10 typed fields (4 numeric, 1 bool) | 45.9 µs |
| Observed | 26.3 µs |

Split for the fixed schema: `model_validate` 25.3 µs, `contract.validate` 3.2 µs, `to_row` 1.5 µs, `SourceRow.valid` 1.1 µs, `dict(zip)` 0.8 µs. Under cProfile about three quarters of `model_validate` is the canonical-number walk.

**Change.** None. 46 µs is 0.2% of the 24 ms the engine spends per row. The only duplicated work is small and deliberate: for a fixed schema `contract.validate` re-checks types pydantic just checked (3 µs), and the number check asks the canonicaliser rather than restating its rule, which its docstring names as the single authority. A fast path for in-range `int` and finite `float` would save about 15 µs per row and duplicate that rule.

**Risk / ruling.** Not applicable.

#### L5-10 — Batching mixin handoffs: leave the mechanics alone

**Finding.** Recon § A.3 and § 3 item 3: three thread handoffs per row and a 30-thread pool that holds one row at a time.

**Mechanism (verified at 1a7ff0e6).** Three handoffs per row, as the recon says:

1. Engine thread → pool worker: `ThreadPoolExecutor.submit` (`plugins/infrastructure/batching/mixin.py:242`).
2. Worker → release thread: `RowReorderBuffer.complete` notifies (`mixin.py:351`).
3. Release thread → engine thread: `self._batch_output.emit` wakes the waiter (`mixin.py:405`), which the engine is blocked on at `engine/executors/transform.py:624`.

Pool sizes and their constants:

| Constant | Value | Where |
|---|---|---|
| `BatchTransformMixin._pool_size` (class default) | 30 | `batching/mixin.py:108` |
| LLM transform override | `config.pool_size`, default **1** | `llm/transform.py:1521`, `pooling/config.py:27` |
| Engine cap | `min(batch_pool_size, concurrency.max_workers)` | `engine/executors/transform.py:365-367` |
| `concurrency.max_workers` default | 4 | `core/config.py:1872-1876` |
| Worker threads | `max_workers = max_pending` | `azure/base.py:218-223`, `mixin.py:184-187` |
| Release-loop idle poll | 1.0 s | `mixin.py:379` |
| Flush poll | 100 ms, once per flush | `mixin.py:461` |

So "a 30-thread pool" overstates it. With defaults the pool is capped at 4 threads for the Azure safety transforms and at 1 for an LLM transform, and `ThreadPoolExecutor` starts threads only on demand: the 300-row runs started exactly one worker (`llm-worker_0`).

**Evidence.** A stand-alone reproduction of the three handoffs (submit → worker → condition notify → release thread → event) costs 55 µs wall and 40 µs CPU per row. That is 0.3% of the 17 ms Azure row.

**Change.** None inside the mixin. Running the row on the calling thread when `max_pending == 1` would save the 55 µs but lose what the worker thread provides: the engine can time out and evict a row while the call is still in flight (`mixin.py:475` onward).

**Risk / ruling.** Not applicable.

**Facts for lane L4's design decision.**

- For an LLM transform `pool_size` is one knob for two things: intra-row query concurrency and the row-level buffer's `max_pending`. At the default of 1 the mixin would admit one row at a time even if the engine offered several.
- The template sandbox allows two renders at once process-wide (`_WORKER_COUNT = 2`, `plugins/infrastructure/templates.py:98-99`). Each render is a pipe round trip to a worker process.
- The rate limiter's default 60 per minute caps any cross-row parallelism before the pool does (L5-8).
- `PooledExecutor.execute_batch` holds `_batch_lock` per batch (recon § A.3); not re-verified here.

---

### Notes for other lanes

For lane L1 (Landscape internals), measured on this path and not designed here:

- `_prepare_call_payloads` serialises each payload twice (`execution/calls.py:334-342`) and the payload store hashes the same bytes a third time (`core/payload_store.py:230`). Because `stable_hash` is `sha256(canonical_json(obj).encode())` (`contracts/hashing.py:167-168`), one serialisation and one digest give all three values. Measured 0.28 ms per Azure row.
- The third transaction, `_record_call_payload_refs`, costs 5 statements, 1 commit and 2.8 ms per call. It exists because the file is written after the row commits. Since the reference is the content hash, it is known before the insert; whether the insert may carry it is a crash-semantics question (a ref pointing at a file that was never written).
- `FilesystemPayloadStore.store` is 4.4 ms per call for two small files, most of it in `_write_temp_payload_file`.
- `allocate_call_index` opens a fenced write transaction and re-selects the parent state to hand out an in-memory counter (5 statements, 1 commit, 1.05 ms).

Startup, prior review E-8a: `read_openrouter_catalog_snapshot_id` → `read_litellm_model_list` took 6.8 s of main-thread samples on the OpenRouter run and 4.4 s on the Azure run, against the prior review's roughly 2.0 s. The box was shared and py-spy was attached, so treat it as "at least as bad as reported", not as a new figure.

litellm prints `Provider List: https://docs.litellm.ai/docs/providers` twice per call (602 lines for 301 calls) when the model is unknown to its price table, as the fake model is. A priced model does not take that branch; it is noise in example runs only.

---

### 1. Item table

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| M-3 | Profile one external-call pipeline | Low | No | M-3 | None (measurement; done) |
| L5-1 | Share one HTTP transport per provider instead of one per row | Medium | No (confirm cookie policy) | — | 6–7 ms CPU/row (12–16 ms with a proxy variable) plus a TLS handshake per row on real endpoints |
| L5-2 | Build the pooled HTTP client only when a pooled request is made | Low | No | — | 7 ms CPU per `web_scrape`/`blob_fetch` row |
| L5-3 | Reuse one TLS context across the ephemeral SSRF clients | Medium | **Yes** | — | 6 ms CPU per SSRF request or hop |
| L5-4 | Take the telemetry hashes from the recorded call | Low | No | — | 0.08–0.25 ms per call |
| L5-5 | Memoise the template's contract hash; decide what it is for | Low | No (one open question) | — | 55–355 µs per query |
| L5-6 | Decide whether HTTP-transport providers need two call records | High | **Yes** | — | Up to 15 statements, 3 commits, 2 files, ~9–13 ms per query |
| L5-7 | Build the stamp table once per transform | Low–medium | No | — | ~40 µs per row per transform |
| E-9b | Derive `ValueTransform` created fields once | Low | No | E-9b | 36 µs per row beyond L5-7 |
| E-6b | Intern equal emitted contracts (plugin side) | Low | No | E-6b | Enabler for E-6a/E-5; depends on L2 |
| L5-8 | Rate limiter sleep-poll | — | — | — | Not fixing |
| L5-9 | CSV source row emission | — | — | — | Not fixing |
| L5-10 | Batching mixin handoffs | — | — | — | Not fixing |

Savings overlap and are not additive.

### 2. Recommended order and dependencies

1. **L5-2**, then **L5-1**. L5-2 is the smaller edit to the same constructor and makes `self._client` optional, which L5-1's `transport_client` parameter builds on. Together they remove every per-row TLS context outside the SSRF path.
2. **L5-4** and **L5-5**. Independent of everything else; each is a few lines plus the equality test that proves it.
3. **L5-7**, then **E-9b**, then **E-6b**. L5-7 makes the stamp table read-only and cached; E-9b removes what is left of the `ValueTransform` recomputation; E-6b gives lane L2's E-6a a stable object to memoise on, so land it with or just before E-6a.
4. **L5-3** only after its ruling.
5. **L5-6** only after its ruling, and only if the maintainer wants the audit shape changed for its own sake.

L5-1, L5-2 and L5-7 edit files that are likely on the signed tier-model allowlist (`clients/http.py`, `infrastructure/base.py`); expect binding churn for the operator to re-sign. None of the designs adds a `getattr`, a suppression or a `.get()` on Tier-1 data.

### 3. Examined and recommended not to fix

| Finding | Reason |
|---|---|
| Rate limiter 10 ms sleep-poll (L5-8) | 0.05% of a core while waiting and about 5 ms on a wait of a second or more |
| CSV source per-row validation (L5-9) | 46 µs per 10-column row, 0.2% of the row; it is the trust boundary and nothing is rebuilt per row |
| Mixin's three thread handoffs (L5-10) | 55 µs per row; the worker thread is what makes timeout and eviction possible |
| `rendered_hash` in `render_with_metadata` | One SHA-256 of a string |
| Per-row `get_limiter` lookups and static header dicts in `web_scrape` / `blob_fetch` | Microseconds |
| Template render round trip to the sandbox worker | 0.55–0.70 ms per row including hashes; the process boundary is the sandbox |
| Per-parent `AuditedLLMClient` / `AuditedHTTPClient` wrapper construction | The wrapper is the audit binding; it is cheap once the transport is shared |

### 4. Not verified

- **Real-endpoint handshake cost** for L5-1. The fake server is plain HTTP on loopback, so only the TLS-context CPU was measured.
- **Equality of telemetry and recorded payloads in the HTTP client** (L5-4): `http.py:478-495` uses separate variables for the two; not read end to end.
- **httpcore's TLS session behaviour** (L5-3): assumed not to resume sessions across connections; source not read.
- **The prior review's 1.7% for E-9b.** My micro-benchmark gives 0.3%; the transform workload was not re-profiled.
- **Web-scrape and Azure-safety pipelines end to end.** L5-2 and the Azure-safety half of L5-1 rest on static reading plus the client micro-benchmark.
- **Multi-query and pooled LLM modes.** Only single-query rows were run; `PooledExecutor` and the sequential retry loop were not exercised.
- **Retry and error paths.** Zero faults were injected, so every figure is the success path.
- **CPU inside the template worker process.** Not sampled.
- **PostgreSQL.** Everything is SQLite; the call-recording statement counts carry over, their cost does not.
- **Bedrock, Gateway readiness, RAG and AWS transforms.** Client lifetimes were read for Bedrock and Gateway; their bodies and the RAG/AWS transforms were not read line by line.
- **Web-run governance** (`llm_call_governance`) was `None` in every run, as it is for the CLI; out of scope.

## Appendix L6 — Findings the draft plan neither remediates nor declines

Lane L6 of the executor remediation effort. Verified at `bcc4e80f` (two
docs-only commits on `1a7ff0e6`), SQLite, one process, CLI entry. Source paths
are under `src/elspeth/` unless they start with `tests/` or `docs/`. Plan line
numbers (`plan:N`) refer to `docs/development/executor-hot-path-remediation.md`.
No tracked file was touched; `git status --short` is empty.

Scope: the twelve findings listed in review finding 11
(`.claude/lanes/exec-remediation/review/review.md`). Each one ends in either an
item in the brief's format or a decline with the number that justifies it.

### 0. What this lane found, in one page

1. **External-call recording** is 16 statements / 3 commits per Azure call and
   31 / 6 per OpenRouter call (L5's counts reproduced exactly). 10 of the 16
   are transaction control, fences and a re-select of two immutable columns;
   6 are work. The whole recording path is 5.0 ms CPU per call; the two
   transactions that write nothing new (`allocate_call_index`,
   `_record_call_payload_refs`) are 1.4 ms CPU each. Against a zero-latency
   fake that is about 8% of the row; against a real provider call of 0.5 s or
   more it is under 0.6%. Three items: one ready (L6-1), one ready with a
   confirmation (L6-2), one ruling (L6-3).
2. **The pre-effect fence in `allocate_call_index` is not waste** and should
   stay. The index is allocated before the provider is called, so its fenced
   transaction is the only item-lease check between claiming the item and
   causing the external effect (`begin_node_state` checks membership only,
   ADR-048 row 29). L5's note and review finding 11 describe it as
   a transaction "to hand out an in-memory counter"; that is true only of the
   four sites that allocate *after* the effect, immediately before
   `record_call` (L6-1).
3. **Carrying the payload ref in the insert changes a documented state.** A
   crash between the row and the file today reads as `HASH_ONLY` ("payload was
   never persisted"); with the ref in the insert it reads as `PURGED`
   ("deleted (retention policy)"), which is false (`core/landscape/row_data.py:17-19`).
   No ADR records the row → file → ref order. L6-3 frames the question.
4. **`SinkEffectMember`**: 7 constructions and 14 canonicalisations per sink
   row, confirmed by call-site trace. One construction is the ownership check;
   the other six are `dataclasses.replace` calls that change only `ordinal`,
   `member_effect_id` or `primary_effect_id` and re-verify content they did not
   change. 250 µs per sink row at 5 columns, 1.9 ms at 100 (L6-4).
5. **`pending_sink_bundle_clause()`** costs 276 µs per build and is built once
   per row: about 1% of a gate row, the cheapest single win in this report
   (L6-6). It needs no gate change.
6. **Fork rows**: 393 statements, 61 transactions (27 write, 34 read) per
   source row, reproduced and broken down by verb. Existing plan items cover
   most of it. Three groups are not covered by any item: 16 statements that
   re-read an immutable `ingest_sequence` (L6-7), 24 that read one token's
   ownership two to four times before each fork and coalesce (L6-8), and 9
   node-cursor existence checks behind a foreign key (L6-9).
7. **Declined with a number**: the thaw-then-freeze at ingest (7 µs/row), the
   plugin double copy as a standalone change (2.5 µs/row against a pinned
   source hash and a corpus manifest re-pin), the fork-child deep copy
   (4.3 µs/child), the sink thaw (needed by four consumers), declaration
   dispatch (three distinct contract surfaces; dispatcher overhead ≈ 10 µs),
   `check_timeouts` (41 ns per pending group), telemetry projection (1.3 µs,
   only when telemetry is enabled), and the follower's leader read (deliberate).

### Method

Everything is under `.claude/lanes/exec-remediation/L6/`.

| File | What it is |
|---|---|
| `trace.py` | SQLAlchemy listeners (`before/after_cursor_execute`, `begin`, `commit`, `rollback`) writing one JSON line per event in order: statement text, thread, elapsed, and the `src/elspeth` call stack (innermost first, 14 frames) |
| `calltable.py` | Ordered statement table for one call's three verbs from a trace |
| `verbclass.py`, `verbstmts.py`, `verbtable.py` | Marginal per-source-row counts between two run sizes, by Landscape verb × statement class, by statement shape, and by engine call site |
| `time_calls.py`, `time_fn.py` | Call counts plus inclusive wall and thread-CPU time for named functions (`perf_counter`, `thread_time`) |
| `count_fn.py` | `sys.setprofile` call counts with a caller chain |
| `bench_misc.py` | Micro-benchmarks, CPU per call, best of five |
| `prof_dispatch.py` | cProfile callees of the declaration dispatcher |
| `chaos.yaml`, `wl/`, `mkwl.py`, `run_any.sh`, `run_trace.sh` | L5's ChaosLLM harness, copied, paths rewritten to L6 |
| `setup_l4copy.py`, `work/` | L4's workload generator, copied |
| `az_call_trace.txt`, `or_call_trace.txt`, `fork_verbclass.md`, `fork_verbstmts.txt`, `fork_verbtable.txt`, `prof_dispatch.txt` | Retained outputs |

Runs (all exit 0, expected row counts): ChaosLLM zero latency, Azure and
OpenRouter providers at 10 rows (traced) and 100 rows (timed); `fork_coalesce`
at 20 and 40 rows (traced); `large_scale_test` gate at 50, 100, 300;
`transform_pipeline` at 100, 150, 300. No other lane was running; load average
was under 0.1 at the start.

Instrument controls: the fork trace reproduces L1's independent totals exactly
(393 statements, 52 pragmas, 45 clock reads, 9 leader CAS, 16 renewals, 20
member fences, 53 commits and 8 rollbacks per source row, `plan:384`); the call
trace reproduces L5's 16/3 and 31/6. `count_fn.py` first returned zero
canonicalisations inside `SinkEffectMember.__post_init__` because it targeted
`core.canonical.canonical_json` and the contract layer calls
`contracts.hashing.canonical_json`; retargeted, it shows the 14.

Limits: SQLite only; no PostgreSQL, no follower, no fault injection, no real
provider. CPU figures carry timer-wrapper overhead of about 1 µs per call. No
prototype was built for any item, so every "expected" count below is by
construction from the trace, not measured after a change.

---

### Part 1 — External-call recording

#### One call, end to end (Azure provider, row 5 of 10, `L6/az_call_trace.txt`)

| # | Verb (`core/landscape/execution/calls.py`) | Statement | Call site | Class |
|---:|---|---|---|---|
| 1 | `allocate_call_index` `:139` | `BEGIN IMMEDIATE` | `database.py:288` | transaction |
| 2 | | `UPDATE run_workers SET status=? WHERE run_id, worker_id, status` | `run_coordination_repository.py:541` | membership fence |
| 3 | | `UPDATE token_work_items SET status=? WHERE run_id, work_item_id, attempt, token_id, row_id, lease_owner, status` | `item_fencing.py:43` | item fence |
| 4 | | `SELECT node_states.run_id, token_id WHERE state_id = ?` | `calls.py:402` | ownership lookup (immutable columns) |
| 5 | | `SELECT max(calls.call_index) WHERE state_id = ?` | `calls.py:193` | lookup; first allocation per state only |
| — | | commit | | |
| | *provider call happens here* | | | |
| 6 | `record_call` `:613` | `BEGIN IMMEDIATE` | | transaction |
| 7 | | membership fence | `:541` | fence |
| 8 | | item fence | `item_fencing.py:43` | fence |
| 9 | | parent select (`FOR UPDATE` on PostgreSQL) | `calls.py:402` | ownership lookup **and** the lock that serialises index remapping |
| 10 | | `SELECT max(call_index), count(*) FILTER (WHERE call_index = ?)` | `calls.py:262` | lookup (collision probe) |
| 11 | | `INSERT INTO calls (…)` | `calls.py:273` | **audit write** |
| — | | commit | | |
| | *two payload files written and fsynced here* (`calls.py:642`) | | | |
| 12 | `_record_call_payload_refs` `:469` | `BEGIN IMMEDIATE` | | transaction |
| 13 | | membership fence | | fence |
| 14 | | item fence | | fence |
| 15 | | parent select | `calls.py:402` | ownership lookup (immutable columns) |
| 16 | | `UPDATE calls SET request_ref=?, response_ref=? WHERE call_id = ?` | `calls.py:452` | **audit write** |
| — | | commit | | |

By class: 3 transaction, 6 fence, 3 ownership lookup, 2 other lookups, 2 audit
writes. No clock read: `calls.created_at` is the process clock
(`calls.py:573`), which ADR-047 leaves alone. There is no read-back.

OpenRouter (and Gateway) record two calls per query, the HTTP transport call
and the logical LLM call: 4 + 5 allocate statements (the second allocation
skips statement 5), 12 record, 10 ref-update = 31 statements, 6 commits
(`L6/or_call_trace.txt`).

#### Cost (`time_calls.py`, 100 rows, per call)

| | Azure CPU ms | Azure wall ms | OpenRouter CPU ms (per record) |
|---|---:|---:|---:|
| `allocate_call_index` | 1.39 | 1.52 | 1.26 |
| `record_call`, whole | 5.04 | 8.34 | 4.77 |
| — `_prepare_call_payloads` | 0.37 | 0.38 | 0.38 |
| — `_insert_allocated_call` (statements 10–11) | 0.84 | 0.89 | 0.78 |
| — `_materialize_call_refs_after_insert` (2 files) | 1.12 | 4.12 | 0.92 |
| — `_record_call_payload_refs` | 1.43 | 1.54 | 1.27 |
| `_verify_state_parent`, each of 3 | 0.22 | 0.23 | 0.22 |
| `FilesystemPayloadStore.store`, each | 0.52 | 2.15 | 0.44 |

Marginal CPU for an Azure row in these runs is about 34 ms, so allocation plus
the ref update is 2.8 ms, about 8% of a zero-latency row. L5's py-spy figures
(1.05 and 2.8 ms) were wall samples under load; the wrapper figures here are
the ones to use. The saving is a statement, commit and write-lock count, not a
wall-clock one, on any pipeline that calls a real provider.

#### What `allocate_call_index` is for

`allocate_call_index` (`calls.py:137-146`) takes the item-fenced transaction,
re-verifies the parent state and hands out a process-local proposal
(`:182-200`). `record_call` then re-fences, re-verifies, probes for a collision
and remaps the proposal if another writer took it (`:258-273`). So the
allocation transaction writes nothing and its index is only a proposal.

What it does do depends on where it is called:

| Site | Position | What the fenced transaction buys |
|---|---|---|
| `plugins/infrastructure/clients/http.py:1469` (read: limiter wait at `:1468`, then allocate, then the request); `:1833`, `:2190`, `clients/retrieval/chroma.py:263`, `:516`, `plugins/transforms/pdf_rasterize.py:632` (same position by the surrounding lines, not read end to end) | before the external effect | **An authority check before the effect.** A worker whose item was reclaimed, or that was evicted, is refused before it calls out |
| `clients/llm.py:735` | before the request is built and before the rate-limit wait at `:769` | The same check, but a limiter wait can separate it from the effect |
| `plugins/transforms/llm/providers/openrouter.py:560`, `:604`; `providers/gateway.py:777`, `:821` | **after** the effect, on the line before `record_call` | Nothing: `record_call` takes the same two fences and the same parent select in the next transaction |

`docs/architecture/adr/048-required-coordination-token-for-landscape-mutations.md`
lists `allocate_call_index` as mutation API 38, ITEM-fenced (`:597`), and the
mutation-fencing gate pins it
(`tests/unit/architecture/test_web_landscape_mutation_fencing.py:245`, `:578`).

#### What a crash leaves behind today

Order in `record_call`: fenced insert of the `calls` row with both hashes and
NULL refs (`:613-628`), commit; store the request file, then the response file,
each temp-file + fsync + rename + directory fsync (`:642`,
`core/payload_store.py:157-266`); fenced update of the refs (`:645`).

| Crash or failure point | `calls` row | Payload files | Reads back as (`calls.py:1353-1370`) |
|---|---|---|---|
| after the provider answered, before the insert commits | none | none | the effect is unrecorded; the open node state is all that shows an attempt |
| after the insert commits | present; `request_hash`, `response_hash` set; refs NULL | none | `HASH_ONLY` — "recorded (hash exists) but payload was never persisted" (`row_data.py:19`) |
| after one or both files are stored | same | 1–2 unreferenced blobs | `HASH_ONLY`, although the bytes are in the store under the recorded hash |
| ref update refused (lease or membership lost after the insert) | same | 2 unreferenced blobs | `HASH_ONLY`; `SchedulerLeaseLostError` propagates |
| store raises (`OSError`) | same | 0–1 blobs | `HASH_ONLY`; `LandscapePostCommitError` (`:372`) |
| ref update commits | refs set | 2 blobs | `AVAILABLE` |

Two facts hold today and are not written down anywhere I could find: a
non-NULL ref has always named a blob that existed, so `PURGED` is only ever
produced by retention; and the `calls` row is durable before any payload file
I/O starts. No reconciler attaches an orphaned blob to a `HASH_ONLY` row (grep
for `request_ref.is_(None)` and `HASH_ONLY` finds only the readers at
`calls.py:1314`, `:1355`, `clients/replayer.py:258`, `clients/verifier.py:397`
and the legacy purge fallback at `reproducibility.py:320-326`).

---

#### L6-1 — Allocate inside the record transaction where nothing separates them

**Finding.** Review finding 11 (a); L5 "Notes for other lanes" (`plan:5133`). No prior-review ID.

**Mechanism (verified at bcc4e80f).** At four sites the allocation is followed directly by `record_call` with no external effect between them: `plugins/transforms/llm/providers/openrouter.py:560-561`, `:604-606`, `providers/gateway.py:777-778`, `:821`. Each opens `fenced_item_transaction` (`core/landscape/execution/calls.py:139`), runs `_verify_state_parent` (`:145`) and `_allocate_call_index_on` (`:146`), commits, and then `record_call` opens the same fenced transaction and runs the same parent select (`:613-619`). `record_operation_call` has the same shape internally: with `call_index=None` it calls `allocate_operation_call_index` in its own leader-fenced transaction (`:729-730`, `:204-211`) before opening its record transaction.

**Evidence.** OpenRouter trace: the second allocation per query is 4 statements and 1 commit (`L6/or_call_trace.txt`); 1.26 ms CPU per allocation (`wl/orfast_100/time.json`).

**Change.** Let `record_call` take `call_index: int | None`. With `None` it calls `self._allocate_call_index_on(conn, state_id)` inside its own transaction, after the fences and `_verify_state_parent`, and proceeds as today (`allocation_is_owned` is then true by construction). The four provider sites drop their `allocate_call_index` line and pass `None`. Do the same inside `record_operation_call`: move the `allocate_operation_call_index` work into its record transaction. `allocate_call_index` stays, unchanged, for the pre-effect callers.
Rejected: removing the pre-effect allocation transaction (see section 3 at the end; it is an authority check). Rejected: allocating in memory with no connection at these four sites (loses the first-use seed from `calls`, and still leaves a signature change).

**Must not change.** The recorded `call_index` values: the HTTP call still takes the lower index because it still allocates before the effect, and the logical call still takes the next. `UNIQUE(state_id, call_index)`, the collision remap under the parent lock (`:258-273`), `_finish_allocation` bookkeeping, both fences as the first statements of the record transaction. What a crash leaves behind is unchanged: the allocation transaction wrote nothing.

**Risk.** Low. The mutation-fencing gate pins the caller set and the mutation API signatures (`tests/unit/architecture/test_web_landscape_mutation_fencing.py:245`, `:578`, `:2244`); `record_call`'s signature and the provider call sites move, so its inventories are re-pinned deliberately. `providers/openrouter.py` and `gateway.py` are plugin-adjacent files; check whether they carry signed tier-model suppressions before editing.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_call_recording.py`, `test_call_token_persistence.py`, `test_expanded_call_identity.py`, `test_ci_coverage_calls.py`; the provider suites under `tests/unit/plugins/transforms/llm/`; the mutation-fencing gate in full. New test: two records on one state, the first allocated pre-effect and the second with `None`, get indices 0 and 1; negative control — a second repository instance that has already inserted index 1 forces the remap and the test asserts index 2, proving the in-transaction allocation still goes through the collision probe. Testcontainer suite owed (lock and transaction change). Measurement: `trace.py` on `wl/orfast_10`, expect 31 → 27 statements and 6 → 5 commits per query.

**Expected saving.** OpenRouter and Gateway: 4 statements, 1 commit, one write-lock acquisition and about 1.3 ms CPU per query. Azure and Bedrock: none. Source and sink operation calls: 1 leader-fenced transaction per call (unmeasured here).

---

#### L6-2 — Stop re-selecting the parent state's immutable columns

**Finding.** Review finding 11 (d). No prior-review ID.

**Mechanism (verified at bcc4e80f).** `_verify_state_parent` (`calls.py:400-408`) selects `node_states.run_id, token_id` for the state and compares them with the claimed item. It runs three times per call: in the allocation (`:145`), the record (`:619`) and the ref update (`:475`). Neither column is ever updated. In `record_call` the select carries `with_for_update(of=node_states_table)` and is the lock the comment at `:259-261` relies on to make the collision probe and the insert atomic on PostgreSQL. In the other two transactions nothing depends on that lock: the allocation inserts nothing, and the ref update addresses the row by `call_id`.

**Evidence.** 0.22 ms CPU per select, three per call (`wl/az_100/time.json`).

**Change.** Keep a bounded per-repository set of verified `(state_id, run_id, token_id)` triples, added to only after a select has returned and matched. `allocate_call_index` and `_record_call_payload_refs` skip the select when the triple is present. `record_call` always runs it, because there it is a lock. Evict on a simple FIFO bound (the natural size is the number of states with a call in flight).
Rejected: dropping the check from the ref update outright and adding `AND state_id = ?` to the `UPDATE` (proves the call belongs to the state, not that the state belongs to the item). Rejected: the same memo for `record_call` (removes a lock).

**Must not change.** The first use of any state by a repository instance still verifies against the database. A mismatch still raises `AuditIntegrityError` before any write. The fences are untouched.

**Risk.** Low. It rests on `node_states.run_id` and `token_id` being immutable and on `node_states` rows never being deleted while a run is live; both hold in every `UPDATE` on that table I read (`execution/node_states.py:630`, `:824`), and I did not find a delete.

**Ruling needed.** No — maintainer confirmation that an ownership check on immutable columns may be per process per state rather than per transaction (the same shape of confirmation review finding 2 asks for on E-5).

**Verification.** `tests/unit/core/landscape/test_call_recording.py`, `test_call_token_persistence.py`. New test: a call recorded against a state that belongs to another token raises on the first use (negative control: seed the memo by hand in the test and show the same call no longer raises, which proves the test is exercising the database check and not the memo). Testcontainer suite owed. Measurement: trace, expect one statement fewer on a state's first call (16 → 15: the allocation's select populates the memo and the ref update skips its own) and two fewer on later calls of the same state (15 → 13).

**Expected saving.** 1 statement and about 0.2 ms per call; 2 per call from the second call on a state. Small. Do it with L6-1 or not at all.

---

#### L6-3 — Decide whether the payload refs need their own transaction

**Finding.** Review finding 11 (b); L5 "Notes for other lanes" (`plan:5131`); recon Appendix B § 8. No prior-review ID.

**Mechanism (verified at bcc4e80f).** As traced above: `record_call` commits the row with NULL refs, stores the files (`calls.py:642`, `_materialize_call_refs_after_insert` `:377-398`), then runs `_record_call_payload_refs` (`:458-476`), a third fenced transaction whose only write is `UPDATE calls SET request_ref, response_ref` (`:451-456`). The ref is the content hash: `store()` returns `sha256(content)` (`core/payload_store.py:230`) and `get_call_response_data` requires `sha256(payload) == response_hash` (`calls.py:1396-1398`), so `ref == hash` for every stored payload and the value is known before the insert. `record_operation_call` has the same three-step shape (`:478-495`).

**Evidence.** 5 statements, 1 commit, 1.43 ms CPU per call (Azure); 10, 2, 2.5 ms per OpenRouter query. The two file writes are 4.1 ms wall per call and are the larger cost; no option below changes them.

**Change.** Three options. Only C is free of a ruling.

| | A. Ref in the insert, files after the commit | B. Files first, then one insert carrying the refs | C. Keep the order, cheapen the third transaction |
|---|---|---|---|
| Transactions per call | 2 | 2 | 3 |
| Crash after the row commits, before the files | row with refs, no blob → reads **`PURGED`** ("deleted (retention policy)") for a payload that never existed | not reachable | row with hashes, NULL refs → `HASH_ONLY` (today) |
| Crash after the files, before the row | not reachable | **2 blobs and no `calls` row**; the effect is unrecorded | not reachable |
| Window in which a completed external call has no `calls` row | as today (≈ 1.2 ms: prepare + insert) | today plus the two file writes (≈ 5.3 ms wall measured here; more on network storage) | as today |
| Store raises | needs a compensating fenced `UPDATE` to NULL the refs — the third transaction returns on the failure path, and if it also fails the ref dangles | insert the row with NULL refs, then raise `LandscapePostCommitError`: same end state as today | as today |
| Deposed worker (lease lost before recording) | refused at the insert, before any file (today) | writes 2 orphan blobs **before** it reaches the fence | refused at the insert (today) |
| "A non-NULL ref named a blob that existed" | **no longer true** | true | true |
| Saving per call | 5 statements, 1 commit, ≈ 1.4 ms CPU | same | with L6-2: 1 statement, ≈ 0.2 ms |

- *C* is L6-2 plus the membership-fence prebuild already in the plan (E-1a part 1). Nothing else in that transaction can go without touching a fence.
- *B* is the form the source-row path already uses, where the blob is written before the row that references it (`core/landscape/data_flow/tokens.py:346-363`). Its cost is the one review finding 4 raised against L3-6: file I/O moves ahead of the fence.
- *A* is the form L5's note suggests. I recommend against it: it makes the retention state lie, and it needs the third transaction back on the failure path.

Rejected: a "payload pending" marker column so A can tell a crash from a purge (a schema change to save one transaction). Rejected: storing only the request file before the effect, since its bytes are known then (the response ref still needs a second write).

**Must not change.** `calls.request_hash` and `response_hash` values; that the hash survives when the payload does not (`docs/release/guarantees.md` § 1.4); the `CallDataState` each reader branches on (`clients/replayer.py:258-266`, `clients/verifier.py:397`); both fences ahead of every `calls` write (ADR-030 D4, ADR-048 rows 38–39).

**Risk.** C: Low. B: Medium — error precedence changes (a store `OSError` can now precede the lease-lost refusal), and the no-row window widens. A: High.

**Ruling needed.** Yes, for A or B. No ADR records the row → file → ref order; the nearest texts are ADR-048 (`docs/architecture/adr/048-required-coordination-token-for-landscape-mutations.md`, the fenced mutation inventory, which a removed DML owner re-pins) and the state definitions in `core/landscape/row_data.py:17-19`. Questions:
1. Is "a non-NULL `request_ref` / `response_ref` always named a blob that was written" an invariant you rely on, so that `PURGED` means retention and nothing else? If yes, A is out.
2. Is "the `calls` row is durable before any payload file I/O" intended — that is, should a provider call that completed be on record within about a millisecond even if the payload store is slow or failing? If yes, B is out and only C remains.
3. If B is acceptable: may a worker that has already lost its lease leave two unreferenced blobs before the fence refuses it?
Whatever the answer, it deserves a paragraph in an ADR or in `row_data.py`, because the order is currently carried only by the code.

**Verification.** `tests/unit/core/landscape/test_call_recording.py`, `test_call_mode_persistence.py`, `test_call_token_persistence.py`; `tests/unit/plugins/` replayer and verifier suites. New tests per option: a store that raises on the second file leaves a row whose `get_call_response_data` state is the one the option's column above states (negative control: assert the *other* option's state and see it fail); for B, a fence that refuses leaves no `calls` row. Testcontainer suite owed. Measurement: trace, expect 16 → 11 statements and 3 → 2 commits per Azure call under A or B.

**Expected saving.** A or B: 5 statements, 1 commit and about 1.4 ms CPU per recorded call (twice that per OpenRouter query). On PostgreSQL that is five round trips and a WAL flush per call; unmeasured. Under 0.3% of the wall time of a real provider call.

#### (c) Double serialisation and the third hash — covered, no new item

`_prepare_call_payloads` hashes and then separately serialises each payload (`calls.py:334-342`); **L2-1** already designs serialising once (`plan:2241-2291`). The payload store's own `sha256` (`core/payload_store.py:230`) is the content address the store derives from the bytes it was handed; it is 1–2 µs on a call-sized payload and is the only derivation independent of the caller. Leave it. One observation for whoever lands L2-1: nothing today compares the ref the store returns with `prepared.request_hash` (`calls.py:370-375`). Before L2-1 that comparison would be a real cross-check of two serialisations; after L2-1 both sides come from one byte string and the comparison proves only the store's addressing.

---

### Part 2 — `SinkEffectMember`

#### L6-4 — Verify a sink member's content once; rebind ordinals without re-verifying

**Finding.** Review finding 11 ("Other orphans"), L2 "Handed to other lanes" (`plan:2801-2827`), corrections row `plan:71`. L3-9 covers two other serialisations, not this. No prior-review ID.

**Mechanism (verified at bcc4e80f).** `SinkEffectMember.__post_init__` (`contracts/sink_effects.py:546-580`) parses `lineage_json`, re-canonicalises it and compares (`:562-566`), re-hashes it (`:569`), walks the row (`:576`), then thaws, canonicalises and hashes the row and compares with `payload_hash` (`:579`), and deep-freezes it (`:581`). `dataclasses.replace` runs `__post_init__` again. Per sink row (`count_fn.py`, gate workload, caller chains in `work/gate_50/sem.json`):

| # | Site | How | Fields that change | Class |
|---:|---|---|---|---|
| 1 | `core/landscape/execution/sink_effect_identity.py:292` | constructor | all | **Ownership check — keep.** `payload_hash` comes from `type_faithful_stable_hash(candidate.row)` (`:271`) and the constructor re-derives it through the contract layer's canonical JSON over the frozen row: two code paths, one stored value |
| 2 | `sink_effect_identity.py:387` | `replace` | `member_effect_id` | incidental re-derivation |
| 3 | `contracts/sink_effects.py:622` | `replace` | `ordinal`, `member_effect_id=None` | incidental |
| 4 | `sink_effect_reservation.py:112` | `replace` | `ordinal`, `member_effect_id=None` | incidental |
| 5 | `sink_effect_reservation.py:153` | `replace` | `member_effect_id` | incidental |
| 6 | `engine/executors/sink_effects.py:1141` | `replace` | `ordinal`, `member_effect_id` | incidental |
| 7 | `engine/executors/sink_effects.py:1242` (also `:1237`, `:1240` on the replacing-target branches) | `replace` | `ordinal` | incidental |

Sites 2–7 copy `row`, `payload_hash`, `lineage_json` and `lineage_hash` by reference from an instance whose constructor verified exactly those objects. The class is `frozen=True, slots=True` and the row is deep-frozen, so the binding cannot have changed. Re-running the check compares a value with itself.

Real cross-checks on this path, which stay exactly as they are (review finding 14):

- construction 1 above;
- `_persist_pipeline_member_payloads` (`engine/executors/sink_effects.py:1360-1364`): re-encodes the row and compares the payload store's content hash with `member.payload_hash`. Do **not** feed it retained bytes;
- `_hydrate_member` (`:1377-1380`): recovery from stored bytes, a construction from durable evidence;
- `compute_pipeline_membership_hash(members) != effect.membership_or_manifest_hash` (`:1349`);
- the `SinkEffectReservationRequest` checks on uniqueness and primary linkage (`contracts/sink_effects.py:612-640`).

**Evidence.** 7.00 constructions and 14.00 `contracts.hashing.canonical_json` calls per sink row (lines 566 and 579, seven callers each). `bench_misc.py`: `replace(member, ordinal=1)` is 41.6 µs at 5 columns and 318 µs at 100; first construction 37.8 and 315 µs. In a transform run the seven cost 428 µs per row (`work/transform_300/timefn.json`), 1.3% of a 33 ms row.

**Change.** Add an instance method that rebinds without re-verifying content:

```python
def rebound(self, *, ordinal: int | None = None,
            member_effect_id: str | None | _Keep = KEEP,
            primary_effect_id: str | None | _Keep = KEEP) -> SinkEffectMember:
    """Same verified content, new position or linkage."""
```

It validates only what it is given (`require_int` on the ordinal, `_require_lower_hex_64` on the ids, and the `routing_context.primary_effect_id` agreement at `:557-560` when the primary changes), builds the new instance with `object.__new__` and `object.__setattr__` on the declared slots, and copies the content fields by reference from `self`. The proof of verification is that `self` exists: the only way to get a `SinkEffectMember` is the constructor. The six `replace` sites call `rebound`. `replace(member, row=…)` and the constructor keep full verification.
Alternative if a gate objects to `object.__new__`: split the class into a verified `SinkEffectMemberContent` and a thin binding that checks `type(content) is SinkEffectMemberContent` (ADR-032), with read-only properties so `member.row` still works. More churn: 11 source files and 22 test files name the class, 17 test constructions.
Rejected: a constructor argument or private field that says "already verified" (a trust statement; review finding 7 objects to that shape). Rejected: caching the canonical bytes on the member and reusing them at `:1360` (turns the divergence refusal there into a tautology; review finding 14).

**Must not change.** Every recorded value: `payload_hash`, `lineage_hash`, `membership_or_manifest_hash`, effect and member ids. Field-wise equality of the resulting member with what `replace` returns today. The five cross-checks listed above. Sink effect ids for existing runs (resume and reconcile rebuild the same members).

**Risk.** Low–Medium. The class is at the centre of the sink effect protocol and its tests construct invalid members on purpose; a `rebound` that forgot one binding check would let an inconsistent routing context through. `contracts/sink_effects.py` is not a plugin file, so no `source_file_hash` moves. Whether a whole-tree gate inventories `object.__setattr__` sites was not checked.

**Ruling needed.** No.

**Verification.** `tests/unit/contracts/test_sink_effect_contract.py`, `test_sink_effect_carrier_boundaries.py`; `tests/unit/core/landscape/test_sink_effect_identity.py`, `test_sink_effect_reservation.py`, `test_sink_effect_reservation_integrity.py`, `test_sink_effect_lifecycle_integrity.py`, `test_sink_effect_result_derived_reconcile.py`. New tests: (1) for each of the six field combinations, `member.rebound(…) == dataclasses.replace(member, …)`; (2) `rebound` with a negative ordinal, a non-hex id, or a primary that disagrees with the routing context raises — negative control: the same three through `replace` raise today, so the test file shows both; (3) a count pin, 1 content verification per sink row, with a mutant that reverts one site to `replace` going red. The smoke gate's sink output digests and effect ids before and after. Testcontainer suite not owed by this change alone (no SQL), but the sink effect lock-order tests there construct members; run them once. Measurement: `count_fn.py` 7 → 1 `__post_init__` and 14 → 2 canonicalisations per sink row; `time_fn.py` on `transform_300`.

**Expected saving.** 6 × 41.6 µs ≈ 250 µs per sink row at 5 columns (≈ 0.8% of the row), 1.9 ms at 100 columns. Twelve of the fourteen canonicalisations.

---

### Part 3 — Row copies

#### L6-5 — Remove the plugin double copy when the file is next edited (not standalone)

**Finding.** Review finding 11; L2 handed it to L5 (`plan:2801-2827`), L5 listed it as "not this pattern" (`plan:4985`).

**Mechanism (verified at bcc4e80f).** `PipelineRow.to_dict()` returns a fresh deep copy (`contracts/schema_contract.py:733-743`, `deep_thaw`). These sites deep-copy that fresh copy again:

| Site | Code |
|---|---|
| `plugins/transforms/value_transform.py:589` | `copy.deepcopy(row.to_dict())` |
| `value_transform.py:593` | a second `row.to_dict()` only to read the keys |
| `value_transform.py:601` | a `PipelineRow` (deep freeze) built per operation |
| `plugins/transforms/type_coerce.py:419` | `copy.deepcopy(row.to_dict())` |
| `plugins/transforms/truncate.py:222` | same |
| `plugins/transforms/passthrough.py:116` | `PipelineRow(copy.deepcopy(row.to_dict()), …)` |
| `plugins/transforms/reference_join.py:1076` | same pattern |
| `plugins/transforms/batch_replicate.py:360` | same, once per copy |

Not the pattern: `blob_text_expand.py:502`, `json_explode.py:554`, `line_explode.py:572`, `blob_csv_expand.py:509`, `blob_json_expand.py:603`, `pdf_rasterize.py:1107` copy a shared `base` once per child, which is needed; `field_mapper.py:825` copies `row_data`, not re-read here.

**Evidence.** `bench_misc.py`, 5 columns / 100 columns: `row.to_dict()` 1.1 / 19.7 µs; `copy.deepcopy(row.to_dict())` 3.7 / 65.5 µs; `PipelineRow(dict, contract)` 2.3 / 39.6 µs. So the redundant copy is 2.5 µs per row at 5 columns and 46 µs at 100; the key-set thaw is 1.4 / 22 µs; the per-operation row is 2.3 / 40 µs per operation. `ValueTransform.process` is 380 µs per call and `TypeCoerce.process` 129 µs (`work/transform_300/timefn.json`), so the copies are about 2% of the plugin and under 0.02% of a 33 ms row.

**Change.** `output = row.to_dict()` at each site; in `value_transform.py`, take `original_fields` from `working_data` before the loop, and keep the per-operation `PipelineRow` (an operation's result has to be visible to the next one under dual-name access). Do it only inside a commit that already edits the same file — E-9b and L5-7 both edit `value_transform.py` — and never as a change of its own.

**What the `source_file_hash` pin is.** Every plugin class declares `source_file_hash = "sha256:<16 hex>"` over its own file's bytes with that line normalised (`value_transform.py:366`, `type_coerce.py:343`; computed by `scripts/cicd/plugin_hash.py`). It is recorded on the run's `nodes` row at registration (`engine/orchestrator/landscape_registration.py:131`). Any byte change to the file moves it. Updating it involves:

1. Recompute after `ruff format` with the command in CONTRIBUTING.md § "Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline" (`CONTRIBUTING.md:383`) and edit the declaration; the lint rule `plugin_contract.plugin_hashes` (PH3, stale hash) runs in pre-commit and CI (`.pre-commit-config.yaml:105`, `.github/workflows/ci.yaml:145`).
2. Re-pin `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, which holds audit records literally. It contains `value_transform`'s current hash `7d4ce6833cb24ca0` three times; it does not contain `type_coerce`'s or `truncate`'s. Then any `resumed_full_projection_sha256`, then `tests/unit/architecture/test_dag_scenario_corpus_contract.py::EXPECTED_CASE_REGISTRY_SHA256`, in that order (`CONTRIBUTING.md:389-393`).
3. Run `tests/integration/core/dag` and `tests/unit/architecture`.
4. Accept the operational consequence: resume compares the recorded hash with the current one and refuses with `PLUGIN_IMPLEMENTATION_CHANGED` (`engine/orchestrator/implementation_compatibility.py:25-33`), and source replay does the same (`engine/orchestrator/source_replay.py:316`). A run interrupted before the upgrade that used the edited plugin cannot be resumed after it. That is the existing refuse contract, but under the 0.8.5 compatibility posture it has to be stated per release. `plugin_version` is not bumped for this (`CONTRIBUTING.md:387-388`).

**Must not change.** The emitted row and its hash; atomic rollback (an error returns before the working copy is used).

**Risk.** Low for the code; the cost is the re-pin and the resume refusal.

**Ruling needed.** No.

**Verification.** `tests/unit/plugins/transforms/test_value_transform.py`, `test_type_coerce.py`, and the equivalents for the other four; new test per plugin: mutate a nested value in the output and assert the input `PipelineRow` is unchanged (negative control: a shallow `dict(row._data)` version fails it). Testcontainer not owed.

**Expected saving.** 2.5–6 µs per row per transform at 5 columns; 46–110 µs at 100. Not worth a hash re-pin by itself.

#### Declined in this part

| Finding | Mechanism (verified) | Number | Decision |
|---|---|---|---|
| 3. `SourceIngestSpec` thaw-then-freeze | `engine/processor.py:2989` `pipeline_row.to_dict()`, then `freeze_fields(self, "data")` at `contracts/scheduler.py:62`. `deep_freeze` always copies a `MappingProxyType` to detach it (`contracts/freeze.py:59-63`), so handing the frozen data over would still copy once | 14.8 µs per row measured in a run for `__post_init__` (4 columns), 7.0 µs for the pair in isolation, 73 µs at 100 columns: 0.05% of a row | **Not fixing.** If L2-4 or L2-5 lands and rows are handed over frozen, pass the `PipelineRow` and let the spec check `type(…) is PipelineRow`; do not open the file for this alone |
| 5. `engine/executors/sink.py:1557` thaw and `node_states.py:493` hash | One `to_dict()` per sink row; the dict then feeds `begin_node_states_many` (`:634-642`), `_validate_sink_input` (`:778`), the canonical round trip (`:810`) and the effect candidates. The hash at `core/landscape/execution/node_states.py:493` is the sink state's recorded `input_hash` | 1 thaw (1.1 µs at 5 columns) with four consumers; 1 `stable_hash` | **Not a separate item.** The thaw is needed. The hash is a recorded audit value derived by the repository; carrying it instead is L2-4 (ruling). L2-5 makes it cheaper |
| 7a. `copy.deepcopy` per fork / expand child | `engine/tokens.py:367` `copy.deepcopy(data)` on a `PipelineRow`, which is `PipelineRow(self.to_dict(), …)` (`schema_contract.py:818-833`); `engine/tokens.py:676` `PipelineRow(copy.deepcopy(row_data), …)`, a copy followed by the freeze's own copy | 4.3 µs per fork child at 5 columns, 61 µs at 100; the redundant half at expand is 2.5 / 39 µs. A fork row costs about 110 ms CPU | **Not fixing.** The deep copy also isolates opaque mutable leaves that `deep_freeze` passes through unchanged (`freeze.py:88-89`); removing it trades 0.004% for a sharing hazard |

---

### Part 4 — Engine control findings

All four are declined.

| Finding | Mechanism (verified) | Number | Decision |
|---|---|---|---|
| 6. Declaration-contract dispatch three times per row × transform | `engine/executors/declaration_dispatch.py:124-170`. The three sites are three contract surfaces: boundary (source `processor.py`, sink `executors/sink.py:564`), pre-emission (`executors/transform.py:536`), post-emission (`:661`). The dispatcher is audit-complete by design: every applicable contract runs, none short-circuits (`:134-136`) | 6.00 dispatches per row on a two-transform pipeline, 70 µs each, 406 µs per row (1.2%). cProfile: the time is inside the four post-emission contract bodies; the dispatcher's own work (registry read under a lock, tuple copy, `applies_to`) is about 13%, ≈ 10 µs per dispatch (`L6/prof_dispatch.txt`) | **Not fixing.** Not a duplicate. The contract bodies' thaws are L2-7's subject. The only removable part is ≈ 3 µs of lock and tuple copy per dispatch once the registry is frozen |
| 8. `check_timeouts` scans pending groups per source row | `engine/coalesce_executor.py:1513-1527`: returns before the scan when `timeout_seconds is None`; otherwise iterates every entry of `self._pending`. Called once per registered coalesce per source row (`engine/orchestrator/outcomes.py:450-455`) | 0.14 µs with one pending group, 41 µs with 1,000, 4.2 ms with 100,000 (`bench_misc.py`). In a single-leader drain each row's branches arrive before the next row is read, so the map holds 0–1 groups | **Not fixing.** Linear in pending groups, 41 ns each, only when a timeout is configured. It becomes worth an ordered structure only if a workload holds tens of thousands of groups open; none was produced here |
| 9. `TelemetryManager.handle_event` projects before the disabled check | `telemetry/manager.py:428` projects, `:434-454` runs observers, `:457-465` checks disabled / no exporters / granularity. The order is deliberate: observers must see the projected event and must run before granularity filtering (`:430-433`). With telemetry off there is no manager at all (`telemetry/factory.py:208-210`, `engine/processor.py:1039`) | 1.29 µs per event (`bench_misc.py`), paid only when telemetry is enabled and the event is then filtered | **Not fixing.** A two-line early return when there are no observers and no exporters is correct but saves about 3 µs per row in a configuration that warns at startup |
| 10. Follower `live_leader` read per claim | `engine/orchestrator/follower.py:373-375`: `ensure_leader_live(force=True)` before each claim. The same closure has an interval-throttled path (`:361-368`) and the claim path opts out of it. `live_leader` is one read transaction, a clock read and a seat select (`core/landscape/run_coordination_repository.py:1219-1231`) | 3 cursor statements per follower claim, against 11 in the claim transaction that follows. No follower was run by any lane | **Not fixing.** Deliberate, pinned by `tests/unit/engine/orchestrator/test_follower_processor.py`, and it stops a follower claiming work in a run whose leader is gone. Measure it under M-1's follower run before reconsidering |

---

### Part 5 — `pending_sink_bundle_clause()`

#### L6-6 — Build the pending-sink bundle predicate once

**Finding.** Prior review finding 9 (`prior-review.md:305`, 1.7%) and its E-1b scope (`:336`); named in neither part of the plan's E-1b.

**Mechanism (verified at bcc4e80f).** `pending_sink_bundle_clause()` (`core/landscape/schema.py:1023-1080`) builds a nested `and_` / `or_` over nine columns from literal enum values on every call. Callers: `scheduler/dispositions.py:992` (inside `_transition_on`, once per row parked as `PENDING_SINK`), `:479`, `scheduler/leases.py:355`, `:621`, `scheduler/queue.py:608`. The function takes no argument and reads no state.

**Evidence.** 1.03 calls per row, 426 µs each inside a run (`work/transform_300/timefn.json`); 276 µs in isolation; `select(<constant>).where(work_item_id == x)` is 12.1 µs against 286 µs today (`bench_misc.py`).

**Change.** A module-level constant in `schema.py`, built once at import after the table is defined, and the five callers reference it. Keep the function as the builder that produces the constant, so the docstring stays with the definition. This is an expression, not a statement: it binds no value at execution, so it is outside what the clock-authority gate inspects (L1's probe V7 added module-level statements without a finding, `plan:505`).

**Which part of E-1b it belongs to.** The statement at `dispositions.py:991-992` is a by-key select with no time value: **E-1b part 1** as the plan defines it (`plan:715-720`), and it is already in E-1b's mechanism list as `:991` (`plan:664`). But E-3a deletes that select and moves the predicate into the `RETURNING` of the `_transition_on` `UPDATE` (`plan:1276`), which binds `updated_at`: **part 2**, behind L1-4. E-3a's sketch still calls `pending_sink_bundle_clause()` per call. So add to E-1b's inventory: "`pending_sink_bundle_clause()` — expression rebuilt per call at `dispositions.py:479`, `:992`, `leases.py:355`, `:621`, `queue.py:608`; hoisted by L6-6 independently of both parts; its host statement at `:991` is part 1 today and part 2 after E-3a." Land L6-6 first; then neither part has to care.

**Must not change.** The SQL text of every statement that embeds the predicate, on both dialects (same expression object, same compiled text). The claim select and its CAS `UPDATE` must keep using the identical predicate (`schema.py:1038-1039`).

**Risk.** Low. SQLAlchemy clause elements are immutable and are shared across statements throughout this package (`token_work_items_table.c.*`).

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_scheduler_pending_sink_claim.py`, `test_scheduler_queue_contract.py`; both source gates. New test: compile each of the five statements on the SQLite and PostgreSQL dialects before and after and assert equal text and parameters (negative control: change one literal in the constant and see the test fail). Testcontainer suite owed (a scheduler statement, even though its text does not move). Measurement: `time_fn.py`, calls per row 1.03 → 0.

**Expected saving.** About 0.27–0.43 ms per row, ≈ 1% of a gate or transform row. The prior review's 1.7% was at a different commit.

---

### Part 6 — Fork / expand / coalesce verbs

#### Per-fork-row statement table

`fork_coalesce` example (truncate → fork to two paths → coalesce `require_all`
→ gate → JSON sink), marginal per source row between 20 and 40 rows. One source
row is 4 tokens (parent, two children, merged). 393 statements, 61 transactions
(27 `BEGIN IMMEDIATE`, 34 plain `BEGIN`), 53 commits, 8 rollbacks.

Statement classes: txn = `BEGIN` / `BEGIN IMMEDIATE`; pragma = `PRAGMA query_only`;
mf / if / lf = membership, item and leader fence; lr = leader deadline renewal;
clk = clock read; wr = audit or journal write; sel = any other select.

| Verb | Engine call site | Stmts | txn | pragma | mf | if | lf | lr | clk | wr | sel | Already covered by | Left |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| `resolve_sink_effect_members` | `executors/sink.py:865` | 44 | 11 | 22 | | | | | | | 11 | E-4 (batch the lineage loads), E-10 | — |
| `claim_ready` ×3 | `scheduler_drain.py:531` | 33 | 3 | | 3 | | | | 9 | 6 | 12 | E-1a part 1 (fence), E-3a (after-image `leases.py:326`), E-1b part 2 (claim `UPDATE`, event insert) | 3 `lock_worker_membership_row` selects (`leases.py:87`). The 9 clock reads (discovery `:132`, re-sample under lock `:225`, commit guard, per claim) are ADR-047's; E-1a part 2 cheapens them, nothing removes them |
| `coalesce_tokens` | `engine/tokens.py:446` | 32 | 5 | 8 | | | 1 | 2 | 3 | 4 | 9 | E-10 (8 pragmas), R-2b (1 renewal + clock), E-1a part 2 | **L6-8**: 4 ownership read transactions |
| `ingest_row_with_initial_claim` | `processor.py:2795` | 30 | 1 | | | | 3 | 4 | 7 | 7 | 8 | R-2a (6), E-3b (6), E-3a, R-2b | — |
| `mark_terminal_with_ready_children` | `scheduler_drain.py:823` | 22 | 1 | | 1 | | | | 3 | 5 | 12 | E-3a (2 after-images), E-1b | **L6-9**: 4 node-cursor selects; 4 reference selects stay |
| `finalize_coalesce_effect` | `engine/tokens.py:458` | 20 | 1 | | | | 1 | 2 | 3 | 3 | 10 | R-2b, E-1a part 2 | 3 `node_states` selects around one `UPDATE` (`node_states.py:808`, `:832`, `:862`) — E-3a's pattern, not in E-3a's site list |
| `complete_barrier` | `processor.py:4793` | 19 | 1 | | | | 1 | 2 | 5 | 5 | 5 | R-2b, E-1a/E-1b part 2 | **L6-9**: 2 node-cursor selects; 2 reference selects stay |
| `adopt_blocked_barrier_item` ×2 | `barrier_coordination.py:596` | 18 | 2 | | | | 2 | 4 | 8 | 2 | | R-2b (one renewal and its clock read per transaction: 4), E-1a part 2 | 2 leader transactions for 2 one-column updates; see "not designed" |
| `fork_token` | `engine/tokens.py:335` | 18 | 3 | 4 | 1 | 1 | | | | 5 | 4 | E-10 (4 pragmas), E-1a part 1 | **L6-8**: 2 ownership read transactions |
| `record_routing_events` (fork gate) | `executors/gate.py:512` | 16 | 2 | 2 | 1 | 1 | | | | 1 | 9 | E-3a (2), E-10 (2), R-7 option A | — |
| `resolve_row_ingest_sequence` ×4 | `scheduler_work_codec.py:105` | 16 | 4 | 8 | | | | | | | 4 | E-10 would halve it | **L6-7**: all 16 |
| `begin_node_state` ×5 | `state_guard.py:219` (3), `coalesce_executor.py:968` (2) | 15 | 5 | | 5 | | | | | 5 | | E-1a part 1, R-7 C/D | — |
| `mark_blocked` ×2 | `scheduler_drain.py:1041` | 14 | 2 | | 2 | | | | 2 | 4 | 4 | E-3a (2 after-images), E-1a part 1 | — |
| `record_routing_event` (route gate) | `executors/gate.py:495` | 13 | 2 | 2 | 1 | | | | | 1 | 7 | E-3a, E-10, R-7 option A | — |
| `complete_node_state` ×3 | `state_guard.py:430` | 12 | 3 | | 3 | | | | | 3 | 3 | E-3a (3 read-backs), R-7 B | — |
| `enqueue_ready` | `scheduler_drain.py:1252` | 10 | 1 | | 1 | | | | 1 | 1 | 6 | E-1b | **L6-9**: 2 node-cursor selects; 2 reference selects stay |
| `mark_pending_sink` | `scheduler_drain.py:786` | 8 | 1 | | 1 | | | | 1 | 2 | 3 | E-3a (2) | — |
| `list_pending_blocked_barrier_items` ×4 | `barrier_coordination.py:457` | 8 | 4 | | | | | | | | 4 | E-9c (ruling) | — |
| `list_unadopted_group_losses` ×4 | `barrier_coordination.py:1225` | 8 | 4 | | | | | | | | 4 | E-9c (ruling) | — |
| `has_completed_group_for_node` ×2 | `coalesce_executor.py:769` | 8 | 2 | 4 | | | | | | | 2 | L4-1 | — |
| checkpoint (`create_checkpoint`) | `core/checkpoint/manager.py:136-171` | 9 | 1 | | | | 1 | 2 | 3 | 1 | 1 | L3-4 (ruling), E-9a | — |
| run-end group settlement (`_group_member_is_settled_or_live`) | `core/checkpoint/recovery.py:356-391` via `run_status.py:65` | 6 | | | | | | | | | 6 | none | 3 selects per lineage group at finalize; see "not designed" |
| `is_release_group` | `engine/tokens.py:231` | 4 | 1 | 2 | | | | | | | 1 | E-10 (2 pragmas) | 1 read transaction for an immutable group kind; small |
| sink `finalize`, `reserve`, `mark_pending_sink_terminal_many`, `update_node_output_contract` | sink phase; `transform.py:803` | 10 | 1 | | 1 | | | | | 1 | 7 | L3-2, E-4, E-5 | — |
| **Total** | | **393** | 61 | 52 | 20 | 2 | 9 | 16 | 45 | 56 | 132 | | |

The checkpoint row is the fenced wrapper line of `L6/fork_verbclass.md` plus
the two checkpoint statements from its "no landscape frame" line; the other six
statements of that line are the run-end group settlement row. The last row is
four small verbs added together. The 9-statement checkpoint agrees with L3-4. Full statement shapes per verb are in `L6/fork_verbstmts.txt`.

What the existing items remove if all of them land, by reading the table:
E-10 the 52 pragmas; E-4 the 11 sink lineage selects and their 11 transactions;
L4-1 2 transactions; E-9c 8 transactions; R-2a 6; E-3a about 12 read-backs;
E-3b 6; E-5 3; R-2b about 14 (one renewal and one clock read per leader
transaction, 7 leader transactions per row: ingest, `coalesce_tokens`,
`finalize_coalesce_effect`, `complete_barrier`, two adoptions, the checkpoint). R-7's options are written for a
gate decision and a transform (`plan:1438-1455`); none of them names a fork,
coalesce or barrier transaction.

What no existing item touches: L6-7 (16), L6-8 (24, or 12 after E-10),
L6-9 (9), and the four groups under "not designed" below.

#### L6-7 — Carry the row's ingest sequence instead of reading it back per continuation

**Finding.** Recon Appendix A "Copies, serialisation, hashing" (`hot-path-recon.md:463`, "DB read or cache UNVERIFIED"); review finding 11. No prior-review ID.

**Mechanism (verified at bcc4e80f).** It is a database read, uncached. `SchedulerWorkCodec.ready_fields` calls `resolve_ingest_sequence(token.row_id)` whenever the caller does not supply the value (`engine/scheduler_work_codec.py:105`), bound to `data_flow.resolve_row_ingest_sequence` (`engine/processor.py:720`), which runs `SELECT rows.ingest_sequence WHERE row_id = ?` through the read-only ops, one read transaction each (`core/landscape/data_flow/ownership.py:55-70`). Only source ingest supplies it (`processor.py:2794`). Every other continuation resolves it: `scheduler_drain.py:773`, `:806`, `:825`, `:1229`; `processor.py:4227`, `:4304`, `:4312`, `:4581`, `:4714`, `:4802`, `:4835`, `:5059`. `rows.ingest_sequence` is written once at ingest. The value handed to the journal is then checked against the `rows` table inside the write transaction: `validate_work_item_references` selects `rows.ingest_sequence` and raises on a mismatch (`core/landscape/scheduler/work_items.py:208-219`).

**Evidence.** 4.00 calls per fork row, 16 statements (4 `BEGIN`, 8 pragmas, 4 selects), 4 read transactions (`L6/fork_verbclass.md`). Linear rows: 0 (their only continuation is the pre-claimed ingest item).

**Change.** Pass the value from the claim. Every continuation is created while a `TokenWorkItem` for the same row is in hand, and `TokenWorkItem.ingest_sequence` is a declared field (`contracts/scheduler.py:151`). `ready_emission(item, *, ingest_sequence: int)` and `ready_fields` take it from the caller; each call site passes `claimed.ingest_sequence` after asserting `claimed.row_id == item.token.row_id` (an `OrchestrationInvariantError` otherwise). For the barrier completion sites, where the tokens come from held arrivals, carry the value on the in-memory arrival record the same way. Keep the resolver for the one case with no claim in hand, if any remains after the call sites are converted.
Alternative: a bounded process-local `{row_id: ingest_sequence}` memo in the codec, seeded at ingest. Simpler to write, but a memo; prefer the explicit carry.
Rejected: dropping the engine-side value and letting the repository fill it from `rows` at insert (moves a read into every write transaction instead of removing it).

**Must not change.** The `ingest_sequence` written to `token_work_items` (it orders claims, `leases.py:134-140`). The repository's check at `work_items.py:208-219` stays: after this change it compares a value carried from the claimed journal row with the `rows` table, which is an independent cross-check, not a tautology. E-3b proposes skipping that validation at source ingest only; that does not interact.

**Risk.** Low. A wrong carried value is refused by the repository check before anything is written.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_scheduler_queue_contract.py`, `test_scheduler_repository_complete_barrier.py`, `tests/unit/engine/test_scheduler_drain_characterization.py`, `tests/integration/core/dag`. New test: a fork row emits children whose journal `ingest_sequence` equals the parent's; negative control: hand `ready_emission` a sequence one higher and assert the repository raises `AuditIntegrityError` (proves the cross-check is live). Add the fork statement pin in L4-8. Testcontainer suite not owed for the engine change; owed if the repository signature moves. Measurement: `verbclass.py`, `resolve_row_ingest_sequence` 16 → 0 statements and 4 → 0 read transactions per fork row.

**Expected saving.** 16 statements and 4 read transactions per fork row on SQLite (4% of the row's statements); 4 round trips on PostgreSQL. Per expand child and per barrier release likewise.

---

#### L6-8 — Resolve a token's ownership once, inside the verb's own transaction

**Finding.** L1 "Not verified" (`plan:1868-1871`: `resolve_token_ownership` at 9 statements and 6 read transactions per fork row). No prior-review ID.

**Mechanism (verified at bcc4e80f).** `fork_token` calls `validate_token_run_ownership(parent_ref)` and then `validate_token_row_ownership(parent_ref.token_id, row_id)` (`core/landscape/data_flow/tokens.py:611-612`). Each calls `resolve_token_ownership` (`data_flow/ownership.py:131`, `:152`), the same `tokens LEFT JOIN rows` query (`:86-94`), each in its own read-only transaction, before the fenced write transaction opens at `:619`. `coalesce_tokens` does the same pair for every parent (`:885-887`): four read transactions for two parents. `expand_token` does it at `:1505-1506`, and the row-union release at `:1791-1794` (one, then two per member). `resolve_token_ownership` and `validate_token_run_ownership` already accept `conn=` (`ownership.py:72`, `:119`); `validate_token_row_ownership` does not.

**Evidence.** Fork row: 2 read transactions (8 statements) in `fork_token`, 4 (16 statements) in `coalesce_tokens`; the two results per token are the same row (`L6/fork_verbstmts.txt`).

**Change.** One helper, `validate_token_ownership_on(conn, refs, row_id)`, that runs one select for all the tokens (`WHERE token_id IN (…)`, chunked like `outcomes.py:138-164`) and checks existence, row-run agreement, run and row for each. `fork_token`, `coalesce_tokens`, `expand_token` and the release verb call it as the first statement after the fence inside their existing write transaction, and the pre-transaction calls go. The error classes and messages stay.
Rejected: keeping the checks outside the transaction but merging each pair into one read (still a transaction per token, and it checks a snapshot the write transaction does not hold). Rejected: an engine-side memo of token ownership (the repository must not take the caller's word for it).

**Must not change.** Every check and every `AuditIntegrityError`. No write before the checks pass: the fence's no-op `UPDATE` is the only statement ahead of them and a failed check rolls the transaction back. The lock order in `coalesce_tokens` ("parent tokens, parent node states, then the effect scope row", the comment above `_lock_coalesce_dependencies` at `:936-948`): the ownership select must not take a lock, or it must be folded into the existing parent-token lock in `_lock_coalesce_dependencies`.

**Risk.** Low–Medium. Error precedence changes: a deposed worker is now refused by the fence before an ownership violation is reported, where today the ownership error comes first. The mutation-fencing gate freezes helper identities; a new `_on` helper re-pins its inventory.

**Ruling needed.** No.

**Verification.** `tests/unit/core/landscape/test_token_recording.py`, `test_token_recording_integrity.py`, `test_coalesce_effects.py`; `tests/integration/core/dag`. New tests: cross-run and cross-row parents still raise, and no `tokens`, `token_parents` or `token_outcomes` row exists afterwards (negative control: a variant that runs the check after the first insert leaves a row and fails the test). Testcontainer suite owed (statement moves inside a locking transaction). Measurement: `verbclass.py`, `fork_token` 18 → 11 and `coalesce_tokens` 32 → 17 statements, 6 read transactions fewer per fork row.

**Expected saving.** 22 statements and 6 read transactions per fork row on SQLite (5.6% of the row's statements); on PostgreSQL 6 round trips become 2. If E-10 lands first the statement saving is 10, the transaction saving the same.

---

#### L6-9 — Check a node cursor against the run once per process

**Finding.** L1's inventory row for `validate_node_cursor` (`plan:443`, 9.00 per fork row); E-3b removes it at ingest only. No prior-review ID.

**Mechanism (verified at bcc4e80f).** `validate_node_cursor` (`core/landscape/scheduler/work_items.py:173-182`) selects `nodes.node_id WHERE run_id, node_id` and raises if absent. `validate_work_item_references` calls it twice per inserted work item, for `node_id` and `coalesce_node_id` (`:220-226`). The `nodes` rows are written at run registration and not afterwards. `token_work_items` already has composite foreign keys for both columns (`core/landscape/schema.py:978-979`), so the select's contribution over the constraint is the named error.

**Evidence.** 9 per fork row: 4 in `mark_terminal_with_ready_children`, 2 in `complete_barrier`, 2 in `enqueue_ready`, 1 at ingest.

**Change.** A per-repository set of `(run_id, node_id)` pairs that have been found, consulted before the select and added to only after a positive result. The set is bounded by the graph size. A miss still runs the select and raises exactly as today.
Rejected: deleting the check and relying on the foreign key (changes the error an operator sees from a named `AuditIntegrityError` to a wrapped constraint failure). Rejected: preloading every node id at run start (a second source of truth for the graph).

**Must not change.** A work item naming a node outside the run is refused with the same error before the insert. The foreign keys stay as the backstop for anything the memo could get wrong.

**Risk.** Low. Rests on `nodes` rows not being deleted during a run; a web process keeps the repository across runs, so key on `run_id` and bound the set.

**Ruling needed.** No — same confirmation as L6-2.

**Verification.** `tests/unit/core/landscape/test_scheduler_queue_contract.py`, `test_scheduler_ingest.py`. New test: a work item for an unknown node raises on first use and again on second use (the memo holds positives only); negative control: with SQLite foreign keys enabled and the memo pre-seeded with the bad pair, the insert still fails on the constraint. Testcontainer suite owed. Measurement: `verbstmts.py`, `validate_node_cursor` 9 → 0 per fork row after the first row.

**Expected saving.** 9 statements per fork row; 1 per linear row until E-3b lands, 0 after.

---

#### Left uncovered and not designed

| Group | Statements per fork row | Why no item |
|---|---:|---|
| `adopt_blocked_barrier_item`: two leader-fenced transactions per row, each a one-column `UPDATE` wrapped in a leader CAS, two renewals and four clock reads (`core/landscape/scheduler/barrier.py:862-940`), each preceded by a member-fenced `mark_blocked` by the same process | 18 + 14 | Folding the deposit and the adoption into one transaction when the leader is its own depositor changes what a crash leaves in the journal and is the mechanism ADR-029 § E.2 specifies. That is a new R-7 option, not a ready fix. R-2b removes 4 of the 18. I did not read `barrier.py` end to end, so no design is offered |
| `lock_worker_membership_row` after the membership fence in `claim_ready` (`scheduler/leases.py:87`, `:211`) | 3 | The fence `UPDATE` on the same `run_workers` row, first in the same transaction, already holds a stronger lock on PostgreSQL, and SQLite ignores `FOR SHARE` (`leases.py:83-85`). Removing it is a lock-shape change in the eviction serialisation fix (`elspeth-6903f82511`) for 3 statements per fork row and 0 per linear row. Not recommended without M-1 |
| `validate_work_item_references` token and row selects outside ingest (`work_items.py:194-219`) | 8 | They are the cross-check that keeps L6-7 honest, and the rows were written by an earlier transaction, so there is no in-transaction witness. Only an R-7-style fold could remove them |
| `finalize_coalesce_effect`: lock, before-image and after-image selects of the parent states around one `UPDATE` (`execution/node_states.py:808`, `:832`, `:862`, `data_flow/tokens.py:1233`) | 3–4 | E-3a's `RETURNING` pattern applies to `_complete_node_states_many`, but that function is not in E-3a's site list (`plan:1238-1244`). Add it there rather than as a new item |
| Run-end group settlement: three selects per lineage group (`core/checkpoint/recovery.py:356`, `:372`, `:391`), reached from `assert_bound_groups_settled_from_audit` (`run_status.py:65`) | 6 | Linear in groups and run once at finalize, so not a hot-path term; it is the same per-group point-select shape E-11 replaced with a set query for the orphan sweep. Counted here, not read |
| `is_release_group` read transaction (`engine/tokens.py:231`) | 4 | One immutable `group_records.kind` read per row; 2 statements after E-10. Could be memoised per group; too small to design |
| Expand and aggregation verbs | — | Not traced. `fork_coalesce` has no expand and no batch. L6-7 and L6-8 apply to `expand_token` by reading (`data_flow/tokens.py:1505-1506`) |

---

### 1. Item table

| ID | Title | Risk | Ruling | Prior-review ID | Expected saving |
|---|---|---|---|---|---|
| L6-1 | Allocate inside the record transaction where nothing separates them | Low | No | — | 4 statements, 1 commit, ≈ 1.3 ms CPU per OpenRouter / Gateway query; 1 transaction per operation call; none for Azure |
| L6-2 | Stop re-selecting the parent state's immutable columns | Low | No (confirmation) | — | 1–2 statements, 0.2–0.4 ms per call |
| L6-3 | Decide whether the payload refs need their own transaction | C Low / B Medium / A High | **Yes** for A or B (ADR-048 inventory; `row_data.py` state definitions; no ADR owns the order) | — | A or B: 5 statements, 1 commit, ≈ 1.4 ms CPU per recorded call |
| L6-4 | Verify a sink member's content once; rebind without re-verifying | Low–Medium | No | — | 250 µs per sink row at 5 columns (≈ 0.8%), 1.9 ms at 100; 12 of 14 canonicalisations |
| L6-5 | Remove the plugin double copy when the file is next edited | Low | No | — | 2.5–6 µs per row per transform; only as a ride-along |
| L6-6 | Build the pending-sink bundle predicate once | Low | No | finding 9 / E-1b | ≈ 0.27–0.43 ms per row (≈ 1%) |
| L6-7 | Carry the row's ingest sequence instead of reading it back | Low | No | — | 16 statements, 4 read transactions per fork row |
| L6-8 | Resolve a token's ownership once, inside the verb's transaction | Low–Medium | No | — | 22 statements, 6 read transactions per fork row (10 statements after E-10) |
| L6-9 | Check a node cursor against the run once per process | Low | No (confirmation) | — | 9 statements per fork row |

Savings overlap with each other and with E-10, E-3a and E-1a; do not add them.

### 2. Recommended order and dependencies

1. **L6-6** — no dependency, no gate change, about 1% of every row. It can go in
   the plan's step 4 ("independent, local"). Land it before E-3a so E-3a's
   `RETURNING` uses the constant.
2. **L6-4** — independent of everything; add its count pin to L4-8 first so the
   7 → 1 is a pinned number moving.
3. **L6-7**, then **L6-8**, then **L6-9** — after L4-8's fork pin exists, in one
   testcontainer batch with the plan's step 5. L6-8 before E-10 gives the
   larger measured delta; after E-10 it is still 6 transactions. L6-9 after
   E-3b, which removes the ingest occurrence.
4. **L6-1**, with **L6-2** in the same commit series (both edit
   `execution/calls.py` and re-pin the same inventory).
5. **L6-3** — raise the three questions with the other rulings in step 8. Its
   option C is L6-2 and needs no answer.
6. **L6-5** — only inside the commits that already edit `value_transform.py`
   (E-9b, L5-7), sharing their hash and manifest re-pin.

Additions to existing plan rows rather than new items: add
`_complete_node_states_many` (`node_states.py:808-832`) to E-3a's site list;
add the `pending_sink_bundle_clause()` line quoted in L6-6 to E-1b's inventory;
note under R-7 that none of options A–D covers the barrier deposit / adoption
pair.

### 3. Examined and recommended NOT fixing

| Finding | Reason | Number |
|---|---|---|
| Remove the fenced transaction from pre-effect `allocate_call_index` (review 11 a, as L5 framed it) | It is the authority check between the claim and the external effect at the pre-effect call sites (seven by position, two read end to end). Removing it lets a worker that has lost its lease call the provider and be refused only when it tries to record. It would also need ADR-048 row 38 reclassified. L6-1 takes the four sites where it buys nothing | 5 statements, 1 commit, 1.4 ms CPU per call, against a provider call of hundreds of milliseconds |
| Payload store's own `sha256` of call payloads (review 11 c) | The store's content address, derived from the bytes it is given; the serialisation duplicate is L2-1 | 1–2 µs per payload |
| Ref in the `calls` insert with files after the commit (L6-3 option A) | A crash leftover would read as `PURGED` by retention | saves the same 5 statements as option B |
| 3. `SourceIngestSpec` thaw-then-freeze | `deep_freeze` detaches by copying anyway; nothing to carry until rows are handed over frozen (L2-4 / L2-5) | 7 µs per row at 5 columns, 73 µs at 100 |
| 4. Plugin double copy as a standalone change | Each file edit moves `source_file_hash`, the scenario-corpus manifest (for `value_transform`) and makes pre-upgrade runs non-resumable | 2.5 µs per row |
| 5. `sink.py:1557` thaw; `node_states.py:493` hash | The thaw has four consumers; the hash is the recorded `input_hash` (L2-4 is the ruling that could carry it) | 1 thaw, 1 hash per sink row |
| 6. Declaration dispatch ×3 per row × transform | Three contract surfaces, audit-complete by design; time is in the contract bodies | 70 µs per dispatch, ≈ 10 µs of it dispatcher |
| 7a. `copy.deepcopy` per fork / expand child | Isolates opaque mutable leaves the freeze does not copy | 4.3 µs per child at 5 columns |
| 8. `check_timeouts` scan | Linear in pending groups, which stay at 0–1 in a single-leader drain | 41 ns per pending group per source row |
| 9. Telemetry projection before the disabled check | Order is deliberate for observers; no manager exists when telemetry is off | 1.3 µs per event |
| 10. Follower `live_leader` per claim | Deliberate forced check, pinned by tests; follower never measured | 3 statements per follower claim |
| `lock_worker_membership_row` in `claim_ready` | Lock-shape change in an eviction-race fix | 3 statements per fork row, 0 per linear row |

### 4. Not verified

- **No prototype.** None of L6-1 to L6-9 was built, even in a private copy. The "expect N → M" figures are read off the trace by construction. No gate was run against any change, so every statement about the mutation-fencing and clock-authority gates is from reading the plan's probe results and the inventory lines cited.
- **PostgreSQL, followers, resume, faults.** Nothing was run on PostgreSQL or with a second worker. The crash table in Part 1 is from reading the order of operations, not from killing a process. Retry and error call paths were not traced; every call figure is the success path.
- **Why the row → file → ref order was chosen.** The checkout has 697 commits of history and `git log -S` reaches only the import commit, so the originating change and its rationale were not found. The two "facts that hold today" in Part 1 are inferred from the code and the state definitions.
- **`node_states` and `nodes` rows are never deleted or re-keyed during a run** (L6-2, L6-9). Inferred from the `UPDATE` statements read; not established by a grep of every `delete()` in the tree.
- **Whether a whole-tree gate inventories `object.__setattr__` / `object.__new__` sites** (L6-4), and whether `providers/openrouter.py`, `gateway.py` or `contracts/sink_effects.py` carry signed tier-model suppressions. The keyless trust-tier scan was not run.
- **`plugins/transforms/field_mapper.py:825`** and the bodies of `reference_join.py`, `batch_replicate.py`, `truncate.py`, `passthrough.py` beyond the cited line. The pattern at those lines is from grep plus the surrounding few lines.
- **Pre-effect allocation sites.** Only `clients/http.py:1469` and `clients/llm.py:735` were read through to the external call. `http.py:1833`, `:2190`, `clients/retrieval/chroma.py:263`, `:516` and `pdf_rasterize.py:632` are classed as pre-effect from the lines around the allocation; `http.py:724` is a replay hop with no effect and was not counted.
- **Barrier verbs.** `scheduler/barrier.py` was read only at `adopt_blocked_barrier_item`'s signature; ADR-029 was not opened. The "not designed" row for the adoption pair is a count and a pointer.
- **Expand and aggregation pipelines** were not run. L6-7 and L6-8 are extended to `expand_token` by reading only.
- **Call sites that have no claimed work item in hand** (L6-7). Twelve call sites of `ready_emission` / `ready_fields` were listed; the three in `scheduler_drain.py:773-825` were confirmed to have the claim in scope, the barrier-completion sites in `processor.py` were not each traced back to a `TokenWorkItem`.
- **ADR texts.** ADR-048 was read at the lines cited (context, § 3, the inventory rows 38–43); ADR-030, ADR-029 and ADR-047 were grepped, not read. The claim that no ADR governs the call-payload order rests on a grep of `docs/architecture/adr/` for `payload`, `response_ref` and `HASH_ONLY`.
- **CPU percentages** are a function's measured CPU over a marginal row cost taken from two instrumented runs each (33–34 ms per row), not interleaved A/B pairs: there was no candidate build to interleave with.
