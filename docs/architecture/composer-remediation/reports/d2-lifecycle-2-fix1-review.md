# Portable lifecycle slice 2 — fix1 rereview

Reviewed 2026-11-09. **Original P2: addressed. Spec verdict: pass for the private slice-2 scope. Code-quality verdict: pass for the fix1 delta.** No new actionable finding was established. This closes the bootstrap reader-handoff blocker in `d2-lifecycle-2-review.md`; it does not accept the still-outstanding public integration or native-platform qualification.

## Candidate and scope

Read the appended Fix round 1 report, the complete four-file incremental diff, current bootstrap/control implementations, added probe/test behavior, and recorded verification evidence. Review stayed on the original P2 and possible regressions introduced by its repair; the original full review was not restarted.

Measured HEAD remains `b4064d2700bd4c9413a1cdcb794a95226681d373` in `/workspace/elspeth/.claude/worktrees/composer-remediation-portable-supervisor`.

* Incremental artifact SHA256: `0d7e9663174e7eba3bf507c1b6e6d71b3ac123182609b4662f309ea561f97285`.
* Complete replacement review artifact SHA256: `f7bc2fff2d509817e48cdfc2047dc93a2b2bf43569854cbef47867842edfddf6`.
* Both artifacts passed `git apply --reverse --check` against the current candidate (exit 0).
* Fix1 before/after manifests are byte-identical, contain 6211 entries, and recompute to canonical SHA256 `b6d115fc24d60160a3ab9bd796c82a9f0c27a6b5026f951262048316f18469a3`.
* Comparing the original final and fix1 manifests identifies exactly `portable_bootstrap.py`, `portable_control.py`, `portable_supervisor_probe.py`, and `test_portable_supervisor.py`. Each current file matches its final manifest hash. No membership change was found in that comparison.

No production/test edits, test reruns, broad scans, agents, installs, providers or commits were performed. This review report is the only file written.

## Why the P2 is addressed

`_ControlStream` now owns the socket, target, one `FrameReader`, a bounded reply sequence and an idempotent shutdown-intent flag for the entire connection. `AuthenticatedSupervisor.claim()` transfers this same object once. Neither claiming the receipt nor starting the asynchronous reader creates another decoder. Partial frame bytes and the original connection-wide frame count therefore survive the handoff.

On coalesced WATCHING_ACK plus SHUTDOWN_REQUEST, the stream retains the shutdown intent and exposes the single acknowledgement to `exchange_sync()`. On WATCHING_ACK plus only the shutdown prefix, that prefix remains in the same decoder until subsequent reads complete it. This removes both concrete failure modes from the original review.

The asynchronous reader checks retained shutdown intent before its next socket receive. Root cancellation still passes through the existing once-only `_graceful_requested` guard. Synchronous bootstrap completion and abort also use the same stream, so a fragmented shutdown crossing either exchange no longer corrupts frame alignment. Bootstrap completion marks draining when it observes retained shutdown; abort already marks draining before exchanging its recovery request.

The repair does not relax protocol authority. `_ControlStream.feed()` validates exact nonce/PID on every frame and admits only shutdown intent or the three closed reply kinds. The synchronous exchange still rejects multiple/unrequested replies and an acknowledgement before its request is sent; callers still check the exact expected reply kind. The asynchronous reader still requires its one pending future and exact expected kind. The parser's original size/count limits remain active across bootstrap, and pending replies have an explicit ceiling. The synchronous one-second budget is fixed and is not reset by shutdown traffic. The parent's independent deadlines, queue limits and actual Popen authority are unchanged.

The endpoint retains one asynchronous consuming reader. Binding the root closes the synchronous bootstrap phase through the existing phase guards, while synchronous bootstrap calls complete before root binding. BEGIN/COMPLETE request tasks, shielding, exchange serialization, witness checks and self-only forced termination are unchanged. There is no new competing socket reader, generic RPC layer, copied codec or wire extension.

## Regression evidence assessment

The pre-repair red log and exit-1 receipt show four actual-Popen early-stop cases failing at the original `Unexpected Composer bootstrap frame` exception. This directly establishes the production-parent HELLO/stop ordering, beyond the original review's socketpair demonstration; these were behavioral failures, not import or fixture-setup failures.

The new tests address the right transition boundaries:

* Nine socketpair controls cover HELLO, BEGIN_RECOVERY and COMPLETE exchanges with ACK only, ACK plus complete shutdown, and ACK plus a two-byte shutdown prefix. They assert identity of the claimed stream, retained receive capacity/prefix, eventual shutdown decoding and refusal of a second claim. These are accurately described as protocol seams, not native process evidence.
* Four positive-then-negative controls verify nonce, PID, direction and preservation of the global eight-frame limit after claim. The ninth frame is refused; receipt transfer cannot reset the count.
* Eight actual-Popen controls cover responsive/unresponsive early stop, synchronous completion/abort, real SIGTERM before HELLO, and fragmented shutdown across async binding and both synchronous paths. The fragment injector only short-writes real socket data and delays the rest; production parent process ownership, selector loop, endpoint and recovery still execute. The `fragmented` marker proves those cases actually exercised the injected boundary.

Inspected all eight final early-stop receipts. The five responsive/completion cases record child status 0, `completed=true`, `failed=false`, `reaped=true`. The unresolved root and two abort cases record child status -9, `completed=false`, `failed=true`, `reaped=true`; the abort cases include `bootstrap-armed`. All eight have empty stdout and stderr. The actual-signal case has both pre-HELLO synchronization markers and stopped/completed markers. Responsive root probes retain the existing assertion that repeated graceful requests do not increase cancellation count.

The final focused exit receipt is 0. Its log records **92 passed, 2 deselected in 36.63s**; parsed XML independently reports 92 tests, zero failures/errors, 36.632s. The two exclusions are the previously proved production-grace and Linux Web armed-channel-loss controls. Their parent/budget code is unchanged by this delta; repeating them was unnecessary for this scoped rereview. The focused cohort includes the remaining 65 private cases and 27 existing Web controls, preserving the prior cancellation/status/direction coverage.

Ruff, formatting, mypy and contracts receipts all contain exit 0, with successful corresponding logs. Mypy covers the two changed production files; Ruff/format cover the four changed Python files. Recorded imports again resolve both project and lints packages inside the assigned worktree. These are verified recorded results, not newly executed suites.

## Remaining limits

The original review's public slice-3, native Windows/macOS/BSD, parent-owned broad-gate and signed/static-corpus limitations remain unchanged. No new claim about those areas is made here. With P2 repaired, there is no outstanding blocker from this review to accepting the private slice-2 implementation and proceeding with the separately authorized integration work.
