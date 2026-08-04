# Brief: acceptance battery round 3

Date written: 2026-08-05. Author: `claude-wave3`. Audience: the session that
runs the next battery. Predecessor: `docs/acceptance/2026-08-04-battery-round-2-report.md`
(read its per-graph table first — round 3 is largely a re-test of what that
round broke).

## Why this round exists

Round 2 filed seven defects. Two P1s and one P2 have since been fixed and
need **live confirmation on a deployed build**, because round 2 proved the
difference between "unit-green" and "true on the instance" twice. The four
remaining P2s are unfixed and should be **re-confirmed as still-live** rather
than assumed.

## Preconditions — do not skip

1. **The acceptance stack expires.** The task definition carries
   `CleanupDeadline 2026-08-06T00:00:00Z`. Confirm the stack still exists
   before planning anything (`aws ecs describe-services --cluster
   acceptance-a-fa1b99c60192978b10f7-cluster --services
   acceptance-a-fa1b99c60192978b10f7-service --profile elspeth-installer`).
   If it is gone, round 3 begins with a cold install
   (`docs/runbooks/aws-ecs-deployment.md`), not a redeploy.
2. **Redeploy is mandatory.** The live service is on `web:9` / release
   `173a81cbb` — that predates every fix under test. Follow
   `docs/runbooks/aws-ecs-existing-service-redeploy.md`. Two traps round 2
   hit, both worth reading the round-2 ledger entries for:
   - `RegisterTaskDefinition` is condition-gated on the run tag; replicate the
     live task-definition tags into the registration or it is AccessDenied by
     design.
   - The stock `schema-init-doctor` task definition pins the **old** image.
     Register a new revision on the candidate digest before `--init-schema`,
     or the store is initialised at the wrong epoch.
3. **`SESSION_SCHEMA_EPOCH` is 45** (44 → 45 landed with ADR-036). The session
   store must be recreated in the same window. The runtime role has no
   CREATEDB, so recreation runs through the `database-bootstrap` task
   definition, not ECS Exec. Landscape stays epoch 30 and is untouched.
4. **Operator owes a task-definition grant**: `ELSPETH_WEB__AWS_TEXTRACT_PROFILES`
   (ADR-036 binds Textract document buckets through an operator profile rather
   than row data). Without it the Textract graph cannot compose. Carry forward
   the round-2 additions too — `ELSPETH_WEB__AWS_S3_SOURCE_PROFILES` and
   `ELSPETH_PLANNER_REJECTION_DETAIL_LOG=1`.

## Driver

`scripts/acceptance_battery.py` — committed after round 2 precisely so this
round does not rebuild it. It encodes the choreography and both execution
gates:

```bash
python scripts/acceptance_battery.py ensure-account
python scripts/acceptance_battery.py api POST /api/sessions body.json g01/session.json
python scripts/acceptance_battery.py resolve-reviews <session-id> g01
python scripts/acceptance_battery.py run-graph <session-id> g01
python scripts/acceptance_battery.py guided-rider 10 --intent "<canonical intent>"
```

Gate 1 is pending interpretation reviews; gate 2 is the 428 LLM-fanout ack.
A compose **correction** can stage further reviews, so re-run
`resolve-reviews` after any repair turn until it reports zero. Compose calls
routinely exceed the client timeout — a timeout does **not** abort the
server-side compose; re-read `GET /messages` to recover the outcome. Round 2
lost a graph to exactly that (and it turned out to be the root cause of a P1).

Every verdict comes from `GET /api/runs/{id}/diagnostics` — per-token node
states, error `reason` codes, sink attribution. Never judge a run by
output-file mtime.

## Scope

### A. Confirm the fixes (the point of the round)

