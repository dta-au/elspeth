<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Actual Astra runtime source composition review 1

**Verdict: bounded source GO for the nine declared composition deltas and exact retained selected inputs; NO-GO for treating this 25-file package alone as a coherent application overlay.** Two reviewed EXECUTE consumer dependencies are omitted. The held route and recovery callers do not supply the acquisition obligation required by the selected execution service. Correct this selection gap before ordinary route/recovery integration or interpreting application diagnostics as coherent-source evidence.

The source assessment can support root's separately scoped, isolated diagnostics of the reviewed selected modules under their actual dependencies. It does not clear native behavior, application operation, canonical admission, source51/52 maps, or final architecture gates. No candidate/project imports, tests, SQL, provider operations, repository edits, application or network transmission occurred in this review. This is internal Astra, not Daybreak.

## C1 — missing EXECUTE acquisition consumers

The package selects `execution/service.py` SHA `566a107c62e7cfe61fef4310bf76cf84c4cb9711d03f7e4b6d87fd3edcf44ca5`, but selects neither `execution/routes.py` nor `execution/recovery.py` from the reviewed consumer V3 package.

Actual held source remains:

| Dependency | Held preimage SHA-256 | Required reviewed consumer V3 SHA-256 |
| --- | --- | --- |
| `execution/routes.py` | `82108abdde9ef9cf528cee70fc5123cd548a19fe5ec58d402124fc93d6b517e1` | `2f91459ea1c65782810c894a3628b798aab22afb2b49fba79b60833683f7463d` |
| `execution/recovery.py` | `eb8a8df8b80e6ae610ba31d53abecfda79bfe756974a5d268183d8097cae8c7a` | `33a837822149e283d4da30b02b85c128c3391638e374be7fc8deba57e0186415` |

The actual EXECUTE acquisition calls are at held route line 1034 and recovery line 238. Both omit `execution_obligation`. The selected lifecycle's `acquire` deliberately retains the generic path when this argument is absent, constructing a lease with `_execution_obligation=None`. The selected service's `_execution_obligation` requires an exact `ExecutionAcquisitionObligation` from its application registry, bound to that same lease, before business dispatch. Therefore these old callers cannot satisfy the selected service contract; this is a source-derived integration failure, not a claimed runtime reproduction.

The reviewed consumer V3 route and recovery replacements pre-admit from the actual application/service registry, forward that exact obligation into acquire, assert business dispatch, and retain pretransfer cleanup only when completion ownership has not transferred. Their complete deltas also preserve the body primary through cleanup. I read those deltas and the relevant complete selected acquire/constructor/getter/service admission bodies. Independent AST checks pair each held missing-argument call with its real reviewed forward-positive; both complete held/reviewed file hashes match consumer V3's guards.

**Minimum correction:** include those exact reviewed replacements, subject to fresh live guards and a frozen successor selection, or explicitly narrow the package to an internal-module diagnostic overlay that never claims ordinary EXECUTE route/recovery coherence. Do not weaken service admission or fabricate obligations in production. This finding is not a demand to rerun an already completed source review of unchanged consumer bodies. It requires truthful dependency selection and composition verification. I have not claimed these are the only possible dependencies in the entire application.

## Independent source evidence

Reviewed package: `runtime-coherent-composite-sol-1`, complete report, manifest/artifact manifest, complete patch, composition and validation generators, source-controls data, selected-overlay descriptor and outstanding-span inputs. The reviewer instrument is [astra-runtime-composite-source-evidence.py](astra-runtime-composite-source-evidence.py); results are [astra-runtime-coherent-composite-code-review-1-evidence.json](astra-runtime-coherent-composite-code-review-1-evidence.json) and [raw log](astra-runtime-composite-source-evidence-1.log).

