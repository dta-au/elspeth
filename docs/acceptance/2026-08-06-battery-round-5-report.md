# Battery round 5 — report of record

Date: 2026-08-06. Author: `claude-r5-battery` (analysis: `armA-analysis`).
Brief: `2026-08-06-battery-round-5-brief.md` — read its dated corrections; they
are part of this round's method. Round 4's report remains the prior-round
record; where its text and this report disagree, the dated corrections in both
files are the record.

## Headline

- **The round-4 P1 pair is confirmed at Level 2**: g01 3/3 clean runs with rows
  flowing (`3664e213c4`), g02 4/4 designed-shape across two wall settings
  (`aed3b69cf0`, by prevention). Both remain open per the brief's stochastic
  rule — one more pass.
- **g08 closure is refuted** (1/3 clean) and the mechanism is identified,
  filed, and already `fixing` (`41bcaa882e`). Not the extra_forbidden family.
- **Six new defects filed** (four P1), all from evidence, none speculative.
- **The full battery was achieved**: every corpus graph produced a run across
  arms A∪B. Cost: **USD 0.3618/session upper bound** (A∪B), with the
  round-3/4-comparable arm-A cohort at **USD 0.3077/session** — below round
  4's 0.3215 partial-battery floor.
- **The advisor FLAG rate is measured, twice over**: 16/20 (80%) early-phase
  `flagged` this round from the CloudWatch log surface; round 4
  retro-measured 9/15 (60%) — the "unmeasurable" verdict is corrected.
- **The stock package render loses a corpus graph at its own wall**: arm C's
  g09 died at 240s; arm A's only g09 success settled at t=265.7s.
- **Guided rider: 10/10** completions with composition state — the guided
  cluster's first live signal, smoke-grade only.

## Configuration and provenance

| | |
|---|---|
| Pinned SHA | `59cb6f75eeb4456b80af054a17b654c4ca752034` (`release/0.7.2`) |
| Stack | run `700e19d5-7894-4087-9a04-25aca8047b26`, `ap-southeast-2`, reused from round 4 — **not** a cold install |
| Images | web `sha256:6214adcb…` / agent `sha256:56cbb8e1…`, both built from a detached worktree at the pin; amd64 children `3a8541bad…` / `e691db8d…` |
| TLS | **ACM certificate on `elspeth.aws.foundryside.dev` via SNI** (cert `451ab653…`, ap-southeast-2), replacing the 24-hour self-signed clock. Terraform's self-signed listener default untouched. Public chain; no CA bundle |
| Arm A (parity) | TD `:3` — 270s / `medium`, serial, 17 composes (g01/g02/g08 ×3 + g03–g07, g09–g11) |
| Arm B (completeness) | TD `:4` — 840s wall / 900s idle ceiling / ALB 900 (targeted terraform, reverted after). **Deliberate deviation; separate cohort** |
| Arm C (stock render) | TD `:5` — 240s / `medium` (the actual package render post-`b5047ef69`), ALB 300 |
| Arm D (guided rider) | ×10 attempts at TD `:5`, canonical corpus intent |

ECR note: the repository's immutable-tag setting refused round-4's
`acceptance-<run-id>` tag; round 5 pushed under
`acceptance-<run-id>-59cb6f75e`. Round-4's image remains addressable — the
immutability gate doing its provenance job.

## P1 gates at the pin

`pytest tests/ -n 12`: **37975 passed, 0 failed** (36 skipped, 1 xfail).
`elspeth-lints check`: exit 0. Wardline: **PASSED**, `--fail-on ERROR
--fail-on-inert`, 66 recognized boundaries.

The first full run (at `991deb740`) failed 11: four deterministic stale
expectations from the same-day fix wave (fixed in `49eb58f4f` — D2
env-namespace deletion, f159 terraform variable, underfunded-disclosure
fixture leak, two unspecced mocks) and seven ADR-038 fixture failures fixed
concurrently by another session (`e9cfd6970`). No product defect. Honesty
note: 9 Azurite e2e tests skipped in the pinned run (local Azurite failed to
start); they passed in the first full run on identical product code.