| Ticket | Fixed by | What must be true live |
|---|---|---|
| `elspeth-558fa5a321` | `6610fa0d4` | A graph with **more auto-wired controls than the old cap of 3** surfaces every disclosure card and reaches a validatable state. Build one deliberately: ≥4 LLM nodes so the required-control pass wires ≥4 controls. This is the exact shape that wedged round 2. |
| `elspeth-9c01c943a5` | `cbc0e99d3` | The two-LLM-arm row_union A/B graph composes without a raw 500. It was intermittent (1 of 2) — **sample it at least 3 times**, and treat any 500 without a coded envelope as a reopen. |
| `elspeth-cd0f6a6cd9` | ADR-036 / `1efeae10d` | Textract binds its bucket through the operator profile. Re-run the round-2 g03b shape and confirm the alias-as-bucket trap is now refused with an actionable message rather than a `bucket_region_unverified` quarantine. |
| `elspeth-03f5728c33` | **open** — check status first | If it has landed: cancel a compose mid-loop (a client timeout reproduces it) and confirm the `llm_prompt_template` reviews still surface. If not landed, skip and note it. |

### B. Re-confirm the still-open P2s (all `triage`, unowned)

Each has a known reproducing shape from round 2 — re-run it, confirm still
live, add a dated datum to the ticket. Do **not** file duplicates.

- `elspeth-9d13900064` — LLM provenance side-fields (`*_usage`, `*_model`)
  break a downstream fixed-schema consumer at runtime.
- `elspeth-ed2c2315d7` — `SchemaConfigModeViolation` on a mapper carrying an
  LLM-produced field.
- `elspeth-5a372d3267` — compose presents a graph that fails execution
  `graph_structure` validation (llm source, dynamic schema).
- `elspeth-2306940c70` — post-advisor-FLAG recovery reply claims the user's
  refused instruction is live.

### C. Coverage sweep

Re-run enough of the round-2 ten to prove no regression on what was green:
linear multi-transform, gate with named routing plus a poisoned row,
fork/coalesce, `json` / `text` sources, sink variety. Those were all clean, so
treat a failure there as a **regression** and stop to diagnose rather than
filing a new defect.

### D. Riders

- **F14 (`elspeth-5904b1683a`, closed)** — re-sample the guided cold start
  ≥10× as a regression watch. Round 2 measured 10/10 against a pre-fix 1/5.
  Anything below 10/10 warrants a reopen, not a shrug.
- **Prompt retention (`elspeth-49b467d91a`, verifying)** — round 2 could not
  sample it because no guided send failed. There is now a cheap recipe:
  the contradictory-instruction shape that FLAGs the advisor reliably
  produces a failing compose. Drive one, then confirm the typed prompt is
  restored rather than discarded. **This is the round's best chance to
  discharge that ticket.**
- **Retry exhaustion (`elspeth-454892147c`, verifying)** — still needs a
  fault-injection lever that does not exist on the acceptance surface. Report
  unsampled again unless someone builds the lever first; do not fake it.

## Reporting

One report at `docs/acceptance/2026-08-05-battery-round-3-report.md` mirroring
round 2's shape: per-graph verdict table **with run ids**, rider ratios, and
new defects labelled `battery-2026-08-XX`. One tracker timeline entry in
`docs/acceptance/2026-08-03-r3-rca-remediation-tracker.md`, and an AWS ledger
row for every mutation. Evidence comments on the tickets the round discharges.

**Recommend closes; do not close.** Operator sign-off closes, then the close
carries `close_commit release/0.7.2@<tip>`. Stochastic items never close on a
single pass.

## Two things round 2 learned the hard way

1. **Reproduce before believing a diagnosis — including your own.** The
   round-2 wedge report initially generalised from the last few rows of an
   event dump and got the mechanism wrong; the correct cause only appeared
   when the full listing was re-queried and the arithmetic checked. Live
   evidence beats a plausible story.
2. **A live repro expires.** The stranded session that produced the P1
   evidence lived on a stack with a two-day cleanup deadline. Capture the
   listing and the node requirement JSON into the ticket while the instance
   is up, not after.
