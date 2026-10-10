<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Actual Astra bounded failed-physical getter control review 1

**NO-GO for the exact control package.** Two source defects remain: G1 makes the parser instrument fail on its nominal input; G2 allows a recorded drain failure to satisfy an expected missing-receipt verdict. The existing narrow production source GO for registry `49cc…` is unchanged. No runtime, mutation, coherent application or full-gate clearance is granted.

Evidence: [astra-execute-getter-failed-physical-bounded-code-review-1-evidence.json](astra-execute-getter-failed-physical-bounded-code-review-1-evidence.json). All prior immutable packages and review reports remain intact.

Reviewed `execute-getter-failed-physical-bounded-control-1/REPORT.md`, complete manifest, full parent, full child, full source guard and complete original `be63…` control. The registry dependency is byte-identical to the previously reviewed full `49cc…` source; its getter, strict success, completion and handoff methods were reread. The actual generation custodian's quarantine/drain implementation and canonical `_core`/`_lease` fixture definitions were inspected. The prior transport dependency review and app successor-3 reaping conclusions were read in full.

Exact bindings:

| Artifact | SHA-256 |
|---|---|
| original control | `be63b7e8baa0c5a64324e7ff4f4b5a21f022922f2b1bee00d774ab825e5685c2` |
| registry dependency | `49cc0c0235dc9e0b30dbeef4ca9a7ef029cf01358514d6f28f19add72692e9f4` |
| child | `07e654106156f8e9d51280c79b8e5559c05691346301c29e564a4202ecb046b0` |
| parent | `e0716490c52296a1dbb60f494dafa957bd409072942f815d603b62ebeb9e063b` |
| source guard | `53334a6bff9b360b322dae1ea844f949776f2240a2652028bb6f73a764e51b0d` |

## G1 — parser instrument omits the record terminator

In `test_execute_failed_physical_join_bounded.py:142`, the healthy call passes `_TERMINAL_PREFIX + json.dumps(healthy)` without a newline. `_extract_actual_snapshot` at line 44 deliberately accepts only complete newline-terminated records. Therefore this input produces no snapshots and returns `None`, contradicting the immediate equality assertion. The malformed-boolean call at line 144 has the same omission, so it would return `None` rather than reaching the intended type assertion.

This deterministic source defect prevents the authored instrument from establishing a healthy baseline and its type-rejection negative. Root subsequently supplied an independent instrument-only run confirming this exact assertion failure; its evidence is recorded below. Append the real framing terminator to both complete-record test inputs and separately assert that an incomplete record remains pending. Preserve the strict parser: its incomplete-record behavior is useful when the parent reads while the child writes. Add malformed/duplicate record negatives if claiming those parser properties. The supplied source guard only checks that this test's name exists; its successful source checks do not establish this instrument's efficacy.

## G2 — a generic drain failure can become accepted expected RED

`_classify_actual_snapshot` at line 57 checks physical prerequisites and then missing counter, callback, generation or getter flags, but never examines `drain_error`. The monitor publishes a non-None drain error's class name in the terminal snapshot, and the parser accepts it. Only the ordinary baseline path rejects non-None `drain_error`; expected-RED mode bypasses that baseline assertion.

The actual `RequiredExecutorGenerationCustodian._drain` calls executor shutdown and reservation release inside one try. Any exception is caught into `self.drain_error`, after which the thread exits. Thus `not drain.is_alive()` does not by itself prove successful drain return. A drain that fails during shutdown or reservation release can have a stopped thread and false `generation.joined`, while the selected private Future and second SQL release have already completed. The monitor can truthfully publish those observations. Given the remaining prerequisite flags, `_classify_actual_snapshot` chooses `generation`, and `ELSPETH_GETTER_EXPECT_CAUSE=generation` permits the parent to raise and accept `CAUSAL_GENERATION_JOIN_RECEIPT_MISSING_AFTER_ACTUAL_DRAIN_EXIT` despite the explicitly recorded competing drain failure.

The JSON record itself is not fabricated; the classification is too broad. The selected receipt omission has not been distinguished from a generic failed drain. Require a clean drain error field before applying this omission classifier, or define a separate fault protocol with the exact independently observed cause. Author a negative that starts with all prerequisites true, sets each selected receipt false and gives `drain_error` a non-None value; none may satisfy the omission protocol. A deliberately failed drain after the selected physical work completes would be a meaningful native negative under an owned process. No such execution was performed here.

## Preserved actual custody and baseline oracles

The child retains the predecessor's real SQLite authority acquisitions, two actual `SessionOperationLease.close` calls, nominal registry/finalizer owner and real private ThreadPoolExecutor. Its transaction hook forwards into the actual `_locked_transaction`; the second SQL marker is emitted after that context exits. The private marker follows actual `shutdown(wait=True, cancel_futures=False)` and exact registry join-return recording. The callback hook faults only the actual capability's reservation, and otherwise forwards to the real generation completion observer.

The monitor reads the exact capability reservation and asserts its registering generation identity. It checks that the capability Future is the reservation Future and is actually done; observes the invocation witness; records the private counter-return and failed-callback-exit receipts; checks retained callback error identity; and reads the actual generation joined Event and registry getter. It does not issue any capability, copied claim, completion receipt or authority. It waits for actual second release retirement and real drain thread exit. That is useful terminal observation for the intended scoped omission mutants once G2 is repaired.

