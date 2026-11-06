# RESUME TASK 0: make every test finish. The release's first priority, done once by named owners.

**Maintainer's order (2026-10-25):** "fixing these tests is the first task universally - I'm not going to have a bunch of threads hung on my server."

On 10-25, hung test runs held the host at load 60 to 78, with RAM down to 9 GB available, for hours. Workers kept burning CPU on logs that hadn't changed in 57 to 175 minutes. Three times the coordinator had to kill runs, with the maintainer's approval, and lanes that ignored the hang rules made it worse. That doesn't happen again.

## Where the hangs come from (measured)

| File | Behaviour on any tree with the Continuation overlay (fa9e91575 and later) |
|---|---|
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | The escape walk never finishes: one retention query in `tool_batch.py` (the `retained_enum_state` DFS) explores paths without bound. With U-46 (b) it FINISHES, but red and slowly: 961 named rows in about 34.5 min wall time for a single process. |
| `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` | Imports the same walk. 1064 ids take minutes each. Two T5e runs went 57 and 94 min with no log growth (STATUS 06:20); U-46's comparison reached only 29% (EVIDENCE.md). |
| `scripts/fencing_inventory.py` | Same walk. Never finishes without (b). |

At the paused base, pytest had **no per-test timeout configured** despite the
installed `pytest-timeout` dependency. The current Task 0 candidate configures
a fixture-inclusive 600-second thread timeout and disables worker restarts;
the measured derivation and remaining qualification are recorded below.

## Task 0: the release's first priority, done ONCE by named owners. Not every lane.

Task 0 is first in the landing queue, and the coordinator lands it before anything else. It is **not** something each lane solves for itself. Only the owners named below touch it. Every other lane leaves the fencing gate, the reserve-contract tests and the pytest configuration alone. If you have information that bears on it, send it to the owner.

| # | Piece | Owner |
|---|---|---|
| 0.1 | **Suite-wide timeout.** The candidate uses 600 seconds including fixtures, `thread`, debugger detection disabled and no worker restarts. The completed base diagnostic's maximum ordinary passing item was 212.772218031 seconds; twice that rounded upward to a five-minute block gives 600. Both hang files were excluded only for that diagnostic. Existing failures and final no-ignore qualification remain separate. | **Task 0 takeover implementer only.** The coordinator owns runtime slots, independent review and landing. Nobody else edits the pytest config. |
| 0.2 | **U-46 (b)**: rebase `aa044516b` onto the target, supply its evidence (row set, plant classification, hang-to-red wording), and land it. The fencing file then finishes. | **u45-firstcompose only.** |
| 0.3 | **U-46 (a)**: revision 3 per `u46fix/mythos-a2-confirm.md`, then the implementation, one Mythos verification pass, and landing. The walk becomes fast and exact, and the fencing file goes green inside the suite timeout. | **u45-firstcompose only.** |
| 0.4 | **`test_web_reserve_reviewed_contracts.py` bounded.** It shares the walk: measure whether (a) and (b) bound it, and fix it in this task if they don't. Its DFS contract changes as Mythos ruled (⊇ on cyclic roots, exact on acyclic roots and on the corpus). | **u45-firstcompose only.** |
| 0.5 | **Completion evidence required:** `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest` completes on the target with NO `--ignore` of any hang file. Require truthful normal terminal exits (0 or 1), complete stage/driver and supervisor receipts, frozen source, complete ordinary-group cleanup and terminal outcome coverage of every selected ID. Compare exact `(test ID, exception type + first message line)` failing pairs against the frozen baseline: candidate pairs must be a subset, with any changed signature under an already failing ID explicitly adjudicated; no previously passing ID becomes red, and every introduced ID passes. Record inherited unresolved failures. Exit 1 can provide complete red-gate evidence; it alone does not establish Task 0 acceptance or a GREEN gate. Only after the coordinator accepts the complete evidence and no-regression comparison may it delete "KNOWN HANG / --ignore" instructions from AGENT-BRIEF.md. | **Coordinator.** |

## Until Task 0 lands

**Current Task 0 launcher update:** public foreground and `--execute --detach`
now run the non-detaching driver under the existing process-group supervisor,
including execution preflight and source sampling. The aggregate
`--run-timeout-seconds` default is 10800 seconds (three hours), with a
10-second grace per cleanup escalation. This is a finite capacity budget,
not a proven suite duration bound, and is separate from the 600-second
fixture-inclusive per-item timeout. The completed 5400-second base-duration
diagnostic was a separate authorization, not final acceptance. Detached callers
wait for `launcher.exit` and inspect the actual
`supervisor.exit`, `supervisor.json` and existing summary/stage/driver receipts; `.done`
is driver-only. Ordinary-group cleanup excludes deliberate session escapes.
See [contributor guidance](../../../CONTRIBUTING.md#running-quality-checks).
The present takeover keeps other lanes paused; the older resume prose below
does not authorize their revival. The coordinator may allocate up to 16
workers for Task 0, with only one broad suite at a time. The old `-n 3` and
`timeout 2400` rule below describes the earlier interim guard, superseded
only by explicit coordinator allocations. No KNOWN HANG instructions are
retired yet. See the [measured policy and limits](../0.8.10-task0-retention-fixpoint.md#measured-timeout-policy).

- **Every other lane resumes its OWN unit right away.** It does not wait for Task 0, and it does not work on Task 0. The guardrails make a hang impossible:
  - one pytest run per lane at a time, `-n 3` at most, always wrapped in `timeout 2400`;
  - both hang files `--ignore`d on every run;
  - no directory-wide runs without a slot from the coordinator.
- **Landings:** Task 0 lands first. Units that are ready (protocol, U-02, the carve stack starting with 506561cd4, U-45 part 1, T5e) may land while 0.3/0.4 are still in progress, as long as their evidence uses the guardrails. The final Q gate needs Task 0 complete.
- A log that hasn't grown in 15 minutes means the run is hung. Its owner stops it immediately.
- The coordinator checks `ps` and log staleness at every checkpoint, not only when asked.
