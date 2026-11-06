# Independent holistic review: timing, caching and validation around a Continuous Composer provider action

> **Root receiving status, 7 November:** This is Fable 5.1's independent source
> review of `c900eebd9c`, preserved below. Its recommendations are proposals,
> not implementation approval or release acceptance. Boundary-controlled
> reconstruction of the existing R333 records corrects the headline preparation
> count to **45 worker invocations and 3,570 SQL statements**. The reported 53
> workers and 5,755 statements include two workers/32 statements before the
> selected entry and six workers/2,153 statements after preparation finished.
> The whole-run counts of 5,640 workers and 55,210 statements are confirmed.
> The positive/negative-controlled counter and per-stage inclusive counts are
> recorded in lane receipt `preparation-value/r340-r333-boundary-counts.json`.
>
> Initial receiving source review also finds that the proposed final-read
> deletions are **not presently dominated by the writers**: they retain live
> relational/current-input and corruption checks. A check-ownership review is
> in progress before any such deletion or policy question. In particular,
> prepared detached restore constructs some inputs from the original capture;
> its final current read supplies fresh ingress/transcript/P4/message-recipe
> proof. No final read has been removed on this report's authority.

### Receiving dispositions

John requested practical remediation of O1–O8 and U1–U7. This table separates
confirmed repairs from recommendations still being checked. It is maintained
independently of the timing fix; an investigation is not a completed repair.

| Finding | Current disposition |
| --- | --- |
| O1: repeated full reads | Blanket deletion rejected by source review: writers lack some fresh relational checks. Preserve refusal/publication boundaries; investigate verification without discarded graph construction. |
| O2–O3: repeated chunk loads and canonical checks | Three-module repair under receiving review: reuse one complete fresh closure inside the transaction. Preserve the original guard count and smaller-allowance SQL bounds; acknowledge one additional root/contract read. The existing canonical memo remains; no digest-only cache or larger budget added. |
| O4: self round trips | Removed the repeated immutable-snapshot transfer encoding after correcting ownership. Initial writer/decode checks remain; remaining round trips are being classified by their actual boundary. |
| O5: state parsing and credential passes | Exact-equal state parsing/hash and successful credential-scan reuse implemented; focused receiving pending. Both stored hashes and distinct-version/credential roles remain checked. Structural and whole-text scans differ; neither was removed on an unsupported equivalence claim. |
| O6: settled semantic history replay | John separately approved historical request comparison for already-paid semantic calls. Implemented request/context/hash/scalar comparison with full new-call/publication admission and unchanged descriptor/evidence/current SQL facts; focused native receiving pending. Broader semantic source/action proofs remain. |
| O7: repeated state/policy validation | Deleted the capture comparison that validated state, discarded the verdict and compared the returned authored input to itself. Policy acquisition, hashes/tools and actual source/credential checks remain. Used validation outputs at restore and expected-capture boundaries stay. |
| O8: session transcript work | Keep fresh transcript custody: an actual SQL mutation control disproves an immutable-prefix-only argument. Bounded remaining candidate: reuse bootstrap values constructed repeatedly within one lexical read. |
| U1: late deadline detection | Original-clock checks implemented at unpaid END/EARLY boundaries and specific preparation-worker entry; focused receiving pending. Generic workers and paid settlement remain unchanged. |
| U2: concurrent capacity | Existing Composer job concurrency defaults to four, separate from the 16-thread/32-submission executor and durable queue cap. No two-session failure claim established. Measure actual one/two/four-job overlap after single-session success; use existing controls before inventing new admission. |
| U3: PostgreSQL | Correct preparation count is 3,570 SQL statements, not 5,755. PostgreSQL execution and lock-duration evidence remain owed. |
| U4: input-controlled scanner work | R353 controlled public-detector probe passed: 6,096 distinct benign URLs in 262,128 bytes took a 60.292 ms median; 16/64/256 KiB cases scaled approximately linearly. This is synthetic/default-cap evidence, not a worst-case proof. Keep bounds; lazy first-hit iteration is a measured-priority candidate, not the demonstrated timing fix. |
| U5: session-lifetime audit work | Actual semantic producer expects at most four items, ordinarily three: local repeated counting is already linear per proof. Counter substitution rejected as a false remedy. Whole-session JSON retrieval/canonicalization and cumulative history scaling remain open; a selective query needs real cohort bindings, not guessed columns. |
| U6: legal history versus time allowance | Still open: original uninstrumented END preparation exceeds the unchanged five-second allowance. No cap/deadline increase is proposed to conceal it. |
| U7: caller-owned snapshot | Confirmed red through actual admission, then repaired by retaining the existing strict decoder's normalized value. R341: 32/32 focused controls pass, native exit 0, frozen tree, cleanup complete. Both direct-input variants retain `AdvisorAbandonmentResponse`, not caller dictionaries. |