The stdlib-only reviewer instrument completed native exit 0, tool chunk `51980a`. It independently checks every full preimage/replacement hash and AST hash, all artifact and outstanding-input hashes, all selected origin bytes, and reconstructs the entire patch byte-for-byte. For non-selected existing inputs it also checks the held repository preimage. New telemetry dispatch remains absent in the held tree. This is not a claim that all 25 package preimages equal the current repository: many are deliberately selected proposal inputs.

| Frozen artifact | SHA-256 |
| --- | --- |
| Manifest | `9460637da1dd757bf0e6e7ee4114138543187dd707ac9e0388f033159113b528` |
| Complete selected-input-to-composite patch | `80a6947028053267d0a57d7beec57368e7209a2123e0751199e5e9f6d60d2742` |
| Worker | `d6a9323ef2e455d86e848cb28073ef3a7d4c7533dee0d94028985486dfaa259b` |
| Turn | `52b167d070ae13ed6ccc83ef2a92c7fff8bad23eed5dfbb29db07da43b3a72b5` |
| Coordinator | `4cf916455dac2577c3a2c886dd7dc26c9fef8a2065eaa15200fd5968967976dc` |

Expected ASTs are constructed independently from the complete preimages and explicitly reviewed additions; all 25 complete resulting ASTs match. Supplier files are additionally byte-identical to their earlier V9/native-lock source artifacts. Six controlled semantic AST mutations reject: replaced worker refusal body, omitted retained turn forward, omitted manual carrier slot, replaced helper refusal body, extra module statement, and changed ordinal supplier. These comparisons do not merely stop at the manifest hash. Every replacement hash and the complete patch also have changed-byte negatives.

The source-controls changed-file and import-binding inventories agree with independently parsed full ASTs. Their enum declaration count is a source fact, not an actual registered-source inventory or admission result. The author's three changed-input controls stop first at the hash gate; they are useful frozen-input guards, not behavioral mutation results. I read the full author generators but did not execute them: their main functions rewrite package artifacts. Their reported Ruff and preparation results are author evidence only, not reviewer reruns or runtime results.

## Composition and ownership assessment

The worker is observer5 `7800ae53…` plus the reviewed entry checks and retained inner coordinator uses. `_run_started` checks exact lease type before its getter, checks the retained non-None coordinator before the setup reservation, then enters the existing lease/settlement flow. `_run_started_under_lease` independently repeats the nominal entry checks before its first SQL read, and uses that same checked local for observation, continuation reservation and the missing-authority branch. Four old `lease.required_work` loads are replaced, and full-module AST parity excludes hidden unrelated changes. None paths, watcher ownership, cancellation retention, setup closure, failure settlement and terminal readback remain the selected observer5 behavior. The historical source38 worker replacement is not used.

The turn retains its actual `observation.required_work` local after exact coordinator validation and `lease.bind_required_work`. That binding checks the coordinator's exact context and rejects replacement of an already bound coordinator. The four recovery forwards use this local, including the nested post-provider closure; no new binding or provider bypass is inserted. Each recovery helper independently checks a non-None coordinator before response construction, telemetry, state persistence or audit effects. Their synchronous None behavior and complete persistence/failure bodies remain unchanged. Provider planning and tutorial behavior are untouched by these edits.

Coordinator storage contains all eleven actual self fields plus `__weakref__`, including `_manual_proposal_close` and `_manual_proposal_carrier`. The independent instrument checks every self-field Store in the complete class against the declared layout, not only a filtered constructor subset. V7 manual issuance, consumption, recursive ticket/child custody, handoff identity and release barriers remain unchanged. The regular authority Enum alone receives identity hashing; no IntEnum hash changes. Native integer extraction, private ordinal mapping and lookup are exactly the reviewed supplier deltas. The complete V7 source declarations/mappings and policy remain otherwise intact, including its rejection sources; this does not admit those sources to canonical maps.

The selected provider custody, telemetry installation and cleanup owner retain reviewed slots/weakref layouts. Native `_thread.RLock` supplies ticket/coordinator/installation allocation with the same required reentrancy; imported bindings and mutable class/global state still require canonical closure. Storage does not create new ownership or make copied live objects independently authoritative.