The doctor on TD `:3` (command override, essential container): all checks ok,
`session_schema`/`landscape_schema` **current** — epoch 45 held, no store
recreation. The `composer_turn_budget_underfunded` structured warning fired at
270s/20 turns — the f159 disclosure working as designed; recorded, not a
defect.

## Level-1 off-stack confirms (deterministic, ×1, not battery evidence)

4/4 PASS, each discriminating (mechanism verifiably absent at `52ab3ec8b`):
config-time rejection naming the normalized form and both escape hatches
(`3664e213c4`); non-null `patch_node_options` suggestion on the live
`graph_structure` path — the pre-existing handler was in a dead phase-3 path,
so grep-level checking would have false-passed (`aed3b69cf0`); all 11 hint
capability claims sandbox-true, 6 negative claims correctly rejected
(`18bcf7dd09`); accounting agreement on the `rows_rejected ==
validation_errors == total` chain — the brief's original criterion named
`routing.discarded`, which is token-units and correctly stays 0 (corrected in
the brief) (`43f52d69a4`).

## Per-graph results

Arm A wall rate: **3/17 ≈ 18%** (g03, g04, g11) — in family with round 4's
~25%. g08-s2/s3 are validate failures, not walls. `Wall s` is driver
end-to-end, not compose time.

| Graph | Arm | rc | Wall s | Session | Run status | Authored vs R4 | Note |
|---|---|---|---|---|---|---|---|
| g01-s1 | A | 0 | 111 | `176ff76c` | completed | **different** — normalized schema names | 4/4 rows, 0 rejected. R4-a1 had the *identical* mixed-case header and discarded 4/4 |
| g01-s2 | A | 0 | 141 | `b586c720` | completed | **different** — incl. space→underscore | Declared `priority_level` from header `Priority Level` — the normalization *function*, not a slogan |
| g01-s3 | A | 0 | 119 | `6e35d8ed` | completed | **different** — normalized | Only sample narrating the normalization rule before authoring |
| g02-s1 | A | 0 | 87 | `d5b69b13` | completed_with_failures | **different** — declares arrives (`str`) not output | Designed 4/1/1, 3 sinks. One repair turn: `conversions` list-vs-dict, **not** the edge |
| g02-s2 | A | 0 | 101 | `e15d7e97` | completed_with_failures | **different** — same field | **Zero repairs** — read the schema first, built right in one call |
| g02-s3 | A | 0 | 86 | `cb9a0c85` | completed_with_failures | **different** — same field | Designed 4/1/1; one `conversions` repair |
| g03 | A | 2 | 272 | `26137cd8` | wall, no state | n/a — nothing authored | Wall cut it mid-second-turn; transcript spans 113s of 272 |
| g04 | A | 1 | 272 | `a05b930a` | wall, partial state | **same** — 3-node explode→flatten→project | Died inside the review byte-match loop (`9d59c33480`) with 42s left |
| g05 | A | 0 | 194 | `edf6fd74` | completed | n/a — R4 declined by design | 6/6 rows. Composer *prospectively* avoided `value_transform`, citing the digest |
| g06 | A | 0 | 91 | `2194acd5` | completed | **same** | 5 rows → 20 tokens, 3 sinks |
| g07 | A | 0 | 104 | `2c6f5b49` | completed_with_failures | **same** — profile-bound Textract | Designed 1/1 error route; ADR-036 `profile: acceptance-docs`, no inline bucket |
| g08-s1 | A | 0 | 224 | `390d2754` | completed | **same** — no required-fields post-union | The only clean g08 of three |
| g08-s2 | A | 1 | 136 | `159588c6` | validate FAILED | **different** — `required_fields` post-`row_union` | `graph_structure`, `suggestion: null`, zero repair attempts |
| g08-s3 | A | 1 | 189 | `5dbc9f8f` | validate FAILED | **different** — same option | Identical failure, identical zero-repair |
| g09 | A | 0 | 312 | `6104c4d8` | completed | n/a — R4 authored nothing | 3/3 rows. Compose settled t=265.7s — 4.3s inside the wall |
| g10 | A | 0 | 164 | `52964367` | completed | **same** | 5/5 rows |
| g11 | A | 1 | 272 | `bee94cc2` | wall, partial state | **different** — `content_safety` where R4 had `value_transform` | Both rounds' partials invalid; not comparable |
| g03 | B | 0 | 514 | `a9ab20c4` | completed | **different** — 8 nodes vs R4's 4 | First `set_pipeline` at **t=413.5s** — unreachable inside 270 at any observed pace |
| g04 | B | 0 | 232 | `f8804602` | completed | **same** | **Under the old ceiling.** Review loop recurred identically; only the driver's `/resolve` settled it |
| g11 | B | 0 | 319 | `afa5cee1` | completed_with_failures | **different** — csv seed + `llm` transform, not an `llm` source | **0/6 rows succeeded, 0 artifacts** (`b19dfe41fb`); authored around the mechanism under test |
| g02 | C | 0 | 93 | `1cfa5245` | completed_with_failures | **different** — arrives form, same field | Clean 4th sample of the arm-A behaviour; fastest of the four |
| g09 | C | 2 | 241 | `e4c54334` | wall, no state | n/a | **Structural at 240, marginal at 270** |
| rider ×10 | D | 0 | — | ten sessions | 10/10 completed, all with state | n/a | Smoke datum only; closes nothing of the guided cluster |

