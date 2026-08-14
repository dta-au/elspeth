# State Engine Proof Matrix

This is the human-readable result for the first full v3 assessment at
`2b4b04a8a852a839b7b395b0bcdfceb95676606b`. The machine authorities are the
[v3 catalog](proof-catalog/v3/catalog.json) and the current dated
[assessment manifest](assessments/2026-08-15-0537/assessment.json).

## Result

**Verdict: not complete.** All 73 v3 legs retain at least one unknown required
cell and `HG-09-mandatory-leg-unresolved` is open. The assessment inherits
nothing: 489 of the catalog's 7,010 required executable cells are promoted
from three retained, reporter-bound local SQLite WAL lane runs (712 + 30 + 3
nodes, every node passed, zero skips) with per-cell node attribution — 417
pass, and 72 partial where independent review judged the evidence real but
short of the catalog's per-family acceptance text (fresh-process execution,
exact per-plugin success images, independent-process coordination races). The
PostgreSQL 16 AWS composition lane and all 38 protected live provider lanes
are unexecuted, and local cells whose dimensions carry no mapped assertion
remain unknown.

| Family | Legs | Confirmed | Gap | Unknown | Main unresolved proof |
| --- | ---: | ---: | ---: | ---: | --- |
| Token transitions | 20 | 0 | 0 | 20 | Live-profile cells and unmapped local dimensions (production composition, concurrency, restart) |
| Auxiliary state | 7 | 0 | 0 | 7 | Live-profile cells; heartbeat/adoption dimensions without mapped local assertions |
| Run coordination | 7 | 0 | 0 | 7 | Live-profile cells; orchestration-level production entry for coordination verbs |
| Production boundaries | 11 | 0 | 0 | 11 | Live provider lanes; provider-backed PB-09 cases without local internal-composition evidence |
| Read models | 14 | 0 | 0 | 14 | Live-profile cells; consumer concurrency/crash dimensions; RM-04/05/07 executed-consumer depth |
| Forbidden paths | 14 | 0 | 0 | 14 | Live-profile cells; refusal dimensions not yet mapped at every profile |
| **Total** | **73** | **0** | **0** | **73** | Every leg has at least one unknown mandatory v3 cell |

`Unknown` means mandatory evidence is absent or not attributable at the exact
case, dimension, and profile cell. It does not mean the implementation is
known broken. Every unresolved leg carries a live tracker owner and an
observable exit gate in the assessment manifest.

## Fresh evidence

| Vector | Current result | Establishes narrowly | Does not establish |
| --- | --- | --- | --- |
| EV-LOCAL-SINGLE-PROCESS (712 nodes) | all passed | 440 exact cells across 63 legs at the SQLite single-process profile, including all 34 local PB-09 plugin lifecycles across five phases | Live provider effects, PostgreSQL/AWS deployment, follower/web profiles, uncited dimensions, WAL-file semantics for in-memory-only cells |
| EV-LOCAL-SAME-HOST-FOLLOWERS (30 nodes) | all passed | 39 exact cells at the same-host claim-only-follower profile (RC-05 admission refusals, PB-08 follower worker, follower dispositions) | Everything beyond its coverage list |
| EV-LOCAL-WEB-CLI-FOLLOWERS (3 nodes) | all passed | 10 exact cells at the web-hosted-leader profile (PB-07 seam matrix probe, PB-08 shared runtime lifecycle, PB-09 transform:passthrough) | Everything beyond its coverage list |

See the current [evidence record](assessments/2026-08-15-0537/evidence.md) for
exact selectors, node-to-cell attribution, environment identity,
adjudications, and limits.

## Open proof lanes

| Lane group | Cells | Owner | Exit condition |
| --- | ---: | --- | --- |
| PostgreSQL 16 AWS composition lane + 38 protected live provider lanes | 1,780 | `elspeth-82592e3aa1` | Operator restores AWS, activates `state-engine-live-provider.yml` on the default branch with its protected environment, and dispatches it once at the frozen SHA; `ingest-live-evidence` binds the artifacts |
| Local cells with no mapped assertion (incl. 38 provider-backed PB-09 cases lacking local internal-composition tests) | 4,741 | `elspeth-efb47cb5fd` | Author tests or extend the reviewed coverage mapping only where an assertion genuinely proves the cell |

The campaign milestone remains `elspeth-4b3d734e3a`. Tracker assignment,
status, priority, and dependencies remain live Filigree authority rather than
evergreen prose.

## Hard gates

`HG-09-mandatory-leg-unresolved` is open. The other nine hard gates remain
unknown because their mapped mandatory cells include unknowns; each records a
per-gate count of promoted versus unknown mapped cells and cites the promoting
local evidence in the assessment manifest. Their evidential positions differ
widely — from `HG-10` (maintenance, two promoted deliberate-absence cells) to
`HG-08` (boundary_composition, whose affected set is one leg narrower because
PB-08's only required cells are the two promoted follower profiles). No gate
can close until its mapped obligations are covered at every required profile.
