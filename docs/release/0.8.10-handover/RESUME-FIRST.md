# RESUME TASK 0: make every test finish. The release's first priority, done once by named owners.

**Maintainer's order (2026-10-25):** "fixing these tests is the first task universally - I'm not going to have a bunch of threads hung on my server."

On 10-25, hung test runs held the host at load 60 to 78, with RAM down to 9 GB available, for hours. Workers kept burning CPU on logs that hadn't changed in 57 to 175 minutes. Three times the coordinator had to kill runs, with the maintainer's approval, and lanes that ignored the hang rules made it worse. That doesn't happen again.

## Where the hangs come from (measured)

| File | Behaviour on any tree with the Continuation overlay (fa9e91575 and later) |
|---|---|
| `tests/unit/architecture/test_web_landscape_mutation_fencing.py` | The escape walk never finishes: one retention query in `tool_batch.py` (the `retained_enum_state` DFS) explores paths without bound. With U-46 (b) it FINISHES, but red and slowly: 961 named rows in about 34.5 min wall time for a single process. |
| `tests/unit/architecture/test_web_reserve_reviewed_contracts.py` | Imports the same walk. 1064 ids take minutes each. Two T5e runs went 57 and 94 min with no log growth (STATUS 06:20); U-46's comparison reached only 29% (EVIDENCE.md). |
| `scripts/fencing_inventory.py` | Same walk. Never finishes without (b). |

pytest has **no per-test timeout configured.** `pytest-timeout>=2.4` is installed (pyproject.toml:103/212), but nothing in `addopts` turns it on. So a single hung test holds its xdist workers indefinitely.

## Task 0: the release's first priority, done ONCE by named owners. Not every lane.

Task 0 is first in the landing queue, and the coordinator lands it before anything else. It is **not** something each lane solves for itself. Only the owners named below touch it. Every other lane leaves the fencing gate, the reserve-contract tests and the pytest configuration alone. If you have information that bears on it, send it to the owner.

| # | Piece | Owner |
|---|---|---|
| 0.1 | **Suite-wide hard timeout.** Configure pytest-timeout in pyproject (`addopts` or `[tool.pytest.ini_options] timeout =`), with the value derived from measured `--durations` on the target and the derivation recorded beside it. Attribute every test the timeout would turn red first: the landing rule applies, so a slow but passing test is fixed or gets a per-test override with a reason. This is the backstop, so that no test can hang the host again. | **The coordinator assigns ONE owner** at resume (default: u45-firstcompose). No `--durations` data exists yet, so the owner's first step is a `--durations=0` run of the default selection with both hang files `--ignore`d, under a coordinator slot, then the derivation. Nobody else edits the pytest config. |
| 0.2 | **U-46 (b)**: rebase `aa044516b` onto the target, supply its evidence (row set, plant classification, hang-to-red wording), and land it. The fencing file then finishes. | **u45-firstcompose only.** |
| 0.3 | **U-46 (a)**: revision 3 per `u46fix/mythos-a2-confirm.md`, then the implementation, one Mythos verification pass, and landing. The walk becomes fast and exact, and the fencing file goes green inside the suite timeout. | **u45-firstcompose only.** |
| 0.4 | **`test_web_reserve_reviewed_contracts.py` bounded.** It shares the walk: measure whether (a) and (b) bound it, and fix it in this task if they don't. Its DFS contract changes as Mythos ruled (⊇ on cyclic roots, exact on acyclic roots and on the corpus). | **u45-firstcompose only.** |
| 0.5 | **Done when** `scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest` completes on the target with NO `--ignore` of any hang file, read from `summary.txt`. Then delete every "KNOWN HANG / --ignore" instruction from AGENT-BRIEF.md. | **Coordinator.** |

## Until Task 0 lands

- **Every other lane resumes its OWN unit right away.** It does not wait for Task 0, and it does not work on Task 0. The guardrails make a hang impossible:
  - one pytest run per lane at a time, `-n 3` at most, always wrapped in `timeout 2400`;
  - both hang files `--ignore`d on every run;
  - no directory-wide runs without a slot from the coordinator.
- **Landings:** Task 0 lands first. Units that are ready (protocol, U-02, the carve stack starting with 506561cd4, U-45 part 1, T5e) may land while 0.3/0.4 are still in progress, as long as their evidence uses the guardrails. The final Q gate needs Task 0 complete.
- A log that hasn't grown in 15 minutes means the run is hung. Its owner stops it immediately.
- The coordinator checks `ps` and log staleness at every checkpoint, not only when asked.
