<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Actual Astra getter bounded-control successor 2 review

**BOUNDED SOURCE GO for the G1/G2 controller repair.** The complete successor repairs both defects identified in Astra3's first review. The parser instrument now exercises complete records, and a recorded drain error prevents every named receipt-omission verdict. This clears these two control-source findings only. Production, child/native mutation execution, coherent application, PostgreSQL, nonempty pipeline handoff, final gates and merge remain HOLD.

I read the entire predecessor report `astra-execute-getter-failed-physical-bounded-code-review-1.md` and its evidence JSON. That earlier review and its historical source reads belong to **Astra3**, not this reviewer. For this successor I independently read the complete report/manifest, current and preimage parent, current and preimage guard, unchanged child, original control and full registry dependency. No candidate function, source guard, project import, pytest collection, application, child, SQL or provider was executed by this reviewer. Only reviewer-owned stdlib byte/AST/XML analysis was run.

## Exact reviewed bindings

| Artifact | SHA256 |
| --- | --- |
| Manifest | `378de99ec15d7b549fb486ff71324bf35668557d3bd43d0cb144902659ec6598` |
| Parent successor | `34bf34f0f80df945acdfa6bde0ac0e6dc095a25136ffdd2a04dadd3dd1d1ce5c` |
| Parent preimage | `e0716490c52296a1dbb60f494dafa957bd409072942f815d603b62ebeb9e063b` |
| Guard successor | `39df28d68bd1e82d468f317c6abf4b355edad5a3ced84e0a40298188a1ee90cb` |
| Guard preimage | `53334a6bff9b360b322dae1ea844f949776f2240a2652028bb6f73a764e51b0d` |
| Child, unchanged | `07e654106156f8e9d51280c79b8e5559c05691346301c29e564a4202ecb046b0` |
| Registry dependency, unchanged | `49cc0c0235dc9e0b30dbeef4ca9a7ef029cf01358514d6f28f19add72692e9f4` |
| Original control, unchanged | `be63b7e8baa0c5a64324e7ff4f4b5a21f022922f2b1bee00d774ab825e5685c2` |

All seven declared source hashes match. The child, registry and renamed `control.original.py` match the complete predecessor copies byte for byte. The parent and guard preimages also match their complete predecessor sources. Restoring the two edited parent functions and removing the one new instrument function reproduces the complete parent preimage bytes. No other parent behavior changed; a controlled alteration to the reaper prevents this comparison from passing.

## G1 repaired: record framing and useful parser controls

The parser implementation itself is unchanged. It reads only newline-terminated records with the exact prefix, rejects multiple complete records, parses JSON, requires the complete exact key set, requires exact booleans and permits only `None` or an exact string for `drain_error`. Therefore a partially written record remains pending rather than being mistaken for malformed terminal evidence.

The healthy instrument now constructs `_TERMINAL_PREFIX + json.dumps(healthy) + "\n"`. The wrong-boolean case also includes the newline, so it reaches the intended type check instead of silently returning `None`. The test separately checks the unterminated version returns `None`, malformed complete JSON raises `JSONDecodeError`, and duplicate complete records raise the explicit duplicate-publication assertion. These are different controlled failure paths; a missing terminator no longer supplies a false negative for the wrong-type test.

Root's supplied five-node instrument execution includes this actual parser test and passes. The source evidence additionally checks the complete-record expression and rejects the same expression with its terminal newline removed. That reviewer check is structural; the supplied root execution is the behavioral evidence. I do not relabel a source-token check as a parser execution.

## G2 repaired: clean drain required before every omission cause

The first statement of `_classify_actual_snapshot` now returns `None` whenever `snapshot.get("drain_error") is not None`. This dominates the seven physical prerequisites and all four ordered omission checks: counter return, callback-exit receipt, generation join and getter. Empty strings or any other non-None values also cannot reach an omission verdict. Actual parent calls feed this classifier through the strict parser, which requires the `drain_error` key; the `.get` operation is not an alternative admission for a missing field.

The new `test_generic_drain_failure_blocks_every_receipt_omission_cause` establishes each clean selected omission as its expected cause, then adds `drain_error="RuntimeError"` and requires `None`. Its baseline has all physical prerequisites and other receipt flags true. Thus the negative is not satisfied merely because an unrelated prerequisite was absent. The existing test also retains the healthy result, each missing-prerequisite rejection and all four clean omission positives. Root's actual selected test execution passed this new test.

Removing the leading guard reproduces the **entire old classifier AST**, independently measured. Under that old classifier, the new test's first clean counter omission still returns `counter`, but adding the drain error also returns `counter`, contradicting the new assertion. The source guard contains an isolated behavioral check of this exact removed-guard mutant: it first exercises the healthy and parser cases, then must fail on the competing drain fault. This is a well-targeted control in source. The author reports executing it, but no separate raw successor guard-run log was supplied in this review; I did not run it and do not claim independently measured execution of that guard.

