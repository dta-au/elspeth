# Battery round 8 — plan and coverage declaration

Pin **`230fd9dfd`**, 23 commits past round 7's `51d3d26c9`. The stack is the
round-7 cold install (run `700e19d5-7894-4087-9a04-25aca8047b26`, cleanup
deadline 2026-08-12); round 8 rolls the image only, it does not reinstall.

This document is written **before** the round runs. Its purpose is the
coverage declaration in §3: a clean round must not be readable as covering
tickets it never touched. Round 7 learned this the expensive way — its guided
rider reported 10/10 completions over sessions that were empty and made zero
LLM calls, and `elspeth-9595abb7b0` had to be recorded as NOT EXERCISED rather
than passed.

## 1. Known state of the pin

The pin moved during preparation. At the **earlier** pin `4976bdfce`,
`pytest tests/` returned **2 failed, 38605 passed, 81 skipped, 1 xfailed**
(17m26s), both failures tracked as `elspeth-62a5aa4da8` (P1) and bisected to
`7201beeb7`:

- `tests/unit/web/test_sessions_composer_attribute_contracts.py::test_sessions_and_composer_use_explicit_attribute_contracts`
- `tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`

`230fd9dfd` ("rewrite the 7201beeb7 getattr sites to direct access") is the fix
for exactly those two. The round-8 pin therefore also **verifies
`elspeth-62a5aa4da8`** — a ticket that was not on the round's target list an
hour ago. The gate is being re-run at the new pin; the report records that
result, not the old one.

Both failures were static-analysis gates over the source tree, so neither could
have influenced live battery behaviour either way. The rule stands regardless:
no arm-A failure may be attributed to them.

Two of the round's targets, `elspeth-15c72686f2` and `elspeth-ac85b0ab0e`, are
in status `fixing`, not `verifying`, and both have review-followup commits
inside the pin (`079eda64a`, `7201beeb7`). **If a sibling lands further work on
either during the round, that ticket's verdict is void, not merely stale.**

## 2. Arms

| Arm | What | Why |
|---|---|---|
| **A** | The 19-compose corpus, byte-identical to round 7 | Change-detector. Same graphs, sampling and timeouts, so any per-graph difference is attributable to the 22 commits and nothing else |
| **B** | `r8_blob_probe.py` — blob custody, negative + concurrent | Arm A cannot reach Lane A at all (below) |
| **C** | `g13` ×3, `g14` ×2 — two new probes | Reach two things no existing graph can |
| ~~D~~ | **dropped** | Round 7 proved it measures nothing |

### Arm C exists because of two structural gaps

**`g13` — the only graph in any round that can write a coalesce branch-loss
row.** `elspeth-74b795208f`'s `gate_routed_to_sink` arm needs
`fork → gate routes a BRANCH to a sink → coalesce barrier`, because
`_notify_coalesce_of_lost_branch` returns `[]` when `branch_name is None`
(`processor.py:3051`). g03 and g09 fork and coalesce but hold no gate; g12
gates but never forks. **Round 7 could not have exercised this ticket**, and a
round-8 arm A alone could not either.

**What g13 does and does not prove.** Under the fix the reason is *always* the
bare 19-char token `gate_routed_to_sink`, independent of sink-name length
(asserted at `tests/unit/engine/test_processor.py:9202`), so a long sink name
cannot overflow the column on current code. g13 is therefore **not** a
falsifier of the current build — calling it one would overclaim. Its value is
that it reaches, on real PostgreSQL, a write path that:

- no corpus graph reaches at all, and
- the existing unit test cannot reach, because that test mocks
  `CoalesceExecutor` and asserts the `notify_branch_lost` *call* — the row
  write in `record_coalesce_branch_loss` is downstream of the mock, and SQLite
  does not enforce `VARCHAR(n)` so no local test can enforce the bound either.

The ticket's actual symptom was *an audit write for a handled condition killing
the run*. g13 completing shows that path survives on PostgreSQL. The verdict is
only complete when the persisted row is read back and its `reason` asserted
equal to `gate_routed_to_sink` — a run that completes because the row was never
written would otherwise read as a pass. The sink name
`escalated_high_value_claims_for_manual_underwriter_review` (56 chars) is kept
so the name recorded on the ROUTED token outcome is exercised at length too.

**`g14` — the control round 7 owed and never ran.** A DIRECT
`llm source → locked text sink` with no exploder. `bb29555e5`'s own control
test says this shape is already rejected at build time. If g14 is instead
accepted and then dies at row preflight, the compose-valid/run-dies seam is
back. Round 7 diagnosed `elspeth-15c72686f2` as an llm-SOURCE declaration
defect while the stack was live and never ran the one graph that would have
falsified it; the true cause was the forwarding walk, at the next hop.

### Arm B is negative and concurrent by design

A single-threaded happy-path upload would execute the custody code and prove
nothing. `elspeth-3d1d1fcb6c` is a **serialization** fix — only concurrent
clients discriminate it. `elspeth-b3feba9a7c` is verified by *attempting*
deletion of a referenced blob and being refused (**409**), which is
deterministic and cheap. B1 carries its own control arm: an unreferenced blob
must still delete cleanly, otherwise a 409 only means "delete is broken".

## 3. Coverage declaration — what round 8 CANNOT reach

Recorded before the round so that a green result cannot be read as covering
these.

| Ticket | Why unreachable |
|---|---|
| `elspeth-1d24bb0d96` | Needs a run whose Landscape store is **missing**. Reaching it means deleting a store on the live stack — destructive fault injection on shared state. **Refused.** Verify by unit test and code reading |
| `elspeth-045ad8de9d` | Only fires on a `database_bootstrap` **failure**. The stack is already installed; reaching it costs a full teardown + fault-injected reinstall. Not worth the spend |
| `elspeth-8363555f05` | `0ce0a0cf1` touches only `frontend/src/stores/executionStore.ts` and its test — client-side dispatch state. No HTTP driver reaches it. It is reachable by a **browser** probe, which this round does not build; if it is wanted, that is a scoped follow-up, not an arm-A result |
| `elspeth-9595abb7b0` | Needs a sink diversion to actually occur. `elspeth-afdf55a17c` closed at 0/3 reproduced, so the natural path is gone. If arm A produces no diversion, this stays NOT EXERCISED for a second round |
| guided cluster | Arm D dropped. **UNMEASURED**, not "no regression" |

`elspeth-d5578ccd98` is a partial: arm A exercises the *validated* token-outcome
path on every run, which is a no-regression datum. The corruption-isolation arm
is not reached without fault injection.

## 4. Cost

Round 7 measured USD 0.3391/session on a complete battery. Arm A (19) ≈ 6.4,
arm C (5) ≈ 1.7, arm B ≈ 0.4 (one compose). Round 8 ≈ **USD 8.5**.