R339's larger 62-item cohort timed out in two existing async fixture paths and
has no complete JUnit verdict. Its source/test bytes were unchanged, but the
whole manifest changed when this report was written; it is not a qualifying
gate. R341 is the completed, frozen two-module receipt. Neither establishes
release acceptance or a timing gain.

The next original-route receipt, R352, is frozen with native exit 1 and complete
cleanup: first END preparation remains **5.945987410 seconds** at cursor 29.
R350 passed six focused semantic-output delivery/lifetime/corruption controls,
but that alone does not prove useful data reuse on the richer original route.
An actual copy-hit control was then added, including a genuine taken-owner miss.
No timing improvement is established by R352.

R357 subsequently completed **41 tests with native exit 0, frozen tree and
complete cleanup**. It receives the corrected authority-policy fixture, the
actual output copy hit and genuine taken-owner miss, the exclusive-lock weakref
lifetime control, and evidence state/ownership controls. The former object-ID
lifetime census was not a reliable ownership observation. R355 is an interrupted
diagnostic with four reported failures; it is not a qualifying gate.

The memo working-set follow-up also changes the priority assessment: R333's
distinct-key weight estimate is approximately 37.33 MiB, above the 32 MiB memo
budget, with eleven recorder recomputations. A controlled 64 MiB memo experiment
is justified before inventing more cache mechanisms. This is engineering memo
residence, distinct from the unchanged 16 MiB operation-payload allowance and
1 MiB document limits. Four concurrent 64 MiB parents would charge up to 256 MiB
per process; that is not an RSS guarantee. Neither a budget change nor a timing
gain has yet been adopted or qualified here.

Pinned target `c900eebd9c`; `git diff --stat c900eebd9c -- src tests/helpers` is empty, so every source citation below is the pinned source. Read-only: nothing was executed, imported, edited or connected to. This is a source review plus counts taken from existing lane artifacts. It is not test or release acceptance.

**Evidence labels:** [F] source fact I read; [M-Root] measurement supplied by Root; [M-me] a count I took from the R333 spans file; [D] delegated read-only finding I did not re-verify; [I] inference; [P] policy choice for John; [Q] unresolved.

## 1. Verdict

The first END misses the 5-second guard on a deliberately tiny history, and no single function is the cause. One provider action rebuilds and re-proves the operation's whole admitted history several times, through 53 worker hops and 5,755 SQL statements [M-me].

The root cause is a bundling error. Three different proofs are fused into one "full current read" primitive:

| Proof | What it establishes | How often it is needed |
|---|---|---|
| Currency / authority | This process still holds claim, lease and head | At each binding write, O(1) |
| Content integrity | Stored bytes match their digest | Once per load of those bytes |
| Content admission | Canonical form, schema, credential-free under a given policy | Once per digest per policy; it is a pure function of the bytes |

Every time an owner needs the cheap first proof, it pays for the two expensive ones over the whole closure. The caches and object handoffs added so far claw back part of that cost one site at a time. The one measured win (R321, 6.464 s to 5.806 s [M-Root]) came from changing what is proved, not how fast.