For the healthy baseline, the forwarding cancellation hook captures each actual CancelledError on the exact joining Task. The child requires delivery of three distinct retained observations before opening the second SQL gate, then compares every group member by identity and order against those actual originals followed by the actual callback error. It records the original group in the registry, retains the expected callback failure, checks both real release retirements, zero outstanding admissions, true physical observation, false strict success, strict completion refusal and false watchdog completion. PASS follows cleanup. No fake Future/Task or synthetic same-marker exception substitutes for these objects.

Expected-RED mode can terminate the child before its final exception group is delivered; it therefore does not prove the baseline's final cancellation-group identity assertions for that mutant. Keep that claim with the native baseline and any independently completed mutant observation. The monitor acknowledges after publishing its snapshot, and the healthy coroutine waits for that acknowledgement before stopping and joining the monitor. Observer failure emits a different marker and supplies no terminal snapshot.

## Finite owner, reaping and verdict protocol

The parent owns the exact child and a new process group, polls a private raw log against a finite deadline, and enters the reaper in finally. ProcessLookupError between poll and kill does not skip `child.wait(timeout=5)`. Other signal errors are retained while wait is still attempted. Cleanup failures are grouped with the existing causal original and checked before any expected-RED acceptance. These preserve the app successor-3 reaping corrections. The authored reaper instruments exercise exit-between-poll-and-kill, exact wait-error identity and simultaneous signal/wait failures; they were source-reviewed, not run.

Child task waits use `asyncio.wait`, so their local deadlines do not depend on a cancellation-retaining Task accepting cancellation. Child cleanup can still remain unresolved or block in a real executor join; the independent parent process boundary supplies the final bound. A failed wait is an explicit cleanup failure, not a claim that the process was reaped. Parent creation/early setup failures remain ordinary failures and cannot manufacture expected RED.

Expected-RED mode intentionally returns pytest success only after matching an exact named parent AssertionError, checking the terminal snapshot and completing the reaper. That is an assertion-checking protocol, not a native pytest exit-1 mutation result. After G1/G2 repair, root must record exact mutant hashes, raw snapshot, named asserted cause, child return code and completed wait separately. Deadline, missing snapshot, wrong cause, malformed evidence or cleanup failure cannot replace the causal assertion. G2 is the current exception that prevents approval of that claim.

## Source guards and limits

Independent stdlib byte/AST measurement verified every manifest file hash and parsed all corresponding Python sources. Both supplied predecessor copies exactly equal their immutable transport package sources. AST evidence records the two unterminated parser-test expressions and absence of `drain_error` from the classifier's string keys. The full source guard was read; its seven mutations remove selected required source fragments. It is a token-presence/AST-parse instrument, not a behavioral oracle, and does not test either finding above. No guard, parent test, child, pytest collection or project module was executed in this review.

The parent checks declared file hashes before and after its run, including the exact unchanged child helper and selected registry source. Mutation mode requires an explicit hash map. It does not establish that every mutated production dependency was declared, freeze the entire source tree, restore any bytes or prove intermediate source stability from endpoint hashes. Those responsibilities remain with root's coherent execution and restoration process. The child's import provenance assertion checks the worktree source root but does not independently pin every dependency. This source review does not apply the proposed replacement or resolve whole-tree gate obligations.

The registry source still requires exact registered capability identity plus private executor join return and genuine core4 completion/failure receipts. `executor_join_succeeded` and `assert_completed` remain strict. This control expects an empty `joined_pipeline_submission_failures()` tuple; nonempty handoff is unproved. SQLite evidence does not establish PostgreSQL persistence, and both release operations start before quarantine, so no fresh post-quarantine SQL admission is claimed.

Held repository HEAD was read as `d2b73990d137e9725200c897544eebc5b958fdf4`. One exploratory source lookup found the canonical test absent from the live repository; the fixture definitions were then read from the explicitly frozen core4 package. No live-source availability or collection clearance is inferred from that workspace copy. No repository source, frozen candidate, SQL, provider, network, deployment or merge operation was performed.

## Root-supplied parent-instrument evidence

After the source findings were communicated, root supplied `getter1-instrument-tests-1.log`, `.exit` and `.xml`. I read all three in full. The exit file is `1`; the complete log reports **1 failed, 3 passed, 1 warning**, and XML records four tests, one failure and zero errors/skips. The failure is the exact line-142 healthy snapshot assertion returning None. The other nodes exercise source-hash rejection and the two reaper controls. The warning is the retained unknown `env_files` pytest configuration option. Root reports the invocation selected only these parent instruments with no child/project/app execution. This is root-authored instrument evidence, not execution performed by this reviewer, native production proof or evidence that G2 was exercised. Evidence hashes and XML outcomes are retained in the evidence file.

Root also supplied `diagnose_getter1_drain_error_classification.py` and its JSON, plus `getter1-drain-error-diagnostic-1.log` and `.exit`; I read each in full. The isolated exact AST classifier accepts the healthy snapshot as None and clean missing generation as `generation`, then also returns `generation` for that snapshot with `drain_error="RuntimeError"`. Its intended rejection assertion exits 1. The JSON pins the unchanged parent `e071…` hash. This confirms G2's classifier behavior with controlled inputs; it does not execute the child or real production drain, and is not a native receipt-omission mutation run.

G1 and G2 require a separate immutable successor. Production approval is unchanged; child/control mutation execution, canonical fixture integration, coherent application, applicable whole-tree gates, original 102 obligations/four collections/two UNKNOWNs remain held. John retains local testing, manual Daybreak and merge decisions.