Full session/run ids: `ops-local/acceptance/r5-preserve/analysis/report-tables.md`
and the per-arm `summary.txt` files.

## Confirm-target verdicts

Recommend, do not close; operator sign-off closes with
`close_commit release/0.7.2@<tip>`. Stochastic items never close on a single
pass — after this round that includes g01 and g02.

| Ticket | Verdict | Evidence (one line) | Recommend close? |
|---|---|---|---|
| `3664e213c4` | **CONFIRMED** | 3/3 ran 4/4 rows, 0 rejected; R4-a1/R5-s1 differ *only* in declared case — the round's cleanest attribution | No — second pass per stochastic rule |
| `aed3b69cf0` | **CONFIRMED by prevention** | 4/4 designed-shape across 270s and 240s; the rejection never occurred, so the repair path stayed unexercised (Level-1 proved it off-stack) | No — same rule; scope limit noted under `41bcaa882e` |
| `18bcf7dd09` | **CONFIRMED** | g05 transcript, before authoring: title-casing "explicitly not achievable in `value_transform`" → chose `llm`. The corrected hints observed being read and acted on | **Yes** |
| `43f52d69a4` | **CONFIRMED** (arm B supplied the zero-succeeded probe) | `rows_rejected == validation_errors`, `total == sum of parts`, per-row reason readable end-to-end; two-surfaces caveat `elspeth-obs-62c0f57b6d` | No — resolve the caveat first |
| `902fc354b2` | **REFUTED** — 1/3 clean | s2/s3: `row_union` edge `graph_structure`, `suggestion: null`, zero repair attempts | No — **blocked by `41bcaa882e`** (fixing) |
| `cd0f6a6cd9` | **CONFIRMED** | g07 profile-bound shape identical to R4; survived `80f9d62af`'s touch | **Yes** |
| `cfcd333f83` | **SAMPLED — CONFIRMED** | `llm` transforms authored and executed in g05, g09, g10, g08-s1; first-ever exercise | **Yes** |
| `5a372d3267` / `39118dd24f` (g11) | **NOT SAMPLED** (both) | Corpus maps g11 to the former, the brief to the latter; arm B's authored graph avoided both mechanisms (csv seed, no `llm` source; rows died at the shield before the transform contract) | No |
| `878dedd7f5` | **CONFIRMED at push time** | 0 findings on both amd64 children (R4 agent child: 33). Part-1 evidence, not the battery's | **Yes** |
| `a9967c55ff` | n/a — runbook-only | Excluded by the brief | n/a |
| `52af290183` | **PARTLY DISCHARGED + one adverse datum** | The named 240+`high` combination was eliminated by `b5047ef69` (its own fix, in the installed package). The actual stock render (240+`medium`) lost g09 at the wall — g09 *is* a compose that flips pass→fail at 240, against the commit's "ZERO flip" claim. n=1, no rate | No |
| `a79f1b2e6b` | **MEASURED** — criterion satisfiable at last | Full battery achieved across A∪B; **USD 0.3618/session upper bound**, arm A comparable cohort 0.3077 | **Yes**, on the operator's read of the numbers |
| `47fa7c01eb` | NOT ASSESSED this round | Carried re-confirm not evaluated; `elspeth-obs-62c0f57b6d` filed against it | No |