My leading recommendation is to unbundle the three proofs. That removes work and code rather than adding carriers. Each removal needs a specific policy decision from John (section 7), because some existing negative controls pin the multiplicity of checks rather than the outcome.

## 2. What one END preparation actually does

The failing route is `test_supported_driver_reaches_original_paid_transitions[read_end_retry]`. Its history before the first END is four provider calls and one tool batch of one read tool, with single-sentence replies and an empty pipeline [D]. So 5.85 s is structural per-action overhead, not data scaling.

Stage times are Root's instrumented R333 spans. Statement counts are mine: the totals are exact greps of the spans file for the target clock `128713036672960`; the per-stage split is estimated from record positions (roughly ±10%).

| Stage | Instrumented time [M-Root] | SQL statements (est.) [M-me] | What it is for |
|---|---|---|---|
| Initial `_primary/current` full read | 1.094 s (read 1.052) | ~710 | Establish current state |
| `dispatch_and_adopt_end_review` | 1.676 s | ~1,490 | Write dispatch checkpoint, reload closure, re-read |
| Capture body | 0.476 s | ~100 | Serialise live runtime |
| Capture final full read | 0.566 s | ~635 | Result discarded (`composer/continuation_runtime.py:1154-1160`) |
| `prepare_v4_checkpoint_source` writer | 1.081 s | ~1,275 | Write second checkpoint |
| Detached restore, excluding final read | 0.726 s | ~810 | Second full runtime, rebuilt from SQL |
| Restore final full read | 0.516 s | ~630 | Only three fields compared (`:409-425`) |
| **Total in this clock** | ~6.27 s | **5,755 exact; 53 worker records** | |

- **Whole test:** 55,210 SQL statements and 5,640 worker hops [M-me].
- **Unreconciled:** Root's summary says 45 selected worker invocations; I count 53 worker records carrying the clock tag [Q].

Two families dominate the selected samples [M-Root]:

- **Full runtime reads:** `read_runtime_source_on_connection` is 176 of 519 samples (34%). The three reads total 2.13 s of 6.27 s, the same 34%.
- **Replay of an already-settled paid call:** `_paid_result_data` is 189 of 519 (36%). The paid semantic Review is replayed in the dispatch writer (`composer/continuation_end_transitions.py:201-204`) and again through `_initial_phase_source` (`sessions/continuation_checkpoint_paid.py:224-232`) [F].
  - That the second site runs inside this preparation is [I].
  - If these two stacks are disjoint, they account for about 70% of samples. I found no call path joining them but did not check the sample tree [I].

By self time, about 53% of samples are JSON parse, canonical write and UTF-8 validation, and about 15% credential regex [M-Root, my arithmetic]. For a history this small, that implies each retained byte is parsed or re-encoded many tens of times per preparation [I, order of magnitude only].

### Why timing, cache churn and validation amplify one another

1. **Re-read loop.** Every owner re-derives its preconditions from SQL instead of receiving them. Adding an owner adds a full read, and each read walks the whole closure. One runtime read does all of the following [F]:
   - Fetches the same chunk from SQL up to three times: `load_selected_continuation`, then `actual_chunk` (`continuation_evidence_restore.py:155-163`), then `load_selected_private_chunks_on_connection` (`sessions/continuation_runtime.py:175`).
   - Issues two statements per `read_chunk`, one of them the immutable read contract (`sessions/continuation.py:426-431`).
   - Re-verifies canonical form by parse and re-encode (`continuation_codec.py:276-279`).
2. **Cache thrash loop.** The memo keys proofs on the full canonical text and keeps text plus encoded bytes (`continuation_retention_memo.py:609`). Its 32 MiB budget is saturated: peak 33,513,114 of 33,554,432 bytes, 14 evictions, 11 recomputations in 46 requests [M-Root]. So evictions are budget-driven and about a quarter of requests recompute something already computed [I]. More re-reads produce more evictions and more recomputation.
3. **Deadline loop.** The guard is wall-clock (`continuation_clock.py:141-153`) over GIL-bound work. Nothing aborts early, so a doomed preparation burns its full cost and slows its neighbours. A retry repeats the same work on the same history. That turns a timing miss into a permanent liveness cliff for that operation [I].

