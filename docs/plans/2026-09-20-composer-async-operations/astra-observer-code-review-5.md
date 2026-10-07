<!-- Durable copy: machine-specific prefixes normalized; private original retained. -->

# Astra observer code review 5

Verdict: **GO for observer5's narrowly reviewed production source; HOLD for control execution, coherent integration and application.** The actual producer-entry handshake repairs O4-1 without reverting to marker-only cancellation masking. No new blocking production defect was found. The companion entry-failure control has a finite-bound defect described below; it is not cleared for the required mutation as written.

Actual internal Astra review of immutable `transport-cleanup-observation-implementation-1/observer-package-5`. No project imports, runtime, test collection, SQL, provider calls, network/transmission, repository writes/application, deployment, merge or new agents. The previous observer4 NO-GO remains valid for its own frozen bytes. John retains local testing, manual Daybreak and merge decisions.

## Exact source review and custody

Read the package README, guards/manifests/complete patch/source inventory, all new production and test bodies, and the complete companion `observer5-entry-failure-control/tests/unit/web/test_operation_loss_entry_failure.py`. The full observer4 reader/worker and full shared async/lifecycle sources were read in the preceding review. Fresh byte comparison confirms reader, observer async and observer lifecycle are unchanged. All changes to the worker were inspected in their full affected methods. An AST comparison that restored only the new loss-owner class, observation fields, finisher and `_watch` to observer4 proved the remaining worker body unchanged. The existing complete test module is an exact byte prefix of observer5's module; every appended control was read.

Evidence: `astra-observer-code-review-5-evidence.json`. All live/stored preimage and replacement guards matched, changed-byte negatives failed, and the complete patch reconstructed exactly. Current worker preimage remains the e218 type repair. AST parsing is syntax evidence only.

- Observer5 patch: `fea6f7b606ff487bfc2e4a70aad2b02addcf4fbc3347bbe99721c147aef5f1ac`.
- Worker: `7800ae53df28e18a718b38d3444a3192cb79faf4030fe9404937c2d2c309ad0e`.
- Main control module: `019c6b93163e58f70bcd6d6e284cfad3c46046984972f8e7c7382dcad989fbda`.
- Original entry-failure companion: `3d5bfba2feb4e67aa97053c16622f9455887ed6588fc203f594cca55e9fc767f`.

The incomplete freeze attempt is not an input or approved artifact.

## O4-1 repair

`_watch` declares `_OperationLossWatchOwner` before allocating the loss Task. Returned Task identity is bound to that owner. The actual producer calls `observe_producer_entry`, proves its actual current Task matches, and sets the Event immediately before calling the canonical lease Event wait. There is no intervening suspension between publishing entry and entering that coroutine's Event wait.

The private finisher verifies the owner refers to its exact Task, then waits until either actual entry is observed or the actual Task is done. Only then can it issue its private cancellation. Consequently, a normal no-yield terminal read no longer allows cleanup to cancel the loss child before its catch boundary exists. A genuinely cancelled/failed Task that ends before entry exits the handshake through `task.done()` and is retrieved as a business outcome. No fabricated Event or marker equality turns that outcome into private cleanup.

Entered-child classification still requires the exact object recorded at the real child Event-wait catch and the issued marker. Caller cancellation caught in the finisher is transferred through the separate caller-reader provenance boundary. The child's returned/thrown result never enters that boundary. A distinct same-marker child CancelledError, actual same-marker SQL fault, or foreign pre-entry cancellation remains a business result. Existing rank policy is unchanged.

## Allocation failure and owner disagreement

The loss coroutine and owner exist before Task allocation. If allocation schedules a hidden Task and then raises, actual producer entry binds that Task; the allocation original is retained and recovery is requested. The waiter does not retry allocation, close the coroutine, fabricate no-work evidence, or infer absence from a missing returned Task. While ownership remains unknown, each delivered outer cancellation is retained separately. Once the actual Task binds, the original failure still escapes after the child is joined. A recovery-request exception is grouped with the allocation original rather than replacing it.

Returned and producer Task disagreement retains the conflicting actual Task and raises integrity failure. The finisher observes retained conflicting owners independently and does not privately cancel them as if they were the selected loss producer. Missing ownership can remain pending under recovery; this is not a successful release receipt. No new exception category or reduced original policy was added.

