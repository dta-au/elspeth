# State Engine Proof Matrix

This is the human-readable result for the current Task 5 delta assessment at
`9f78d2b2ae58cd93d8fafc51bf77c3fef65ed5ba`. The machine authority is the
[v2 catalog](proof-catalog/v2/catalog.json) and the current dated
[assessment manifest](assessments/2026-08-12-0425/assessment.json).

## Result

**Verdict: not complete.** All 73 v2 legs retain at least one unknown required
cell and `HG-09-mandatory-leg-unresolved` is open. The delta does not inherit
any July pass. It promotes four exact cells from a 113-node reporter-bound SQLite
single-process cohort; required PostgreSQL 16 AWS, SQLite follower, and uncited
dimension cells remain unknown.

| Family | Legs | Confirmed | Gap | Unknown | Main unresolved proof |
| --- | ---: | ---: | ---: | ---: | --- |
| Token transitions | 20 | 0 | 0 | 20 | Production composition, independent-process winners, restart, and profile parity |
| Auxiliary state | 7 | 0 | 0 | 7 | Heartbeat/lease loss, membership, leader fencing, and restart |
| Run coordination | 7 | 0 | 0 | 7 | Leader/follower contention, takeover, teardown, and backend parity |
| Production boundaries | 11 | 0 | 0 | 11 | Real plugins, barriers, sink publication, PostgreSQL semantics, and lifecycle |
| Read models | 14 | 0 | 0 | 14 | Complete positive/negative consumer truth tables, including abandoned tokens |
| Forbidden paths | 14 | 0 | 0 | 14 | Repository-wide refusal and complete zero-mutation evidence |
| **Total** | **73** | **0** | **0** | **73** | Every leg has at least one unknown mandatory v2 cell |

`Unknown` means mandatory evidence is absent or not attributable at the exact
case, dimension, and profile cell. It does not mean the implementation is
known broken. The assessment manifest gives every unresolved leg a live cohort
owner and an observable exit gate.

## Fresh focused evidence

| Vector | Current result | Establishes narrowly | Does not establish |
| --- | --- | --- | --- |
| Task 5 queue/source/plugin cohort | 113 passed | Four exact TS-00/01/02 and F-11 cells under SQLite single-process | PB-01/02/03 generic cases, boundary composition, PostgreSQL 16 AWS, follower profiles, crash/restart, concurrency, read models, and uncited cells |

See the current [evidence record](assessments/2026-08-12-0425/evidence.md) for
exact selectors, node-to-cell attribution, environment identity, and limits.

## Live proof cohorts

| Cohort | Primary legs | Owner | Existing context/dependencies | Exit condition |
| --- | --- | --- | --- | --- |
| Queue, source, transform, and gate | TS-00–02, TS-07–10, PB-01–03, F-01/02/11 | `elspeth-d262ace360` | `elspeth-c0d4a28e11`, `elspeth-9cd07962c7`, `elspeth-2e66723070` | [Task 5](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-5-close-queue-source-transform-and-gate-transition-cohorts) production, rollback, restart, refusal, and profile evidence passes |
| Lease, coordination, process, and read model | TS-03–06, AUX-01/02/06/07, RC-01–07, PB-11, RM-01–14, F-04/06/07/10/12 | `elspeth-eefd990b46` | `elspeth-9a52eb80f9`, `elspeth-2aba594afb` | [Tasks 4, 6, and 7](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-4-build-one-durable-image-assertion-and-process-crash-harness), including all five PB-11 PostgreSQL cases, pass across every required dimension |
| Aggregation, coalesce, and row union | TS-15–18, AUX-03–05, PB-04/05/10, F-03/05/09/13 | `elspeth-cc0b256aca` | None duplicated | [Task 8](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-8-complete-aggregation-coalesce-and-row-union-barrier-recovery) crash/restart and all supported-profile barrier evidence passes |
| Sink publication and repair | TS-11–14, PB-06/07, F-08 | `elspeth-f227dd8d2f` | None duplicated | [Task 9](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-9-complete-sink-effect-and-post-publication-recovery) ambiguous-loss, repair, external-provider, and profile evidence passes |
| Lifecycle, abandonment, follower, and plugins | TS-19, PB-08/09, F-14 | `elspeth-67be892457` | `elspeth-6f6bbbec00`; Python 3.14 defect `elspeth-61350c4744` | [Tasks 10 and 11](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-10-complete-run-finalization-abandoned-follower-and-lifecycle-behavior) lifecycle and every first-party plugin/profile case passes, and Python 3.14 Runtime-VAL compatibility is restored |
| Final assessment and maintained gates | All | `elspeth-f89d82e925` | Depends on all five cohorts and `elspeth-61350c4744` | [Task 12](../../plans/2026-08-11-state-engine-pinning-and-completion.md#task-12-publish-the-complete-assessment-and-install-maintained-gates) full assessment and maintained release/CI gates pass |

The milestone is `elspeth-4b3d734e3a`. Tracker assignment, status, priority,
and dependencies remain live Filigree authority rather than evergreen prose.

## Hard gates

`HG-09-mandatory-leg-unresolved` is open. The other nine hard gates remain
unknown because their mapped mandatory cells are unknown. ADR-041 and catalog
v2 resolve the earlier PostgreSQL support contradiction at the normative
surface, but `HG-10` cannot close until current executable evidence covers its
mapped obligations.