## 3. Boundaries: where the intervention is misplaced

**3.1 Currency is being proved by re-admitting content.**
- The capture path already accepts an O(1) currency check on its "before" side: `read_continuation_env_selection` when the source is unchanged (`composer/continuation_runtime.py:1122-1128`). Its "after" side then does a full read whose result is discarded [F].
- The reads run in separate transactions from the writes, so they are not atomic with anything. Only the checks inside the fenced writer are binding: `_require_current_continuation_running`, head compare and the append with `expected_checkpoint_id`/`expected_cursor` (`continuation_end_transitions.py:179-186`) [F].
- The sandwich reads narrow a window; they do not close it.
- If the writer's checks are a superset of the read's, the read is dominated and removable with no loss of protection [I]. That needs a check-ownership table first (section 8, step 1).

**3.2 Settled history is replayed on every touch.**
- `_paid_result_data` is a full forensic audit of one paid call: effect, descriptor, six source chunks, physical attempt, ledger, both recorder decodes, evidence closure, response and decision (`sessions/continuation_semantic_advisor.py:1585-1740`) [F].
- That is the right check at commit and at cold recovery. Repeating it within one warm action, minutes after the same process committed it, proves nothing new unless immutable rows changed. Row tampering is outside the stated threat model, and corruption is caught by the digest on load.

**3.3 Mutable isolation is bought with a durable round trip.**
- The detached runtime is rebuilt from SQL before SDK entry (`continuation_checkpoint_end_detached.py:210-266`).
- Its only consumers are the post-return `complete_*` modules: `continuation_checkpoint_end_complete_repair.py:92`, `…_clean.py:69`, `…_terminal.py:107`, `…_review.py:109`, `sessions/…_complete_pass.py:61` [F, grep].
- Pre-SDK, only identity checks touch it. Building it before payment keeps restore failures unpaid, which is a real value, but the cost sits inside the guard for something not used until later.

**3.4 The largest admitted bytes are probably ELSPETH's own.**
- PRIMARY requests carry the real system prompt and tool palette, and the recorder reconstructs a full request string per call [D, and R330].
- If so, most re-scanned and re-canonicalised bytes are first-party static text treated as hostile on every pass [I, unmeasured].

## 4. Cache identity, residency and accounting

- **Identity.** Proof families key on `(producer, full text)`, for example `PrivateChunkVerificationKey(reference, canonical)` and `CanonicalTextEncodingKey` [F]. Storage already identifies chunks by SHA-256 (`chunk_key = value:codec:digest`). Refusing digest identity for admission facts defends against a collision that would already break storage.
  - The identity that fits the facts is `(codec, digest, byte_length, detector version, environment-name set)`, after one hash check on load.
- **Residency.** Text-keyed proofs retain megabytes to remember a boolean. Digest-keyed facts are about 100 bytes each. The budget would then hold only decoded recorder data, which is the part with real reuse value.
- **Accounting.** `recorder_data_retained_bytes` walks the object graph on every insert (`continuation_retention_memo.py:175-267`). There are also 13 families with hand-written eviction orders. This accounting is itself O(data) work, created by the entry design.
- **Scope.** All of this can stay action-scoped and closed at the clock cut, as today. I am not recommending any process-wide cache. If John wants first-party static blocks admitted once per process, that needs an explicit entry bound and is his call [P].
- **R329's caution stands:** do not blanket-retain recorder sources.

## 5. Validation: over-care and under-care

### Over-care (redundant under the stated threat model)