A non-None actual drain error now leaves expected-RED mode without a causal omission assertion. A later process exit or deadline cannot change that into one of the exact named causes. The parent instead reports the real ordinary failure, absence of the expected causal assertion, or inconclusive timeout. A deliberately failed real drain remains a root-owned native negative to execute later; these dictionary instruments do not establish actual executor-drain behavior.

## Unchanged physical custody, completion and finite process protocol

The complete unchanged child still uses actual registry/finalizer ownership, a real private ThreadPoolExecutor, exact registering-generation identity, actual reservation/Future identity, invocation witness, counter-return and failed-callback-exit observations. It observes the actual second SQL transaction exit and release retirement, actual private join return and actual generation drain-thread exit. The terminal JSON does not mint a capability or receipt. Its drain field reports the actual generation error class when present; the repaired parent now treats that observation as a competing failure.

The completed healthy child path retains the three actual cancellation objects delivered to the exact joining Task, then compares the final group's members by identity and order with those originals and the actual callback failure. It keeps strict success false, retains the original failure group, requires strict completion refusal, and leaves watchdog COMPLETE false. The full unchanged registry preserves the separate physical-observation and strict-success predicates, actual registered-capability ownership checks, and strict `assert_completed`. No new provider or tutorial path is introduced.

The parent still owns the exact child and new process group, uses the finite 65-second observation deadline, always attempts `wait(timeout=5)` during reaping, and retains signaling/wait faults before considering expected RED. A ProcessLookupError after poll does not skip wait. Cleanup errors are grouped with the actual primary error and block acceptance. The parent cannot turn timeout, missing/malformed/duplicate snapshot, wrong cause, child failure or reaping error into its expected named assertion.

Expected-RED mode intentionally returns pytest success **after verifying an exact named parent AssertionError** and reaping. It is an assertion-verification protocol, not a native pytest exit-1 mutation result. Future evidence must retain the exact mutant source hashes, terminal JSON, named observed cause, child return code and completed wait. Early parent termination of a mutant does not establish that the child completed its later cancellation-group assertions; those remain claims of a successfully completed native baseline or separately observed completed mutant.

Source hashes are checked at the declared endpoints and mutated runs require an explicit map containing the registry and exact unchanged child. This does not inventory every production dependency, prove intermediate immutability, apply or restore source, or itself establish coherent composition. Root owns the complete mutation inventory, exact final source epoch and restoration measurements. The child has local bounded waits, but a blocking real executor shutdown still relies on the independent parent process bound; a failed parent wait is an explicit cleanup fault, not proof of successful reaping.

## Verification provenance and limits

Reviewer evidence is `astra-execute-getter-failed-physical-bounded-code-review-2-evidence.json`, produced by `astra-getter2-source-evidence.py`. Its successful run completed native 0, chunk `a8d118`, with full raw output in `astra-getter2-source-evidence-02.log`. It verifies all hashes/parity, complete parent byte projection, unchanged parser AST, the leading clean-drain guard, all four paired omission cases, nine authored source controls, and the root XML/exit bindings. Controlled missing-newline, removed-drain-guard, changed-reaper and changed-parent-byte checks fail their intended source comparisons.

The first reviewer projection attempt ended at an assertion because the reviewer removed only two of the three newline characters preceding the inserted function, leaving one extra blank line. Its full native-1 log is retained as `astra-getter2-source-evidence-01.log`. A complete diff identified that sole projection error; the reviewer instrument was corrected to remove the exact three-newline separator and then passed. No candidate bytes were changed to obtain that result.

I read `getter2-instrument-tests-1.log`, `.exit` and `.xml` in full. Root's native completion `8b4b26` records exit 0, five passed tests and one retained unknown-`env_files` configuration warning. XML names exactly the source-hash test, parser/classifier test, new drain-error test and two reaper tests, with zero failures/errors/skips. Root's `getter2-root-instrument-provenance.json` confirms the exact external successor parent was selected directly, without a copy or child-test selection; its currently measured hash equals the frozen manifest. This is root-supplied instrument execution, not this reviewer's execution. The selected path/current hash record does not independently prove an intermediate source freeze, and no such stronger claim is made.

The guard's nine string-removal controls establish required-source-fragment sensitivity. Its new isolated parser/classifier exercise is narrower behavioral evidence when actually run. Neither proves the entire subprocess protocol, production receipt issuance, native SQL order, source restoration or PostgreSQL. The five supplied tests also do not invoke the child, application or real drain.

The child continues to require an **empty** `joined_pipeline_submission_failures()` tuple. Nonempty handoff remains unproved. Both SQL releases were initiated before quarantine, so no fresh post-quarantine admission is proved. No new production application or canonical/source-map admission follows from this bounded GO. The canonical globals-closure NO-GO remains separate and unchanged. The original 102 obligations, four collections and two UNKNOWNs remain; John retains final local testing, manual scan and merge decisions. Prior artifacts are preserved, and the denied external Daybreak operation remains stopped.