**Not covered** (stated so this report cannot over-claim): `9d13900064`,
`82d4c5146c` (no graph reached their shape), `49b467d91a` (frontend DOM —
now *possible* with real browser TLS, not attempted), `454892147c` (no
provider-failure lever exists).

## New defects filed (`battery-2026-08-06`, `battery-r5`)

| Id | Title | Severity |
|---|---|---|
| `elspeth-155947ca47` | Interpretation-review resolution erases a live `graph_structure` violation from persisted state (`is_valid` flips false→true, stays wrong through v9 while `/validate` and `/state/yaml` still fail) | P1 |
| `elspeth-41bcaa882e` | `row_union` publishes no output guarantees; downstream `required_fields` failure carries `suggestion: null` | P1 — **fixing**; blocks `902fc354b2` |
| `elspeth-9d59c33480` | `request_interpretation_review` demands a byte-identical `llm_draft` the composer cannot reproduce — 4-attempt loop both arms, never resolves in-band (the driver's `/resolve` settled it) | P1 |
| `elspeth-b19dfe41fb` | Auto-wired prompt shield on a declared-`int` field passes `/validate` (`is_valid: true`) then kills 100% of rows at runtime | P1 |
| `elspeth-3d391accce` | `graph_structure` violation absent from structured `validation_errors` at compose settle; free-text-only disclosure | P2 |
| `elspeth-09c91778f5` | Shipped envelope cannot fund the shipped corpus: g03's first authoring call lands at t=413s against the 270s structural ceiling | P2 |
| `elspeth-obs-62c0f57b6d` | (obs) Two discard surfaces report 0 and 6 for the same run; reason lives on a third | P2 obs |
| `elspeth-obs-340d35c55c` | (obs) `type_coerce.conversions` shape undiscoverable without `get_plugin_schema`; 4/4 blind composes spent a repair turn on it, 1/1 schema-first got it right | P2 obs |

## Advisor measurement (P2 resolved as option (a), zero code)

Per-pass verdicts from the `composer.advisor_checkpoint_pass` log surface
(archived: `r5-preserve/r5-advisor-checkpoint-events.json`):

| Cohort | flagged | clean | FLAG rate | No event |
|---|---|---|---|---|
| Arm A | 13 | 3 | 81% | g03 (wall) |
| Arm B | 2 | 1 | — | — |
| Arm C | 1 | 0 | — | g09 (wall) |
| **Compose arms total** | **16** | **4** | **80%** | walls only |
| Arm D (guided ×10) | 0 | 0 | — | **all 10 — the guided path emits no advisor checkpoint events** |

Reference: round 4 Sydney retro-measured **9/15 (60%)**; round 3 Singapore
14/16 (87.5%). All events are `phase=early`/`pass_index=1`; **zero
`phase=end` events in either round** — the END gate (the advisor cluster's
actual subject) is measurable only via the `completion_gates` envelope, a
separate surface not measured here. Round 4's g01 anti-correlation caveat
stands: an early `clean` does not predict a good outcome.

## Cost

Read-only `database-bootstrap` query, cohorts filtered by per-arm session
sets (never key presence). Arm D excluded by design (different instrument).

| Cohort | Sessions | Calls | Tokens | USD | USD/session |
|---|---|---|---|---|---|
| **A (comparable to R3/R4)** | 17 | 118 | 5,980,773 | 5.2314 | **0.3077** |
| B (longer by construction) | 3 | 35 | 1,912,725 | 2.0043 | 0.6681 |
| C (stock render, n=2) | 2 | 8 | 340,579 | 0.6799 | 0.3400 |
| **A∪B (full battery — `a79f1b2e6b`)** | 20 | 153 | 7,893,498 | 7.2357 | **0.3618 (upper bound)** |

Advisor line (GLM-5), arm A: 17 calls, USD 0.3010 — **USD 0.0177/call, 5.75%
of cohort cost** — consistent with round 4 (0.0175, 5.0%). Reference points:
R3 pre-cache 1.2915 → post-cache 0.4946 → R4 partial-battery floor 0.3215 →
**R5 full-battery 0.3618 upper bound / parity cohort 0.3077**.

## The wall, resolved into three causes

Raising the ceiling proved all three arm-A wall graphs composable — and
showed the ceiling was the defect in **one of three**:

- **g03 — genuine envelope shortfall** (`09c91778f5`): first `set_pipeline`
  at t=413.5s even at arm B's faster pace; 2× first-turn latency variance is
  real but the shortfall exceeds the spread.
- **g04 — the review byte-match loop** (`9d59c33480`): recurred identically
  at both walls; finished in 232s — *under* the old ceiling — once the loop
  was survivable. Fix the loop, not the timeout.
- **g11 — auto-wire type mismatch** (`b19dfe41fb`): a raised ceiling changes
  nothing; the composed run destroyed 100% of its rows post-validation.

## Tooling notes (instrument, not product)

The guided rider crashed on first invocation: `main()` never ensures an
account for `guided-rider` and the rider dereferenced `session["id"]` off an
unchecked 401 body. Fixed this round in `scripts/acceptance_battery.py`
(status check + `ensure_account`). Arm A's driver artifacts do not include
tool-result messages; capturing them would close the last inferential step in
the `41bcaa882e` chain (round-6 item). The driver now captures `/state`,
`/state/yaml` and (via post-arm pulls) `/messages` per session.

## Proportionality

The battery covered what it is the right instrument for: the compose loop
(corpus), run observability (g01/g11 probes), guided smoke (arm D), cost, and
the advisor early gate. It did **not** cover: the advisor cluster per-ticket
(END-gate surface unmeasured), schema/contract tickets (unit-verifiable),
per-ticket guided verification, or the four tickets named above. The
`verifying` queue is ~70 tickets; this round touched the subset above and no
more.

## AWS ledger (mutations)

| UTC (2026-08-06) | Mutation | Detail |
|---|---|---|
| 04:4x | `acm request-certificate` | `451ab653…` for `elspeth.aws.foundryside.dev`, ap-southeast-2; validation CNAME pre-existing (SG-era), no DNS write |
| 04:47 | `route53 UPSERT` | ALIAS `elspeth.aws.foundryside.dev` → Sydney ALB (change `C03731441UKLMEC6T68B7`) |
| 04:48 | `elbv2 add-listener-certificates` | SNI attach to the 443 listener; terraform default untouched |
| 08:0x | `ecr push` ×2 | web (round-5 tag; immutability refused reuse) + agent; digests above |
| 08:1x | `ecs register-task-definition` + `update-service` | TD `:3` (images-only diff CLEAN), service stable |
| 08:2x | `ecs run-task` (doctor) | TD `:3` command override; all ok; schemas current |
| 09:4x | terraform **targeted** apply `aws_lb.web` | `idle_timeout` 300→900. Full plan REJECTED at plan-gate (5 TD replaces + 8 alarm drifts from moved module code) |
| 09:5x | TD `:4` + roll | 840/900 ceilings (diff CLEAN, two settings) |
| 10:2x | terraform **targeted** apply `aws_lb.web` | `idle_timeout` 900→300 revert; tracked file restored |
| 10:3x | TD `:5` + roll | stock render (single 240 change from `:3`) |
| 10:4x | `ecs run-task` (cost query, read-only) | task `1693e732…`, explicit `READ ONLY` transaction |

Service remains on TD `:5` (stock render). Stack cleanup deadline
**2026-08-09** stands. Evidence archive:
`ops-local/acceptance/r5-preserve/` (246 files — all four arms' driver
artifacts including authored states and message histories, advisor events,
ledger, analysis reports).

## For round 6

Capture tool-result messages in the driver; measure the END gate via the
`completion_gates` envelope in the cost pass; re-sample g01/g02 for the
second stochastic pass; the frontend DOM ticket is now Playwright-reachable
over real TLS; settle the g11 corpus-vs-brief ticket mapping before
composing it again.