| # | Check | What it proves | Why it is redundant here |
|---|---|---|---|
| O1 | Full read before and after capture, restore and adopt | Currency plus everything else | Only currency is new information |
| O2 | Triple SQL fetch and byte compare of one chunk per read | Bytes equal bytes | Same transaction, same digest |
| O3 | Canonical parse and re-encode on every touch | Canonical form | Property of the bytes; once per digest |
| O4 | Self round trips: decode-then-dump compare, `decode(encode(stack)) != stack` (`sessions/continuation_selection.py:42`), validate own freshly encoded JSON (`continuation_evidence.py:863-867`) | Codec has no bug | Belongs at write and in tests, not on each read |
| O5 | State JSON parsed four times per evidence admission (validator `:292-293`, field semantics `:786-795`), then the whole chunk credential-scanned again as one string (`:506-511`) | Same bytes clean | Structural scan already covered them |
| O6 | Forensic replay of a settled paid call per writer | History consistent | Proved at commit |
| O7 | `_policy_context.build` plus `validate_composition_state` three times (capture `:865-870`, expected capture, `_restore_live` `:323-324`) | Policy unchanged | Policy hot-swap in one action is out of scope |
| O8 | Session-wide transcript reselect and re-canonicalise per read (`continuation_prompt_producers.py:185-228`) | Prefix unchanged | Immutable prefix |

O5's whole-text scan also has different semantics from the structural scan. Nested JSON strings are escaped, so key/value patterns cannot match inside them; it is effectively a second token-shape pass [I].

### Under-care (real gaps)

- **U1. No early abort.** The preparation guard is only checked at a few sites and at `finish_preparation`. The runtime read has no clock check, and SQL deadlines are enforced only on PostgreSQL [D]. Doomed work runs to completion.
- **U2. No measured capacity.** There is one shared 16-thread pool and a 32-slot admission counter, with no CPU- or latency-aware admission [D]. One preparation alone uses 117% of its budget, so two concurrent END preparations in one process would both miss [I]. Hostile outsiders can open several sessions. This must be measured, not inferred.
- **U3. PostgreSQL exposure.**
  - 5,755 statements per preparation each become a round trip on PostgreSQL [I]. The default selection excludes the PostgreSQL suites (AGENTS.md).
  - The read transaction holds a per-session advisory lock across the CPU-heavy callable [D].
  - Prediction: the route is slower on PostgreSQL by statements times round-trip time.
- **U4. Scanner cost is input-controlled.** `_http_url_candidates` runs `urlparse`, `parse_qs` and about twenty regexes per candidate (`contracts/credential_material.py:369-398`). Strings are bounded in bytes, not in candidate count. A user- or provider-authored string dense with short URLs could cost seconds per scan, and it is scanned repeatedly [I, needs a probe].
- **U5. Session-lifetime scaling inside per-action proofs.** `_physical_source_data` selects every audit row in the session and runs `retained.count(item)` per expected item (`sessions/continuation_semantic_advisor.py:1144-1151`) [F]. Cost grows with session age, quadratically in the worst case.
- **U6. Contract inconsistency.** Policy admits 16 MiB aggregate, yet a history of four small calls already exceeds the time guard. A session can legally grow into a permanent pre-SDK `TimeoutError`, which is a timing accident rather than an honest resource refusal [I].
- **U7. Direct-snapshot admission** keeps the caller's object and discards the validated one (R312; I concur).
  - The uncommitted probe's input appears artificial for the three production call sites inspected; four more were not inspected [D].
  - Its final assertions would probably fail without the mutation, so it needs a no-mutation control arm before it discriminates anything [D].

## 6. Workers, cancellation and test structure

**Completion polling.** `run_required_sql_finish_once` waits with `while not future.done(): await asyncio.sleep(0.01)` (`async_workers.py:870-874`) [F]. Every hop's completion is seen on a 10 ms grid.
- Expected cost is about 5 to 10 ms per hop: roughly 0.27 to 0.53 s of this preparation, and 28 to 56 s of the ~120 s test [I].
- It is directly measurable from the existing spans, as the gap between each worker's end and the next loop-side record.
- Deferred cancellation needs the awaiter to outlive cancellation, not a poll.