The owner object is private invocation bookkeeping, not an external authority DTO. Its actual Task/entry relationship controls only this selected cleanup path. The lease, running context, SQL receipt, cutoff and generation authority checks remain those already reviewed in observer4.

## Actual control source

The new no-yield control uses a real shared concurrent Future. A test wrapper waits synchronously for that actual Future to finish before returning from submission, making the terminal read complete without yielding to the queued loss child. It then uses the actual `_watch` and finisher. The private-close case requires one actual child entry, no business errors and no owner cancellation. Removing entry waiting should turn that causal oracle red.

The foreign pre-entry case cancels the actual Task before entry with the **same object** later used by the helper's private marker constructor. It requires no child entry and retains a cancellation excluded from reader-private provenance. The returned/thrown same-marker variants create distinct actual child exception objects and compare exact identity and worker-lost selection. This distinguishes the repair from a marker-only workaround.

Scope caveat: the foreign pre-entry variant checks type, marker arguments and private-provenance refusal, but does not capture and compare the original object from the first Task.result retrieval. The returned/thrown variants do assert their exact child originals. Do not report the former as a measured exact-first-result identity oracle. No production identity replacement was found in the reviewed source.

The before/after allocation controls intercept the actual Task allocation boundary. The before variant retains the exact coroutine, delivers three originals while no child exists, requires recovery/Unknown to persist, then the controlled supplier schedules that same retained coroutine once. That is fixture resolution of a known supplier boundary, not production retry or a forged entry receipt. The after variant schedules the actual hidden Task before raising. Both require the original allocation failure and retained cancellations after the one actual child joins. A separate owner control rejects a foreign returned Task and retains both identities.

Existing full-pool/no-submission, cutoff-before/after-slot, copied/replayed refusal, actual SQL timeout, repeated cancellation, same-marker SQL, cadence, nested entered-child, installed permanent drain and owned LEASE_RELEASE controls are unchanged byte-for-byte. Their earlier substantive source assessment carries forward. No source inventory count establishes a passing case or coverage gate.

## T5-1 — companion timeout is not an independent finite bound

The original companion wraps `_finish_operation_watcher` in `asyncio.wait_for(..., timeout=1.0)`. The required deliberate mutation removes the `watcher.done()` arm and waits only for the entry Event. In that mutant, the helper catches and retains the cancellation issued by `wait_for`; `wait_for` then waits for cancellation completion that never arrives. The one-second value therefore does not make this negative control finite.

This is a control defect, not evidence that the healthy producer-entry repair is wrong. Transport agreed and announced a separate test-only successor; its mutable/prepared bytes are not reviewed or approved here. Root must use an independently owned finite process/runner or a separately reviewed safe test repair, obtain the intended assertion failure, and join the actual mutant owner. A generic timeout/kill alone is not the requested causal red. The companion as frozen cannot clear this mutation obligation.

## Integration and remaining gates

Observer5 retains the observer4 shared-file dependency: its standalone lifecycle file refers to `_retain_cancellation` without importing it. Core4 already contains the byte-identical Event-wait seam and supplies the import, so compose that seam once. Do not overwrite core4 async/finalizer custody with the observer full-file replacement, or overwrite it with the storage full file. The fresh in-memory composition uses exact observer function/import changes and only storage V5's guarded finish-once function, retains unrelated core functions and the e218 worker repair, and parses. Its async hash is `4b310a6589ae398512bc3353a0edd08ce10eaefb666d14053de8c3d801a73861`; this is not a frozen applied candidate.

Core4's app/consumer failed-completion integration is separately outstanding. No prospective approval is given to forthcoming getter, app or consumer overlays. Their source and all controls must be reviewed before coherent native validation. Saturated32 TLS cause remains unproved. No provider/tutorial/server-authored graph, pool/capacity, rank, renewal-retry or golden weakening was added.

Runtime/collection/mutations/Ruff/mypy/whole-tree/PostgreSQL checks: **NOT RUN by this reviewer**. Original 102 obligations, four collections and two UNKNOWNs remain. Branch HOLD; no application, merge/deployment or production/paid testing clearance follows from narrow source GO.