Telemetry bootstrap now invokes the exact V9 `3d77…` validator before `assert_process` and installation `reserve`. The validator checks nominal owner/installation, ordinary lookup, canonical member descriptors, integer PIDs, native lock and exact inherited process/reserve methods before those effects. Supported setter/getter/cleanup overrides remain distinct from forbidden reservation dispatch changes. No V8 old-helper overwrite occurs. The new import edge is coherent in the inspected source: custody's installation type import is guarded and its constructor import is local; installation imports the already defined custody types; dispatch imports those two modules. This is source reasoning, not a measured full application import.

## Preserved physical failure and success contracts

Complete bytes preserve shared worker `4ba4114d…`, lifecycle `b20314c1…`, app `145a21bf…`, getter `49cc0c02…`, finalizers `6df51b41…` and service `566a107c…`. Their prior bounded reviews remain the source anchors, including their stated limitations. I reread the actual selected acquisition, binding, finalizer physical predicates, registry getters/COMPLETE predicate, service admission, failed-handoff and successful-retirement bodies.

Known failed finalizer closure still requires the actual claimed capability, exact reservation/Future, completed/exited invocation, retained failure, actual admission-counter return and exact registering-generation join. An early released flag or done Future is insufficient. The registry separately requires the actual private-executor join return. Missing counter return stays Unknown even after generation join. The service uses physical observation only for the existing literal-submit-no-return handoff and retains failures; success and retirement still require successful actual receipts. Registry `assert_completed` still requires executor success and no pending/failure custody. This composition adds no route from known failure to COMPLETE.

Shared observer admission remains distinct from finalizer/ticket authority, with actual generation/cutoff checks. Lifecycle retains the Event cancellation seam and single required import through its exact combined source. V7's finish-once function overlay remains part of the exact combined worker; its raw original/cancellation and actual handoff requirements are not replaced by a stale whole-file source. No rank, SQL retry, provider, pool-capacity, Unknown or public-selector waiver is added.

## Reading scope and remaining holds

Fresh full-file reading covers all eight changed production modules other than the large `_helpers.py`: quota, telemetry/bootstrap, telemetry custody, telemetry dispatch, telemetry installation, required work, composer worker and composer turn. For `_helpers.py`, I read all imports and all three complete changed helper functions. Its unrelated remainder is carried only through complete AST/byte provenance to the earlier reviewed helper input; it is not newly audited line by line.

The sixteen unchanged selected files are fully byte/AST checked, with the named earlier bounded assessments carried forward: CODE7 rejection, shared core4/observer5/V7 assembly, coherent app assembly, core4, observer5, consumer3 and failed-physical app/consumer source review. I read those review anchors. This is not a fresh line-by-line audit of the entire large app, execution service, sessions service/protocol, planning and route bodies. Previous control defects and remaining unknowns are not cleared by source equality. The omitted consumers are why a package-wide coherent application GO is withheld.

The five supported telemetry test subtype slot repairs are outside these 25 production files; final test assembly must preserve their previously reviewed compatibility changes rather than treating old dictionary-bearing fixtures as newly admitted. Likewise, no constructor/control successor or TLS diagnostic is approved by this composition review.

Root must capture actual current-live preimages before any application; this package's patch is selected-input-to-composite, not a ready current-tree application patch. Optional import sorting/formatting remains unapplied. Any later formatting changes need honest refreshed source guards; no alias padding or suppressions are warranted. Final applicable Ruff/type/whole-tree checks remain necessary.

Canonical dynamic-selector/global closure remains NO-GO. Fresh recipes for the final eleven-field source, source51/52 writer admission, actual native provenance and causal controls, SQLite/PostgreSQL lock tests, frozen full gates, operator signatures, John's local testing/manual Daybreak and merge all remain held. No test or collection result follows from this report. The original **102 obligations, four collections and two UNKNOWNs** remain unchanged; external Daybreak denial remains stopped.