**Link to the fixture stall.** The product bridge polls, so it cannot lose a wakeup. Tests use raw `asyncio.to_thread` widely (147 occurrences in 44 continuation test files, regex proxy [D]) and so depend on native future delivery, which is where the stall appeared.
- The cause remains unestablished [Q].
- R323's JSON only calibrates the observer on a synthetic case [D].
- A hybrid wait in the bridge (native wake with the 10 ms poll as fallback, counting fallbacks) would both remove the quantisation and produce direct evidence on delivery reliability.

**Test-builder effects** [D, regex proxies, not registry counts]:
- **No shared builder.** Every full-app test drives 20 to 30 real actions per run at several seconds each.
- **Tests pin structure.** About 406 `setattr` calls on private names and 883 `pytest.raises(AuditIntegrityError, match=…)`. These pin which helper proves what, in which order, with which text. Removing a redundant proof turns tests red, while nothing turns red when a proof is added.
- **No contention protection.** The guarded test runs under the default `-n 12` with no serial group. A wall-clock guard at 117% has no headroom.

## 7. Prior work: what mattered, and cross-check of the four reports

| Item | Outcome | My reading |
|---|---|---|
| R321 historical PRIMARY compare | −0.66 s [M-Root] | Rule-level change; the only material win |
| Canonical fragment/hash reuse | No endpoint gain [M-Root] | Sped up one pass; pass count is the lever |
| Prior-evidence copies | Both hit [M-Root] | Working, but most constructor entries are other cold admissions |
| Memo families | 28 hits, 11 recomputations [M-Root] | Saturated budget; entry design, not budget size |

- **R312:** facts concur. Fix at the constructor (retain the validated value). Keep the graph value/alias projection.
- **R329:** concur.
- **R330:** accurate tracing, but every ranked option holds the number of reads, hops and statements fixed.
  - Its leading OUTPUT handoff targets one evidence construction in the initial read, bounded by the 58-sample current-evidence bucket. Scaled to the uninstrumented 5.85 s that is at most ~0.65 s, against the ≥0.85 s needed [I].
  - It also adds a one-slot lifetime protocol.
  - I rank it below unbundling, not against it.
- **R334:** the lifetime hold is well reasoned. I did not verify the custody and issuer references it cites.

**How to stop repeating local fixes.** Adopt a count ledger as the acceptance instrument: per action, hops, statements, per-digest touch multiplicity, and bytes parsed or scanned divided by retained bytes. A proposed fix must predict which count falls. A change that moves no count is not a timing fix. Counts are deterministic and insensitive to instrumentation overhead.

### Policy choices for John

- **P1.** Must late same-head corruption of mutable rows be refused at every stage, or once inside the fenced dispatch transaction? The outcome property (no SDK entry, no graph use, honest durable result) holds either way.
- **P2.** May a settled paid decision, fully proved earlier in the same action, be consumed by later owners in that action without replay? This is narrower than the approved PRIMARY trade-off: the first proof stays full.
- **P3.** May the detached runtime be built from the in-memory capture, or after provider return? This changes which failures occur before payment.
- **P4.** Is digest equality accepted as identity for action-scoped admission facts?

## 8. Repair sequence and discriminating measurements

Dependency-ordered. Steps 0 and 1 need no production change.

| Step | Action | Predicted effect [I] | Depends on |
|---|---|---|---|
| 0 | Count ledger from the existing R333 spans, plus test-only touch counters | None; decides everything below | — |
| 1 | Check-ownership table: for each binding writer, list the mutable-row and head checks it performs in-transaction; list what only the reads check | Identifies dominated reads and any check to move | — |
| 2 | Replace capture-final and restore-final full reads with the existing O(1) selection read; move any orphaned check into the writer | Up to ~17% (1.08 of 6.27 s instrumented) | Steps 0, 1; P1 |
| 3 | One paid-Review proof per action, carried to later owners; recheck mutable effect disposition O(1) | Up to about half of the 36% bucket, if two replays | Step 0; P2 |
| 4 | Read contract once per transaction; hybrid completion wait | ~5–8% from the poll; contract share unknown | Step 0 |
| 5 | Re-run the original uninstrumented route, serial on a quiet host | Gate: proceed only on measured result | Steps 2–4 |
| 6 | If still short, or for scaling: digest-keyed admission facts; detached restore; remove triple fetch | Removes thrash; cost follows the delta, not the history | P3, P4 |
| 7 | Protection: in-worker deadline checkpoints; bound the session-wide scans; measured concurrency capacity; one count-budget test | Closes U1, U2, U5 | — |

Steps 2 to 4 together plausibly bring the route to roughly 3.5 to 4.1 s. These estimates are not additive guarantees and come from instrumented proportions.

### Minimum measurements

1. **Touch multiplicity** per closure digest: SQL reads, canonical verifications, credential scans, schema decodes.
   - Prediction: at least 10 SQL reads and at least 3 canonical verifications per digest; `_paid_result_data` entered at least twice; three full reads.
2. **Touch ratio:** bytes parsed, written, validated and scanned, divided by retained closure bytes.
   - Prediction: at least 30×.
3. **Poll gap** from existing spans.
4. **One-step history probe:** the same route with one extra tool batch; record time and statement delta.
5. **PostgreSQL:** statement count times round trip on the same route (owed anyway).
6. **Scanner probe:** 256 KiB of dense URL candidates versus benign text through `find_credential_material`, unit-level.
7. **Concurrency:** one, two and four simultaneous END preparations, when Root has capacity.

### Negative controls that must still fail after steps 2 and 3

- Head moved, or lease/generation lost: no SDK entry.
- Ledger or ingress row corrupted before dispatch: refused in the dispatch transaction.
- Chunk bytes corrupted in SQL: refused on first load.
- New request containing a credential: refused with full admission.
- Paid semantic outcome corrupted before its first proof: refused.
- Mutation control: disable the one remaining dispatch-transaction check and confirm the tests go red.

### What would falsify the leading recommendation

- Touch multiplicities near 1 or a touch ratio under about 5×. Unique cold work would then dominate and the answer is smaller representations, not unbundling.
- The ownership table showing the final reads perform checks no writer can host.
- The history probe showing flat time, meaning fixed per-hop cost dominates and step 4 outranks steps 2 and 3.
- The two sample families overlapping heavily in the sample tree.

## Confidence Assessment

**Overall Confidence:** Moderate. High on the structure and counts; Moderate on the size of each gain.

| Finding | Confidence | Basis |
|---|---|---|
| 5,755 SQL statements and 53 worker records in the target clock; 55,210 and 5,640 in the run | High | Exact grep of the R333 spans file |
| Per-stage statement split | Moderate | Record positions, ~16 lines per record |
| Capture-final result discarded; restore-final compares three fields | High | `composer/continuation_runtime.py:1154-1160`, `:409-425` |
| Chunk fetched up to three times per read; contract re-queried per `read_chunk` | High | Cited source |
| Paid Review replayed in the dispatch writer | High | `continuation_end_transitions.py:201-204` |
| Second replay runs inside this preparation | Moderate | Source reachability; no label in the spans |
| 10 ms completion poll | High | `async_workers.py:870-874` |
| Poll cost 0.27–0.53 s per preparation | Moderate | Arithmetic; measurable from spans |
| Detached runtime consumed only after return | Moderate | Grep of consumers; cited modules not read in full |
| Memo thrash is budget-driven | Moderate | Root's counters and the 32 MiB constant |
| Tiny workload, so the overhead is structural | Moderate | Delegated fixture reading |
| Final reads are dominated by writer checks | Low | Requires the ownership table |
| First-party prompt text dominates bytes | Low | Not measured |
| Scanner cost is input-controlled | Moderate | Source; no timing |
| Two concurrent preparations both miss | Low | Structural inference |
| Executor size, admission and PostgreSQL-only SQL deadline | Moderate | Delegated |

## Risk Assessment

**Implementation Risk:** Medium for steps 2 to 4; High for step 6.
**Reversibility:** Easy for steps 2 to 4 (deleting reads, carrying one object, a wait primitive); Moderate for step 6.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| A removed read was the only owner of a mutable-row check | High | Medium | Step 1 table first; move the check into the writer; mutation control |
| Existing controls pin per-stage refusals and go red | Medium | High | P1 decision first; restate controls as outcome properties, not fewer controls |
| Native future delivery is unreliable in this environment | High | Low | Hybrid wait keeps the poll as fallback and counts fallbacks |
| In-action reuse of a paid proof hides a mid-action change | Medium | Low | Recheck mutable effect and ledger rows O(1) in the binding transaction |
| Gains smaller than the instrumented proportions suggest | Medium | Medium | Step 5 gate on the uninstrumented route |
| PostgreSQL behaves materially worse than SQLite | High | Medium | Measurement 5 before any acceptance claim |
| Digest-keyed facts reused across a changed environment-name set | High | Low | Policy inputs are part of the key; action scope |

## Information Gaps

1. [ ] **Per-digest touch multiplicity and touch ratio.** Decides whether unbundling or representation size is the lever.
2. [ ] **Check-ownership table for the binding writers.** Decides which reads are dominated.
3. [ ] **Sample-tree overlap** between the runtime-read and paid-replay families, and the number of `_paid_result_data` entries per preparation.
4. [ ] **Actual byte sizes** of recorder, evidence and request documents, and the first-party share.
5. [ ] **The 45 versus 53 worker-count difference** in R333.
6. [ ] **PostgreSQL timing** for this route; production pool and dialect settings.
7. [ ] **Cause of the fixture `to_thread` stall.**
8. [ ] **Unverified delegated claims:** tickets accumulating two per hop; product `asyncio.to_thread` at `execution/recovery.py:310` and `execution/service.py:2937` bypassing admission; `locking.py` topology.
9. [ ] **Whether PRIMARY and tool-batch actions show the same proportions.** I traced only END.

## Caveats & Required Follow-ups

### Before Relying on This Analysis

You MUST:
- [ ] Run step 0 and compare against my predictions before authorising any production change.
- [ ] Complete step 1 before deleting any read.
- [ ] Have John rule on P1 to P4. None is implied by existing approvals.

### Assumptions Made

- The R333 spans count statements faithfully and the instrument adds no SQL of its own.
- The default test route uses SQLite.
- Row tampering by trusted operators and policy hot-swap inside one action are out of scope, as stated.

### Limitations

- **Read in full:** `continuation_retention_memo.py`, `continuation_evidence.py`, `continuation_codec.py`, `continuation_evidence_restore.py`, `sessions/continuation_runtime.py`, `sessions/continuation_selection.py`, `continuation_clock.py`, the END physical, dispatch and detached modules, and the four `contracts` files cited.
- **Read in part:** `composer/continuation_runtime.py`, `continuation_prompt_producers.py`, `continuation_semantic_actions.py`, `sessions/continuation_semantic_advisor.py`, `sessions/continuation.py`, `async_workers.py`.
- **Not read:** the approved comparison body in `sessions/continuation_primary.py`, `continuation_audit_buffer.py`, the application graph, the provider gateway and quota, the driver, `locking.py`, and about 150 other continuation modules. R329 was read only to line 90.
- All timings are Root's. Instrumented proportions may not hold uninstrumented.
- Delegated test counts are regex proxies, not registry counts.
- `docs/release/0.8.10-builders-systemic-review-2026-11-06.md` is modified in the live tree and was not reconciled with the pinned version.

### Recommended Next Steps

1. Derive the count ledger and the poll gap from the existing R333 spans; no new run is needed.
2. Build the check-ownership table.
3. Put P1 to P4 to John, with the ledger attached.
